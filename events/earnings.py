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

        revenue   = meta['assets'] * random.uniform(0.04, 0.10)
        base_cost = {"대형주": 0.015, "중형주": 0.025, "소형주": 0.04}.get(tier, 0.04)
        tier_bonus = {"대형주": 0.02, "중형주": 0.00, "소형주": -0.01}.get(tier, 0)

        op_margin  = meta['efficiency'] - base_cost + tier_bonus
        op_income  = revenue * op_margin
        interest_rate  = self.s.macro.get('interest_rate', 4.0)
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

        # 리스크 반영
        if net_income < 0:
            meta['continuous_loss_count'] = meta.get('continuous_loss_count', 0) + 1
            sensitivity = meta.get('risk_sensitivity', 1.0)
            risk_up = (abs(net_income) / max(1, meta['assets'])) * 10 * sensitivity
            meta['risk_score'] += risk_up
        else:
            meta['continuous_loss_count'] = 0
            recovery = (net_income / max(1, meta['assets'])) * 10
            meta['risk_score'] = max(0, meta['risk_score'] - recovery - 0.5)

        # 히스토리 기록
        self.s.earnings_history.setdefault(name, {}).setdefault(year_str, {})[quarter] = {
            "date":       self.s.current_date.strftime("%m월 %d일"),
            "revenue":    revenue,
            "op_income":  op_income,
            "net_income": net_income,
            "is_surplus": net_income > 0,
            "surprise":   "공시 완료",
        }

        meta['assets'] += net_income
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
