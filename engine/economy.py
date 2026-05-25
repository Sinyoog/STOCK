"""
engine/economy.py
MacroEngine: 경기선행지수 기반 사이클, 피드백 루프 3개, 섹터 로테이션.
"""
import random
import math
from datetime import timedelta
from .constants import SECTOR_MAP


class MacroEngine:
    def __init__(self, state):
        self.s = state

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
            if current_lv == 1:
                # Lv1→Lv2: 2013년 이전 불가, GRI 1800 이상 필요
                if cy < 2013 or self.s.gri < 1800:
                    evolution_chance = 0.0
                else:
                    t1_probs = {2013: 5, 2014: 10, 2015: 20, 2016: 30,
                                2017: 20, 2018: 10, 2019: 5}
                    base_chance      = t1_probs.get(cy, 8) / 100 / 252
                    gri_mult         = min(2.0, max(0.5, self.s.gri / 2500))
                    evolution_chance = base_chance * gri_mult
                    if cy >= 2020:
                        evolution_chance = 1.0

            elif current_lv == 2:
                # Lv2→Lv3: 2030년 이전 불가, GRI 7000 이상 필요
                if cy < 2030 or self.s.gri < 7000:
                    evolution_chance = 0.0
                else:
                    t2_probs = {2030: 2, 2031: 3, 2032: 5, 2033: 8,
                                2034: 12, 2035: 18, 2036: 15, 2037: 12,
                                2038: 10, 2039: 8, 2040: 5}
                    base_chance      = t2_probs.get(cy, 8) / 100 / 252
                    gri_mult         = min(2.0, max(0.5, self.s.gri / 12000))
                    evolution_chance = base_chance * gri_mult
                    if cy >= 2042:
                        evolution_chance = max(evolution_chance, 0.15 / 252)
                    if cy >= 2045:
                        evolution_chance = 1.0

            elif current_lv == 3:
                if is_t3_world:   return 3
                # Lv3→Lv4: 2055년 이전 불가, 분기점 선택 후에만
                elif is_t4_world and cy >= 2055:
                    evolution_chance = 0.1 / 252
                elif cy >= 2060 and not is_depression and not is_t3_world:
                    evolution_chance = 0.05 / 252

        if evolution_chance > 0 and random.random() < evolution_chance:
            jump_date = self.s.current_date + timedelta(days=30)
            target_lv = current_lv + 1
            lv_name   = {2: "2단계 (모바일·클라우드 혁명)",
                         3: "3단계 (AI·양자 혁명)",
                         4: "4단계 (기술 특이점)"}.get(target_lv, f"{target_lv}단계")
            self.s.pending_events["tech_jump"] = {
                "target_lv": target_lv,
                "date":      jump_date,
                "lv_name":   lv_name,
                "notified":  False,
            }

        if self.s.pending_events.get("tech_jump"):
            jump_info = self.s.pending_events["tech_jump"]

            raw_date = jump_info.get("date")
            if isinstance(raw_date, str):
                try:
                    from datetime import datetime as _dt
                    jump_info["date"] = _dt.strptime(raw_date[:10], "%Y-%m-%d")
                except Exception:
                    self.s.pending_events["tech_jump"] = None
                    return self.s.max_tech_reached

            if not jump_info.get("notified"):
                jump_info["notified"] = True
                lv_name  = jump_info.get("lv_name", "")
                d_date   = jump_info["date"]
                date_str = d_date.strftime('%Y년 %m월 %d일')
                if self.s.has_paid_news_access:
                    self.s.daily_news.append(
                        f"💎 [테크 도약 D-30 예고] {cy}년, {date_str}에 문명이 {lv_name}로 도약합니다! "
                        f"(프리미엄 전용 정보)"
                    )

            jump_info = self.s.pending_events["tech_jump"]
            if jump_info and self.s.current_date.date() >= jump_info["date"].date():
                new_lv  = jump_info["target_lv"]
                lv_name = jump_info.get("lv_name", f"{new_lv}단계")
                self.s.max_tech_reached   = new_lv
                self.s._tech_upgrade_year = self.s.current_date.year
                # 테크 전환 랠리: 심리 과열 + 경기 확장 강제 전환
                self.s.sentiment   = min(85.0, getattr(self.s, 'sentiment', 50.0) + 25.0)
                self.s.cycle_stage = "확장"
                self.s.cycle_day   = 0
                self.s.daily_news.append(
                    f"🚀 [시대 진화] {cy}년, 문명이 {lv_name}로 도약했습니다! "
                    f"산업 전반에 대규모 기술 혁신이 시작됩니다."
                )
                # 테크 전환 섹터 충격
                self._apply_tech_shock(new_lv)
                self.s.pending_events["tech_jump"] = None

        return self.s.max_tech_reached

    def _apply_tech_shock(self, new_lv: int):
        """테크 전환 시 섹터별 efficiency 영구 조정"""
        # Lv1→Lv2
        if new_lv == 2:
            boost  = {"IT": 0.20, "커뮤니케이션": 0.15, "자유소비재": 0.10, "금융": 0.08}
            penalty= {"에너지": -0.10}
        # Lv2→Lv3
        elif new_lv == 3:
            boost  = {"건강관리": 0.25, "IT": 0.20, "산업재": 0.15, "소재": 0.10}
            penalty= {"필수소비재": -0.05, "유틸리티": -0.10}
        # Lv3→Lv4
        elif new_lv == 4:
            boost  = {"IT": 0.30, "건강관리": 0.25, "금융": 0.15}
            penalty= {"에너지": -0.15, "부동산": -0.05}
        else:
            return

        for stock in self.s.stocks:
            meta   = stock['meta']
            sector = SECTOR_MAP.get(meta.get('ind', ''), 'Value')
            ind    = meta.get('ind', '')
            # sector 이름과 ind 이름 모두 체크
            for key, delta in {**boost, **penalty}.items():
                if key in ind or key == sector:
                    old_eff = meta.get('efficiency', 0.05)
                    meta['efficiency'] = max(0.005, min(0.30, old_eff + old_eff * delta))
                    break

        lv_name = {2: "모바일·클라우드", 3: "AI·양자", 4: "기술 특이점"}.get(new_lv, "")
        self.s.daily_news.append(
            f"📊 [테크 충격] {lv_name} 혁명 — 수혜 섹터 효율 상승, 구시대 산업 타격"
        )

    # ─────────────────────────────────────────────
    # 경기선행지수 계산
    # ─────────────────────────────────────────────
    def calc_leading_index(self) -> float:
        """
        Conference Board 방식의 경기선행지수.
        게임 내 지표들로 구성:
          1. 금리 방향성   : 최근 금리가 내리면 +, 오르면 -
          2. GRI 모멘텀    : 최근 20일 GRI 변화율
          3. 실적 방향성   : avg_earnings_growth
          4. 물가 안정성   : CPI vs 목표치
        반환값: -1.0 ~ +1.0
        """
        macro = self.s.macro
        lv    = self.s.max_tech_reached
        target_cpi = {1: 2.0, 2: 2.5, 3: 4.0, 4: 1.0}.get(lv, 2.0)

        # 1. 금리 방향성 (전일 스냅샷과 비교)
        prev = getattr(self.s, '_prev_macro_snapshot', {})
        prev_rate = prev.get('interest_rate', macro['interest_rate'])
        rate_delta = macro['interest_rate'] - prev_rate
        rate_score = max(-0.4, min(0.4, -rate_delta * 10))  # 금리 오르면 음수

        # 2. GRI 모멘텀 (최근 20일)
        hist = getattr(self.s, '_gri_history_20', [])
        if len(hist) >= 20:
            gri_mom = (hist[-1] - hist[0]) / max(1.0, hist[0])
            gri_score = max(-0.3, min(0.3, gri_mom * 5))
        else:
            gri_score = 0.05  # 초반엔 약간 긍정

        # 3. 실적 방향성 (초반 2년은 최솟값 보정)
        earn = getattr(self.s, 'avg_earnings_growth', 0.05)
        cur_year = self.s.current_date.year
        if cur_year <= 2001:
            earn = max(0.02, earn)  # 초반엔 음수로 안 내려가게
        earn_score = max(-0.2, min(0.2, earn * 2))

        # 4. 물가 안정성 (CPI가 목표보다 낮으면 +)
        cpi_diff = target_cpi - macro['cpi']
        cpi_score = max(-0.2, min(0.2, cpi_diff * 0.05))

        leading = rate_score + gri_score + earn_score + cpi_score
        return max(-1.0, min(1.0, leading))

    def _update_cycle_stage(self, leading: float):
        """경기 사이클 단계 업데이트 - 최소 지속 기간 적용"""
        s = self.s
        prev_stage = getattr(s, 'cycle_stage', '확장')
        s.cycle_day = getattr(s, 'cycle_day', 0) + 1

        # 선행지수 기반 단계 결정
        if leading >= 0.15:
            new_stage = "확장"
        elif leading >= 0.0:
            new_stage = "정점"
        elif leading >= -0.15:
            new_stage = "수축"
        else:
            new_stage = "저점"

        # 최소 지속 기간 (현실 경기 사이클: 각 국면 최소 6개월~2년)
        # 박스피처럼 수축이 오래 지속될 수 있어야 함
        MIN_DAYS = {
            "확장": 63,     # 최소 3개월 (선행지수가 나쁘면 빨리 전환 가능)
            "정점": 21,     # 최소 1개월
            "수축": 126,    # 최소 6개월
            "저점": 42,     # 최소 2개월
        }

        if new_stage != prev_stage:
            min_d = MIN_DAYS.get(prev_stage, 63)
            if s.cycle_day < min_d:
                return  # 최소 기간 미달 시 전환 안 함

            # 선행지수가 강하게 긍정적이면 확장 유지 (강제 전환 방지)
            # 예) 확장기인데 leading=0.5면 정점으로 안 넘어감
            if prev_stage == "확장" and new_stage == "정점":
                if leading > 0.30:
                    return   # 선행지수가 충분히 좋으면 확장 유지
            elif prev_stage == "정점" and new_stage == "수축":
                if leading > 0.10:
                    return   # 선행지수가 아직 양수면 수축 안 함

            s.cycle_day = 0
            s.cycle_stage = new_stage
            if not s.silent_mode:
                emoji = {"확장": "📈", "정점": "🔝", "수축": "📉", "저점": "🔻"}.get(new_stage, "")
                s.daily_news.append(
                    f"{emoji} [경기 전환] 경기 국면이 '{prev_stage}' → '{new_stage}'로 전환됩니다."
                )

    # ─────────────────────────────────────────────
    # 투자심리 업데이트
    # ─────────────────────────────────────────────
    def _update_sentiment(self, leading: float):
        """투자심리 (0~100): 50 중립, 80+ 과열, 20- 공포"""
        s = self.s
        sentiment = getattr(s, 'sentiment', 50.0)

        # 선행지수에 따라 서서히 이동
        target = 50.0 + leading * 40.0   # leading +1.0 → target 90, -1.0 → target 10
        diff   = (target - sentiment) * 0.02  # 하루 2% 속도로 수렴
        noise  = random.uniform(-0.5, 0.5)
        s.sentiment = max(5.0, min(95.0, sentiment + diff + noise))

    # ─────────────────────────────────────────────
    # 매크로 지표 업데이트
    # ─────────────────────────────────────────────
    def update_macro_logic(self):
        scenario = self.s.current_scenario
        lv       = self.get_tech_level()
        macro    = self.s.macro

        # ── GRI 히스토리 20일 유지 ────────────────
        hist = getattr(self.s, '_gri_history_20', [])
        hist.append(self.s.gri)
        if len(hist) > 20:
            hist.pop(0)
        self.s._gri_history_20 = hist

        # ── 선행지수 & 사이클 단계 ────────────────
        is_depression = "대공황" in scenario and "극복" not in scenario
        if not is_depression:
            leading = self.calc_leading_index()
            self.s.leading_index = leading
            self._update_cycle_stage(leading)
            self._update_sentiment(leading)
        else:
            leading = self.s.leading_index  # 대공황 중엔 선행지수 고정

        cycle = getattr(self.s, 'cycle_stage', '확장')

        # ── 기준 목표값 ───────────────────────────
        target_cpi      = {1: 2.0, 2: 2.5, 3: 4.0, 4: 1.0}.get(lv, 2.0)
        target_interest = target_cpi + 1.5
        target_oil      = {1: 45, 2: 90, 3: 140, 4: 25}.get(lv, 50)
        target_fx       = {1: 1150, 2: 1250, 3: 1350, 4: 950}.get(lv, 1150)

        MAX_RATE_CHANGE = 0.05

        # ── 소조정 ───────────────────────────────
        if "📉 소조정" in scenario:
            if getattr(self.s, 'scenario_timer', 0) > 180:
                self.s.current_scenario = "📈 일반 성장"
                self.s.daily_news.append("📈 [소조정 종료] 시장이 안정을 되찾았습니다.")
            else:
                self.s.scenario_timer = getattr(self.s, 'scenario_timer', 0) + 1
                diff_r = (5.0 - macro["interest_rate"]) * 0.05
                macro["interest_rate"] += max(-0.1, min(0.1, diff_r))
                cpi_step = (3.0 - macro["cpi"]) * 0.01
                macro["cpi"] = max(0.5, min(45.0, macro["cpi"] + cpi_step))
                oil_drift = (40.0 - macro["oil_price"]) * 0.005
                macro["oil_price"] = max(5, macro["oil_price"] + oil_drift)
                self.s.cumulative_inflation *= (1 + (macro["cpi"] / 100) / 252)
                self.s.base_item_price = 1000.0 * self.s.cumulative_inflation
                # 전일 스냅샷 업데이트
                self.s._prev_macro_snapshot = {k: macro[k] for k in macro}
                return

        # ── 대공황 ───────────────────────────────
        if is_depression or "✨ 대공황V" in scenario:
            total_days   = 252 * 10
            timer        = getattr(self.s, 'scenario_timer', 0)
            elapsed_days = total_days - timer
            progress     = max(0.0, min(1.0, elapsed_days / total_days))

            # 현실적: 초기 금리 인하(양적완화), 중기 물가 상승, 후기 정상화
            if progress < 0.2:
                # 초기: 중앙은행 대응 → 금리 인하
                target_interest = max(0.25, target_cpi - 1.0)
                target_cpi      = target_cpi * 0.8   # 디플레 압력
                target_oil      *= 0.6                # 유가 급락
            elif progress < 0.6:
                # 중기: 저금리 유지 + 물가 서서히 상승
                target_interest = target_cpi + 0.5
                target_cpi      = target_cpi + 6.0 * (progress - 0.2) / 0.4
                target_oil      = target_oil * (0.6 + 0.4 * (progress - 0.2) / 0.4)
            else:
                # 후기: 정상화
                target_interest = target_cpi + 2.0
                target_oil      *= 0.9

            # 상한 (현실적: 금리 최대 12%, CPI 최대 10%)
            target_interest = min(12.0, target_interest)
            target_cpi      = min(10.0, target_cpi)
            target_fx       = target_fx + 300 * progress

            if "대공황V" in scenario:
                target_interest *= 0.8
                target_cpi      *= 0.9

        elif "극복" in scenario:
            target_cpi      = 5.0
            target_interest = 5.5
            target_oil      = target_oil * 1.1
            target_fx       = target_fx + 150

        elif "🚀 T4 발전" in scenario:
            target_cpi      = 0.5
            target_interest = 1.0
            target_oil      = 15.0
            target_fx       = 900.0

        else:
            # ── 정상 국면: 경기 사이클 연동 ─────────
            # 피드백 루프 1: GRI 고성장 → CPI 목표 상승 → 금리 인상
            gri_growth_annual = 0.0
            if len(hist) >= 20:
                gri_growth_annual = (hist[-1] / max(1.0, hist[0]) - 1.0) * (252 / 20)
            # GRI 연 30% 이상 성장할 때만 CPI 압력 (기존 15% → 30%로 완화)
            if gri_growth_annual > 0.30:
                target_cpi += min(1.0, (gri_growth_annual - 0.30) * 3)

            # 경기 사이클별 금리 목표
            if cycle == "확장":
                target_interest = target_cpi + 0.5   # 완화: 1.5 → 0.5
            elif cycle == "정점":
                target_interest = target_cpi + 1.5   # 완화: 2.5 → 1.5
            elif cycle == "수축":
                target_interest = max(0.5, target_cpi - 0.5)  # 인하
            elif cycle == "저점":
                target_interest = max(0.25, target_cpi - 1.5)  # 적극 인하

        # ── CPI 업데이트 ──────────────────────────
        cpi_step = (target_cpi - macro["cpi"]) * 0.02 + random.uniform(-0.05, 0.05)
        macro["cpi"] = max(0.3, min(15.0, macro["cpi"] + cpi_step))

        if macro["cpi"] > target_cpi + 1.0:
            target_interest += 0.5

        # ── 금리 업데이트 ─────────────────────────
        diff_r = (target_interest - macro["interest_rate"]) * 0.03 + random.uniform(-0.02, 0.02)
        macro["interest_rate"] += max(-MAX_RATE_CHANGE, min(MAX_RATE_CHANGE, diff_r))
        # Lv1 정상 성장기 금리 상한 3% (이자비용 폭증 방지)
        if lv == 1 and not is_depression and "대공황" not in scenario:
            r_max = 3.5
        elif is_depression:
            r_max = 12.0
        else:
            r_max = 8.0
        macro["interest_rate"] = max(0.1, min(r_max, macro["interest_rate"]))

        # ── 유가 업데이트 ─────────────────────────
        oil_drift = (target_oil - macro["oil_price"]) * 0.01
        macro["oil_price"] = max(5, macro["oil_price"] + oil_drift + random.uniform(-1.5, 1.5))

        # ── 환율 업데이트 ─────────────────────────
        rate_impact  = (macro["interest_rate"] - 3.5) * -15
        final_target_fx = target_fx + rate_impact
        diff_fx = (final_target_fx - macro["exchange_rate"]) * 0.02 + random.uniform(-10, 10)
        macro["exchange_rate"] += max(-5.0, min(5.0, diff_fx))
        macro["exchange_rate"]  = max(800, min(2200, macro["exchange_rate"]))

        # ── 물가 누적 ────────────────────────────
        self.s.cumulative_inflation *= (1 + (macro["cpi"] / 100) / 252)
        self.s.base_item_price = 1000.0 * self.s.cumulative_inflation

        # ── 전일 스냅샷 갱신 ─────────────────────
        self.s._prev_macro_snapshot = {k: macro[k] for k in macro}

    # ─────────────────────────────────────────────
    # 동적 목표 지수
    # ─────────────────────────────────────────────
    def get_dynamic_target(self) -> float:
        base_growth_rates = {1: 0.08, 2: 0.12, 3: 0.10, 4: 0.15}
        annual_rate = base_growth_rates.get(self.s.max_tech_reached, 0.08)

        if "💀 대공황" in self.s.current_scenario:
            return self.s.gri * 0.96
        elif "📉 소조정" in self.s.current_scenario:
            return self.s.gri * 0.985
        elif "🚀 T4 발전" in self.s.current_scenario:
            return self.s.gri * 1.07
        elif "🟢 T3 유지" in self.s.current_scenario:
            annual_rate *= 0.5

        daily_rate = annual_rate / 252
        return self.s.gri * (1 + daily_rate)

    # ─────────────────────────────────────────────
    # 섹터 민감도 (경기 사이클 + 거시경제 연동)
    # ─────────────────────────────────────────────
    def apply_macro_sector_sensitivity(self, stock: dict, perf: float) -> float:
        meta   = stock['meta']
        sector = SECTOR_MAP.get(meta['ind'], "Value")
        ind    = meta.get('ind', '')
        tier   = meta.get('tier', '소형주')
        macro  = self.s.macro

        ex_rate       = macro["exchange_rate"]
        oil_price     = macro["oil_price"]
        interest_rate = macro["interest_rate"]
        cycle         = getattr(self.s, 'cycle_stage', '확장')
        sentiment     = getattr(self.s, 'sentiment', 50.0)
        scenario      = self.s.current_scenario

        # 모든 값은 연간 기준 → /252로 일별 변환
        annual_adj = 0.0

        # ── 환율 영향 ─────────────────────────────
        fx_diff = (ex_rate - 1100.0) / 100.0
        if sector in ["IT", "산업재", "커뮤니케이션"]:
            annual_adj += fx_diff * 0.0018
        elif sector == "에너지":
            annual_adj += fx_diff * 0.0010
        elif sector in ["필수소비재", "유틸리티", "부동산"]:
            annual_adj -= fx_diff * 0.0012

        # ── 유가 영향 ─────────────────────────────
        oil_diff = (oil_price - 30.0) / 50.0
        prev_oil  = self.s._prev_macro_snapshot.get('oil_price', oil_price)
        oil_shock = abs(oil_price - prev_oil) / max(1.0, prev_oil) >= 0.10

        if sector == "에너지" or "에너지" in ind:
            annual_adj += oil_diff * 0.0025
            if oil_shock and oil_price > prev_oil:
                perf += 0.005  # 유가 충격은 당일 즉시 반영
        elif sector in ["산업재", "유틸리티"]:
            annual_adj -= oil_diff * 0.0015
            if oil_shock and oil_price > prev_oil:
                perf -= 0.003
        elif sector in ["필수소비재", "자유소비재"]:
            annual_adj -= oil_diff * 0.0012

        # ── 금리 영향 ─────────────────────────────
        rate_diff = interest_rate - 4.0
        if sector in ["IT", "건강관리", "커뮤니케이션"] or "Growth" in sector:
            annual_adj -= rate_diff * 0.0020 * (1.5 if tier == "소형주" else 1.0)
        elif sector == "금융":
            annual_adj += rate_diff * 0.0015
        elif sector in ["부동산", "유틸리티"]:
            annual_adj -= rate_diff * 0.0012
        elif sector in ["필수소비재"]:
            annual_adj -= rate_diff * 0.0006

        if interest_rate >= 8.0:
            if sector in ["IT", "건강관리", "커뮤니케이션"]:
                extra = (interest_rate - 8.0) * 0.004
                annual_adj -= extra
                if self.s.has_paid_news_access:
                    annual_adj += extra * 0.3

        # ── 섹터 로테이션 (경기 사이클) ──────────
        ROTATION = {
            "확장": {
                "IT": +0.10, "자유소비재": +0.08, "산업재": +0.08,
                "금융": +0.05, "필수소비재": -0.03, "유틸리티": -0.03,
            },
            "정점": {
                "에너지": +0.10, "소재": +0.08, "IT": +0.03,
                "건강관리": -0.02, "금융": -0.03,
            },
            "수축": {
                "필수소비재": +0.08, "유틸리티": +0.08, "건강관리": +0.06,
                "IT": -0.08, "자유소비재": -0.08, "산업재": -0.06,
            },
            "저점": {
                "금융": +0.08, "부동산": +0.06, "산업재": +0.04,
                "에너지": -0.03, "소재": -0.03,
            },
        }
        rot = ROTATION.get(cycle, {})
        for key, delta in rot.items():
            if key in ind or key == sector:
                annual_adj += delta  # 연간 % 기준
                break

        # ── 재건 섹터 조건부 강세 ─────────────────
        if "재건" in ind:
            is_depression = "대공황" in scenario and "극복" not in scenario
            timer = getattr(self.s, 'scenario_timer', 0)
            if is_depression or cycle == "수축":
                if interest_rate < 5.0 and timer > 252 * 3:
                    annual_adj += 0.15
                elif interest_rate > 8.0 or timer < 252:
                    annual_adj -= 0.10

        # ── 투자심리 반영 ─────────────────────────
        sent_diff = (sentiment - 50.0) / 50.0
        sent_mult = 1.5 if tier == "소형주" else 0.8
        annual_adj += sent_diff * 0.03 * sent_mult

        # 연간 조정값을 일별로 변환해서 perf에 추가
        perf += annual_adj / 252

        return perf

    # ─────────────────────────────────────────────
    # 외인/기관 충격
    # ─────────────────────────────────────────────
    def apply_foreign_inst_shock(self, stock: dict, perf: float) -> float:
        meta       = stock['meta']
        risk_score = meta.get('risk_score', 0.0)
        total_inst = meta.get('foreign_share', 0.0) + meta.get('inst_share', 0.0)

        cur_day       = self.s.current_date.day
        current_month = self.s.current_date.month
        is_ipo_period = 14 <= cur_day <= 21 and current_month in [2, 5, 8, 11]

        ipo_multiplier = 1.0
        if is_ipo_period and meta['tier'] == "소형주":
            ipo_multiplier = 2.0 if self.s.has_paid_news_access else 1.2

        if risk_score > 60.0:
            # 충격 크기 축소: 기존 0.005~0.025 → 0.001~0.005
            shock_factor = -random.uniform(0.001, 0.005) * min(2.0, total_inst * 3)
        else:
            shock_factor = random.uniform(-0.001, 0.001) * total_inst

        return perf + (shock_factor * ipo_multiplier)
    # ─────────────────────────────────────────────
    # ★ 공급망 패널티 적용 (6순위)
    # update_macro_logic에서 매일 호출
    # 의존 대상 산업이 수축/저점기이면 피해 산업 efficiency에 패널티
    # ─────────────────────────────────────────────
    def apply_supply_chain_penalty(self):
        from engine.constants import SUPPLY_CHAIN
        cycle = getattr(self.s, 'cycle_stage', '확장')

        # 산업별 현재 경기 단계 판단 (전체 사이클 기준으로 단순화)
        is_downturn = cycle in ("수축", "저점")
        is_depression = ("대공황" in self.s.current_scenario
                         and "극복" not in self.s.current_scenario)

        if not (is_downturn or is_depression):
            return   # 확장/정점기엔 패널티 없음

        # 산업별 평균 efficiency 사전 계산
        ind_efficiency = {}
        for stock in self.s.stocks:
            ind = stock['meta'].get('ind', '')
            eff = stock['meta'].get('efficiency', 0.05)
            if ind not in ind_efficiency:
                ind_efficiency[ind] = []
            ind_efficiency[ind].append(eff)
        ind_avg_eff = {ind: sum(v)/len(v) for ind, v in ind_efficiency.items()}

        # 공급망 패널티 적용
        penalty_mult = 1.5 if is_depression else 1.0
        for stock in self.s.stocks:
            meta = stock['meta']
            ind  = meta.get('ind', '')
            if ind not in SUPPLY_CHAIN:
                continue

            total_penalty = 0.0
            for dep_ind, dep_ratio in SUPPLY_CHAIN[ind]:
                dep_avg = ind_avg_eff.get(dep_ind, 0.05)
                # 의존 대상 산업의 efficiency가 낮을수록 패널티 강화
                if dep_avg < 0.03:   # 매우 낮은 efficiency → 강한 패널티
                    total_penalty += dep_ratio * 0.5 * penalty_mult
                elif dep_avg < 0.05:
                    total_penalty += dep_ratio * 0.25 * penalty_mult

            if total_penalty > 0:
                old_eff = meta.get('efficiency', 0.05)
                meta['efficiency'] = max(0.005, old_eff * (1.0 - total_penalty))

                # 뉴스 (낮은 확률로 공급망 충격 뉴스 발행)
                if not self.s.silent_mode and total_penalty > 0.1 and random.random() < 0.02:
                    dep_names = ', '.join(d for d, _ in SUPPLY_CHAIN[ind])
                    self.s.daily_news.append(
                        f"🔗 [공급망 충격] {meta['c_name']} — "
                        f"{dep_names} 침체로 {ind} 마진 압박 "
                        f"({dep_names} 공급 차질 → {ind} efficiency 하락)"
                    )