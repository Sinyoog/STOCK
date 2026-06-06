"""
engine/economy.py
MacroEngine: 경기선행지수 기반 사이클, 피드백 루프 3개, 섹터 로테이션.

[변경 사항]
- get_current_phase(): 신규 — _tech_upgrade_years 기반 페이즈 계산
- get_tech_level(): _tech_upgrade_year → _tech_upgrade_years 기록으로 변경
                   LV3→LV4 확률 누적 방식으로 변경 (5판에 1판 확률)
- apply_macro_sector_sensitivity(): _SECTOR_LV_ADJ 대신 PHASE_SECTOR_COEFF 사용
                                    temp_sector_buff 반영 추가
- _update_commodity_prices(): SOX 페이즈별 목표값 수렴 + 동적 상한 적용
                               200K 하드캡 제거, 페이즈별 장기 목표값으로 수렴
- _apply_macro_spillover(): 신규 — 지표 간 연쇄 인과관계 (유가→CPI, 금리→SOX 등)
                             시나리오와 독립적으로 매일 작동하는 물리 법칙 레이어
- 재건 섹터 관련 코드 제거 (temp_sector_buff로 대체)
"""
import random
import math
from datetime import timedelta
from .constants import (SECTOR_MAP, TECH_PHASE, PHASE_SECTOR_COEFF, SOX_GROWTH_RATE,
                         TECH_PRODUCTIVITY_SPILLOVER, TECH_LEVEL_UPGRADE,
                         NORMAL_PER_BY_SECTOR)


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

        [개선] _phase_offset_bonus로 A→B 전환 시점 유동화
        - 호재 시나리오(수출호황/혁신기술붐) → bonus 음수 → 조기 전환
        - 악재(대공황/전쟁) → bonus 양수 → 전환 지연
        - 경계값: 최소 3년, 최대 기본값+5년
        """
        lv = self.s.max_tech_reached
        upgrade_years = getattr(self.s, '_tech_upgrade_years', {1: 2000})
        lv_start = upgrade_years.get(lv) or 2000
        elapsed  = self.s.current_date.year - lv_start

        # ── phase_offset_bonus 실시간 계산 ────────
        # 매번 현재 경제 상태를 보고 보너스 산출
        bonus = 0
        macro   = self.s.macro
        sox     = macro.get('semi_index', 1000.0)
        scenario = self.s.current_scenario
        cycle   = getattr(self.s, 'cycle_stage', '확장')
        anchor  = self.s.gri / max(1.0, self.s.gri)  # 간이 계산

        # 가속 조건 (elapsed를 늘려서 더 빨리 B에 도달하게)
        if any(x in scenario for x in ["수출 호황", "혁신 기술 붐", "팬데믹 극복"]):
            bonus -= 2   # 최대 2년 앞당김
        elif any(x in scenario for x in ["유동성 장세", "외국인 대규모 유입", "내수 소비 붐"]):
            bonus -= 1

        # SOX 급성장 시 가속
        if lv == 1 and sox >= 3000:
            bonus -= 1
        elif lv == 2 and sox >= 20000:
            bonus -= 1
        elif lv >= 3 and sox >= 100000:
            bonus -= 1

        # GRI 목표 초과(호황) 시 가속
        lv_gri_targets = {1: 2500, 2: 7000, 3: 15000, 4: 30000}
        if self.s.gri >= lv_gri_targets.get(lv, 2500) * 1.3:
            bonus -= 1

        # 지연 조건
        if any(x in scenario for x in ["대공황", "전쟁", "팬데믹"]) and "극복" not in scenario:
            bonus += 3
        elif any(x in scenario for x in ["외부충격", "스태그플레이션", "환율위기"]):
            bonus += 2
        elif cycle in ('수축', '저점') and "극복" not in scenario:
            bonus += 1

        # 보너스 클램프: 최대 앞당김 -3년, 최대 지연 +5년
        bonus = max(-3, min(5, bonus))

        # elapsed에 bonus 반영 (bonus 음수면 elapsed 증가 → 더 빨리 B 도달)
        adjusted_elapsed = elapsed - bonus

        for phase in TECH_PHASE.get(lv, []):
            if phase["offset_start"] <= adjusted_elapsed < phase["offset_end"]:
                return phase["id"]

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
                lv_cfg = TECH_LEVEL_UPGRADE[1]
                lv1_start = upgrade_years.get(1, 2000)
                years_in_lv1 = cy - lv1_start

                # 강제전환: 13년 이상이면 무조건
                if years_in_lv1 >= lv_cfg["force_years"]:
                    evolution_chance = 1.0
                # 최소 조건: 10년 + GRI 1800
                elif years_in_lv1 < lv_cfg["min_years"] or self.s.gri < lv_cfg["gri_required"]:
                    evolution_chance = 0.0
                else:
                    # 10년 이후 점진적 확률 증가
                    base = min(0.15, (years_in_lv1 - lv_cfg["min_years"]) * 0.03) / 100 / 252
                    gri_mult = min(2.0, max(0.5, self.s.gri / lv_cfg["gri_required"]))
                    evolution_chance = base * gri_mult

            elif current_lv == 2:
                lv_cfg = TECH_LEVEL_UPGRADE[2]
                lv2_start = upgrade_years.get(2, cy)
                years_in_lv2 = cy - lv2_start if lv2_start else 0

                # 강제전환: 15년 이상
                if years_in_lv2 >= lv_cfg["force_years"]:
                    evolution_chance = 1.0
                # 최소 조건: 8년 + GRI 7000
                elif years_in_lv2 < lv_cfg["min_years"] or self.s.gri < lv_cfg["gri_required"]:
                    evolution_chance = 0.0
                else:
                    base = min(0.20, (years_in_lv2 - lv_cfg["min_years"]) * 0.025) / 100 / 252
                    gri_mult = min(2.0, max(0.5, self.s.gri / lv_cfg["gri_required"]))
                    evolution_chance = base * gri_mult
                    if years_in_lv2 >= 12:
                        evolution_chance = max(evolution_chance, 0.15 / 252)

            elif current_lv == 3:
                # LV3 → LV4: 복합 조건 기반 (dispatcher._check_lv4_conditions()에서 처리)
                # 여기서는 복합 조건이 충족됐을 때만 evolution_chance 설정
                lv4_days = getattr(self.s, '_lv4_condition_days', 0)
                from .constants import LV4_UNLOCK_CONDITIONS
                sustain = LV4_UNLOCK_CONDITIONS.get("sustain_days", 252)
                if lv4_days >= sustain:
                    evolution_chance = 1.0
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
                # ★ [수정] 절대 경과일로 전환 시점 기록 (버그7)
                self.s._phase_transition_abs = getattr(self.s, '_total_days_elapsed', 0)
                self.s._phase_transition_day = getattr(self.s, 'cycle_day', 0)  # 하위 호환 유지

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
        """
        테크 전환 시 처리:
        1. GRI 즉각 부양 (기대감 선반영 — 주가가 먼저 뜀)
        2. efficiency는 즉시 올리지 않고 시차 큐에 적재
           → lag_years 후 실제 실적에 반영 (버블 → 크래시 → 실적 확인 구조)
        3. catalyst 산업(반도체 등)에만 즉시 소폭 효율 부스트 (수요 폭발 반영)
        """
        spillover = TECH_PRODUCTIVITY_SPILLOVER.get(new_lv, {})
        catalyst  = spillover.get("catalyst", "IT")
        lag_years = spillover.get("lag_years", 3)
        수혜_map   = spillover.get("수혜", {})
        피해_map   = spillover.get("피해", {})

        if new_lv == 2:
            gri_boost   = 0.10
            new_sentiment = 75
            ff_boost    = 30
        elif new_lv == 3:
            gri_boost   = 0.15
            new_sentiment = 80
            ff_boost    = 35
        elif new_lv == 4:
            gri_boost   = 0.20
            new_sentiment = 85
            ff_boost    = 40
        else:
            return

        # ── 1. GRI 즉각 부양 (기대감) ─────────────
        bubble = getattr(self.s, 'bubble_index', 0.0)
        if bubble >= 150:
            gri_boost *= 0.3
        elif bubble >= 100:
            gri_boost *= 0.6

        self.s.gri = self.s.gri * (1.0 + gri_boost)
        self.s.sentiment = max(getattr(self.s, 'sentiment', 50.0), float(new_sentiment))
        self.s.foreign_flow_index = min(
            100.0,
            getattr(self.s, 'foreign_flow_index', 0.0) + ff_boost
        )

        # ── 2. catalyst 산업 즉시 소폭 효율 부스트 ─
        # 반도체/IT 수요 폭발은 즉시 반영 (단, 소폭만 — 대부분은 시차 큐로)
        for stock in self.s.stocks:
            meta = stock['meta']
            ind  = meta.get('ind', '')
            if ind == catalyst:
                old_eff = meta.get('efficiency', 0.05)
                # 즉시 부스트: 파급 강도의 20%만 (나머지 80%는 시차 큐)
                instant_boost = 수혜_map.get(catalyst, 0.5) * 0.20
                meta['efficiency'] = max(0.005, min(0.30, old_eff * (1.0 + instant_boost)))
            # 피해 산업도 즉시 소폭 반영
            elif ind in 피해_map:
                old_eff = meta.get('efficiency', 0.05)
                instant_pen = 피해_map[ind] * 0.15
                meta['efficiency'] = max(0.005, meta.get('efficiency', 0.05) * (1.0 - instant_pen))

        # ── 3. 나머지 파급 효과는 시차 큐에 적재 ──
        # lag_years 후에 economy.py _apply_productivity_lag()에서 실제 반영
        expire_year = self.s.current_date.year + lag_years + 10  # 여유 기간
        lag_queue = getattr(self.s, '_productivity_lag_queue', {})

        for ind, strength in 수혜_map.items():
            if ind == catalyst:
                remaining_boost = strength * 0.80  # catalyst는 20% 즉시 반영했으므로
            else:
                remaining_boost = strength
            lag_queue[ind] = {
                "boost":          remaining_boost * 0.15,  # efficiency 최대 +15%
                "expire_year":    expire_year,
                "lag_remaining_years": lag_years,
                "direction":      1,
            }

        for ind, strength in 피해_map.items():
            existing = lag_queue.get(ind, {})
            if existing.get("direction", 1) == 1:  # 수혜와 피해가 겹치면 수혜 우선
                continue
            lag_queue[ind] = {
                "boost":          -strength * 0.10,
                "expire_year":    expire_year,
                "lag_remaining_years": lag_years,
                "direction":      -1,
            }

        self.s._productivity_lag_queue = lag_queue

        lv_name = {2: "모바일·클라우드", 3: "AI·양자", 4: "기술 특이점"}.get(new_lv, "")
        self.s.daily_news.append(
            f"📊 [테크 충격] {lv_name} 혁명 — "
            f"GRI +{gri_boost*100:.0f}% 즉각 상승! "
            f"핵심 산업({catalyst}) 즉시 수혜, 전 산업 파급은 {lag_years}년 후 반영"
        )

    def _apply_productivity_lag(self):
        """
        매년 1월 시차 큐 감소 처리.
        lag_remaining_years가 0이 되면 해당 산업 전 종목에 efficiency 반영.
        """
        cur_year = self.s.current_date.year
        if self.s.current_date.month != 1 or self.s.current_date.day > 7:
            return

        lag_queue = getattr(self.s, '_productivity_lag_queue', {})
        to_apply  = []
        to_remove = []

        for ind, info in list(lag_queue.items()):
            if cur_year > info.get("expire_year", 9999):
                to_remove.append(ind)
                continue
            lag_queue[ind]["lag_remaining_years"] -= 1
            if lag_queue[ind]["lag_remaining_years"] <= 0:
                to_apply.append((ind, info["boost"]))
                to_remove.append(ind)

        # 실제 efficiency 반영
        for ind, boost in to_apply:
            affected = [s for s in self.s.stocks if s['meta'].get('ind') == ind]
            for stock in affected:
                meta    = stock['meta']
                old_eff = meta.get('efficiency', 0.05)
                new_eff = max(0.005, min(0.30, old_eff * (1.0 + boost)))
                meta['efficiency'] = new_eff

            direction = "수혜" if boost > 0 else "피해"
            if not self.s.silent_mode and affected:
                self.s.daily_news.append(
                    f"⚙️ [생산성 파급] {ind} 산업 기술 시차 반영 — "
                    f"efficiency {direction} ({boost*100:+.1f}%)"
                )

        for ind in to_remove:
            lag_queue.pop(ind, None)
        self.s._productivity_lag_queue = lag_queue

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

        # ★ [수정] 초반 GRI 하락 → 수축기 자기강화 루프 차단
        # 게임 시작 2년 내: GRI 하락이 leading을 음수로 만들어 수축기를 조기 유발하는 문제
        # GRI가 하락 중이어도 초반엔 경기 사이클이 수축으로 넘어가지 않도록 gri_score 하한 설정
        _start_yr = getattr(self.s, 'start_date', self.s.current_date).year
        _elapsed  = self.s.current_date.year - _start_yr
        if _elapsed <= 1:
            gri_score = max(0.05, gri_score)   # 시작 1년: gri_score 항상 양수
        elif _elapsed <= 2:
            gri_score = max(-0.05, gri_score)  # 2년차: 약한 음수까지만 허용

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

        if leading >= 0.10:     # 기존 0.15 → 0.10 (확장기 유지 범위 확대)
            new_stage = "확장"
        elif leading >= -0.05:  # 기존 0.0 → -0.05 (정점 구간 하향)
            new_stage = "정점"
        elif leading >= -0.20:  # 기존 -0.15 → -0.20
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

        # ── 생산성 파급 시차 처리 (매년 1월) ────────
        self._apply_productivity_lag()

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
        # ★ [수정] 유가 기준 목표값 현실화
        # LV1: 장기 평균 $55이지만 전쟁/공급망 충격 시 $150 가능 (2008년 $147)
        target_oil      = {1: 55, 2: 90, 3: 120, 4: 25}.get(lv, 55)
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

        # ★ [신규] 유가 → CPI 피드백
        # 현실: 유가 10% 상승 → CPI 약 0.3~0.5%p 상승 (에너지·운송 비용 전가)
        _oil_prev = self.s._prev_macro_snapshot.get('oil_price', macro["oil_price"])
        _oil_chg_rate = (macro["oil_price"] - _oil_prev) / max(1.0, _oil_prev)
        if abs(_oil_chg_rate) >= 0.02:  # 2% 이상 변동 시만 반영
            cpi_step += _oil_chg_rate * 0.04   # 유가 10% 상승 → CPI +0.4%p 압력

        # ★ [신규] 환율 → CPI 피드백 (수입물가 경로)
        # 현실: 원/달러 100원 상승(원화 10% 약세) → CPI 약 0.5~1.0%p 상승
        _fx_prev = self.s._prev_macro_snapshot.get('exchange_rate', macro.get("exchange_rate", 1100))
        _fx_now  = macro.get("exchange_rate", 1100)
        _fx_chg_rate = (_fx_now - _fx_prev) / max(1.0, _fx_prev)
        if _fx_chg_rate >= 0.03:  # 환율 3% 이상 상승(원화 약세) 시
            cpi_step += _fx_chg_rate * 0.06   # 원화 10% 약세 → CPI +0.6%p 압력

        macro["cpi"] = max(0.3, min(15.0, macro["cpi"] + cpi_step))
        if macro["cpi"] > target_cpi + 1.0:
            target_interest += 0.5

        # ── 금리 업데이트 (금통위 방식) ──────────
        # 현실: 중앙은행은 격월 회의에서 0.25%p 단위로 결정, 회의 사이엔 고정
        # 구현: 짝수달 1일 = 금통위 회의일, 0.25%p 단위 인상/동결/인하 결정
        #        회의 사이에는 시장금리 소폭 등락만 허용 (±0.02% 이내)
        cur_rate = macro["interest_rate"]
        cur_month = self.s.current_date.month
        cur_day   = self.s.current_date.day
        is_meeting_day = (cur_month % 2 == 0) and (cur_day == 1)

        if lv == 1 and not is_depression and "대공황" not in scenario:
            r_max = 6.0    # 3.5 → 6.0 (현실: 2000년대 한국 금리 4~5.5% 수준)
        elif is_depression:
            r_max = 12.0
        else:
            r_max = 8.0

        if is_meeting_day:
            # 금통위 결정: 0.25%p 단위
            gap = target_interest - cur_rate
            if gap >= 0.20:        # 목표보다 낮음 → 인상
                decision = 0.25
            elif gap <= -0.20:     # 목표보다 높음 → 인하
                decision = -0.25
            else:                  # 목표 근접 → 동결
                decision = 0.0
            # 긴급 인상: CPI 과열 + 확장기 → 0.5%p 빅스텝 가능
            if macro["cpi"] > target_cpi + 2.5 and cycle in ("확장", "정점"):
                decision = 0.50
            # 긴급 인하: 대공황/수축기 + 금리 3% 이상
            if is_depression and cur_rate >= 3.0:
                decision = -0.50
            macro["interest_rate"] = max(0.1, min(r_max, cur_rate + decision))
            # 금통위 결정 뉴스
            if decision != 0.0 and not self.s.silent_mode:
                arrow = "인상" if decision > 0 else "인하"
                new_rate_val = macro['interest_rate']
                cpi_val      = macro['cpi']
                self.s.daily_news.append(
                    f"🏦 [금통위] {self.s.current_date.year}년 {cur_month}월 "
                    f"기준금리 {abs(decision):.2f}%p {arrow} 결정 "
                    f"({cur_rate:.2f}% → {new_rate_val:.2f}%) | "
                    f"CPI {cpi_val:.2f}% / 목표 {target_cpi:.1f}%"
                )
        else:
            # 회의 외 기간: 시장금리 소폭 등락만 (실제 기준금리는 변하지 않음)
            market_noise = random.uniform(-0.015, 0.015)
            macro["interest_rate"] = max(0.1, min(r_max, cur_rate + market_noise))

        # ── 유가 업데이트 ─────────────────────────
        # ★ [수정] 유가: 하드캡 먼저 계산 후 target_oil 시나리오 보정 → 수렴
        _oil_hard_cap = {1: 150.0, 2: 200.0, 3: 250.0, 4: 80.0}.get(lv, 150.0)
        _war_now2 = getattr(self.s, 'war_event', {})
        if _war_now2.get('phase') == '진행중':
            _oil_hard_cap = min(_oil_hard_cap * 1.5, 300.0)

        # ★ [신규] 시나리오별 target_oil 보정
        _war2        = getattr(self.s, 'war_event', {})
        _war_region2 = _war2.get('region', '')
        _war_phase2  = _war2.get('phase', '')
        _war_type2   = _war2.get('type', '')

        if _war_phase2 == '진행중':
            if '중동' in _war_region2:
                _oil_mult = 1.8 if _war_type2 == '대규모전쟁' else 1.4
                target_oil = target_oil * _oil_mult
            elif '동유럽' in _war_region2 or '러시아' in _war_region2:
                _oil_mult = 1.6 if _war_type2 == '대규모전쟁' else 1.3
                target_oil = target_oil * _oil_mult
            else:
                target_oil *= 1.15
        elif _war_phase2 == '종전':
            target_oil *= 1.05

        if '팬데믹' in scenario and '극복' not in scenario:
            _timer2   = getattr(self.s, 'scenario_timer', 0)
            _elapsed2 = max(0, 252 * 3 - _timer2)
            _prog2    = min(1.0, _elapsed2 / (252 * 3))
            if _prog2 < 0.3:
                target_oil *= 0.70
            elif _prog2 < 0.7:
                target_oil *= 0.85
            else:
                target_oil *= 1.05
        elif any(x in scenario for x in ['팬데믹 극복', '팬데믹V', '비대면 전환']):
            target_oil *= 1.20

        if '공급망' in scenario:
            target_oil *= 1.50
        if '스태그' in scenario:
            target_oil *= 1.35
        if any(x in scenario for x in ['금융위기', '글로벌 침체']) and '극복' not in scenario:
            target_oil *= 0.75
        if '긴축 쇼크' in scenario:
            target_oil *= 0.85
        if any(x in scenario for x in ['FTA', '실적 장세', '내수 소비 붐', '슈퍼사이클']):
            target_oil *= 1.10

        target_oil = max(10.0, min(_oil_hard_cap, target_oil))

        # ★ [수정] 수렴 속도 현실화: 0.01 → 0.025 (기존 너무 느려서 50달러 차이도 수년 소요)
        oil_drift = (target_oil - macro["oil_price"]) * 0.025
        # 단기 스파이크: 낮은 확률로 ±5~15% 충격 (일 기준)
        _oil_spike = 0.0
        if random.random() < 0.003:   # 연 약 0.75회 스파이크
            _spike_dir = 1 if random.random() < 0.5 else -1
            _oil_spike = macro["oil_price"] * random.uniform(0.05, 0.15) * _spike_dir
            if not self.s.silent_mode and abs(_oil_spike) > macro["oil_price"] * 0.08:
                _dir_str = "급등" if _spike_dir > 0 else "급락"
                self.s.daily_news.append(
                    f"⛽ [유가 {_dir_str}] 국제유가 단기 충격 "
                    f"(${macro['oil_price']:.0f} → ${macro['oil_price'] + _oil_spike:.0f})"
                )
        macro["oil_price"] = max(5, min(_oil_hard_cap, macro["oil_price"] + oil_drift + _oil_spike + random.uniform(-1.5, 1.5)))

        # ── 환율 업데이트 ─────────────────────────
        # 결정 요인 (우선순위순):
        # 1. 금리 수준: 높을수록 달러 강세 → 환율 상승
        # 2. 경상수지(수출): SOX/수출 강세 → 달러 유입 → 원화 강세
        # 3. 외국인 자금 흐름: ff 양수 → 원화 강세, 음수 → 약세
        # 4. 금리 인하 방향 × 경기 사이클
        # 5. 위기/전쟁/CPI 과열 → 원화 약세

        # ① 금리 수준 영향 (기준금리 3.5% 대비 편차)
        rate_impact = (macro["interest_rate"] - 3.5) * 18

        # ② 외국인 자금 흐름 직접 연결
        # ff +10 = 달러 공급 → 원화 강세 약 -7원
        ff = getattr(self.s, 'foreign_flow_index', 0.0)
        ff_impact = -(ff * 0.7)   # ff +100이면 -70원 하락 압력

        # ③ 수출 경쟁력 (SOX 기반 경상수지 대리 변수)
        _SOX_LONG_TARGET_FX = {
            '1A': 500.0, '1B': 1500.0, '2A': 5000.0, '2B': 15000.0,
            '3A': 50000.0, '3B': 120000.0, '4A': 300000.0, '4B': 999999.0,
        }
        _phase_now = self.get_current_phase()
        sox_now    = macro.get('semi_index', 1000.0)
        sox_tgt    = _SOX_LONG_TARGET_FX.get(_phase_now, 1000.0)
        sox_ratio  = sox_now / max(1.0, sox_tgt)
        # SOX가 목표 초과(수출 호황) → 원화 강세 압력
        if sox_ratio > 1.1:
            target_fx -= min(80, (sox_ratio - 1.0) * 120)
        # SOX가 목표 미달(수출 부진) → 원화 약세 압력
        elif sox_ratio < 0.5:
            target_fx += min(60, (0.5 - sox_ratio) * 80)

        # ④ 금리 인하 방향 × 경기 사이클
        prev_rate_for_fx = self.s._prev_macro_snapshot.get('interest_rate', macro["interest_rate"])
        rate_delta_fx    = macro["interest_rate"] - prev_rate_for_fx
        if abs(rate_delta_fx) >= 0.20:  # 금통위 결정 있는 날만
            if rate_delta_fx < 0:       # 금리 인하
                if cycle in ('확장', '정점'):   # 경기 좋을 때 인하 → 유동성 → 원화 강세
                    target_fx -= abs(rate_delta_fx) * 60
                else:                           # 침체 대응 인하 → 위기 신호 → 원화 약세
                    target_fx += abs(rate_delta_fx) * 40
            else:                       # 금리 인상
                if cycle in ('수축', '저점'):   # 스태그 → 원화 급약세
                    target_fx += rate_delta_fx * 50
                else:
                    target_fx += rate_delta_fx * 20

        # ⑤ 시나리오별 압력
        _boom = getattr(self.s, 'boom_event', {})
        if _boom.get('phase') == '진행중':
            btype = _boom.get('type', '')
            if btype == '수출호황':
                target_fx -= 100    # 수출 달러 유입 → 강한 원화 강세
            elif btype == '유동성장세':
                target_fx -= 60
            elif btype == '외국인유입':
                target_fx -= 80

        # ★ 대공황V/팬데믹 극복(장기 호황) 중 GRI 성장률 기반 원화 강세 압력
        # 현실: 장기 경기 회복기엔 수출 증가 → 경상수지 흑자 → 원화 강세
        if '대공황V' in scenario or '팬데믹 극복' in scenario:
            hist20 = getattr(self.s, '_gri_history_20', [])
            if len(hist20) >= 20:
                gri_20d_growth = (hist20[-1] - hist20[0]) / max(1.0, hist20[0])
                if gri_20d_growth > 0.01:   # 20일간 1% 이상 성장 중
                    # 성장률에 비례한 원화 강세 압력 (최대 -60원)
                    target_fx -= min(60, gri_20d_growth * 400)

        war = getattr(self.s, 'war_event', {})
        if war.get('phase') == '진행중':
            if war.get('type') == '대규모전쟁':
                target_fx += 150
            else:
                target_fx += 80

        if is_depression:
            target_fx += 200

        if macro["cpi"] > 4.0:
            target_fx += (macro["cpi"] - 4.0) * 25

        sanctions = getattr(self.s, 'export_sanctions', {})
        if any(s.get('phase') == '단기충격' for s in sanctions.values()):
            target_fx += 50

        # 수렴 속도 세분화:
        # - 위기(전쟁/대공황) 중: 0.015 (충격 후 급복귀 방지)
        # - 평시/호황: 0.025 (빠른 정상화)
        final_target_fx = max(900, min(2000, target_fx + rate_impact + ff_impact))
        _fx_in_crisis = (
            is_depression or
            getattr(self.s, 'war_event', {}).get('phase') == '진행중'
        )
        _fx_converge = 0.015 if _fx_in_crisis else 0.025
        diff_fx = (final_target_fx - macro["exchange_rate"]) * _fx_converge + random.uniform(-6, 6)
        # 일별 최대 변동폭: 위기 중 ±15원, 평시 ±10원
        _fx_max_move = 15.0 if _fx_in_crisis else 10.0
        macro["exchange_rate"] += max(-_fx_max_move, min(_fx_max_move, diff_fx))
        macro["exchange_rate"]  = max(900, min(2000, macro["exchange_rate"]))

        # ── 물가 누적 ────────────────────────────
        self.s.cumulative_inflation *= (1 + (macro["cpi"] / 100) / 252)
        self.s.base_item_price = 1000.0 * self.s.cumulative_inflation

        # ── 지표 간 연쇄 인과 적용 ───────────────
        # 시나리오와 독립적인 물리 법칙 레이어 (유가→CPI, 금리→SOX 등)
        self._apply_macro_spillover()

        # ★ [신규] 외국인 수급 지수(ff) 자연 평균회귀
        # 현실: 외국인 수급은 장기적으로 0 근방으로 수렴
        # +100 고착 문제: 기술도약/이머징붐 등에서 계속 더해지는데 빼는 로직 없음
        # ★ [수정] ff 고착 방지 (버그2: +100 포화 → 회귀율 상향 + 포화 시 가속)
        ff_now = getattr(self.s, 'foreign_flow_index', 0.0)
        if ff_now != 0.0:
            ff_revert = ff_now * 0.035  # 2% → 3.5% 기본 회귀
            # 포화 상태(절대값 > 75)이면 추가 가속 — 극단값 고착 방지
            if abs(ff_now) > 75:
                ff_revert *= 1.8
            scenario = self.s.current_scenario
            if getattr(self.s, 'boom_event', {}).get('phase') == '진행중':
                ff_revert *= 0.4   # 호황 중 느린 회귀
            elif any(x in scenario for x in ['대공황', '전쟁', '분쟁', '팬데믹', '금융위기']):
                ff_revert *= 2.5   # 위기 중 빠른 이탈
            self.s.foreign_flow_index = max(-100.0, min(100.0, ff_now - ff_revert))

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
        # [수정] 재건 시나리오 GRI 성장률 분리
        # 대공황V/팬데믹 극복: 큰 폭 하락 후 반등이므로 1.004 유지
        # 재건(전쟁 후): 전쟁 피해가 상대적으로 작고 기간이 길어 완만하게
        if any(x in scenario for x in ["✨ 대공황V", "✨ 팬데믹 극복"]):
            return self.s.gri * 1.004
        if "🏗️" in scenario:
            # 재건: 연 +5~8% 수준 (일별 +0.02~0.03% = 연 +5~7.5%)
            # 현실: 전후 재건 호황은 GDP 연 5~8% 추가 성장 수준
            return self.s.gri * 1.0002

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
            elif boom_type == '기업실적장세':
                return self.s.gri * 1.0030   # 실적 장세: 완만하고 꾸준한 상승
            elif boom_type == '혁신기술붐':
                return self.s.gri * 1.0042
            elif boom_type == '반도체슈퍼사이클':
                return self.s.gri * 1.0050   # 반도체 슈퍼사이클: 강한 상승
            elif boom_type == '경기정상화':
                return self.s.gri * 1.0025   # 정상화: 완만하고 지속적
            elif boom_type == '금융완화':
                return self.s.gri * 1.0035
            elif boom_type == 'FTA무역확대':
                return self.s.gri * 1.0028   # 완만하고 지속적
            elif boom_type == '인프라투자붐':
                return self.s.gri * 1.0032   # 건설/산업재 주도 상승
            elif boom_type == '이머징붐':
                return self.s.gri * 1.0040   # 외국인 수급 주도 강세

        # ── 신규 악재 시나리오 ───────────────────
        if "🔥 인플레이션 충격" in scenario:
            return self.s.gri * 0.998   # 완만한 하락 (성장은 유지되므로 대공황급 아님)
        if "⚔️ 기술 패권 경쟁" in scenario:
            return self.s.gri * 0.997   # IT 타격으로 지수 하락 압력
        if "🏚️ 부동산 버블 붕괴" in scenario:
            return self.s.gri * 0.995   # 금융/부동산 연쇄 → 지수 하락
        if "📉 구조적 저성장" in scenario:
            return self.s.gri * 0.9995  # 거의 제자리 (장기 정체)

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
        # ★ [수정] 절대 경과일 기준 부스트 (버그7: cycle_day 리셋 오용 방지)
        _trans_abs = getattr(self.s, '_phase_transition_abs', -9999)
        _cur_abs   = getattr(self.s, '_total_days_elapsed', 0)
        _days_since_trans = _cur_abs - _trans_abs
        if 0 <= _days_since_trans <= 252:
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
                annual_adj += 0.04
                if sector == 'Growth':
                    annual_adj += 0.04
            elif boom_type == '내수붐':
                if ind in ('필수소비재', '자유소비재', '커뮤니케이션'):
                    annual_adj += 0.10
                if ind == '부동산':
                    annual_adj += 0.06
            elif boom_type == '기업실적장세':
                # 전 섹터 고른 상승 — Value 먼저, Growth 후반
                annual_adj += 0.04
                if sector == 'Value':
                    annual_adj += 0.04   # 금융/산업재/소재 추가 수혜
                if sector == 'Cyclical':
                    annual_adj += 0.03
            elif boom_type == '반도체슈퍼사이클':
                if ind in ('IT', '소재'):
                    annual_adj += 0.12
                if ind == '산업재':
                    annual_adj += 0.06
            elif boom_type == '경기정상화':
                # 전 섹터 균형 상승 — 쏠림 없음
                annual_adj += 0.03
                if sector == 'Value':
                    annual_adj += 0.02
            elif boom_type == '금융완화':
                if ind in ('금융', '부동산'):
                    annual_adj += 0.10
                elif sector == 'Growth':
                    annual_adj += 0.05
                else:
                    annual_adj += 0.03
            elif boom_type == 'FTA무역확대':
                # 수출 산업 수혜: IT/산업재/소재
                if ind in ('IT', '산업재', '소재'):
                    annual_adj += 0.08
                elif ind == '에너지':
                    annual_adj += 0.03
                else:
                    annual_adj += 0.02
            elif boom_type == '인프라투자붐':
                # 건설/에너지/산업재/소재 강세
                if ind in ('산업재', '소재', '유틸리티'):
                    annual_adj += 0.10
                elif ind == '에너지':
                    annual_adj += 0.06
                elif ind == '건강관리':
                    annual_adj += 0.02
                else:
                    annual_adj += 0.015
            elif boom_type == '이머징붐':
                # 외국인 선호: 대형 성장주 + 금융
                if sector == 'Growth':
                    annual_adj += 0.09
                elif ind == '금융':
                    annual_adj += 0.06
                else:
                    annual_adj += 0.03

        # ── 신규 악재 시나리오 섹터 영향 ─────────
        # (boom 블록 밖 — 시나리오 직접 체크)
        if "🔥 인플레이션 충격" in scenario:
            if ind in ('에너지', '소재'):
                annual_adj += 0.08
            if ind == '부동산':
                annual_adj += 0.04
            if ind in ('자유소비재', '필수소비재'):
                annual_adj -= 0.08
            if sector == 'Growth':
                annual_adj -= 0.05
        elif "⚔️ 기술 패권 경쟁" in scenario:
            if ind in ('소재', '산업재'):
                annual_adj += 0.06
            if ind == 'IT':
                annual_adj -= 0.10
            if ind == '커뮤니케이션':
                annual_adj -= 0.05
        elif "🏚️ 부동산 버블 붕괴" in scenario:
            if ind == '부동산':
                annual_adj -= 0.15
            if ind == '금융':
                annual_adj -= 0.10
            if ind in ('필수소비재', '유틸리티'):
                annual_adj += 0.04
            if ind == '자유소비재':
                annual_adj -= 0.06
        elif "📉 구조적 저성장" in scenario:
            if ind in ('유틸리티', '필수소비재'):
                annual_adj += 0.03
            if sector == 'Growth':
                annual_adj -= 0.04
            if ind == '자유소비재':
                annual_adj -= 0.05

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
        # 구리 상한 조정: LV별 동적 상한 (무한 상승 방지)
        _metal_cap = {1: 8000.0, 2: 12000.0, 3: 16000.0, 4: 20000.0}.get(lv, 12000.0)
        macro['metal_price'] = max(500.0, min(_metal_cap, metal + metal_step))

        # ── SOX 반도체 지수 ───────────────────────
        semi  = macro.get('semi_index', 1000.0)
        phase = self.get_current_phase()

        # ★ 페이즈별 SOX 장기 목표값 (현실 SOX 궤적 참고)
        # 1A: 닷컴버블 전 ~500 / 1B: 회복기 ~1,500
        # 2A: 모바일 붐 ~5,000 / 2B: 클라우드 ~15,000
        # 3A: AI 혁명 ~50,000 / 3B: 양자/바이오 ~120,000
        # 4A/4B: 특이점 이후 상한 없음
        _SOX_LONG_TARGET = {
            '1A':   500.0,     '1B':   1_500.0,
            '2A':   5_000.0,   '2B':  15_000.0,
            '3A':  50_000.0,   '3B': 120_000.0,
            '4A': 300_000.0,   '4B': 999_999.0,
        }
        sox_long_target = _SOX_LONG_TARGET.get(phase, 1000.0)

        # 동적 상한: 목표값의 3배까지 버블 허용 (버블 붕괴 시 자연 수렴)
        sox_cap = sox_long_target * 3.0

        # 목표값 대비 편차 → 수렴력 (멀수록 강하게 당김, 목표 초과 시 역방향)
        sox_gap_pull = (sox_long_target - semi) / max(1.0, sox_long_target) * 0.002

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
        # 노이즈 크기를 현재값에 비례시켜 초기/후기 스케일 불균형 해소
        noise_scale  = min(semi * 0.005, 500.0)
        semi_step    = semi * (semi_dir - 1.0) + semi_natural + semi * sox_gap_pull + random.uniform(-noise_scale, noise_scale)
        raw_next = semi + semi_step

        # ★ SOX 일별 최대 하락폭 제한 (급락 속도 캡)
        # 현실: SOX 최악의 붕괴(닷컴버블 -82%)도 2년에 걸쳐 발생
        # 일별 최대 하락: -3% (연환산 -53% 수준)
        # 단, 대공황 + 전쟁 동시는 -4%까지 허용
        _semi_scenario_now = self.s.current_scenario
        _war_now = getattr(self.s, 'war_event', {})
        if '대공황' in _semi_scenario_now and _war_now.get('phase') == '진행중':
            _max_daily_drop_pct = 0.04
        else:
            _max_daily_drop_pct = 0.03
        _max_daily_drop = semi * _max_daily_drop_pct
        raw_next = max(semi - _max_daily_drop, raw_next)

        # ★ SOX 페이즈 하한선 (100 박스 방지)
        # 페이즈 목표값의 1%를 절대 하한으로 설정
        # (LV1A 목표 500 → 하한 5, LV2B 목표 15000 → 하한 150)
        sox_floor = max(100.0, sox_long_target * 0.01)
        macro['semi_index'] = max(sox_floor, min(sox_cap, raw_next))

    # ─────────────────────────────────────────────
    # ★ 지표 간 연쇄 인과관계 (신규)
    # 시나리오와 독립적으로 매일 작동하는 물리 법칙 레이어.
    # update_macro_logic() 마지막에서 호출됨.
    # ─────────────────────────────────────────────
    def _apply_macro_spillover(self):
        """
        거시 지표 간 연쇄 인과관계.

        전파 경로:
          유가/곡물 → CPI 전가
          CPI 과열  → 금리 긴급 압력
          금리 인상 → SOX 할인율 압박 (성장주 PER 압축)
          금리 인상 → 환율 상승 (달러 강세)
          구리 급등 → 금리 선행 압력 (닥터 코퍼 효과)
          환율 약세 → SOX 단기 수혜 (수출 채산성 개선)
          SOX 장기 침체 → IT 섹터 efficiency 점진 악화

        모든 값은 일별 소폭 적용 — 시나리오 레이어와 중복되지 않음.
        """
        macro = self.s.macro
        prev  = getattr(self.s, '_prev_macro_snapshot', {})
        lv    = self.s.max_tech_reached

        oil   = macro.get('oil_price', 30.0)
        rate  = macro.get('interest_rate', 4.0)
        cpi   = macro.get('cpi', 2.0)
        fx    = macro.get('exchange_rate', 1200.0)
        semi  = macro.get('semi_index', 1000.0)
        grain = macro.get('grain_price', 250.0)
        metal = macro.get('metal_price', 1800.0)

        prev_oil   = prev.get('oil_price',   oil)
        prev_rate  = prev.get('interest_rate', rate)
        prev_fx    = prev.get('exchange_rate', fx)
        prev_grain = prev.get('grain_price',  grain)
        prev_metal = prev.get('metal_price',  metal)

        # ── 유가 → CPI 전가 ──────────────────────
        # 현실: 유가 10% 상승 → 약 3~6개월 후 CPI 0.3~0.5%p 상승
        # 일별로 분산하면 매우 작은 값 (0.3%p / 126일 ≈ 0.0024/일)
        oil_chg = (oil - prev_oil) / max(1.0, prev_oil)
        if abs(oil_chg) > 0.003:   # 0.008 → 0.003 (0.3% 이상이면 CPI 전가)
            # 전가율: 유가 10% 상승 → CPI +0.06%p/일 (6개월 누적 ~0.3%p)
            macro['cpi'] = max(0.3, min(15.0, cpi + oil_chg * 0.008))  # 0.006 → 0.008

        # ── 곡물 → CPI 전가 ─────────────────────
        grain_chg = (grain - prev_grain) / max(1.0, prev_grain)
        if abs(grain_chg) > 0.003:   # 0.008 → 0.003
            macro['cpi'] = max(0.3, min(15.0, macro['cpi'] + grain_chg * 0.004))  # 0.003 → 0.004

        # CPI 재참조 (위에서 갱신됐을 수 있음)
        cpi = macro['cpi']

        # ── CPI 과열 → 금리 긴급 압력 ───────────
        # 목표 CPI 1.5%p 초과 시 추가 압력 (중앙은행 긴급 대응)
        target_cpi = {1: 2.0, 2: 2.5, 3: 4.0, 4: 1.0}.get(lv, 2.0)
        cpi_overshoot = max(0.0, cpi - (target_cpi + 1.5))
        if cpi_overshoot > 0:
            # 일별 금리 압력 (연간 환산 약 0.5%p/1%p overshoot)
            rate_pressure = cpi_overshoot * 0.0004
            macro['interest_rate'] = min(12.0, rate + rate_pressure)
            rate = macro['interest_rate']

        # ── 금리 인상 → SOX 할인율 압박 ─────────
        # 현실: 금리 0.25%p 인상 → 나스닥/SOX 약 3~8% 하락 압력
        # 단기 충격은 이미 시나리오에서 처리 — 여기선 누적 압력만
        rate_delta = rate - prev_rate
        # ★ [수정] SOX 금리 압박 계수 절반 축소 (버그: _update_commodity와 이중 적용 방지)
        if rate_delta > 0.03:
            sox_rate_pressure = -(rate_delta * semi * 0.03)  # 0.06 → 0.03
            macro['semi_index'] = max(100.0, semi + sox_rate_pressure)
            semi = macro['semi_index']

        # ── 금리 인상 → 환율 상승 (달러 강세) ───
        # 현실: 금리 0.25%p 인상 → 원/달러 약 5~15원 절하
        if rate_delta > 0.03:
            macro['exchange_rate'] = min(2000.0, fx + rate_delta * 12)
            fx = macro['exchange_rate']

        # ── 구리 급등 → 금리 선행 압력 (닥터 코퍼) ─
        # 현실: 구리가 경기 선행지표 — 급등은 과열 신호 → 중앙은행 선제 대응
        metal_chg = (metal - prev_metal) / max(1.0, prev_metal)
        if metal_chg > 0.008:   # 0.020 → 0.008 (0.8% 이상이면 금리 선행 압력)
            macro['interest_rate'] = min(12.0, macro['interest_rate'] + metal_chg * 0.0010)  # 0.0006 → 0.0010

        # ── 환율 약세 → SOX 단기 수혜 ───────────
        # 현실: 원화 약세 시 반도체 수출 원화 환산 이익 증가 → 반도체주 단기 상승
        # 단, 글로벌 달러 강세 동반이면 상쇄됨 — 여기선 순수 환율 채산성만
        fx_chg = (fx - prev_fx) / max(1.0, prev_fx)
        if fx_chg > 0.003:  # 0.3% 이상 원화 약세
            sox_fx_boost = semi * fx_chg * 0.08
            macro['semi_index'] = max(100.0, min(macro['semi_index'] + sox_fx_boost,
                                                  macro['semi_index'] * 1.02))  # 일 최대 +2%

        # ── SOX 장기 침체 → IT efficiency 점진 악화 ─
        # 현실: 반도체 불황 → IT 기업 CAPEX 감소 → 실적 악화
        # 페이즈 목표의 30% 미만이면 IT 기업 efficiency 소폭 감소
        phase = self.get_current_phase()
        _SOX_LONG_TARGET = {
            '1A':   500.0,     '1B':   1_500.0,
            '2A':   5_000.0,   '2B':  15_000.0,
            '3A':  50_000.0,   '3B': 120_000.0,
            '4A': 300_000.0,   '4B': 999_999.0,
        }
        sox_target = _SOX_LONG_TARGET.get(phase, 1000.0)
        sox_ratio  = macro['semi_index'] / max(1.0, sox_target)
        if sox_ratio < 0.3:
            # SOX가 목표의 30% 미만 = 반도체 심각한 침체
            # IT 기업 일별 0.03% efficiency 감소 (연간 약 -7.5%)
            for stock in self.s.stocks:
                if stock['meta'].get('ind') == 'IT':
                    stock['meta']['efficiency'] = max(
                        0.005,
                        stock['meta'].get('efficiency', 0.05) * 0.9997
                    )

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

        # SOX diff: 고정 기준값(1000) 대신 현재 페이즈 목표값 기준으로 계산
        # 목표값 대비 얼마나 앞서가는지/뒤처지는지가 섹터에 영향
        _SOX_LONG_TARGET = {
            '1A':   500.0,     '1B':   1_500.0,
            '2A':   5_000.0,   '2B':  15_000.0,
            '3A':  50_000.0,   '3B': 120_000.0,
            '4A': 300_000.0,   '4B': 999_999.0,
        }
        _phase_for_sox = self.get_current_phase()
        sox_base  = _SOX_LONG_TARGET.get(_phase_for_sox, 1000.0)
        semi_diff = (semi - sox_base) / max(1.0, sox_base)

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