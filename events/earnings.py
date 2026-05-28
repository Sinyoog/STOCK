"""
events/earnings.py
실적 예측(calculate_potential_earnings) 및 공시 확정(announce_earnings).
UI 코드 금지.
"""
import random
from datetime import timedelta


class EarningsManager:
    def __init__(self, state):
        self.s = state

    # ─────────────────────────────────────────────
    # 실적 수치 사전 확정 (PENDING 상태로 금고에 박제)
    # ─────────────────────────────────────────────
    def calculate_potential_earnings(self, stock: dict,
                                      _current_lv_cache: dict = None,
                                      _old_lv_cache: dict = None) -> dict:
        meta = stock['meta']
        tier = meta.get('tier', '소형주')

        # ★ 경기 사이클 → 실적 연동
        cycle = getattr(self.s, 'cycle_stage', '확장')
        cycle_revenue_mult = {
            "확장": 1.10,   # 확장기: 매출 10% 증가
            "정점": 1.00,   # 정점기: 보통
            "수축": 0.88,   # 수축기: 매출 12% 감소
            "저점": 0.80,   # 저점기: 매출 20% 감소
        }.get(cycle, 1.0)
        cycle_cost_mult = {
            "확장": 0.95,   # 확장기: 비용 절감
            "정점": 1.00,
            "수축": 1.10,   # 수축기: 비용 증가
            "저점": 1.15,   # 저점기: 비용 더 증가
        }.get(cycle, 1.0)

        # ★ 주가 하락률 기반 efficiency 강제 하락
        # 주가 -80% 이상 = 시장 신뢰 상실 → 영업환경 악화 → 실질 마진 하락
        initial_price = meta.get('initial_price', 0)
        price_now     = stock.get('price', initial_price)
        if initial_price > 10 and price_now > 0:
            price_drop_now = 1.0 - (price_now / max(1.0, initial_price))
            if   price_drop_now > 0.95: eff_penalty = 0.60   # -95%: efficiency 40%만 남음
            elif price_drop_now > 0.90: eff_penalty = 0.70   # -90%: 30% 하락
            elif price_drop_now > 0.80: eff_penalty = 0.85   # -80%: 15% 하락
            else:                       eff_penalty = 1.0
            effective_efficiency = meta.get('efficiency', 0.05) * eff_penalty
        else:
            effective_efficiency = meta.get('efficiency', 0.05)

        revenue   = meta['assets'] * random.uniform(0.04, 0.10) * cycle_revenue_mult

        # ★ 테크 레벨 성장 가중치 (캐시 우선 사용, 없으면 직접 계산)
        try:
            ind          = meta.get('ind', '')
            current_subs = [x.strip() for x in meta.get('sub', '').split(',')]
            if _current_lv_cache is not None:
                current_lv_list = _current_lv_cache.get(ind, [])
                old_lv_list     = _old_lv_cache.get(ind, [])
            else:
                from engine.constants import INDUSTRY_LEVELS
                current_lv    = self.s.max_tech_reached
                lv_industries = INDUSTRY_LEVELS.get(ind, {})
                current_lv_list = lv_industries.get(current_lv, [])
                old_lv_list = [s for lv in range(1, current_lv)
                               for s in lv_industries.get(lv, [])]

            if any(s in current_lv_list for s in current_subs):
                tech_mult = 1.3
            elif any(s in old_lv_list for s in current_subs):
                tech_mult = 1.0
            else:
                tech_mult = 1.0
        except Exception:
            tech_mult = 1.0

        revenue *= tech_mult
        base_cost = {"대형주": 0.015, "중형주": 0.020, "소형주": 0.030}.get(tier, 0.030)
        base_cost *= cycle_cost_mult   # 경기 사이클 비용 반영
        tier_bonus = {"대형주": 0.02, "중형주": 0.01, "소형주": 0.00}.get(tier, 0)

        op_margin  = effective_efficiency - base_cost + tier_bonus
        op_income  = revenue * op_margin
        interest_rate  = self.s.macro.get('interest_rate', 4.0)
        # 피드백 루프 3: 금리↑ → 기업 이자비용↑ → 실적↓
        # 기존 0.01 → 0.05 (5배 강화, 금리 10%에서 실질 타격)
        # 이자비용 완화: 0.05 → 0.01 (전 종목 적자 방지)
        interest_cost  = meta['assets'] * (interest_rate / 100) * 0.01
        net_income = op_income - interest_cost

        quarter_name = {2: "1분기", 5: "2분기", 8: "3분기", 11: "4분기"}.get(
            self.s.current_date.month, "분기"
        )
        announce_dt = self.s.current_date + timedelta(days=7)

        pending_text = (
            f"분기: {quarter_name}\n"
            f"공시 예정일: {announce_dt}\n"
            f"[실적 발표 예정 리포트]"
        )
        fixed_text = (
            f"분기: {quarter_name} (확정)\n"
            f"발표일: {announce_dt.strftime('%m월 %d일')}\n"
            f"매출액: {int(revenue):,} 원\n"
            f"영업이익: {int(op_income):,} 원\n"
            f"당기순이익: {int(net_income):,} 원"
        )

        # ★ 기관 컨센서스 저장 (선행매매 트리거용)
        # 실제 발표값에 ±20% 노이즈를 더해 기관의 "예측" 수치로 저장
        import random as _rnd
        consensus_noise = _rnd.uniform(-0.20, 0.20)
        consensus_ni    = net_income * (1.0 + consensus_noise)
        name = meta.get('c_name', '')
        if name:
            prev_consensus = self.s.earnings_consensus.get(name, {}).get('expected_ni', 0)
            self.s.earnings_consensus[name] = {
                "expected_ni":  consensus_ni,
                "direction":    1 if consensus_ni > 0 else -1,
                "confidence":   max(0.3, 1.0 - abs(consensus_noise)),  # 노이즈 작을수록 확신 높음
                "prev_ni":      prev_consensus,
            }

        return {
            "revenue":              revenue,
            "op_income":            op_income,
            "net_income":           net_income,
            "expected_net_income":  net_income,
            "is_surplus":           net_income > 0,
            "status":               "PENDING",
            "pending_text":         pending_text,
            "fixed_text":           fixed_text,
        }

    # ─────────────────────────────────────────────
    # 실적 공시 확정 (FIXED 상태로 전환 + 히스토리 기록)
    # ─────────────────────────────────────────────
    def announce_earnings(self, stock: dict, year_str: str = None) -> bool:
        meta = stock['meta']
        name = meta['c_name']

        if year_str is None:
            year_str = str(self.s.current_date.year)

        quarter_map = {
            3: "1분기", 4: "1분기",
            6: "2분기", 7: "2분기",
            9: "3분기", 10: "3분기",
            12: "4분기", 1: "4분기",
        }
        quarter = quarter_map.get(self.s.current_date.month, "수시")

        if not meta.get('expected_earnings'):
            meta['expected_earnings'] = self.calculate_potential_earnings(stock)

        earning_data = meta['expected_earnings']
        if earning_data.get('status') == 'FIXED':
            return True

        earning_data['status'] = 'FIXED'

        revenue    = earning_data['revenue']
        op_income  = earning_data['op_income']
        net_income = earning_data['net_income']

        # ★ efficiency 점진적 수렴 (PER 정상화 핵심)
        # 기존 종목들이 낮은 efficiency를 가진 채 생성됐으므로
        # 분기 실적 확정 시마다 목표값으로 서서히 수렴
        _tier      = meta.get('tier', '소형주')
        _eff_now   = meta.get('efficiency', 0.05)
        _eff_target = {
            '대형주': random.uniform(0.12, 0.20),
            '중형주': random.uniform(0.08, 0.15),
            '소형주': random.uniform(0.04, 0.10),
        }.get(_tier, random.uniform(0.04, 0.10))
        # 10% 속도로 목표값에 수렴 (너무 급격한 변화 방지)
        meta['efficiency'] = _eff_now + (_eff_target - _eff_now) * 0.10

        # ── HP / Shield 반영 ──────────────────────────────────────
        assets      = max(1, meta['assets'])
        sensitivity = meta.get('risk_sensitivity', 1.0)
        hp          = meta.get('hp', 50.0)
        soft_cap    = meta.get('hp_soft_cap', 60.0)
        shield      = meta.get('shield', 0.0)   # 단위: 원(₩)
        tier        = meta.get('tier', '소형주')
        market_cap  = max(1, stock.get('market_cap', assets))

        # 체급별 쉴드 적립률 및 상한 비율
        _SHIELD_SPEC = {
            '대형주': {'accum': 0.00005, 'cap_ratio': 0.05},
            '중형주': {'accum': 0.00003, 'cap_ratio': 0.03},
            '소형주': {'accum': 0.00001, 'cap_ratio': 0.01},
        }
        spec       = _SHIELD_SPEC.get(tier, _SHIELD_SPEC['소형주'])
        shield_cap = market_cap * spec['cap_ratio']

        if net_income < 0:
            # 연속 적자 카운트
            loss_cnt = meta.get('continuous_loss_count', 0) + 1
            meta['continuous_loss_count'] = loss_cnt

            from engine.constants import SECTOR_MAP
            scenario = self.s.current_scenario
            sector   = SECTOR_MAP.get(meta.get('ind', ''), 'Value')
            if '대공황' in scenario and '극복' not in scenario:
                sw = 1.3 if sector == 'Growth' else 0.6 if sector == 'Defensive' else 1.0
            else:
                sw = 1.0

            loss_ratio = abs(net_income) / max(1.0, revenue)
            hp_dmg_raw = loss_ratio * 100 * sensitivity * sw

            # ★ 연속 적자 가속 (재무 기반 상폐 핵심)
            # 연속 적자 횟수에 따라 HP 차감 가속
            if   loss_cnt >= 8: hp_dmg_raw *= 4.0   # 2년 연속 → 4배
            elif loss_cnt >= 4: hp_dmg_raw *= 2.0   # 1년 연속 → 2배
            elif loss_cnt >= 2: hp_dmg_raw *= 1.5   # 반년 연속 → 1.5배

            # ★ 부채비율 높으면 추가 차감
            debt_ratio = meta.get('debt_ratio', 0.5)
            if   debt_ratio >= 10.0: hp_dmg_raw += 5.0  # 1000%+: 즉각 타격
            elif debt_ratio >= 5.0:  hp_dmg_raw += 3.0  # 500%+
            elif debt_ratio >= 3.0:  hp_dmg_raw += 2.0
            elif debt_ratio >= 2.0:  hp_dmg_raw += 1.0
            elif debt_ratio >= 1.5:  hp_dmg_raw += 0.5

            # ★ HP 캡 연속적자에 따라 상향 (가속 효과 실현)
            _BASE_CAP = {'대형주': 0.5, '중형주': 1.0, '소형주': 1.5}
            base_cap  = _BASE_CAP.get(tier, 1.5)
            if   loss_cnt >= 8: cap_mult = 4.0   # 2년+ 연속 → 4배
            elif loss_cnt >= 4: cap_mult = 2.5   # 1년+ 연속 → 2.5배
            elif loss_cnt >= 2: cap_mult = 1.5   # 반년+ 연속 → 1.5배
            else:               cap_mult = 1.0
            hp_dmg = min(hp_dmg_raw, base_cap * cap_mult)

            # 방어막 상한 재조정 + 소진 처리
            shield_cap_now = market_cap * spec['cap_ratio']
            if shield > shield_cap_now:
                meta['shield'] = shield = round(shield_cap_now, 2)
            if shield < 100:
                meta['shield'] = shield = 0.0

            hp_ratio    = hp / max(1.0, soft_cap)
            shield_mode = hp_ratio < 0.30

            if shield_mode and shield > 0:
                shield_hp_val = shield / max(1.0, assets) * 100
                if shield_hp_val >= hp_dmg:
                    meta['shield'] = round(max(0.0, shield - hp_dmg / 100 * assets), 2)
                else:
                    remaining_dmg  = hp_dmg - shield_hp_val
                    meta['shield'] = 0.0
                    meta['hp']     = round(max(0.0, hp - remaining_dmg), 2)
            else:
                meta['hp'] = round(max(0.0, hp - hp_dmg), 2)

            # ★ 적자 시 assets 감소 (하한선 완화: 10% → 1%)
            # 자본잠식 가능하도록 하한선 낮춤
            init_assets = meta.get('initial_assets', assets)
            meta['assets'] = max(
                init_assets * 0.01,   # 하한선: 초기값의 1% (자본잠식 가능)
                meta['assets'] + net_income
            )

            # ★ 적자 시 부채 증가 (차입으로 버티는 구조)
            debt = meta.get('debt', 0.0)
            debt_increase = abs(net_income) * 0.5   # 손실의 50%만큼 부채 증가
            meta['debt'] = debt + debt_increase
            # 부채비율 갱신
            if meta['assets'] > 0:
                meta['debt_ratio'] = meta['debt'] / meta['assets']

            # ★ 자본잠식 체크 (assets < debt)
            if meta['assets'] < meta.get('debt', 0):
                # 자본잠식 발생 → HP 추가 즉시 차감
                meta['hp'] = max(0.0, meta.get('hp', 0) - 5.0)

            # ★ 적자 시 assets 감소 (하한선 보장)
            init_assets = meta.get('initial_assets', assets)
            meta['assets'] = max(
                init_assets * 0.1,
                meta['assets'] + net_income
            )

        else:
            # ── 흑자: 연속 적자 초기화 + HP 회복 + 부채 감소 ──
            meta['continuous_loss_count'] = 0

            # ★ 흑자 시 부채 상환 — 기업별 목표 부채비율로 수렴 (고정 하한선 제거)
            debt = meta.get('debt', 0.0)
            assets_now = meta.get('assets', 1.0)

            # 기업 생성 시 배정된 목표 부채비율 사용 (없으면 티어별 기본값)
            _DEFAULT_TARGET = {'대형주': 0.40, '중형주': 0.50, '소형주': 0.60}
            target_debt_ratio = meta.get('target_debt_ratio',
                                         _DEFAULT_TARGET.get(tier, 0.40))
            min_debt = assets_now * target_debt_ratio

            debt_repay = min(max(0.0, debt - min_debt), net_income * 0.3)
            meta['debt'] = max(min_debt, debt - debt_repay)
            if meta['assets'] > 0:
                meta['debt_ratio'] = meta['debt'] / meta['assets']

            # HP 회복량 (0.8배)
            heal   = (net_income / assets * 100) * 0.8
            new_hp = hp + heal

            if new_hp <= soft_cap:
                meta['hp'] = round(new_hp, 2)
            else:
                # soft_cap 초과분 → 전 체급 쉴드 적립 (상한선 적용)
                overflow      = new_hp - soft_cap
                meta['hp']    = soft_cap
                shield_gain   = overflow * (assets * spec['accum'])
                meta['shield'] = round(min(shield_cap, shield + shield_gain), 2)

                # 소형주는 추가로 주가 펌핑 (최대 +30%)
                if '소형' in tier:
                    pump = min(0.30, overflow * 0.02)
                    meta['_hp_overflow_pump'] = pump   # market.py에서 price에 반영
        # ────────────────────────────────────────────────────────

        # 히스토리 기록
        self.s.earnings_history.setdefault(name, {}).setdefault(year_str, {})[quarter] = {
            "date":       self.s.current_date.strftime("%m월 %d일"),
            "revenue":    revenue,
            "op_income":  op_income,
            "net_income": net_income,
            "is_surplus": net_income > 0,
            "surprise":   "공시 완료",
        }

        # assets는 적자/흑자 각 블록에서 처리됨 (위에서 처리)
        # ★ 흑자 시 assets 증가 — 재투자 비율 제한 + 상한 강화
        if net_income > 0:
            init_assets_v = meta.get('initial_assets', meta['assets'])
            # 티어별 재투자율 (나머지는 배당/자사주매입으로 유출)
            # 현실: 대기업은 이익의 30~50%만 재투자
            _reinvest = {
                '대형주': 0.30,  # 30% 재투자
                '중형주': 0.50,  # 50% 재투자
                '소형주': 0.70,  # 70% 재투자 (성장기)
            }.get(tier, 0.50)
            reinvested = net_income * _reinvest
            meta['assets'] = meta['assets'] + reinvested

        # ★ 신용등급 복합 판정 (HP + 부채비율 + 연속적자)
        hp_now     = meta.get('hp', 50.0)
        sc_now     = meta.get('hp_soft_cap', 60.0)
        hp_pct_now = hp_now / max(1.0, sc_now)
        dr_now     = meta.get('debt_ratio', 0.5)
        lc_now     = meta.get('continuous_loss_count', 0)

        if hp_pct_now >= 0.80 and dr_now < 1.0 and lc_now == 0:
            meta['credit_grade'] = 'AA'
        elif hp_pct_now >= 0.50 and dr_now < 2.0 and lc_now < 4:
            meta['credit_grade'] = 'BB'
        else:
            meta['credit_grade'] = 'CCC'

        # market.py 지배구조/momentum 반영용 플래그
        meta['_earnings_just_released'] = True

        # ★ 실적 서프라이즈/쇼크 — 컨센서스 대비로 계산
        consensus = self.s.earnings_consensus.get(name, {})
        consensus_ni = consensus.get('expected_ni', net_income)  # 컨센서스 없으면 실제값 사용

        if abs(consensus_ni) > 0:
            surprise_ratio = (net_income - consensus_ni) / abs(consensus_ni)
        elif net_income > 0:
            surprise_ratio = 0.15   # 흑자 전환
        else:
            surprise_ratio = -0.15  # 적자 전환

        # 서프라이즈 강도에 따라 shock 값 결정
        if surprise_ratio > 0.30:      # 컨센서스 30%+ 초과 → 강한 서프라이즈
            meta['_earnings_shock'] = min(0.20, surprise_ratio * 0.3)
        elif surprise_ratio > 0.10:    # 10~30% 초과 → 약한 서프라이즈
            meta['_earnings_shock'] = min(0.10, surprise_ratio * 0.2)
        elif surprise_ratio < -0.30:   # 컨센서스 30%+ 미달 → 강한 쇼크
            meta['_earnings_shock'] = max(-0.25, surprise_ratio * 0.3)
        elif surprise_ratio < -0.10:   # 10~30% 미달 → 약한 쇼크
            meta['_earnings_shock'] = max(-0.12, surprise_ratio * 0.2)
        else:                          # ±10% 이내 → 무반응
            meta['_earnings_shock'] = 0.0

        # 컨센서스 업데이트 (발표 후 실제값으로 갱신)
        self.s.earnings_consensus[name] = {
            "expected_ni": net_income,
            "direction":   1 if net_income > 0 else -1,
            "confidence":  0.9,
            "prev_ni":     consensus_ni,
        }

        return True

    # ─────────────────────────────────────────────
    # 실적 공시 스케줄 처리 (next_day에서 호출)
    # ─────────────────────────────────────────────
    def process_earnings_schedule(self, silent: bool):
        cur_month = self.s.current_date.month
        cur_day   = self.s.current_date.day

        # 1일: 다음 실적 발표일 예약 + 수치 확정
        if cur_month in [2, 5, 8, 11] and cur_day == 1:
            # ★ INDUSTRY_LEVELS 캐시 — 400종목 루프에서 매번 import 방지
            from engine.constants import INDUSTRY_LEVELS as _IL
            current_lv = self.s.max_tech_reached
            # 현재 레벨 산업 목록 및 구세대 산업 목록을 미리 계산 (루프 밖 1회)
            _current_lv_cache = {
                ind: _IL.get(ind, {}).get(current_lv, [])
                for ind in _IL
            }
            _old_lv_cache = {
                ind: [s for lv in range(1, current_lv) for s in _IL.get(ind, {}).get(lv, [])]
                for ind in _IL
            }
            for stock in self.s.stocks:
                meta = stock['meta']
                meta['report_day']        = random.randint(7, 28)
                meta['earning_news_date'] = self.s.current_date.strftime('%Y-%m-%d')
                meta['expected_earnings'] = self.calculate_potential_earnings(
                    stock, _current_lv_cache, _old_lv_cache
                )
                is_surplus = meta['expected_earnings'].get('is_surplus', True)
                meta['momentum'] += 0.05 if is_surplus else -0.05

        # 공시 발표일 처리
        report_target_months = [3, 4, 6, 7, 9, 10, 12]
        if cur_month not in report_target_months:
            return

        q_map = {3: "1분기", 4: "1분기", 6: "2분기", 7: "2분기",
                 9: "3분기", 10: "3분기", 12: "4분기"}
        target_q        = q_map.get(cur_month, "분기")
        target_year_str = str(self.s.current_date.year)

        for stock in self.s.stocks:
            meta    = stock['meta']
            history = self.s.earnings_history.setdefault(meta['c_name'], {}).setdefault(target_year_str, {})

            if target_q not in history:
                report_day  = meta.get('report_day', 15)
                is_deadline = cur_month == 12 and cur_day >= 24

                if (cur_day >= report_day or is_deadline) and self.s.is_market_open:
                    self.announce_earnings(stock, target_year_str)
                    if not silent:
                        self.s.daily_news.append(
                            f"📊 [실적공시] {meta['c_name']} {target_q} 발표"
                        )