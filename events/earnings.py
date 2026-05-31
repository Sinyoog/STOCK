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

        # ★ 페이즈별 매출 성장 승수 (핵심 — 이익 성장의 근원)
        # 현실: 삼성전자 2000년 매출 30조 → 2025년 300조 (10배)
        # 페이즈가 올라갈수록 시장 규모 자체가 커짐
        # Growth 섹터는 더 크게, Value/Defensive는 완만하게
        from engine.constants import SECTOR_MAP as _SM
        _sector = _SM.get(meta.get('ind', ''), 'Value')
        _cur_phase = getattr(self.s, '_last_processed_phase', '1A')

        _PHASE_REVENUE_MULT = {
            # Growth 섹터: IT/건강/커뮤
            # 목표: 26년간 최상위 기업 10~15배 성장 (현실 삼성전자 수준)
            ("1A", "Growth"):    1.00,
            ("1B", "Growth"):    1.50,   # 1.30 → 1.50
            ("2A", "Growth"):    2.50,   # 2.00 → 2.50
            ("2B", "Growth"):    4.00,   # 3.20 → 4.00
            ("3A", "Growth"):    7.00,   # 5.00 → 7.00  ★ AI 상용화 붐
            ("3B", "Growth"):   12.00,   # 7.50 → 12.0  ★ 양자/바이오 폭발
            ("4A", "Growth"):   18.00,   # 11.0 → 18.0
            ("4B", "Growth"):   28.00,   # 16.0 → 28.0
            # Cyclical: 자유소비재
            ("1A", "Cyclical"):  1.00,
            ("1B", "Cyclical"):  1.25,   # 1.20 → 1.25
            ("2A", "Cyclical"):  1.80,   # 1.70 → 1.80
            ("2B", "Cyclical"):  2.80,   # 2.50 → 2.80
            ("3A", "Cyclical"):  4.20,   # 3.50 → 4.20
            ("3B", "Cyclical"):  6.00,   # 5.00 → 6.00
            ("4A", "Cyclical"):  8.50,   # 7.00 → 8.50
            ("4B", "Cyclical"): 12.00,   # 10.0 → 12.0
            # Value: 에너지/금융/산업재/소재/부동산
            ("1A", "Value"):     1.00,
            ("1B", "Value"):     1.20,   # 1.15 → 1.20
            ("2A", "Value"):     1.50,   # 1.40 → 1.50
            ("2B", "Value"):     2.00,   # 1.80 → 2.00
            ("3A", "Value"):     2.80,   # 2.30 → 2.80
            ("3B", "Value"):     3.80,   # 3.00 → 3.80
            ("4A", "Value"):     5.00,   # 3.80 → 5.00
            ("4B", "Value"):     7.00,   # 5.00 → 7.00
            # Defensive: 필수소비재/유틸 — 완만 (큰 변화 없음)
            ("1A", "Defensive"): 1.00,
            ("1B", "Defensive"): 1.08,
            ("2A", "Defensive"): 1.20,
            ("2B", "Defensive"): 1.40,   # 1.38 → 1.40
            ("3A", "Defensive"): 1.65,   # 1.60 → 1.65
            ("3B", "Defensive"): 1.90,   # 1.85 → 1.90
            ("4A", "Defensive"): 2.20,   # 2.15 → 2.20
            ("4B", "Defensive"): 2.60,   # 2.50 → 2.60
        }
        phase_rev_mult = _PHASE_REVENUE_MULT.get((_cur_phase, _sector), 1.0)

        # ★ 버그 방지: 승수 상한 — 너무 빠른 성장 억제
        scenario = self.s.current_scenario
        if "대공황" in scenario and "극복" not in scenario:
            phase_rev_mult = max(1.0, phase_rev_mult * 0.5)
        elif cycle in ("수축", "저점"):
            phase_rev_mult = max(1.0, phase_rev_mult * 0.75)

        # ★ 수출/내수 의존도 기반 성장 차등
        from engine.constants import EXPORT_DEPENDENCY, EXPORT_SANCTION_TYPES
        ind = meta.get('ind', '')
        export_dep = EXPORT_DEPENDENCY.get(ind, 0.3)

        # 수출 호황 시나리오 — 수출 의존도 높을수록 더 큰 수혜
        boom = getattr(self.s, 'boom_event', {})
        if boom.get('phase') == '진행중' and boom.get('type') == '수출호황':
            export_bonus = 1.0 + (export_dep * 0.8)  # IT(0.75): +60%, 유틸(0.03): +2.4%
            phase_rev_mult *= export_bonus

        # 내수 붐 시나리오 — 내수 의존도 높을수록 더 큰 수혜
        elif boom.get('phase') == '진행중' and boom.get('type') == '내수붐':
            domestic_dep = 1.0 - export_dep
            domestic_bonus = 1.0 + (domestic_dep * 0.5)
            phase_rev_mult *= domestic_bonus

        # ★ 내수형 섹터 자연 성장 상한 (유틸/부동산/금융)
        # 아무리 페이즈가 올라가도 정부규제/인구한계로 제한
        _DOMESTIC_CAP = {
            "유틸리티":   2.0,   # 한전급 — 정부 요금 통제
            "부동산":     2.5,   # 국내 시장 한계
            "금융":       2.8,   # 예대마진 한계
            "필수소비재": 2.0,   # 인구 정체 (K-식품 수출 예외는 boom에서 처리)
        }
        if ind in _DOMESTIC_CAP:
            phase_rev_mult = min(phase_rev_mult, _DOMESTIC_CAP[ind])

        # ★ 수출 규제 이벤트 적용
        sanctions = getattr(self.s, 'export_sanctions', {})
        for sanction_id, sanction_state in sanctions.items():
            s_def = EXPORT_SANCTION_TYPES.get(sanction_id, {})
            if ind not in s_def.get('target_inds', []):
                continue
            phase = sanction_state.get('phase', '')
            if phase == '단기충격':
                # 단기 패널티: 매출 승수 축소
                penalty = s_def.get('short_penalty', -0.10)
                phase_rev_mult *= (1.0 + penalty)
                phase_rev_mult = max(0.5, phase_rev_mult)
            elif phase == '중장기수혜':
                # 공급망 재편 수혜: 매출 승수 소폭 증가
                benefit = s_def.get('long_benefit', 0.05)
                phase_rev_mult *= (1.0 + benefit)

        # ★ 시총 기반 성장 감쇠 — 기업이 커질수록 기본 성장 둔화
        # 호재(boom/수출/내수/수출규제)는 이미 위에서 phase_rev_mult에 반영됨
        # 여기서는 기업 자체 규모에 따른 자연 둔화만 적용
        # 단, 페이즈 전환 직후 1년간은 감쇠 완화 (LV전환 랠리 보호)
        _mc = stock.get('market_cap', 0)
        _조 = 1_000_000_000_000
        if   _mc < 1 * _조:    _mc_decay = 1.00   # 1조 미만: 풀 성장
        elif _mc < 5 * _조:    _mc_decay = 0.95
        elif _mc < 10 * _조:   _mc_decay = 0.88
        elif _mc < 30 * _조:   _mc_decay = 0.78
        elif _mc < 80 * _조:   _mc_decay = 0.65
        elif _mc < 200 * _조:  _mc_decay = 0.52
        else:                  _mc_decay = 0.42   # 200조+: 대형 우량주 수준

        # 페이즈 전환 부스트: 전환 후 252일간 감쇠 완화
        _trans_day   = getattr(self.s, '_phase_transition_day', -9999)
        _cur_day     = getattr(self.s, 'cycle_day', 0)
        _since_trans = _cur_day - _trans_day
        if 0 <= _since_trans <= 252:
            _boost = 1.0 - (_since_trans / 252.0)
            _mc_decay = _mc_decay + (1.0 - _mc_decay) * _boost

        # 감쇠는 기본 페이즈 성장 부분에만 적용
        # boom/수출/수출규제 등 이벤트 승수는 별도 보호
        _base_rev_mult = _PHASE_REVENUE_MULT.get((_cur_phase, _sector), 1.0)
        _event_bonus   = phase_rev_mult / max(0.01, _base_rev_mult)  # 이벤트가 올린 배율
        phase_rev_mult = (_base_rev_mult * _mc_decay) * _event_bonus

        revenue *= phase_rev_mult

        # ★ 테크 레벨 성장 가중치 — 페이즈 기반으로 수정
        try:
            ind = meta.get('ind', '')
            # sub_list 우선, 없으면 sub 단일값으로 fallback
            sub_list = meta.get('sub_list') or [meta.get('sub', '')]
            current_subs = [s.strip() for s in sub_list if s]

            if _current_lv_cache is not None:
                current_phase_subs = _current_lv_cache.get(ind, set())
                old_phase_subs     = _old_lv_cache.get(ind, set())
            else:
                from engine.constants import INDUSTRY_LEVELS, TECH_PHASE
                ind_data   = INDUSTRY_LEVELS.get(ind, {})
                cur_phase  = getattr(self.s, '_last_processed_phase', '1A')
                # 현재 페이즈 전체 subs
                current_phase_subs = set()
                for t_list in ind_data.get(cur_phase, {}).values():
                    current_phase_subs.update(t_list)
                for t_list in ind_data.get('common', {}).values():
                    current_phase_subs.update(t_list)
                # 과거 페이즈 subs
                old_phase_subs = set()
                lv = self.s.max_tech_reached
                all_phases = [p["id"] for p in TECH_PHASE.get(lv, [])]
                for ph in all_phases:
                    if ph == cur_phase:
                        break
                    for t_list in ind_data.get(ph, {}).values():
                        old_phase_subs.update(t_list)

            if any(s in current_phase_subs for s in current_subs):
                # ★ 현재 페이즈 사업: 페이즈가 높을수록 더 큰 보너스
                _phase_tech_bonus = {
                    "1A": 1.10, "1B": 1.15,
                    "2A": 1.25, "2B": 1.35,
                    "3A": 1.45, "3B": 1.55,
                    "4A": 1.65, "4B": 1.80,
                }.get(_cur_phase, 1.30)
                tech_mult = _phase_tech_bonus
            elif any(s in old_phase_subs for s in current_subs):
                # ★ 구시대 사업: 페이즈 격차가 클수록 더 큰 패널티
                tech_mult = 0.85  # 0.9 → 0.85 강화
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

        # ★ efficiency 점진적 수렴 — 코스피 기준 + 페이즈 성장 반영
        # 코스피 평균 PER 10~13배 목표
        # 페이즈가 올라갈수록 기업들의 수익성이 자연스럽게 개선됨
        _tier      = meta.get('tier', '소형주')
        _eff_now   = meta.get('efficiency', 0.03)
        _cur_phase = getattr(self.s, '_last_processed_phase', '1A')

        # 페이즈별 efficiency 상한 — 높은 페이즈일수록 수익성 개선 허용
        _PHASE_EFF_CAP = {
            '1A': {'대형주': 0.14, '중형주': 0.10, '소형주': 0.07},
            '1B': {'대형주': 0.16, '중형주': 0.12, '소형주': 0.08},
            '2A': {'대형주': 0.18, '중형주': 0.14, '소형주': 0.10},
            '2B': {'대형주': 0.20, '중형주': 0.16, '소형주': 0.12},
            '3A': {'대형주': 0.23, '중형주': 0.18, '소형주': 0.14},
            '3B': {'대형주': 0.26, '중형주': 0.21, '소형주': 0.16},
            '4A': {'대형주': 0.30, '중형주': 0.24, '소형주': 0.18},
            '4B': {'대형주': 0.35, '중형주': 0.28, '소형주': 0.22},
        }
        _eff_cap = _PHASE_EFF_CAP.get(_cur_phase, {'대형주': 0.10, '중형주': 0.07, '소형주': 0.04})
        _cap_val = _eff_cap.get(_tier, 0.04)

        # 목표값: 현재 페이즈 상한의 70~90% 수준으로 수렴
        _eff_target = random.uniform(_cap_val * 0.70, _cap_val * 0.90)
        _eff_target = min(_eff_target, _cap_val)  # 상한 초과 방지

        # 시총 기반 수렴 속도 감쇠 — 대기업일수록 효율성 개선 더딤
        _mc_now = stock.get('market_cap', 0)
        _조_e   = 1_000_000_000_000
        if   _mc_now < 1  * _조_e:  _conv_speed = 0.10
        elif _mc_now < 10 * _조_e:  _conv_speed = 0.07
        elif _mc_now < 50 * _조_e:  _conv_speed = 0.04
        else:                        _conv_speed = 0.02

        # 10% → 속도로 수렴 (급격한 변화 방지)
        meta['efficiency'] = min(_cap_val, _eff_now + (_eff_target - _eff_now) * _conv_speed)

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

            # ★ 최소 4분기(1년)치 실적이 쌓인 후에만 HP 차감
            # 첫 발표 때는 비교 기준이 없으므로 HP 차감 없음
            # 현실: 신규 상장 후 첫 적자로 재무위기 판정은 불합리
            _name = meta.get('c_name', '')
            _hist = self.s.earnings_history.get(_name, {})
            _total_q = sum(len(qd) for qd in _hist.values())
            if _total_q < 4:
                # 실적 기록 부족 — HP 차감 없이 assets/debt만 반영
                meta['assets'] = max(
                    meta.get('initial_assets', assets) * 0.01,
                    meta['assets'] + net_income
                )
                debt = meta.get('debt', 0.0)
                meta['debt'] = debt + abs(net_income) * 0.5
                if meta['assets'] > 0:
                    meta['debt_ratio'] = meta['debt'] / meta['assets']
                return True

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
        cur_date  = self.s.current_date
        start_date = getattr(self.s, 'start_date', cur_date)

        # 1일: 다음 실적 발표일 예약 + 수치 확정
        if cur_month in [2, 5, 8, 11] and cur_day == 1:
            # ★ 페이즈 기반 캐시 (기존 숫자 LV 키 → 페이즈 ID 기반)
            from engine.constants import INDUSTRY_LEVELS as _IL, TECH_PHASE
            cur_phase = getattr(self.s, '_last_processed_phase', '1A')
            lv        = self.s.max_tech_reached

            # 현재 페이즈 subs 캐시
            _current_lv_cache = {}
            for ind, ind_data in _IL.items():
                subs = set()
                for t_list in ind_data.get(cur_phase, {}).values():
                    subs.update(t_list)
                for t_list in ind_data.get('common', {}).values():
                    subs.update(t_list)
                _current_lv_cache[ind] = subs

            # 구시대 페이즈 subs 캐시
            all_phases = [p["id"] for p in TECH_PHASE.get(lv, [])]
            _old_lv_cache = {}
            for ind, ind_data in _IL.items():
                old_subs = set()
                for ph in all_phases:
                    if ph == cur_phase:
                        break
                    for t_list in ind_data.get(ph, {}).values():
                        old_subs.update(t_list)
                _old_lv_cache[ind] = old_subs

            for stock in self.s.stocks:
                meta = stock['meta']
                meta['report_day']        = random.randint(7, 28)
                meta['earning_news_date'] = cur_date.strftime('%Y-%m-%d')
                meta['expected_earnings'] = self.calculate_potential_earnings(
                    stock, _current_lv_cache, _old_lv_cache
                )
                is_surplus = meta['expected_earnings'].get('is_surplus', True)
                meta['momentum'] += 0.02 if is_surplus else -0.02

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