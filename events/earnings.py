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
    def calculate_potential_earnings(self, stock: dict) -> dict:
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

        revenue   = meta['assets'] * random.uniform(0.04, 0.10) * cycle_revenue_mult
        base_cost = {"대형주": 0.015, "중형주": 0.020, "소형주": 0.030}.get(tier, 0.030)
        base_cost *= cycle_cost_mult   # 경기 사이클 비용 반영
        tier_bonus = {"대형주": 0.02, "중형주": 0.01, "소형주": 0.00}.get(tier, 0)

        op_margin  = meta['efficiency'] - base_cost + tier_bonus
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
            meta['continuous_loss_count'] = meta.get('continuous_loss_count', 0) + 1

            from engine.constants import SECTOR_MAP
            scenario = self.s.current_scenario
            sector   = SECTOR_MAP.get(meta.get('ind', ''), 'Value')
            if '대공황' in scenario and '극복' not in scenario:
                sw = 1.3 if sector == 'Growth' else 0.6 if sector == 'Defensive' else 1.0
            else:
                sw = 1.0

            loss_ratio = abs(net_income) / max(1.0, revenue)
            hp_dmg_raw = loss_ratio * 100 * sensitivity * sw
            _HP_DMG_CAP = {'대형주': 0.5, '중형주': 1.0, '소형주': 1.5}
            hp_dmg = min(hp_dmg_raw, _HP_DMG_CAP.get(tier, 1.5))

            # ★ 방어막 버그 수정: assets 감소해도 방어막 금액 자체가 줄어야 함
            # shield_cap 기준으로 방어막 상한 재조정
            shield_cap_now = market_cap * spec['cap_ratio']
            if shield > shield_cap_now:
                meta['shield'] = shield = round(shield_cap_now, 2)

            # 방어막이 너무 작으면 소진 처리 (무한 지속 방지)
            if shield < 100:
                meta['shield'] = shield = 0.0

            # HP가 soft_cap의 30% 미만일 때만 쉴드 발동
            hp_ratio    = hp / max(1.0, soft_cap)
            shield_mode = hp_ratio < 0.30

            if shield_mode and shield > 0:
                # 방어막으로 HP 차감 방어
                # shield_hp_val: 방어막이 HP 몇 포인트 방어 가능한지
                shield_hp_val = shield / max(1.0, assets) * 100
                if shield_hp_val >= hp_dmg:
                    meta['shield'] = round(max(0.0, shield - hp_dmg / 100 * assets), 2)
                else:
                    remaining_dmg  = hp_dmg - shield_hp_val
                    meta['shield'] = 0.0
                    meta['hp']     = round(max(0.0, hp - remaining_dmg), 2)
            else:
                meta['hp'] = round(max(0.0, hp - hp_dmg), 2)

            # ★ 적자 시 assets 감소 (하한선 보장)
            init_assets = meta.get('initial_assets', assets)
            meta['assets'] = max(
                init_assets * 0.1,   # 하한선: 초기값의 10%
                meta['assets'] + net_income
            )

            # ★ 적자 시 assets 감소 (하한선 보장)
            init_assets = meta.get('initial_assets', assets)
            meta['assets'] = max(
                init_assets * 0.1,
                meta['assets'] + net_income
            )

        else:
            # ── 흑자: 연속 적자 초기화 + HP 회복 + 오버플로우 쉴드 적립 ──
            meta['continuous_loss_count'] = 0

            # HP 회복량 상향 (0.5 → 0.8배)
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
        # 흑자 시 assets 증가
        if net_income > 0:
            init_assets_v = meta.get('initial_assets', meta['assets'])
            meta['assets'] = min(
                init_assets_v * 100,  # 상한선: 초기값의 100배
                meta['assets'] + net_income
            )

        # market.py 지배구조/momentum 반영용 플래그
        meta['_earnings_just_released'] = True
        # 실적 서프라이즈 충격 (momentum에 반영됨)
        if net_income > 0:
            surprise = min(0.15, net_income / max(1.0, meta['assets']) * 2.0)
            meta['_earnings_shock'] = surprise
        else:
            shock = max(-0.20, -(abs(net_income) / max(1.0, meta['assets']) * 3.0))
            meta['_earnings_shock'] = shock

        return True

    # ─────────────────────────────────────────────
    # 실적 공시 스케줄 처리 (next_day에서 호출)
    # ─────────────────────────────────────────────
    def process_earnings_schedule(self, silent: bool):
        cur_month = self.s.current_date.month
        cur_day   = self.s.current_date.day

        # 1일: 다음 실적 발표일 예약 + 수치 확정
        if cur_month in [2, 5, 8, 11] and cur_day == 1:
            for stock in self.s.stocks:
                meta = stock['meta']
                meta['report_day']         = random.randint(7, 28)
                meta['earning_news_date']  = self.s.current_date.strftime('%Y-%m-%d')
                meta['expected_earnings']  = self.calculate_potential_earnings(stock)
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