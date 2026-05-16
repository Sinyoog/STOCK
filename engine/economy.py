"""
engine/economy.py
거시경제(MacroEngine): 금리·유가·환율·CPI 연산, 시나리오 전환, 기술 레벨 진화.
UI 코드 금지.
"""
import random
import math
from datetime import timedelta
from .constants import SECTOR_MAP


class MacroEngine:
    def __init__(self, state):
        self.s = state  # MarketState 참조

    # ─────────────────────────────────────────────
    # 기술 레벨
    # ─────────────────────────────────────────────
    def get_tech_level(self) -> int:
        cy = self.s.current_date.year
        current_lv = self.s.max_tech_reached

        is_depression = "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario
        is_t4_world   = "T4 발전"  in self.s.current_scenario
        is_t3_world   = "T3 유지"  in self.s.current_scenario

        if is_depression:
            return current_lv

        evolution_chance = 0.0

        if self.s.pending_events.get("tech_jump"):
            evolution_chance = 0.0
        else:
            if current_lv == 1 and self.s.gri >= 2500:
                t1_probs = {2013: 12, 2014: 18, 2015: 30, 2016: 18, 2017: 12}
                if cy in t1_probs:   evolution_chance = t1_probs[cy] / 100 / 252
                elif cy >= 2018:     evolution_chance = 0.10 / 252

            elif current_lv == 2 and self.s.gri >= 14000:
                t2_probs = {2035: 2, 2036: 4, 2037: 5, 2038: 9, 2039: 13,
                            2040: 25, 2041: 13, 2042: 9, 2043: 5, 2044: 5}
                if cy in t2_probs:   evolution_chance = t2_probs[cy] / 100 / 252
                elif cy >= 2045:     evolution_chance = 0.10 / 252

            elif current_lv == 3:
                if is_t3_world:   return 3
                elif is_t4_world: evolution_chance = 0.1 / 252
                elif cy >= 2050 and self.s.gri >= 150000 and not is_depression:
                    evolution_chance = 0.05 / 252

        # 예약
        if evolution_chance > 0 and random.random() < evolution_chance:
            jump_date = self.s.current_date + timedelta(days=30)
            self.s.pending_events["tech_jump"] = {
                "target_lv": current_lv + 1,
                "date":      jump_date,
            }

        # D-Day 실행
        if self.s.pending_events.get("tech_jump"):
            jump_info = self.s.pending_events["tech_jump"]
            if self.s.current_date.date() >= jump_info["date"].date():
                self.s.max_tech_reached = jump_info["target_lv"]
                if not self.s.silent_mode:
                    self.s.daily_news.append(
                        f"🚀 [시대 진화] {cy}년, 문명이 {self.s.max_tech_reached}단계로 도약했습니다!"
                    )
                self.s.pending_events["tech_jump"] = None

        return self.s.max_tech_reached

    # ─────────────────────────────────────────────
    # 매크로 지표 업데이트
    # ─────────────────────────────────────────────
    def update_macro_logic(self):
        scenario = self.s.current_scenario
        lv = self.get_tech_level()

        progress = 0.0
        MAX_RATE_CHANGE = 0.05 if lv < 3 else 0.1
        MAX_FX_CHANGE   = 5.0

        target_cpi      = {1: 2.0, 2: 2.5, 3: 4.0, 4: 1.0}.get(lv, 2.0)
        target_interest = target_cpi + 1.5
        target_oil      = {1: 45, 2: 90, 3: 140, 4: 25}.get(lv, 50)
        target_fx       = {1: 1150, 2: 1250, 3: 1350, 4: 950}.get(lv, 1150)

        if "💀 대공황" in scenario or "✨ 대공황V" in scenario:
            total_days   = 252 * 10
            elapsed_days = total_days - self.s.scenario_timer
            progress     = max(0.0, min(1.0, elapsed_days / total_days))

            target_cpi      = 2.0  + 23.0  * progress
            target_interest = 4.0  + 31.0  * progress
            target_oil      = target_oil + 150 * progress
            target_fx       = target_fx  + 700 * progress

            if self.s.scenario_timer <= 30:
                recovery_premium = 0.85 if self.s.has_paid_news_access else 0.95
                target_interest *= recovery_premium
                target_cpi      *= recovery_premium

            if "대공황V" in scenario:
                target_interest *= 0.8
                target_cpi      *= 0.9

        elif "극복" in scenario:
            target_cpi      = 8.2
            target_interest = 9.5
            target_oil      = 155.0
            target_fx       = 1450.0

        elif "🚀 T4 발전" in scenario:
            target_cpi      = 0.5
            target_interest = 1.0
            target_oil      = 15.0
            target_fx       = 900.0

        # CPI
        cpi_step = (target_cpi - self.s.macro["cpi"]) * 0.02 + random.uniform(-0.05, 0.05)
        v_factor = 5.0 if "대공황" in scenario and progress > 0.5 else 1.0
        self.s.macro["cpi"] = max(0.5, min(45.0, self.s.macro["cpi"] + cpi_step * v_factor))

        if self.s.macro["cpi"] > target_cpi:
            target_interest += 1.0

        # 금리
        diff_r = (target_interest - self.s.macro["interest_rate"]) * 0.03 + random.uniform(-0.02, 0.02)
        self.s.macro["interest_rate"] += max(-MAX_RATE_CHANGE, min(MAX_RATE_CHANGE, diff_r))
        r_max = 40.0 if "대공황" in scenario else 15.0
        self.s.macro["interest_rate"] = max(0.25, min(r_max, self.s.macro["interest_rate"]))

        # 유가
        oil_drift = (target_oil - self.s.macro["oil_price"]) * 0.01
        self.s.macro["oil_price"] = max(5, self.s.macro["oil_price"] + oil_drift + random.uniform(-1.5, 1.5))

        # 환율
        rate_impact  = (self.s.macro["interest_rate"] - 3.5) * -15
        fear_impact  = progress * 300 if "대공황" in scenario else 0
        final_target_fx = target_fx + rate_impact + fear_impact
        diff_fx = (final_target_fx - self.s.macro["exchange_rate"]) * 0.02 + random.uniform(-10, 10)
        self.s.macro["exchange_rate"] += max(-MAX_FX_CHANGE, min(MAX_FX_CHANGE, diff_fx))
        self.s.macro["exchange_rate"]  = max(800, min(2500, self.s.macro["exchange_rate"]))

        # 물가
        self.s.cumulative_inflation *= (1 + (self.s.macro["cpi"] / 100) / 252)
        self.s.base_item_price       = 1000.0 * self.s.cumulative_inflation

    # ─────────────────────────────────────────────
    # 동적 목표 지수
    # ─────────────────────────────────────────────
    def get_dynamic_target(self) -> float:
        base_growth_rates = {1: 0.08, 2: 0.12, 3: 0.10, 4: 0.15}
        annual_rate = base_growth_rates.get(self.s.max_tech_reached, 0.08)

        if "💀 대공황" in self.s.current_scenario:
            return self.s.gri * 0.96
        elif "🚀 T4 발전" in self.s.current_scenario:
            return self.s.gri * 1.07
        elif "🟢 T3 유지" in self.s.current_scenario:
            annual_rate *= 0.5

        daily_rate = annual_rate / 252
        return self.s.gri * (1 + daily_rate)

    # ─────────────────────────────────────────────
    # 섹터 민감도
    # ─────────────────────────────────────────────
    def apply_macro_sector_sensitivity(self, stock: dict, perf: float) -> float:
        meta   = stock['meta']
        sector = SECTOR_MAP.get(meta['ind'], "Value")

        ex_rate      = self.s.macro["exchange_rate"]
        oil_price    = self.s.macro["oil_price"]
        interest_rate = self.s.macro["interest_rate"]

        fx_diff  = (ex_rate   - 1100.0) / 100.0
        oil_diff = (oil_price - 30.0)   / 50.0

        if sector in ["IT", "산업재", "커뮤니케이션"]:
            perf += fx_diff * 0.0012
        elif sector in ["필수소비재", "유틸리티"]:
            perf -= fx_diff * 0.001

        if sector == "에너지":
            perf += oil_diff * 0.0015
        elif sector in ["산업재", "유틸리티", "필수소비재"]:
            perf -= oil_diff * 0.001

        if sector == "금융":
            interest_diff = (interest_rate - 4.0) / 5.0
            perf += interest_diff * 0.0008

        is_interest_sensitive = interest_rate >= 8.0 or interest_rate <= 2.0
        if is_interest_sensitive:
            rate_multiplier = 1.5 if self.s.has_paid_news_access else 1.05
            perf *= rate_multiplier

        return perf

    # ─────────────────────────────────────────────
    # 외인/기관 충격
    # ─────────────────────────────────────────────
    def apply_foreign_inst_shock(self, stock: dict, perf: float) -> float:
        meta       = stock['meta']
        risk_score = meta.get('risk_score', 0.0)
        total_inst = meta.get('foreign_share', 0.0) + meta.get('inst_share', 0.0)

        cur_day = self.s.current_date.day
        current_month = self.s.current_date.month
        is_ipo_period = 14 <= cur_day <= 21 and current_month in [2, 5, 8, 11]

        ipo_multiplier = 1.0
        if is_ipo_period and meta['tier'] == "소형주":
            ipo_multiplier = 2.0 if self.s.has_paid_news_access else 1.2

        if risk_score > 60.0:
            shock_factor = -random.uniform(0.005, 0.025) * min(2.0, total_inst * 3)
        else:
            shock_factor = random.uniform(-0.005, 0.005) * total_inst

        return perf + (shock_factor * ipo_multiplier)
