"""
engine/economy.py
MacroEngine: 경기선행지수 기반 사이클, 피드백 루프 3개, 섹터 로테이션.

[변경 사항]
- get_current_phase(): 신규 — _tech_upgrade_years 기반 페이즈 계산
- get_tech_level(): _tech_upgrade_year → _tech_upgrade_years 기록으로 변경
                   LV3→LV4 확률 누적 방식으로 변경 (5판에 1판 확률)
- apply_macro_sector_sensitivity(): _SECTOR_LV_ADJ 대신 PHASE_SECTOR_COEFF 사용
                                    temp_sector_buff 반영 추가
- _update_commodity_prices(): SOX 페이즈별 자연성장 로직 추가
- 재건 섹터 관련 코드 제거 (temp_sector_buff로 대체)
"""
import random
import math
from datetime import timedelta
from .constants import SECTOR_MAP, TECH_PHASE, PHASE_SECTOR_COEFF, SOX_GROWTH_RATE


class MacroEngine:
    # ★ 섹터 기본 베이스 수익 — 인플레 반영, 장기 마이너스 방지
    # ★ 페이즈별 섹터 베이스 수익 — LV가 높을수록 내수형 둔화, 성장형 가속
    # 사용처: apply_macro_sector_sensitivity에서 phase 참조
    _SECTOR_BASE_ADJ = {
        "1A": {
            "에너지":       +0.025, "금융":         +0.028,
            "부동산":       +0.028, "소재":         +0.022,
            "유틸리티":     +0.020, "산업재":       +0.022,
            "필수소비재":   +0.020, "IT":           +0.018,
            "건강관리":     +0.020, "커뮤니케이션": +0.015,
            "자유소비재":   +0.018,
        },
        "1B": {
            "에너지":       +0.026, "금융":         +0.026,
            "부동산":       +0.025, "소재":         +0.024,
            "유틸리티":     +0.020, "산업재":       +0.025,
            "필수소비재":   +0.020, "IT":           +0.022,
            "건강관리":     +0.022, "커뮤니케이션": +0.018,
            "자유소비재":   +0.020,
        },
        "2A": {
            "에너지":       +0.022, "금융":         +0.022,
            "부동산":       +0.020, "소재":         +0.022,
            "유틸리티":     +0.018, "산업재":       +0.024,
            "필수소비재":   +0.018, "IT":           +0.026,
            "건강관리":     +0.026, "커뮤니케이션": +0.022,
            "자유소비재":   +0.022,
        },
        "2B": {
            "에너지":       +0.020, "금융":         +0.020,
            "부동산":       +0.018, "소재":         +0.022,
            "유틸리티":     +0.016, "산업재":       +0.022,
            "필수소비재":   +0.016, "IT":           +0.030,
            "건강관리":     +0.028, "커뮤니케이션": +0.024,
            "자유소비재":   +0.020,
        },
        "3A": {
            "에너지":       +0.018, "금융":         +0.016,
            "부동산":       +0.014, "소재":         +0.020,
            "유틸리티":     +0.014, "산업재":       +0.018,
            "필수소비재":   +0.014, "IT":           +0.034,
            "건강관리":     +0.032, "커뮤니케이션": +0.026,
            "자유소비재":   +0.018,
        },
        "3B": {
            "에너지":       +0.016, "금융":         +0.012,
            "부동산":       +0.010, "소재":         +0.018,
            "유틸리티":     +0.012, "산업재":       +0.015,
            "필수소비재":   +0.012, "IT":           +0.036,
            "건강관리":     +0.038, "커뮤니케이션": +0.024,
            "자유소비재":   +0.016,
        },
        "4A": {
            "에너지":       +0.014, "금융":         +0.008,
            "부동산":       +0.006, "소재":         +0.016,
            "유틸리티":     +0.012, "산업재":       +0.010,
            "필수소비재":   +0.010, "IT":           +0.042,
            "건강관리":     +0.040, "커뮤니케이션": +0.026,
            "자유소비재":   +0.012,
        },
        "4B": {
            "에너지":       +0.016, "금융":         +0.006,
            "부동산":       +0.004, "소재":         +0.018,
            "유틸리티":     +0.014, "산업재":       +0.008,
            "필수소비재":   +0.008, "IT":           +0.050,
            "건강관리":     +0.046, "커뮤니케이션": +0.028,
            "자유소비재":   +0.010,
        },
    }

    def __init__(self, state):
        self.s = state

    # ─────────────────────────────────────────────
    # 페이즈 계산 (신규)
    # _tech_upgrade_years 기반 — 연도 고정 아님
    # ─────────────────────────────────────────────
    def get_current_phase(self) -> str:
        """
        현재 테크 레벨과 진입 후 경과 년수를 기반으로
        페이즈 ID를 반환합니다. (예: "1A", "2B", "3A" 등)
        """
        lv = self.s.max_tech_reached
        upgrade_years = getattr(self.s, '_tech_upgrade_years', {1: 2000})
        lv_start = upgrade_years.get(lv) or 2000
        elapsed = self.s.current_date.year - lv_start

        for phase in TECH_PHASE.get(lv, []):
            if phase["offset_start"] <= elapsed < phase["offset_end"]:
                return phase["id"]

        # fallback: 마지막 페이즈
        phases = TECH_PHASE.get(lv, [])
        return phases[-1]["id"] if phases else str(lv)

    # ─────────────────────────────────────────────
    # 기술 레벨
    # ─────────────────────────────────────────────
    def get_tech_level(self) -> int:
        cy          = self.s.current_date.year
        current_lv  = self.s.max_tech_reached
        is_depression = "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario

        if is_depression:
            return current_lv

        evolution_chance = 0.0

        if self.s.pending_events.get("tech_jump"):
            evolution_chance = 0.0
        else:
            upgrade_years = getattr(self.s, '_tech_upgrade_years', {1: 2000})

            if current_lv == 1:
                lv1_start = upgrade_years.get(1, 2000)
                years_in_lv1 = cy - lv1_start
                # LV1 진입 후 10년 이전 불가, GRI 1800 이상 필요
                if years_in_lv1 < 10 or self.s.gri < 1800:
                    evolution_chance = 0.0
                else:
                    # 10~14년: 낮은 확률, 14년 이상: 점점 높아짐
                    base = min(0.15, (years_in_lv1 - 10) * 0.025) / 100 / 252
                    gri_mult = min(2.0, max(0.5, self.s.gri / 2500))
                    evolution_chance = base * gri_mult
                    # 20년 이상이면 강제 전환
                    if years_in_lv1 >= 20:
                        evolution_chance = 1.0

            elif current_lv == 2:
                lv2_start = upgrade_years.get(2, cy)
                years_in_lv2 = cy - lv2_start if lv2_start else 0
                # LV2 진입 후 12년 이전 불가, GRI 7000 이상 필요
                if years_in_lv2 < 12 or self.s.gri < 7000:
                    evolution_chance = 0.0
                else:
                    base = min(0.20, (years_in_lv2 - 12) * 0.02) / 100 / 252
                    gri_mult = min(2.0, max(0.5, self.s.gri / 12000))
                    evolution_chance = base * gri_mult
                    if years_in_lv2 >= 25:
                        evolution_chance = max(evolution_chance, 0.15 / 252)
                    if years_in_lv2 >= 30:
                        evolution_chance = 1.0

            elif current_lv == 3:
                lv3_start = upgrade_years.get(3, cy)
                years_in_lv3 = cy - lv3_start if lv3_start else 0

                # LV3→LV4: 진입 후 20년 경과부터 누적 확률
                # 5판에 1판 도달 목표 (약 20% 확률)
                # 20년 후부터 매일 0.003% 누적, 안정기 보너스 있음
                if years_in_lv3 >= 20 and not is_depression:
                    base_daily = 0.003 / 252
                    # 안정기(버블 낮음) 보너스
                    if self.s.bubble_index < 80:
                        base_daily *= 1.5
                    # 누적 (대공황 중엔 누적 중단)
                    self.s.lv4_chance_accum = getattr(self.s, 'lv4_chance_accum', 0.0)
                    self.s.lv4_chance_accum += base_daily
                    evolution_chance = self.s.lv4_chance_accum
                else:
                    evolution_chance = 0.0

        if evolution_chance > 0 and random.random() < evolution_chance:
            jump_date = self.s.current_date + timedelta(days=30)
            target_lv = current_lv + 1
            lv_name   = {
                2: "2단계 (모바일·클라우드 혁명)",
                3: "3단계 (AI·양자 혁명)",
                4: "4단계 (기술 특이점)",
            }.get(target_lv, f"{target_lv}단계")
            self.s.pending_events["tech_jump"] = {
                "target_lv": target_lv,
                "date":      jump_date,
                "lv_name":   lv_name,
                "notified":  False,
            }
            # LV4 전환 시 누적 확률 리셋
            if target_lv == 4:
                self.s.lv4_chance_accum = 0.0

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
                self.s.max_tech_reached = new_lv

                # ★ _tech_upgrade_years에 진입 연도 기록
                upgrade_years = getattr(self.s, '_tech_upgrade_years', {1: 2000})
                upgrade_years[new_lv] = self.s.current_date.year
                self.s._tech_upgrade_years = upgrade_years

                # ★ 페이즈 전환 부스트 시작 — 감쇠 1년간 완화
                self.s._phase_transition_day = getattr(self.s, 'cycle_day', 0)

                # 테크 전환 랠리
                self.s.sentiment   = min(85.0, getattr(self.s, 'sentiment', 50.0) + 25.0)
                self.s.cycle_stage = "확장"
                self.s.cycle_day   = 0
                self.s.daily_news.append(
                    f"🚀 [시대 진화] {cy}년, 문명이 {lv_name}로 도약했습니다! "
                    f"산업 전반에 대규모 기술 혁신이 시작됩니다."
                )
                self._apply_tech_shock(new_lv)
                self.s.pending_events["tech_jump"] = None

        return self.s.max_tech_reached

    def _apply_tech_shock(self, new_lv: int):
        """테크 전환 시 섹터별 efficiency 영구 조정"""
        if new_lv == 2:
            boost   = {"IT": 0.20, "커뮤니케이션": 0.15, "자유소비재": 0.10, "금융": 0.08}
            penalty = {"에너지": -0.10}
        elif new_lv == 3:
            boost   = {"건강관리": 0.25, "IT": 0.20, "산업재": 0.15, "소재": 0.10}
            penalty = {"필수소비재": -0.05, "유틸리티": -0.10}
        elif new_lv == 4:
            boost   = {"IT": 0.30, "건강관리": 0.25, "금융": 0.15}
            penalty = {"에너지": -0.15, "부동산": -0.05}
        else:
            return

        for stock in self.s.stocks:
            meta   = stock['meta']
            ind    = meta.get('ind', '')
            for key, delta in {**boost, **penalty}.items():
                if key == ind:
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
        macro = self.s.macro
        lv    = self.s.max_tech_reached
        target_cpi = {1: 2.0, 2: 2.5, 3: 4.0, 4: 1.0}.get(lv, 2.0)

        prev      = getattr(self.s, '_prev_macro_snapshot', {})
        prev_rate = prev.get('interest_rate', macro['interest_rate'])
        rate_delta = macro['interest_rate'] - prev_rate
        rate_score = max(-0.4, min(0.4, -rate_delta * 10))

        hist = getattr(self.s, '_gri_history_20', [])
        if len(hist) >= 20:
            gri_mom = (hist[-1] - hist[0]) / max(1.0, hist[0])
            gri_score = max(-0.3, min(0.3, gri_mom * 5))
        else:
            gri_score = 0.05

        earn = getattr(self.s, 'avg_earnings_growth', 0.05)
        cur_year = self.s.current_date.year
        if cur_year <= 2001:
            earn = max(0.02, earn)
        earn_score = max(-0.2, min(0.2, earn * 2))

        cpi_diff  = target_cpi - macro['cpi']
        cpi_score = max(-0.2, min(0.2, cpi_diff * 0.05))

        leading = rate_score + gri_score + earn_score + cpi_score
        return max(-1.0, min(1.0, leading))

    def _update_cycle_stage(self, leading: float):
        s = self.s
        prev_stage = getattr(s, 'cycle_stage', '확장')
        s.cycle_day = getattr(s, 'cycle_day', 0) + 1

        if leading >= 0.15:
            new_stage = "확장"
        elif leading >= 0.0:
            new_stage = "정점"
        elif leading >= -0.15:
            new_stage = "수축"
        else:
            new_stage = "저점"

        MIN_DAYS = {"확장": 63, "정점": 21, "수축": 126, "저점": 42}

        if new_stage != prev_stage:
            min_d = MIN_DAYS.get(prev_stage, 63)
            if s.cycle_day < min_d:
                return
            if prev_stage == "확장" and new_stage == "정점" and leading > 0.30:
                return
            elif prev_stage == "정점" and new_stage == "수축" and leading > 0.10:
                return

            s.cycle_day   = 0
            s.cycle_stage = new_stage
            if not s.silent_mode:
                emoji = {"확장": "📈", "정점": "🔝", "수축": "📉", "저점": "🔻"}.get(new_stage, "")
                s.daily_news.append(
                    f"{emoji} [경기 전환] 경기 국면이 '{prev_stage}' → '{new_stage}'로 전환됩니다."
                )

    def _update_sentiment(self, leading: float):
        s = self.s
        sentiment = getattr(s, 'sentiment', 50.0)
        target    = 50.0 + leading * 40.0
        diff      = (target - sentiment) * 0.02
        noise     = random.uniform(-0.5, 0.5)
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

        # ── 선행지수 & 사이클 ─────────────────────
        is_depression = "대공황" in scenario and "극복" not in scenario
        if not is_depression:
            leading = self.calc_leading_index()
            self.s.leading_index = leading
            self._update_cycle_stage(leading)
            self._update_sentiment(leading)
        else:
            leading = self.s.leading_index

        cycle = getattr(self.s, 'cycle_stage', '확장')

        # ── 원자재 업데이트 (SOX 포함) ────────────
        self._update_commodity_prices()

        # ── 기준 목표값 ───────────────────────────
        target_cpi      = {1: 2.0, 2: 2.5, 3: 4.0, 4: 1.0}.get(lv, 2.0)
        target_interest = target_cpi + 1.5
        target_oil      = {1: 45, 2: 90, 3: 140, 4: 25}.get(lv, 50)
        # ★ 환율 기준값 상향 (현실 반영)
        # LV1: 1,250 (2000년대 평균)
        # LV2: 1,350 (2010년대 평균 — 수출 강세 시 1,050, 위기 시 1,500+)
        # LV3: 1,200 (기술 강국 → 원화 강세)
        # LV4: 900  (포스트휴먼 경제 — 달러 패권 약화)
        target_fx       = {1: 1250, 2: 1350, 3: 1200, 4: 900}.get(lv, 1250)
        MAX_RATE_CHANGE = 0.05

        # ── 소조정 ────────────────────────────────
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
                self.s._prev_macro_snapshot = {k: macro[k] for k in macro}
                return

        # ── 대공황 ────────────────────────────────
        if is_depression or "✨ 대공황V" in scenario:
            total_days   = 252 * 10
            timer        = getattr(self.s, 'scenario_timer', 0)
            elapsed_days = total_days - timer
            progress     = max(0.0, min(1.0, elapsed_days / total_days))

            if progress < 0.2:
                target_interest = max(0.25, target_cpi - 1.0)
                target_cpi      = target_cpi * 0.8
                target_oil      *= 0.6
            elif progress < 0.6:
                target_interest = target_cpi + 0.5
                target_cpi      = target_cpi + 6.0 * (progress - 0.2) / 0.4
                target_oil      = target_oil * (0.6 + 0.4 * (progress - 0.2) / 0.4)
            else:
                target_interest = target_cpi + 2.0
                target_oil      *= 0.9

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
            gri_growth_annual = 0.0
            if len(hist) >= 20:
                gri_growth_annual = (hist[-1] / max(1.0, hist[0]) - 1.0) * (252 / 20)
            if gri_growth_annual > 0.30:
                target_cpi += min(1.0, (gri_growth_annual - 0.30) * 3)

            if cycle == "확장":
                target_interest = target_cpi + 0.5
            elif cycle == "정점":
                target_interest = target_cpi + 1.5
            elif cycle == "수축":
                target_interest = max(0.5, target_cpi - 0.5)
            elif cycle == "저점":
                target_interest = max(0.25, target_cpi - 1.5)

        # ── CPI 업데이트 ──────────────────────────
        cpi_step = (target_cpi - macro["cpi"]) * 0.02 + random.uniform(-0.05, 0.05)
        macro["cpi"] = max(0.3, min(15.0, macro["cpi"] + cpi_step))
        if macro["cpi"] > target_cpi + 1.0:
            target_interest += 0.5

        # ── 금리 업데이트 ─────────────────────────
        diff_r = (target_interest - macro["interest_rate"]) * 0.03 + random.uniform(-0.02, 0.02)
        macro["interest_rate"] += max(-MAX_RATE_CHANGE, min(MAX_RATE_CHANGE, diff_r))
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
        # 현실 원달러 환율 결정 요인:
        # 1. 금리차: 미국 금리 > 한국 금리 → 달러 강세 → 환율 상승
        # 2. 수출 호황: 달러 유입 → 원화 강세 → 환율 하락
        # 3. 위기/전쟁: 안전자산 달러 수요 → 환율 급등
        # 4. 인플레이션: 물가 상승 → 화폐가치 하락 → 환율 상승

        # 금리 영향: 금리 높을수록 달러 강세 (원화 약세)
        rate_impact = (macro["interest_rate"] - 3.5) * 20

        _boom = getattr(self.s, 'boom_event', {})
        if _boom.get('phase') == '진행중' and _boom.get('type') == '수출호황':
            target_fx -= 80
        elif _boom.get('phase') == '진행중' and _boom.get('type') == '유동성장세':
            target_fx -= 50

        war = getattr(self.s, 'war_event', {})
        if war.get('phase') == '진행중':
            if war.get('type') == '대규모전쟁':
                target_fx += 150  # 대규모 전쟁: 환율 급등 (1,500원 가능)
            else:
                target_fx += 80

        if is_depression:
            target_fx += 200

        if macro["cpi"] > 4.0:
            target_fx += (macro["cpi"] - 4.0) * 30

        sanctions = getattr(self.s, 'export_sanctions', {})
        if any(s.get('phase') == '단기충격' for s in sanctions.values()):
            target_fx += 50

        final_target_fx = max(900, min(2000, target_fx + rate_impact))
        diff_fx = (final_target_fx - macro["exchange_rate"]) * 0.015 + random.uniform(-8, 8)
        macro["exchange_rate"] += max(-7.0, min(7.0, diff_fx))
        macro["exchange_rate"]  = max(900, min(2000, macro["exchange_rate"]))

        # ── 물가 누적 ────────────────────────────
        self.s.cumulative_inflation *= (1 + (macro["cpi"] / 100) / 252)
        self.s.base_item_price = 1000.0 * self.s.cumulative_inflation

        # ── 전일 스냅샷 갱신 ─────────────────────
        self.s._prev_macro_snapshot = {k: macro[k] for k in macro}

    # ─────────────────────────────────────────────
    # 동적 목표 지수
    # ─────────────────────────────────────────────
    def get_dynamic_target(self) -> float:
        """
        GRI 목표값 — 고정 LV 성장률 대신 시나리오+사이클 기반으로 결정.
        LV는 상한/하한 밴드만 제공하고, 실제 방향은 시나리오가 결정.
        """
        scenario = self.s.current_scenario
        cycle    = getattr(self.s, 'cycle_stage', '확장')
        lv       = self.s.max_tech_reached

        # ── 대공황: 무조건 하락 ───────────────────
        if "💀 대공황" in scenario:
            return self.s.gri * 0.96

        # ── 소조정: 완만 하락 ─────────────────────
        if "📉 소조정" in scenario:
            return self.s.gri * 0.987

        # ── 대공황V / 팬데믹 극복 / 재건: 회복 랠리
        if any(x in scenario for x in ["✨ 대공황V", "✨ 팬데믹 극복", "🏗️"]):
            return self.s.gri * 1.004

        # ── 팬데믹 진행중: 초기 하락 → 후기 반등 ─
        pandemic = getattr(self.s, 'pandemic_event', {})
        if pandemic.get('phase') == '진행중':
            pan_timer = pandemic.get('timer', 0)
            if pan_timer > 300:   # 초기: 하락
                return self.s.gri * 0.992
            elif pan_timer > 100: # 중기: 보합
                return self.s.gri * 1.001
            else:                 # 후기: 반등 기대
                return self.s.gri * 1.003

        # ── 호재 시나리오 ─────────────────────────
        boom = getattr(self.s, 'boom_event', {})
        if boom.get('phase') == '진행중':
            boom_type = boom.get('type', '')
            if boom_type == '수출호황':
                return self.s.gri * 1.0045
            elif boom_type == '유동성장세':
                return self.s.gri * 1.0040
            elif boom_type == '내수붐':
                return self.s.gri * 1.0035

        # ── 전쟁 진행중: 하락 압력 ───────────────
        war = getattr(self.s, 'war_event', {})
        if war.get('phase') == '진행중':
            if war.get('type') == '대규모전쟁':
                return self.s.gri * 0.994
            else:
                return self.s.gri * 0.997

        # ── 기본: 경기 사이클 + LV 밴드 ─────────
        # LV는 장기 성장의 상한/하한만 제공
        # 실제 일별 방향은 사이클이 결정
        _cycle_rate = {
            "확장": 0.0004,   # 연 ~10%
            "정점": 0.0001,   # 연 ~2.5% (둔화)
            "수축": -0.0002,  # 연 -5% (하락)
            "저점": -0.0001,  # 연 -2.5% (바닥 탐색)
        }.get(cycle, 0.0002)

        # LV 밴드: 너무 빠르거나 느린 성장 완화
        # 연 최대 성장률 상한 (LV별 기술 혁신 한계)
        _lv_cap = {1: 0.0005, 2: 0.0006, 3: 0.0005, 4: 0.0003}.get(lv, 0.0004)
        daily_rate = max(-0.0008, min(_lv_cap, _cycle_rate))

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

        annual_adj = 0.0

        # ── 환율 영향 ─────────────────────────────
        # ★ EXPORT_DEPENDENCY 기반 수출/내수 차등 적용
        # 수출형: 환율 상승 → 원화 환산 이익 증가 → 주가 상승
        # 내수형: 환율 상승 → 수입 비용 증가 → 주가 하락
        from engine.constants import EXPORT_DEPENDENCY as _EXP_DEP
        _export_dep   = _EXP_DEP.get(ind, 0.3)
        _domestic_dep = 1.0 - _export_dep
        fx_diff = (ex_rate - 1100.0) / 100.0  # 100원 단위

        _export_benefit  = fx_diff * 0.0020 * _export_dep
        _domestic_cost   = fx_diff * 0.0015 * _domestic_dep
        annual_adj += _export_benefit - _domestic_cost
        # IT(수출0.75):   +0.0015 - 0.0004 = +0.0011/100원 (수혜)
        # 유틸(내수0.97): +0.0006 - 0.0015 = -0.0009/100원 (피해)
        # 소재(수출0.60): +0.0012 - 0.0009 = +0.0003/100원 (소폭 수혜)

        # ── 유가 영향 ─────────────────────────────
        oil_diff  = (oil_price - 30.0) / 50.0
        prev_oil  = self.s._prev_macro_snapshot.get('oil_price', oil_price)
        oil_shock = abs(oil_price - prev_oil) / max(1.0, prev_oil) >= 0.10

        if sector == "에너지" or "에너지" in ind:
            annual_adj += oil_diff * 0.0025
            if oil_shock and oil_price > prev_oil:
                perf += 0.005
        elif sector in ["산업재", "유틸리티"]:
            annual_adj -= oil_diff * 0.0015
            if oil_shock and oil_price > prev_oil:
                perf -= 0.003
        elif sector in ["필수소비재", "자유소비재"]:
            annual_adj -= oil_diff * 0.0012

        # ── 금리 영향 ─────────────────────────────
        rate_diff = interest_rate - 4.0
        if sector in ["IT", "건강관리", "커뮤니케이션"]:
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

        # ── 섹터 기본 베이스 수익 ────────────────
        # ★ market.py sector_adj에서 담당 → 여기서 제거 (중복 방지)

        # ── ★ 페이즈별 섹터 계수 적용 (감쇠 포함) ──────────────────────────
        phase = self.get_current_phase()
        phase_coeff = PHASE_SECTOR_COEFF.get(phase, {})
        raw_coeff   = phase_coeff.get(ind, 0.0)

        # ★ GRI 기반 성장 감쇠 — 시장이 커질수록 기본 성장률 둔화
        # 단, 호재(boom/war/pandemic 섹터 조정)는 이미 위에서 처리됐으므로
        # 여기서는 기본 페이즈 계수만 감쇠시킴
        # 삼성전자처럼 실적/이벤트 호재로 뚫고 올라가는 건 위 블록들이 담당
        _gri_now = max(1.0, self.s.gri)
        if   _gri_now < 1500:   _decay = 1.00   # 초기: 풀 성장
        elif _gri_now < 2500:   _decay = 0.88
        elif _gri_now < 4000:   _decay = 0.75
        elif _gri_now < 7000:   _decay = 0.62
        elif _gri_now < 12000:  _decay = 0.50
        else:                   _decay = 0.40   # 성숙 시장: 40%만 작동

        # ★ 페이즈/LV 전환 직후 1년간 감쇠 완화 (삼성전자 AI 반도체처럼)
        # _phase_transition_day: 페이즈 전환 시 기록, 252일간 부스트
        _trans_day = getattr(self.s, '_phase_transition_day', 0)
        _cur_day   = getattr(self.s, 'cycle_day', 0)
        _days_since_trans = _cur_day - _trans_day
        if 0 <= _days_since_trans <= 252:
            # 전환 직후: decay 1.0 → 252일에 걸쳐 원래 decay로 수렴
            _boost_ratio = 1.0 - (_days_since_trans / 252.0)
            _decay = _decay + (1.0 - _decay) * _boost_ratio

        # 감쇠는 양수(성장) 계수에만 적용 — 음수(페널티)는 그대로 유지
        if raw_coeff > 0:
            annual_adj += raw_coeff * _decay
        else:
            annual_adj += raw_coeff

        # ── ★ 임시 섹터 버프 적용 (재건 이벤트 등) ───────────────────────
        temp_buff = getattr(self.s, 'temp_sector_buff', {})
        today = self.s.current_date
        for buff_ind, (buff_val, expire_dt) in list(temp_buff.items()):
            if today >= expire_dt:
                del self.s.temp_sector_buff[buff_ind]
            elif buff_ind == ind:
                annual_adj += buff_val

        # ── 섹터 로테이션 ────────────────────────
        # ★ market.py cycle_sector에서 담당 → 여기서 제거 (중복 방지)

        # ── 거시 필터들 ───────────────────────────
        cpi        = macro.get('cpi', 2.0)
        prev_rate  = self.s._prev_macro_snapshot.get('interest_rate', interest_rate)
        rate_speed = interest_rate - prev_rate

        # CPI 수준
        if cpi > 4.0:
            if ind in ('에너지', '소재'):
                annual_adj += (cpi - 4.0) * 0.012
            if ind in ('필수소비재', '자유소비재'):
                annual_adj -= (cpi - 4.0) * 0.008
            if sector == 'Growth':
                annual_adj -= (cpi - 4.0) * 0.005
        elif cpi < 2.0:
            if ind in ('IT', '자유소비재'):
                annual_adj += (2.0 - cpi) * 0.005

        # 금리 인상 속도
        if rate_speed > 0.05:
            if sector == 'Growth' or ind in ('IT', '건강관리', '커뮤니케이션'):
                annual_adj -= rate_speed * 0.8
            if ind == '금융':
                annual_adj += rate_speed * 0.4
        elif rate_speed < -0.05:
            if sector == 'Growth' or ind in ('IT',):
                annual_adj += abs(rate_speed) * 0.6
            if ind == '금융':
                annual_adj += abs(rate_speed) * 0.3

        # 전쟁 단계별 섹터 영향 — LV별 차등 (LV4는 핵융합 시대라 유가 충격 약함)
        war       = getattr(self.s, 'war_event', {})
        war_phase = war.get('phase', '')
        _lv_war   = self.s.max_tech_reached
        # LV가 높을수록 에너지 충격 감소 (재생에너지/핵융합 비중 증가)
        _energy_war_mult = {1: 1.0, 2: 0.75, 3: 0.45, 4: 0.20}.get(_lv_war, 1.0)
        # LV가 높을수록 전쟁 충격 전반 감소 (글로벌 협력체계 발달)
        _war_impact_mult = {1: 1.0, 2: 0.85, 3: 0.60, 4: 0.35}.get(_lv_war, 1.0)

        if war_phase == '진행중':
            war_timer = war.get('timer', 0)
            is_early  = war_timer > 365
            is_late   = war_timer < 126

            if '에너지' in ind:
                if is_early:
                    annual_adj += 0.08 * _energy_war_mult
                elif is_late:
                    annual_adj -= 0.04 * _energy_war_mult
                else:
                    annual_adj += 0.02 * _energy_war_mult
            if '소재' in ind and is_early:
                annual_adj += 0.05 * _war_impact_mult
            if sector == 'Growth' and not is_late:
                annual_adj -= 0.03 * _war_impact_mult

        elif war_phase == '종전':
            if sector == 'Growth':
                annual_adj += 0.04 * _war_impact_mult

        # 투자심리 반영
        sent_diff = (sentiment - 50.0) / 50.0
        sent_mult = 1.5 if tier == "소형주" else 0.8
        annual_adj += sent_diff * 0.03 * sent_mult

        # 팬데믹 단계별 효과 — LV별 차등
        # LV가 높을수록 의료 인프라 발달 → 충격 약화, 회복 빠름
        pandemic    = getattr(self.s, 'pandemic_event', {})
        if pandemic.get('phase') == '진행중':
            pan_timer    = pandemic.get('timer', 0)
            is_pan_early = pan_timer > 300
            is_pan_late  = pan_timer < 100
            _lv_pan      = self.s.max_tech_reached
            # LV1: 풀 충격 / LV2: 75% / LV3: 45% / LV4: 20%
            _pan_mult    = {1: 1.0, 2: 0.75, 3: 0.45, 4: 0.20}.get(_lv_pan, 1.0)

            if is_pan_early:
                if ind in ('IT', '건강관리', '필수소비재'):
                    annual_adj += 0.15 * _pan_mult
                if ind in ('자유소비재', '부동산'):
                    annual_adj -= 0.20 * _pan_mult
            elif is_pan_late:
                if ind in ('IT', '커뮤니케이션') and interest_rate > 3.0:
                    annual_adj -= 0.08 * _pan_mult
                if sector == 'Defensive':
                    annual_adj += 0.06 * _pan_mult
                if ind in ('IT', '금융') and interest_rate < 3.0:
                    annual_adj += 0.10 * _pan_mult
            else:
                if sector == 'Defensive':
                    annual_adj += 0.04 * _pan_mult
                if ind == 'IT' and rate_speed > 0.03:
                    annual_adj -= 0.06 * _pan_mult

        # ── 호재 시나리오 섹터 영향 ──────────────
        boom = getattr(self.s, 'boom_event', {})
        if boom.get('phase') == '진행중':
            boom_type = boom.get('type', '')
            if boom_type == '수출호황':
                if ind in ('IT', '산업재', '소재'):
                    annual_adj += 0.08
                if ind == '커뮤니케이션':
                    annual_adj += 0.04
            elif boom_type == '유동성장세':
                # 전 섹터 완만한 상승
                annual_adj += 0.04
                if sector == 'Growth':
                    annual_adj += 0.04   # 성장주 추가 수혜
            elif boom_type == '내수붐':
                if ind in ('필수소비재', '자유소비재', '커뮤니케이션'):
                    annual_adj += 0.10
                if ind == '부동산':
                    annual_adj += 0.06

        # 금융위기/침체 QE 반등
        if '금융위기' in scenario or '침체' in scenario:
            timer = getattr(self.s, 'scenario_timer', 0)
            is_crisis_late = timer < 180
            if is_crisis_late:
                if ind in ('IT', '금융') and interest_rate < 3.0:
                    annual_adj += 0.08
                elif sector == 'Defensive':
                    annual_adj -= 0.03
            else:
                if sector == 'Defensive' or ind == '필수소비재':
                    annual_adj += 0.05
                if sector == 'Growth':
                    annual_adj -= 0.04

        # 연간 조정값 → 일별 변환
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
            shock_factor = -random.uniform(0.001, 0.005) * min(2.0, total_inst * 3)
        else:
            shock_factor = random.uniform(-0.001, 0.001) * total_inst

        return perf + (shock_factor * ipo_multiplier)

    # ─────────────────────────────────────────────
    # 공급망 패널티
    # ─────────────────────────────────────────────
    def apply_supply_chain_penalty(self):
        try:
            from engine.constants import SUPPLY_CHAIN
        except ImportError:
            return

        cycle        = getattr(self.s, 'cycle_stage', '확장')
        is_downturn  = cycle in ("수축", "저점")
        is_depression = ("대공황" in self.s.current_scenario
                         and "극복" not in self.s.current_scenario)

        if not (is_downturn or is_depression):
            return

        ind_efficiency = {}
        for stock in self.s.stocks:
            ind = stock['meta'].get('ind', '')
            eff = stock['meta'].get('efficiency', 0.05)
            if ind not in ind_efficiency:
                ind_efficiency[ind] = []
            ind_efficiency[ind].append(eff)
        ind_avg_eff = {ind: sum(v)/len(v) for ind, v in ind_efficiency.items()}

        penalty_mult = 1.5 if is_depression else 1.0
        for stock in self.s.stocks:
            meta = stock['meta']
            ind  = meta.get('ind', '')
            if ind not in SUPPLY_CHAIN:
                continue

            total_penalty = 0.0
            for dep_ind, dep_ratio in SUPPLY_CHAIN[ind]:
                dep_avg = ind_avg_eff.get(dep_ind, 0.05)
                if dep_avg < 0.03:
                    total_penalty += dep_ratio * 0.5 * penalty_mult
                elif dep_avg < 0.05:
                    total_penalty += dep_ratio * 0.25 * penalty_mult

            if total_penalty > 0:
                old_eff = meta.get('efficiency', 0.05)
                meta['efficiency'] = max(0.005, old_eff * (1.0 - total_penalty))

                if not self.s.silent_mode and total_penalty > 0.1 and random.random() < 0.02:
                    dep_names = ', '.join(d for d, _ in SUPPLY_CHAIN[ind])
                    self.s.daily_news.append(
                        f"🔗 [공급망 충격] {meta['c_name']} — "
                        f"{dep_names} 침체로 {ind} 마진 압박"
                    )

    # ─────────────────────────────────────────────
    # 원자재 지수 업데이트 (SOX 페이즈별 자연성장 추가)
    # ─────────────────────────────────────────────
    def _update_commodity_prices(self):
        macro = self.s.macro
        cycle = getattr(self.s, 'cycle_stage', '확장')
        war   = getattr(self.s, 'war_event', {})
        lv    = self.s.max_tech_reached

        # ── 곡물 ──────────────────────────────────
        grain = macro.get('grain_price', 250.0)
        grain_long   = {1: 300.0, 2: 380.0, 3: 480.0, 4: 400.0}.get(lv, 300.0)
        grain_target = grain_long * {
            '확장': 1.05, '정점': 1.15, '수축': 0.92, '저점': 0.85,
        }.get(cycle, 1.0)
        if war.get('region') == '동유럽' and war.get('phase') == '진행중':
            grain_target *= 1.6
        grain_step = (grain_target - grain) * 0.008 + random.uniform(-3.0, 3.0)
        macro['grain_price'] = max(100.0, min(900.0, grain + grain_step))

        # ── 금속 ──────────────────────────────────
        metal = macro.get('metal_price', 1800.0)
        metal_long   = {1: 3000.0, 2: 6000.0, 3: 8000.0, 4: 7000.0}.get(lv, 3000.0)
        metal_target = metal_long * {
            '확장': 1.10, '정점': 1.20, '수축': 0.85, '저점': 0.75,
        }.get(cycle, 1.0)
        if war.get('region') in ('아프리카', '동남아') and war.get('phase') == '진행중':
            metal_target *= 1.5
        metal_step = (metal_target - metal) * 0.006 + random.uniform(-50.0, 50.0)
        macro['metal_price'] = max(500.0, min(20000.0, metal + metal_step))

        # ── SOX 반도체 지수 ───────────────────────
        semi  = macro.get('semi_index', 1000.0)
        phase = self.get_current_phase()

        # ★ SOX 목표값 — 페이즈 고정값 제거
        # 시나리오+사이클이 방향 결정, 페이즈는 자연성장률만 제공
        _semi_scenario = self.s.current_scenario
        _semi_boom     = getattr(self.s, 'boom_event', {})
        _semi_pandemic = getattr(self.s, 'pandemic_event', {})
        _semi_war      = getattr(self.s, 'war_event', {})

        if "💀 대공황" in _semi_scenario:
            semi_dir = 0.990
        elif "✨ 대공황V" in _semi_scenario or "✨ 팬데믹 극복" in _semi_scenario:
            semi_dir = 1.015
        elif _semi_pandemic.get('phase') == '진행중':
            pan_t = _semi_pandemic.get('timer', 0)
            semi_dir = 0.985 if pan_t > 300 else (1.005 if pan_t < 100 else 0.998)
        elif _semi_boom.get('phase') == '진행중' and _semi_boom.get('type') == '수출호황':
            semi_dir = 1.012
        elif _semi_war.get('region') == '동남아' and _semi_war.get('phase') == '진행중':
            semi_dir = 0.970
        elif _semi_war.get('region') == '동남아' and _semi_war.get('phase') == '종전':
            semi_dir = 1.008
        else:
            semi_dir = {
                '확장': 1.004, '정점': 1.001,
                '수축': 0.994, '저점': 0.990,
            }.get(cycle, 1.001)

        daily_growth = SOX_GROWTH_RATE.get(phase, 0.0003)
        semi_natural = semi * daily_growth
        semi_step    = semi * (semi_dir - 1.0) + semi_natural + random.uniform(-15.0, 15.0)
        macro['semi_index'] = max(100.0, min(200000.0, semi + semi_step))

    # ─────────────────────────────────────────────
    # 원자재 → 섹터 민감도
    # ─────────────────────────────────────────────
    def get_commodity_adj(self, stock: dict) -> float:
        meta   = stock['meta']
        sector = SECTOR_MAP.get(meta.get('ind', ''), 'Value')
        ind    = meta.get('ind', '')
        macro  = self.s.macro

        grain = macro.get('grain_price', 250.0)
        metal = macro.get('metal_price', 1800.0)
        semi  = macro.get('semi_index',  1000.0)

        grain_diff = (grain - 250.0) / 250.0
        metal_diff = (metal - 1800.0) / 1800.0
        semi_diff  = (semi  - 1000.0) / 1000.0

        annual_adj = 0.0

        if sector == 'Defensive' or '필수소비재' in ind:
            annual_adj -= grain_diff * 0.04
        elif '농업' in ind or '비료' in ind:
            annual_adj += grain_diff * 0.06

        if '소재' in sector or '금속' in ind or '철강' in ind:
            annual_adj += metal_diff * 0.06
        elif '자동차' in ind or '건설' in ind or '조선' in ind:
            annual_adj -= metal_diff * 0.03
        elif sector == 'Value' and '산업재' in ind:
            annual_adj -= metal_diff * 0.02

        if 'IT' in sector or 'IT' in ind or '반도체' in ind:
            annual_adj += semi_diff * 0.05
        elif '전자' in ind or '통신장비' in ind:
            annual_adj += semi_diff * 0.03

        return annual_adj / 252