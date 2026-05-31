"""
engine/market.py
StockMarket: 주가 변동, 상장폐지, 그룹 확장, 신규 상장, 자사주/분할/병합.

★ 수정 내역 (1단계):
1. 액면분할 정상화 - 트리거 상향, 쿨다운 3년, 생애 5회 제한
2. 버블 지수 억제력 강화 - 구간별 drift 보정, 자연감소율 강화
3. PER/PBR/ROE 밸류에이션 천장 - 섹터별 PER 상한, 주가 하락 압력
4. 부채-금리 연동 - 금리 상승 시 고부채 기업 HP 추가 차감
5. 수축기 하락 압력 강화 - drift 보정 강화
6. 52주 신고가/신저가 갱신
7. 시장 집중도 제한 - 단일 종목 GRI 기여 15% 상한
8. 신용등급 갱신 - HP 기반 매일 재산정
"""
import random
import math
from datetime import datetime, timedelta
from .constants import SECTOR_MAP, MAIN_INDUSTRIES, INDUSTRY_LEVELS


# ★ 섹터별 PER 허용 상한
_PER_LIMIT = {
    "Growth":    30.0,   # 50 → 30
    "Value":     13.0,   # 20 → 13
    "Defensive": 18.0,   # 30 → 18
    "Cyclical":     25.0,   # 40 → 25
}

# ★ 신용등급별 이자비용 가중치
_CREDIT_COST = {
    "AA":  1.0,
    "BB":  1.3,
    "CCC": 1.8,
}


class StockMarket:
    def __init__(self, state, economy, company_mgr, db=None):
        self.s   = state
        self.eco = economy
        self.cm  = company_mgr
        self._db = db  # SaveManager 참조 — 상폐 시 SQLite 저장용

    # ─────────────────────────────────────────────
    # 주가 변동 (핵심 엔진)
    # ─────────────────────────────────────────────
    def apply_price_change(self) -> float:
        interest_rate = self.s.macro["interest_rate"]
        cycle         = getattr(self.s, 'cycle_stage', '확장')
        sentiment     = getattr(self.s, 'sentiment', 50.0)
        bubble_index  = getattr(self.s, 'bubble_index', 0.0)
        lv            = self.s.max_tech_reached
        cur_date      = self.s.current_date
        total_market_cap  = 0.0
        total_net_income  = 0.0

        is_depression = (
            "대공황" in self.s.current_scenario
            and "V" not in self.s.current_scenario
            and "극복" not in self.s.current_scenario
        )

        # ── 시장 공통 일별 drift ─────────────────
        if is_depression:
            market_drift = -0.0008
        elif "극복" in self.s.current_scenario:
            market_drift = +0.0006
        else:
            market_drift = {
                "확장": +0.00080,   # 0.00060 → 0.00080 (연 20%)
                "정점": +0.00030,   # 0.00025 → 0.00030
                "수축": -0.00005,
                "저점": +0.00015,   # 0.00010 → 0.00015
            }.get(cycle, +0.00040)

        # ★ 초반 성장률 억제 — 연도 하드코딩 제거, 게임 시작 기준 상대 연수로
        _start_year = getattr(self.s, 'start_date', cur_date).year
        _years_elapsed = cur_date.year - _start_year
        if _years_elapsed <= 2:
            market_drift = min(market_drift, 0.00020)   # 상승 상한: 연 ~5%
            market_drift = max(market_drift, -0.00010)  # 하락 하한: 연 ~-2.5% (완화)
        elif _years_elapsed <= 5:
            market_drift = min(market_drift, 0.00030)   # 상승 상한: 연 ~7.5%
            market_drift = max(market_drift, -0.00015)  # 하락 하한: 연 ~-3.8% (완화)

        prev_rate  = getattr(self.s, '_prev_macro_snapshot', {}).get('interest_rate', interest_rate)
        rate_delta = interest_rate - prev_rate
        if   rate_delta >  0.1: market_drift -= 0.00020
        elif rate_delta < -0.1: market_drift += 0.00020
        market_drift += (sentiment - 50) / 50 * 0.00008

        # ★ 버블 drift 보정 — 상승 중일 때만 억제 적용
        # 이미 하락 중이면 건드리지 않음 (폭락 방지)
        if market_drift > 0:
            if   bubble_index >= 280: market_drift *= -0.1   # -0.3 → -0.1 (급반전 완화)
            elif bubble_index >= 250: market_drift *=  0.25  # -0.1 → 0.25 (성장 유지)
            elif bubble_index >= 200: market_drift *=  0.55  # 0.3  → 0.55
            elif bubble_index >= 150: market_drift *=  0.80  # 0.7  → 0.80

        peak_gri    = getattr(self.s, 'peak_gri', self.s.gri)
        if self.s.gri > peak_gri: self.s.peak_gri = peak_gri = self.s.gri
        gri_ratio   = self.s.gri / max(1.0, peak_gri)
        panic_factor = 0.97 if gri_ratio < 0.60 else (0.99 if gri_ratio < 0.80 else 1.0)  # 완화

        fear_mult = 1.0
        if self.s.macro.get("fear_index", 10) >= 50 and is_depression:
            fear_mult = 1.3

        tech_upgrade_year = getattr(self.s, '_tech_upgrade_years', {1: 1999}).get(lv) or 1999

        # ★ 산업별 경쟁도 갱신
        self._update_industry_competition()

        # ★ 산업별 평균 등락률 사전 계산 — _calc_cluster_adj O(n²) → O(n) 최적화
        _ind_rates: dict = {}
        for _s in self.s.stocks:
            _ind = _s['meta'].get('ind', '')
            if _ind:
                _ind_rates.setdefault(_ind, []).append(_s.get('rate', 0.0))
        _ind_avg_rate: dict = {
            k: sum(v) / len(v) for k, v in _ind_rates.items()
        }

        # ★ 서킷브레이커 사전 계산 (루프 밖에서 한 번만)
        # prev_gri 없는 첫날은 발동 안 함
        _prev_gri_for_cb = getattr(self.s, 'prev_gri', None)
        _circuit_breaker = False
        if _prev_gri_for_cb and _prev_gri_for_cb > 0:
            _gri_daily_chg = (self.s.gri - _prev_gri_for_cb) / _prev_gri_for_cb
            if _gri_daily_chg <= -0.05:
                _circuit_breaker = True

        # ★ 테마 모멘텀 — 루프 전 갱신 (elapsed +1, 만료 제거)
        # 루프 밖에서 1회만 갱신해야 함 (종목별로 중복 갱신 방지)
        _themes_to_remove = []
        for _t in self.s.active_themes:
            _t['elapsed'] += 1
            if _t['elapsed'] >= _t['duration']:
                _themes_to_remove.append(_t)
                # bull 테마 종료 시 쿨다운 기록
                if _t['type'] == 'bull':
                    self.s._theme_cooldown[_t['ind']] = cur_date.year
        for _t in _themes_to_remove:
            self.s.active_themes.remove(_t)

        # 테마 강도 계산 함수 (루프 안에서 재사용)
        def _calc_theme_intensity(theme: dict) -> float:
            """테마 진행 단계에 따라 강도 반환 (사인 곡선 기반)"""
            import math
            ratio = theme['elapsed'] / max(1, theme['duration'])
            # 초기 30% 상승, 중기 40% 피크 유지, 후기 30% 하락
            if ratio < 0.30:
                return theme['peak'] * (ratio / 0.30)
            elif ratio < 0.70:
                return theme['peak']
            else:
                return theme['peak'] * (1.0 - (ratio - 0.70) / 0.30)

        # ★ PER 계산은 메인 루프 안에서 함께 처리 (O(n) 루프 1회 절약)
        for stock in self.s.stocks:
            meta      = stock['meta']
            name      = meta['c_name']
            old_price = float(stock['price'])
            tier      = meta.get('tier', '소형주')
            ind       = meta.get('ind', '')
            sector    = SECTOR_MAP.get(ind, 'Value')

            # ★ 수급 구조 기반 변동성 (tier hard cap 제거)
            # 기관·외국인 비중 높을수록 변동성 낮아짐, 개인 비중 높을수록 커짐
            foreign_share = meta.get('foreign_share', 0.1)
            inst_share    = meta.get('inst_share',    0.15)
            retail_share  = meta.get('retail_share',  0.5)

            if   "대형" in tier: base_vol = 0.010; tier_mult = 0.8
            elif "중형" in tier: base_vol = 0.015; tier_mult = 1.0
            else:                base_vol = 0.020; tier_mult = 1.2

            vol_multiplier = 1.0 + (retail_share * 1.5) - (inst_share * 1.0) - (foreign_share * 0.8)
            vol_multiplier = max(0.4, min(2.5, vol_multiplier))
            vol = base_vol * vol_multiplier

            # ★ 섹터별 변동성 추가 조정
            if sector == "Cyclical":       vol *= 1.4   # 테마주: 변동성 가장 큼
            elif sector == "Growth":    vol *= 1.2   # 성장주: 변동성 큼
            elif sector == "Defensive": vol *= 0.8   # 방어주: 변동성 작음

            eff   = meta.get('efficiency', 0.05) * tier_mult
            # ★ 경기 사이클별 efficiency 변동
            cycle_eff_mult = {
                "확장": 1.10,   # 호황기: 마진 개선
                "정점": 1.00,
                "수축": 0.85,   # 불황기: 마진 악화
                "저점": 0.75,
            }.get(cycle, 1.0)
            eff  *= cycle_eff_mult
            alpha = (eff - 0.05) * 0.05 / 252  # ★ /252: 연간 기준 → 일별 변환

            # 섹터/테크 조정
            # ★ 섹터 기본 베이스 (레벨/사이클 무관 장기 추세)
            # cycle_sector는 사이클마다 등락하므로 이 베이스가 장기 우상향의 핵심
            sector_adj = {
                "Growth":    0.07 / 252,   # 5% → 7%
                "Value":     0.05 / 252,
                "Defensive": 0.040 / 252,
                "Cyclical":  0.05 / 252,   # 4% → 5%
            }.get(sector, 0.03 / 252)

            # ★ 레벨별 섹터 보너스 (LV가 높을수록 Growth 가속)
            if lv >= 4 and sector == "Growth":
                sector_adj += 0.05 / 252   # LV4: +5%
            elif lv >= 3 and sector == "Growth":
                sector_adj += 0.03 / 252   # LV3: +3%
            elif lv >= 2 and sector == "Growth":
                sector_adj += 0.03 / 252   # LV2: +3% (기존과 동일)
            elif lv >= 2 and sector == "Cyclical":
                sector_adj += 0.02 / 252
            if lv >= 3 and sector == "Value":
                sector_adj -= 0.02 / 252

            years_since_lv_up = cur_date.year - tech_upgrade_year
            if 0 <= years_since_lv_up <= 3:
                if   sector == "Growth":    sector_adj += 0.04 / 252
                elif sector == "Cyclical":  sector_adj += 0.03 / 252
                elif sector == "Defensive": sector_adj += 0.01 / 252

            # ★ PHASE_SECTOR_COEFF는 economy.py에서 이미 적용
            # market.py에서 중복 적용하지 않음

            cycle_sector = {
                # ★ 확장기
                ("확장", "Growth"):    +0.10 / 252,
                ("확장", "Value"):     +0.06 / 252,
                ("확장", "Defensive"): -0.03 / 252,
                ("확장", "Cyclical"):  +0.07 / 252,
                # ★ 정점기 — 완화 (시나리오 없는 자연 하락 방지)
                ("정점", "Defensive"): +0.05 / 252,
                ("정점", "Growth"):    -0.01 / 252,   # -0.03 → -0.01
                ("정점", "Cyclical"):  -0.02 / 252,   # -0.05 → -0.02
                ("정점", "Value"):     +0.02 / 252,
                # ★ 수축기 — 완화 (시나리오 없는 폭락 방지)
                ("수축", "Defensive"): +0.08 / 252,
                ("수축", "Growth"):    -0.02 / 252,   # -0.04 → -0.02
                ("수축", "Value"):     -0.004 / 252,  # -0.01 → -0.004
                ("수축", "Cyclical"):  -0.03 / 252,   # -0.06 → -0.03
                # ★ 저점기
                ("저점", "Value"):     +0.10 / 252,
                ("저점", "Growth"):    +0.05 / 252,
                ("저점", "Defensive"): +0.04 / 252,
                ("저점", "Cyclical"):  +0.03 / 252,
            }.get((cycle, sector), 0.0)
            sector_adj += cycle_sector

            # ★ 산업 경쟁 패널티 — 완화 (IT 과밀 패널티 누적 방지)
            ind_count = self.s.industry_competition.get(meta.get('ind', ''), 1)
            if ind_count > 40:  # 20 → 40으로 임계값 상향
                competition_penalty = min(0.005, (ind_count - 40) * 0.0003) / 252
                sector_adj -= competition_penalty

            # 실적 신호
            earnings_adj = 0.0
            hist   = self.s.earnings_history.get(name, {})\

            years  = sorted(hist.keys())
            annual_ni = self._calc_annual_net_income(name, hist)
            if years:
                last_q_data = hist[years[-1]]
                if last_q_data:
                    last_q   = sorted(last_q_data.keys())[-1]
                    last_ni  = last_q_data[last_q].get('net_income', 0)
                    assets   = max(1.0, meta.get('assets', 1.0))
                    loss_cnt = meta.get('continuous_loss_count', 0)
                    if last_ni > 0:
                        # ★ earnings_adj: 실적이 시나리오에 따라 주가에 반영
                        # earn_cap = 연간 기준 상한 (LV별 차등)
                        # 평시 ROE 5~7% → 연 2~4% / 호황 ROE 15% → 연 7.5%
                        # 역대급 ROE 25%+ → 최대 연 12% (상한)
                        _earn_cap = {1: 0.08, 2: 0.12, 3: 0.16, 4: 0.20}.get(lv, 0.08)
                        roe_annual = (last_ni / assets) * 0.5
                        earnings_adj = min(_earn_cap, roe_annual) / 252
                    else:
                        hist_q_count = sum(len(qd) for qd in hist.values())
                        if hist_q_count >= 4:
                            revenue      = max(1.0, last_q_data[last_q].get('revenue', assets * 0.05))
                            loss_ratio   = abs(last_ni) / revenue
                            _loss_cap = {1: -0.08, 2: -0.10, 3: -0.12, 4: -0.12}.get(lv, -0.08)
                            earnings_adj = max(_loss_cap, -loss_ratio * 0.3) / 252
                            if loss_cnt >= 3:
                                earnings_adj *= 1.3

            # HP 패닉 + HP → 주가 하락 압력
            hp       = meta.get('hp', 50.0)
            soft_cap = meta.get('hp_soft_cap', 60.0)
            hp_ratio = hp / max(1.0, soft_cap)
            shield   = meta.get('shield', 0.0)

            # ★ hp_panic 직접 곱셈 제거 — HP는 간접 경로로만 영향
            # HP 개념(호재→상승, 악재→하락, HP=0→상폐)은 유지
            # 대신 수급 이탈 + efficiency 하락 + 워크아웃 이벤트로 간접 전달

            # HP 30% 미만: 수급 이탈 가속
            if hp_ratio < 0.30:
                meta['foreign_share'] = max(0.0, meta.get('foreign_share', 0.0) - 0.003)
                meta['inst_share']    = max(0.0, meta.get('inst_share', 0.0)    - 0.004)

            # HP 15% 미만: efficiency 점진 하락 → 다음 분기 실적 악화
            if hp_ratio < 0.10:   # 0.15 → 0.10으로 강화 (정상 기업 efficiency 보호)
                meta['efficiency'] = meta.get('efficiency', 0.05) * 0.98  # 0.97 → 0.98

            # HP 5% 미만: 워크아웃 이벤트 → 단기 급등 가능 (좀비주 현상)
            if hp_ratio < 0.05 and random.random() < 0.02:
                meta['_hp_overflow_pump'] = random.uniform(0.10, 0.30)
                if not self.s.silent_mode:
                    self.s.daily_news.append(
                        f"🔔 [워크아웃] {name} 구조조정 기대감으로 단기 급등 발생"
                    )

            # ★ HP → 주가 하락 압력 (간접 — 수급 이탈 후 시장 반응)
            hp_pressure = 0.0
            if   hp_ratio < 0.15: hp_pressure = -0.002
            elif hp_ratio < 0.30: hp_pressure = -0.0008
            elif hp_ratio < 0.50: hp_pressure = -0.0002

            # ★ 방어막 발동 중 → 소폭 호재 (버텨준다는 신호)
            shield_active = hp_ratio < 0.30 and shield > 0
            shield_boost  = 0.001 if shield_active else 0.0

            noise = random.gauss(0, vol)

            # ★ momentum → daily_return 연결
            momentum     = meta.get('momentum', 0.0)
            momentum_adj = momentum * 0.1 / 252  # 연간 momentum의 일별 반영

            # ★ 금리 인하 → 주가 상승 강화
            rate_boost = 0.0
            if rate_delta < -0.1:   # 금리 인하 시
                rate_boost = abs(rate_delta) * 0.0005
                if sector in ["IT", "Growth"]:
                    rate_boost *= 1.5   # 성장주 더 강하게 반응

            daily_return = (
                market_drift + alpha + sector_adj + earnings_adj
                + momentum_adj + rate_boost + hp_pressure + shield_boost + noise
            ) * panic_factor * fear_mult   # ★ hp_panic 제거 — HP는 간접 경로로만 작동

            pump = meta.pop('_hp_overflow_pump', 0.0)
            daily_return += pump

            # ★ 테마 모멘텀 adj
            # 해당 종목 산업의 활성 테마 강도 합산
            _theme_adj = 0.0
            _theme_vol_mult = 1.0
            _tier_theme_mult = {'대형주': 1.0, '중형주': 1.8, '소형주': 3.5}.get(tier, 1.0)
            _tier_vol_boost  = {'대형주': 0.3, '중형주': 0.8, '소형주': 1.8}.get(tier, 0.5)

            for _t in self.s.active_themes:
                if _t['ind'] != ind:
                    continue
                _intensity = _calc_theme_intensity(_t)
                if _intensity <= 0:
                    continue

                # 테마 방향별 일별 조정값
                # base_effect: 연간 기준, /252로 일별 변환
                # 대형주 bull: 연 최대 30%, 소형주 bull: 연 최대 150%
                if _t['type'] == 'bull':
                    _base = 0.30 * _tier_theme_mult  # 대형주 30%, 소형 105%
                    _theme_adj += _intensity * _base / 252
                else:  # bear
                    _base = 0.20 * _tier_theme_mult
                    _theme_adj -= _intensity * _base / 252

                # vol 동적 상향 (테마 강도 * 체급 배율)
                _theme_vol_mult = max(_theme_vol_mult,
                                      1.0 + _intensity * _tier_vol_boost)

            daily_return += _theme_adj

            # ★ 테마 반영 vol 재조정 (noise 재계산)
            if _theme_vol_mult > 1.0:
                # 기존 noise를 테마 vol로 보정
                # 이미 noise가 적용된 daily_return에서 원래 noise 제거 후 재적용
                daily_return -= noise
                noise = random.gauss(0, vol * _theme_vol_mult)
                daily_return += noise

            # ★ 상하한가 캡 ±30%
            daily_return = max(-0.30, min(0.30, daily_return))

            # ★ PER/PBR 밸류에이션 압력 + 적자 압력
            market_cap = stock['price'] * stock['shares']
            per_limit  = _PER_LIMIT.get(sector, 30.0)
            assets_v   = max(1.0, meta.get('assets', 1.0))

            # PBR 계산
            pbr_v = market_cap / assets_v

            # PBR 저평가 반등 압력 (주가 ↔ 기업가치 연결)
            if pbr_v < 0.05:
                daily_return += 0.008   # 극도 저평가: 강한 반등
            elif pbr_v < 0.1:
                daily_return += 0.004   # 심각 저평가
            elif pbr_v < 0.3:
                daily_return += 0.0015  # 저평가
            elif pbr_v < 0.5:
                daily_return += 0.0005  # 약한 저평가
            # PBR 고평가 하락 압력
            elif pbr_v > 10.0:
                daily_return -= 0.003   # 극도 고평가
            elif pbr_v > 5.0:
                daily_return -= 0.001   # 고평가

            if annual_ni > 0:
                per = market_cap / max(1.0, annual_ni)
                # ★ 섹터별 PER 허용 범위 차등
                # tolerance 완화: 압력 시작점을 현실적으로 낮춤
                per_tolerance = {
                    "Growth":    1.2,   # 30×1.2=36배부터 압력
                    "Cyclical":     1.1,   # 25×1.1=27.5배부터 압력
                    "Value":     2.0,   # 13×2.0=26배부터 압력 (에너지/소재 마이너스 방지)
                    "Defensive": 1.3,   # 18×1.3=23.4배부터 압력
                }.get(sector, 1.2)

                eff_limit = per_limit * per_tolerance  # 실효 PER 한도

                # ★ PER 패널티 — alpha /252 수정 후 균형 재설계
                # Growth 균형점: ~50배 / Value 균형점: ~22배
                if per > eff_limit * 2.5:
                    val_penalty = -min(0.008, (per - eff_limit) / eff_limit * 0.003)
                elif per > eff_limit * 1.5:
                    val_penalty = -min(0.004, (per - eff_limit) / eff_limit * 0.002)
                elif per > eff_limit:
                    val_penalty = -min(0.002, (per - eff_limit) / eff_limit * 0.001)
                else:
                    val_penalty = 0.0
                daily_return += val_penalty

                if per < per_limit * 0.5:
                    daily_return += 0.0004   # 저평가 반등
            elif annual_ni < 0:
                hist_ni_count = len([x for yv in hist.values() for x in yv.values()])
                if hist_ni_count >= 4:
                    loss_cnt     = meta.get('continuous_loss_count', 0)
                    loss_ratio   = abs(annual_ni) / max(1.0, assets_v)
                    base_penalty = -min(0.003, loss_ratio * 0.3)
                    loss_mult    = min(2.0, 1.0 + loss_cnt * 0.1)
                    daily_return += base_penalty * loss_mult

            # ★ 주가 기반 HP 차감
            # 주가 -90% 이상: 흑자여도 시장 신뢰 완전 상실 → 소폭 HP 차감
            initial_price = meta.get('initial_price', 0)
            if initial_price <= 10:
                p52h = meta.get('price_52w_high', 0)
                if p52h > 10:
                    initial_price = p52h
                else:
                    shares_v = max(1, stock.get('shares', 1))
                    initial_price = meta.get('assets', old_price * shares_v) / shares_v
                initial_price = max(old_price, initial_price)

            # ★ 52주 신고가도 함께 참조해서 더 합리적인 기준 선택
            # 분할 후 initial_price가 과거 고가 기준으로 남아있을 수 있음
            p52h = meta.get('price_52w_high', 0)
            if p52h > 10 and initial_price > p52h * 5:
                # initial_price가 52주 고가의 5배 이상이면 분할 전 가격으로 의심
                # → 52주 신고가 기준으로 대체
                initial_price = p52h

            if initial_price > 10:
                price_drop_v = 1.0 - (old_price / max(1.0, initial_price))
                if   price_drop_v > 0.95: hp_price_drain = 0.3
                elif price_drop_v > 0.90: hp_price_drain = 0.1
                else:                     hp_price_drain = 0.0

                if hp_price_drain > 0:
                    meta['hp'] = max(0.0, meta.get('hp', 50.0) - hp_price_drain)

            # ★ 외국인/기관 이탈 → 주가 하락 압력
            foreign_share = meta.get('foreign_share', 0.0)
            inst_share    = meta.get('inst_share', 0.0)
            inst_total    = foreign_share + inst_share
            # 기관/외국인 합산 지분이 낮을수록 하락 압력
            if inst_total < 0.02 and tier != "대형주":
                daily_return -= 0.001   # 기관 이탈 압력

            # ★ 버블 시 소형주 추가 하락
            if bubble_index > 200 and "소형" in tier:
                daily_return -= min(0.002, (bubble_index - 200) / 100 * 0.002)

            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
            # ★ 신규 adj 레이어 (기존 로직에 더하기만)
            # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

            # [1] 컨센서스 선행매매 adj
            # 실적 발표 D-30 ~ D-7: 기관이 컨센서스 방향으로 서서히 포지션
            daily_return += self._calc_consensus_adj(name, cur_date, tier, sector)

            # [2] 테마 군집 adj
            # 같은 산업 내 평균 등락에 일부 동조
            daily_return += self._calc_cluster_adj(stock, sector, _ind_avg_rate)

            # [3] 유동성 함정 adj
            # 소형주: 거래량 희박 → 변동성 증폭
            daily_return += self._calc_liquidity_adj(stock, tier)

            # [4] 대주주 행동 adj
            # 대주주 매수/매도 공시 → 주가 압력
            daily_return += self._calc_major_holder_adj(name, meta, tier)

            # [5] 신용잔고 반대매매 adj
            # 소형 테마주 하락 시 신용잔고 → 반대매매 → 추가 하락
            daily_return += self._calc_margin_call_adj(name, stock, tier, sector)

            # ★ 계절성 보정
            daily_return += self._get_seasonal_adj(cur_date, sector, tier)

            # ★ 52주 신고가 모멘텀
            if old_price > meta.get('price_52w_high', old_price) * 1.001:
                daily_return += 0.0003   # 신고가 돌파 모멘텀
            elif old_price < meta.get('price_52w_low', old_price) * 0.999:
                daily_return -= 0.0003   # 신저가 하향 압력

            # ★ 전 종목 ±30% (현실 상한가/하한가)
            # 대형주가 덜 움직이는 건 vol(수급 연동)이 낮아서 자연스럽게 결정됨
            cap = 0.30

            # ★ 서킷브레이커: GRI 하루 -5% 이상이면 당일 변동폭 절반
            if _circuit_breaker:
                cap = 0.15

            # ★ 확장기에는 하루 하락 하한선 적용 (무조건 하락 방지)
            if cycle == "확장" and not is_depression:
                daily_return = max(-0.05, daily_return)
            daily_return = max(-cap, min(cap, daily_return))

            new_price = old_price * (1.0 + daily_return)
            new_price = max(10.0, new_price)
            if not math.isfinite(new_price): new_price = old_price

            # ★ assets 갱신: 주가 변화 소폭 반영 + 주가/초기주가 괴리 보정
            init_assets   = meta.get('initial_assets', meta['assets'])
            initial_price = meta.get('initial_price', old_price)
            cap_change    = new_price / max(1.0, old_price) - 1.0

            # 기본 assets 갱신 (주가 변화 소폭 반영)
            new_assets = meta['assets'] * (1 + cap_change * 0.005)

            # 주가가 초기 대비 많이 떨어지면 assets도 서서히 감소
            # 시장이 기업가치를 낮게 평가하는 것을 반영
            if initial_price > 0:
                price_ratio = new_price / max(1.0, initial_price)
                if price_ratio < 0.1:    # 초기 주가의 10% 미만
                    # assets를 시총 기준으로 보정 (PBR 1.0 기준)
                    market_implied = new_price * stock['shares']
                    new_assets = min(new_assets, max(init_assets * 0.1, market_implied * 2.0))
                elif price_ratio < 0.3:  # 초기 주가의 30% 미만
                    market_implied = new_price * stock['shares']
                    new_assets = min(new_assets, max(init_assets * 0.1, market_implied * 3.0))

            meta['assets'] = max(
                init_assets * 0.1,       # 하한선: 초기값의 10%
                min(init_assets * 30, new_assets)   # 상한선: 초기값의 30배 (100배 → 30배)
            )

            # ★ efficiency 자연 회복 — 섹터×티어×페이즈별 상한 차등 적용
            if hp_ratio > 0.50 and annual_ni > 0:
                eff_now = meta.get('efficiency', 0.05)
                sector  = SECTOR_MAP.get(ind, 'Value')
                cur_phase = getattr(self.s, '_last_processed_phase', '1A')

                # 페이즈 진행에 따라 상한 상향 (현실: 기업 경쟁력 누적)
                _PHASE_EFF_MULT = {
                    "1A": 1.00, "1B": 1.20,
                    "2A": 1.45, "2B": 1.75,
                    "3A": 2.10, "3B": 2.50,
                    "4A": 3.00, "4B": 3.50,
                }
                phase_mult = _PHASE_EFF_MULT.get(cur_phase, 1.0)

                # 섹터 × 티어 기본 상한
                _EFF_CAP = {
                    ("Growth",    "대형주"): 0.25,
                    ("Growth",    "중형주"): 0.18,
                    ("Growth",    "소형주"): 0.12,
                    ("Value",     "대형주"): 0.14,
                    ("Value",     "중형주"): 0.10,
                    ("Value",     "소형주"): 0.07,
                    ("Defensive", "대형주"): 0.12,
                    ("Defensive", "중형주"): 0.09,
                    ("Defensive", "소형주"): 0.06,
                    ("Cyclical",  "대형주"): 0.16,
                    ("Cyclical",  "중형주"): 0.12,
                    ("Cyclical",  "소형주"): 0.08,
                }
                base_cap = _EFF_CAP.get((sector, tier), 0.12)

                # ★ 버그 방지: 페이즈 승수 적용하되 절대 상한 설정
                # Growth 대형주: 최대 0.25 * 3.5 = 0.875 → 너무 높음
                # 절대 상한: Growth 0.60, Cyclical 0.45, Value 0.35, Defensive 0.25
                _ABS_CAP = {
                    "Growth": 0.60, "Cyclical": 0.45,
                    "Value": 0.35,  "Defensive": 0.25,
                }.get(sector, 0.35)

                tier_eff_cap = min(_ABS_CAP, base_cap * phase_mult)

                if eff_now < tier_eff_cap:
                    meta['efficiency'] = min(tier_eff_cap, eff_now * 1.0003)

            # ★ 부채 갱신 (금리 연동 이자비용)
            self._update_debt(stock, interest_rate)

            rate_raw            = (new_price / old_price - 1) * 100
            stock['price']      = int(new_price)
            stock['rate']       = round(rate_raw, 2) if math.isfinite(rate_raw) else 0.0
            stock['market_cap'] = stock['price'] * stock['shares']
            total_market_cap   += stock['market_cap']
            # ★ PER용 net_income 집계 (별도 루프 제거)
            _hist_ni = self.s.earnings_history.get(name, {})
            _ni = self._calc_annual_net_income(name, _hist_ni)
            if _ni > 0:
                total_net_income += _ni

            # earnings_shock → momentum 반영
            earnings_shock = meta.pop('_earnings_shock', 0.0)
            if earnings_shock != 0.0:
                cur_mom = meta.get('momentum', 0.0)
                meta['momentum'] = max(-0.3, min(0.3, cur_mom + earnings_shock * 0.2))

            # ★ 52주 신고가/신저가 갱신
            self._update_52w_range(stock)

            # ★ 신용등급 갱신
            self._update_credit_grade(meta)

            self._handle_survival_strategy(stock)
            self._update_shareholder_structure(stock, cycle, bubble_index, rate_delta)

        # ── GRI 갱신 (시장 집중도 제한 포함) ───────
        # total_w_cap은 메인 루프에서 이미 누적된 값 사용 (별도 sum comprehension 제거)
        import math as _math
        tier_weights = {"대형주": 3.0, "중형주": 1.5, "소형주": 0.2}
        w_sum = 0.0; wr_sum = 0.0
        total_w_cap = sum(
            tier_weights.get(s['meta'].get('tier', '소형주'), 0.5) * s['market_cap']
            for s in self.s.stocks
        )

        for stock in self.s.stocks:
            r = stock.get('rate', 0.0)
            if not _math.isfinite(r): continue

            # ★ 신규 상장 30일 미만 종목 GRI 계산 제외
            try:
                ld = stock['meta'].get('listed_date_dt')
                if not ld:
                    ld = __import__('datetime').datetime.strptime(
                        stock['meta']['listed_date'], '%Y-%m-%d')
                if (cur_date - ld).days < 30:
                    # 초기 종목(1999-12-31 상장)은 예외 — 이미 오래된 기업이므로 제한 불필요
                    if ld.strftime('%Y-%m-%d') != '1999-12-31':
                        continue
            except Exception:
                pass

            w = tier_weights.get(stock['meta'].get('tier', '소형주'), 0.2)

            # ★ 단일 종목 GRI 기여 상한 10%
            if total_w_cap > 0:
                contribution = w * stock['market_cap'] / total_w_cap
                if contribution > 0.10:
                    w *= (0.10 / contribution)

            w_sum += w; wr_sum += w * r

        weighted_avg_rate = (wr_sum / w_sum / 100.0) if w_sum > 0 else 0.0

        # ★ 하락 상한을 상승 상한보다 작게 설정 (비대칭 보정 - 하락 방지)
        up_cb   = 0.03 if is_depression else 0.02
        down_cb = 0.05 if is_depression else (0.015 if cycle in ("수축", "저점") else 0.012)
        weighted_avg_rate = max(-down_cb, min(up_cb, weighted_avg_rate))

        years_elapsed = max(0, cur_date.year - 2000)
        # ★ lv_target: GDP 누적치 연동 — 경기침체 누적 시 낮아지고 호황 지속 시 높아짐
        # GDP 기준(2000년 600조) 대비 성장 비율을 0.7승으로 반영 (완전 연동 아님)
        gdp_now       = getattr(self.s, 'gdp', 600_000_000_000_000.0)
        gdp_base      = 600_000_000_000_000.0
        gdp_ratio     = gdp_now / max(1.0, gdp_base)
        gdp_factor    = (gdp_ratio ** 0.7)   # 0.7승: GDP 변화를 70% 반영

        lv_base = {
            # ★ 연 5.5% 복리 (현실 코스피 장기 평균 수준)
            # 26년: 1000 * 1.055^26 = 4,000 수준 목표
            1: 1000 * (1.055 ** years_elapsed),
            2: 1000 * (1.055 ** 15) * (1.060 ** max(0, years_elapsed - 15)),
            3: 1000 * (1.055 ** 15) * (1.060 ** 20) * (1.045 ** max(0, years_elapsed - 35)),
            4: 1000 * (1.055 ** 15) * (1.060 ** 20) * (1.045 ** 25) * (1.035 ** max(0, years_elapsed - 60)),
        }.get(lv, 1000.0)
        lv_target = lv_base * gdp_factor

        anchor = self.s.gri / max(1.0, lv_target)
        # ★ anchor 브레이크 강화 — 목표 대비 과도한 상승 억제
        if   anchor > 8.0: weighted_avg_rate -= 0.0060
        elif anchor > 5.0: weighted_avg_rate -= 0.0035
        elif anchor > 3.0: weighted_avg_rate -= 0.0015
        elif anchor > 2.0: weighted_avg_rate -= 0.0008  # 2배 초과 시 브레이크
        elif anchor > 1.5: weighted_avg_rate -= 0.0004  # 1.5배 초과 시 약한 브레이크
        elif anchor > 1.2: weighted_avg_rate -= 0.0001
        elif anchor < 0.3: weighted_avg_rate += 0.0025
        elif anchor < 0.5: weighted_avg_rate += 0.0015
        elif anchor < 0.7: weighted_avg_rate += 0.0010
        elif anchor < 0.85: weighted_avg_rate += 0.0005
        elif anchor < 1.0:  weighted_avg_rate += 0.0002

        # ★ 초반 3년 추가 보정
        if cur_date.year <= 2003 and anchor < 1.0:
            weighted_avg_rate += 0.0010  # 0.0008 → 0.0010 강화

        # ★ prev_gri는 GRI 갱신 전에 저장 (등락률 계산용)
        self.s.prev_gri = self.s.gri

        raw_gri = self.s.gri * (1.0 + weighted_avg_rate)
        if not _math.isfinite(raw_gri): raw_gri = self.s.gri
        self.s.gri = max(100.0, raw_gri)

        # ★ GDP 연간 성장 (매년 1월 1일) — 경기 사이클 연동
        if cur_date.month == 1 and cur_date.day == 1:
            # 기술 레벨별 기본 성장률 상향 (현실 한국 GDP 연 4~5% 수준)
            lv_base_growth = {1: 0.065, 2: 0.075, 3: 0.050, 4: 0.030}.get(lv, 0.065)

            # 경기 사이클별 보정 — 현실화 (저점 -4% → -1.5%로 완화)
            cycle_gdp_mult = {
                "확장": random.uniform(0.05, 0.08),    # 기존 4~6% → 5~8%
                "정점": random.uniform(0.02, 0.04),    # 기존 2~3% → 2~4%
                "수축": random.uniform(-0.005, 0.005), # 기존 -1~-0.5% → 거의 횡보
                "저점": random.uniform(-0.015, -0.005),# 기존 -4~-2% → -0.5~-1.5%
            }.get(cycle, lv_base_growth)

            # 대공황: GDP 급격히 역성장
            if is_depression:
                cycle_gdp_mult = random.uniform(-0.10, -0.06)  # -6~-10%

            self.s.gdp = getattr(self.s, 'gdp', 600_000_000_000_000.0) * (1 + cycle_gdp_mult)
            self.s.gdp_growth_rate = cycle_gdp_mult   # 뉴스 표시용

            # GDP 성장률 뉴스 (교육 효과)
            if not self.s.silent_mode:
                gdp_pct = cycle_gdp_mult * 100
                if gdp_pct < 0:
                    self.s.daily_news.append(
                        f"📉 [GDP 발표] {cur_date.year}년 GDP 성장률 {gdp_pct:+.1f}% "
                        f"(GDP↓ → 버핏 지수↑ → 시장 고평가 압력)"
                    )
                else:
                    self.s.daily_news.append(
                        f"📈 [GDP 발표] {cur_date.year}년 GDP 성장률 {gdp_pct:+.1f}%"
                    )

        # ★ 버핏 지수 갱신
        gdp = getattr(self.s, 'gdp', 600_000_000_000_000.0)
        self.s.buffett_index = (total_market_cap / max(1.0, gdp)) * 100

        # ★ 버블 지수: 버핏 지수(시총/GDP) 기반으로 재계산
        # 현실: 버블은 실물 경제 대비 금융 자산의 괴리
        # 버핏 60% 미만 → 버블 0~30 / 100~130% → 버블 80~150 / 160%+ → 버블 250~300
        buffett = getattr(self.s, 'buffett_index', 0.0)

        if buffett < 80:
            target_bubble = buffett * 0.4                            # 0~32
        elif buffett < 130:
            target_bubble = 32 + (buffett - 80) * 0.96              # 32~80
        elif buffett < 180:
            target_bubble = 80 + (buffett - 130) * 1.40             # 80~150
        elif buffett < 260:         # 230 → 260 (더 여유 있게)
            target_bubble = 150 + (buffett - 180) * 1.25            # 150~250
        else:
            target_bubble = min(300.0, 250 + (buffett - 260) * 1.43)  # 260%+부터 극단

        # T3 유지 / 대공황 극복 시 버블 억제
        if "T3 유지" in self.s.current_scenario:
            target_bubble *= 0.6
        elif "극복" in self.s.current_scenario:
            target_bubble *= 0.5

        # 실제 버블 지수를 목표값으로 서서히 수렴 (급변 방지)
        bi             = getattr(self.s, 'bubble_index', 0.0)
        bubble_diff    = target_bubble - bi

        # ★ 자연감소: bi가 높을수록 중력처럼 끌어내림 (mean-reversion)
        # target이 300이어도 bi가 높으면 자연감소가 상쇄 → 평형점 형성
        if bi >= 270:
            natural_decay = -1.5
        elif bi >= 240:
            natural_decay = -1.0
        elif bi >= 200:
            natural_decay = -0.5
        elif bi >= 150:
            natural_decay = -0.15
        else:
            natural_decay = 0.0

        bubble_delta_final = max(-4.0, min(2.0, bubble_diff * 0.05 + natural_decay))
        self.s.bubble_index = max(0.0, min(300.0, bi + bubble_delta_final))

        # 버블 경고 뉴스 (교육 효과)
        bi_now = self.s.bubble_index
        if not self.s.silent_mode:
            if 149 < bi_now <= 151:
                self.s.daily_news.append(
                    f"⚠️ [버블 주의] 버블 지수 {bi_now:.0f} — "
                    f"시총/GDP 비율 과열 (역사적으로 1~2년 내 조정 선행 신호)"
                )
            elif 249 < bi_now <= 251:
                self.s.daily_news.append(
                    f"🚨 [버블 경고] 버블 지수 {bi_now:.0f} — "
                    f"닷컴버블(2000) 수준 도달, 대규모 조정 위험"
                )

        # ★ 버블 300 도달 시 대공황 예약 (D-90, 아직 예약 없을 때만)
        if self.s.bubble_index >= 300 and not self.s.pending_events.get("crash"):
            crash_date = self.s.current_date + timedelta(days=90)
            self.s.pending_events["crash"] = {
                "date":     crash_date,
                "notified": False,
            }
            if not self.s.silent_mode:
                self.s.daily_news.append(
                    f"🚨 [버블 임계점] 시장 버블 지수가 최고조에 달했습니다. "
                    f"90일 후({crash_date.strftime('%Y-%m-%d')}) 대규모 조정이 예고됩니다."
                )

        # ★ 대공황 예약 D-Day 처리
        crash_info = self.s.pending_events.get("crash")
        if crash_info:
            c_date = crash_info.get("date")
            if isinstance(c_date, str):
                c_date = datetime.strptime(c_date, "%Y-%m-%d")
            if c_date and self.s.current_date.date() >= c_date.date():
                self.s.current_scenario = "💀 대공황 (시스템 붕괴)"
                self.s.scenario_timer   = 252 * 10
                self.s.pending_events["crash"] = None
                self.s.daily_news.append("💀 [대공황 발동] 시장이 붕괴되기 시작합니다.")
            elif not crash_info.get("notified"):
                days_left = (c_date.date() - self.s.current_date.date()).days
                if days_left <= 30:
                    crash_info["notified"] = True
                    self.s.daily_news.append(
                        f"⚠️ [붕괴 D-{days_left}] 시장 붕괴가 임박했습니다. 포트폴리오를 점검하세요."
                    )

        return total_market_cap

    # ─────────────────────────────────────────────
    # ★ 연간 순이익 계산 (최근 4분기 합산)
    # ─────────────────────────────────────────────
    def _calc_annual_net_income(self, name: str, hist: dict) -> float:
        if not hist:
            return 0.0
        all_quarters = []
        for year_data in hist.values():
            for q_data in year_data.values():
                ni = q_data.get('net_income', 0)
                all_quarters.append(ni)
        # 최근 4분기
        recent = all_quarters[-4:] if len(all_quarters) >= 4 else all_quarters
        if not recent:
            return 0.0
        total = sum(recent)
        # 데이터 부족 시 연환산
        if len(recent) < 4:
            total = total * (4 / len(recent))
        return total

    # ─────────────────────────────────────────────
    # ★ 부채 갱신 (금리 연동)
    # ─────────────────────────────────────────────
    def _update_debt(self, stock: dict, interest_rate: float):
        meta  = stock['meta']
        debt  = meta.get('debt', 0.0)
        if debt <= 0:
            return

        credit = meta.get('credit_grade', 'BB')
        cost_mult = _CREDIT_COST.get(credit, 1.3)

        # 연간 이자비용 (일별)
        annual_interest = debt * (interest_rate / 100) * cost_mult
        daily_interest  = annual_interest / 252

        # 자산에서 이자 차감
        meta['assets'] = max(10000.0, meta['assets'] - daily_interest)

        # ★ 부채비율 독립 HP 차감 (금리 무관 — 부채 자체의 구조적 위험)
        # 부채비율 200% 이상이면 매일 소량 HP 차감
        # 현실: 이자보상배율 악화, 자금조달 비용 상승, 신용경색 리스크
        debt_ratio = meta.get('debt_ratio', 0.5)
        tier       = meta.get('tier', '소형주')

        if debt_ratio >= 5.0:       # 500%+: 자본잠식 수준
            debt_hp_dmg = 0.08
        elif debt_ratio >= 3.0:     # 300%+: 심각한 과부채
            debt_hp_dmg = 0.04
        elif debt_ratio >= 2.0:     # 200%+: 위험 수준
            debt_hp_dmg = 0.015
        else:
            debt_hp_dmg = 0.0

        # 대형주는 자금조달 능력이 있으므로 50% 완화
        if "대형" in tier:
            debt_hp_dmg *= 0.5

        if debt_hp_dmg > 0:
            meta['hp'] = max(0.0, meta.get('hp', 50.0) - debt_hp_dmg)

        # ★ 금리 + 고부채 → HP 추가 차감 (기존 7% → 3%로 완화)
        # Lv1 정상 금리(3~4%)에서도 고부채 기업 타격 가능하도록
        if interest_rate >= 3.0 and debt_ratio >= 1.5:
            extra_hp_dmg = (interest_rate - 3.0) * debt_ratio * 0.002
            meta['hp'] = max(0.0, meta.get('hp', 50.0) - extra_hp_dmg)

    # ─────────────────────────────────────────────
    # ★ 신용등급 갱신
    # ─────────────────────────────────────────────
    def _update_credit_grade(self, meta: dict):
        hp      = meta.get('hp', 50.0)
        soft_cap = meta.get('hp_soft_cap', 60.0)
        hp_pct  = hp / max(1.0, soft_cap)
        debt_ratio = meta.get('debt_ratio', 0.5)

        # HP + 부채비율 복합 판정
        if hp_pct >= 0.80 and debt_ratio < 1.0:
            meta['credit_grade'] = "AA"
        elif hp_pct >= 0.50 and debt_ratio < 2.0:
            meta['credit_grade'] = "BB"
        else:
            meta['credit_grade'] = "CCC"

    # ─────────────────────────────────────────────
    # ★ 52주 신고가/신저가 갱신
    # ─────────────────────────────────────────────
    def _update_52w_range(self, stock: dict):
        meta  = stock['meta']
        price = float(stock['price'])

        meta['price_52w_days'] = meta.get('price_52w_days', 0) + 1

        # 252일(1년)마다 리셋
        if meta['price_52w_days'] >= 252:
            meta['price_52w_high'] = price
            meta['price_52w_low']  = price
            meta['price_52w_days'] = 0
            return

        if price > meta.get('price_52w_high', price):
            meta['price_52w_high'] = price
        if price < meta.get('price_52w_low', price):
            meta['price_52w_low'] = price

    # ─────────────────────────────────────────────
    # ★ 계절성 보정
    # ─────────────────────────────────────────────
    def _get_seasonal_adj(self, cur_date: datetime, sector: str, tier: str) -> float:
        month = cur_date.month
        adj   = 0.0

        # 1월 효과: 소형주 + 테마주 보너스 (신년 테마 기대감)
        if month == 1 and "소형" in tier:
            adj += 0.0008
        if month == 1 and sector == "Cyclical":
            adj += 0.0010

        # 4월/10월: 실적시즌 → Growth/Theme 변동성 확대
        if month in [4, 10] and sector == "Growth":
            adj += 0.0005
        if month in [4, 10] and sector == "Cyclical":
            adj += 0.0008   # 테마주 실적시즌 더 민감

        # 12월: 방어주/배당주 상승 + 테마주 연말 정리
        if month == 12 and sector == "Defensive":
            adj += 0.0006
        if month == 12 and sector == "Cyclical":
            adj -= 0.0005   # 연말 테마주 차익실현

        # 여름(7~8월): 거래량 감소 → 테마주 변동성 축소
        if month in [7, 8] and sector == "Cyclical":
            adj -= 0.0003

        return adj / 252

    # ─────────────────────────────────────────────
    # ★ 산업별 경쟁도 갱신
    # ─────────────────────────────────────────────
    def _update_industry_competition(self):
        comp = {}
        for stock in self.s.stocks:
            ind = stock['meta'].get('ind', '')
            comp[ind] = comp.get(ind, 0) + 1
        self.s.industry_competition = comp

    # ─────────────────────────────────────────────
    # 지배구조 업데이트 (기존 유지)
    # ─────────────────────────────────────────────
    def _update_shareholder_structure(self, stock: dict, cycle: str,
                                      bubble_index: float, rate_delta: float):
        meta       = stock['meta']
        tier       = meta.get('tier', '소형주')
        rate       = stock.get('rate', 0.0) / 100.0
        sector     = SECTOR_MAP.get(meta.get('ind', ''), 'Value')
        macro      = self.s.macro
        free_float = max(0.05, 1.0 - meta.get('treasury_share', 0.0) - meta.get('owner_share', 0.0))

        LIMITS = {
            "대형주": {"foreign": (0.05, 0.55), "inst": (0.05, 0.30), "retail": (0.05, 0.50)},
            "중형주": {"foreign": (0.01, 0.25), "inst": (0.03, 0.25), "retail": (0.15, 0.65)},
            "소형주": {"foreign": (0.00, 0.08), "inst": (0.01, 0.15), "retail": (0.30, 0.85)},
        }
        lim = LIMITS.get(tier, LIMITS["소형주"])

        foreign_delta = 0.0
        # ★ 확장기 대형주도 매일 방향이 바뀌도록 — 편향은 작게, 노이즈는 크게
        cycle_base = {
            "확장": +0.0002, "정점": 0.0,
            "수축": -0.0004, "저점": -0.0002,
        }.get(cycle, 0.0)
        if "대형" in tier:
            foreign_delta += cycle_base + random.gauss(0, 0.0012)
        elif "중형" in tier:
            foreign_delta += cycle_base * 0.5 + random.gauss(0, 0.0008)
        else:
            foreign_delta += random.gauss(0, 0.0003)  # 소형주: 외국인 거의 없음

        foreign_delta -= rate_delta * 0.0008
        if macro.get('exchange_rate', 1100) > 1400: foreign_delta -= 0.0008
        elif macro.get('exchange_rate', 1100) < 1050: foreign_delta += 0.0005
        if meta.get('_earnings_just_released'):
            lc = meta.get('continuous_loss_count', 0)
            foreign_delta += -0.008 if lc >= 2 else (-0.003 if lc == 1 else +0.005)
        if "소형" in tier: foreign_delta *= 0.15
        if "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario:
            foreign_delta -= 0.0015

        # ★ 외국인 수급 지수 반영
        flow_idx = getattr(self.s, 'foreign_flow_index', 0.0)
        foreign_delta += flow_idx * 0.00005

        inst_delta = 0.0
        # ★ 기관: 대형주에서 외국인 반대 성향 + 노이즈
        if "대형" in tier:
            # 외국인이 강하게 매수하면 기관은 차익실현 경향
            inst_delta += -foreign_delta * 0.4 + random.gauss(0, 0.0010)
        elif "중형" in tier:
            inst_delta += random.gauss(0, 0.0008)
        else:
            inst_delta += random.gauss(0, 0.0005)

        if meta.get('_earnings_just_released'):
            lc = meta.get('continuous_loss_count', 0)
            inst_delta += -0.012 if lc >= 2 else (-0.005 if lc == 1 else +0.007)
        hp_r = meta.get('hp', 50.0) / max(1.0, meta.get('hp_soft_cap', 60.0))
        if hp_r < 0.15: inst_delta -= 0.004
        elif hp_r < 0.30: inst_delta -= 0.0015
        if bubble_index > 200: inst_delta -= 0.001
        elif bubble_index > 150: inst_delta -= 0.0005
        cm = self.s.current_date.month; cd = self.s.current_date.day
        if cm in [3, 6, 9, 12] and cd >= 25:
            inst_delta += -0.004 if meta.get('continuous_loss_count', 0) > 0 else +0.002
        tech_upgrade_year = getattr(self.s, '_tech_upgrade_years', {1: 1999}).get(
            self.s.max_tech_reached) or 1999
        if 0 <= self.s.current_date.year - tech_upgrade_year <= 2 and sector == "Growth":
            inst_delta += 0.003

        retail_delta = 0.0
        # ★ 개인: 역매매 성향 + 노이즈 (대형 우량주는 장기보유로 변동 작음)
        if "대형" in tier:
            retail_delta += -(foreign_delta + inst_delta) * 0.3 + random.gauss(0, 0.0008)
        else:
            retail_delta += random.gauss(0, 0.0015)

        if   rate < -0.05: retail_delta += 0.010   # 급락 시 저점 매수
        elif rate < -0.02: retail_delta += 0.004
        elif rate >  0.05: retail_delta -= 0.005   # 급등 시 차익실현
        elif rate >  0.02: retail_delta -= 0.002
        sentiment = getattr(self.s, 'sentiment', 50.0)
        if sentiment > 75 and bubble_index > 150: retail_delta += 0.002
        elif sentiment < 25: retail_delta -= 0.001

        char = meta.get('char', 'Normal')
        if char == 'BANKRUPT':
            foreign_delta -= meta.get('foreign_share', 0) * 0.90
            inst_delta    -= meta.get('inst_share', 0) * 0.95
        elif char == 'DANGER':
            foreign_delta -= 0.003; inst_delta -= 0.004
        elif char == 'WARNING':
            inst_delta -= 0.001

        nf = max(lim['foreign'][0], min(lim['foreign'][1], meta.get('foreign_share', 0.0) + foreign_delta))
        ni = max(lim['inst'][0],    min(lim['inst'][1],    meta.get('inst_share', 0.0)    + inst_delta))
        nr = max(lim['retail'][0],  min(lim['retail'][1],  meta.get('retail_share', 0.0)  + retail_delta))
        total = nf + ni + nr
        if total > 0:
            scale = free_float / total
            nf = max(lim['foreign'][0], min(lim['foreign'][1], nf * scale))
            ni = max(lim['inst'][0],    min(lim['inst'][1],    ni * scale))
            nr = max(lim['retail'][0],  min(lim['retail'][1],  free_float - nf - ni))
        meta['foreign_share'] = round(max(0.0, nf), 4)
        meta['inst_share']    = round(max(0.0, ni), 4)
        meta['retail_share']  = round(max(0.0, nr), 4)
        meta.pop('_earnings_just_released', None)

        # ★ 수급 트렌드 연속성 업데이트
        name = meta['c_name']
        trends = self.s.investor_trends.setdefault(name, {
            "foreign": {"dir": 0, "days": 0},
            "inst":    {"dir": 0, "days": 0},
            "retail":  {"dir": 0, "days": 0},
        })

        for key, delta in [("foreign", foreign_delta), ("inst", inst_delta), ("retail", retail_delta)]:
            t = trends[key]
            new_dir = 1 if delta > 0.0005 else (-1 if delta < -0.0005 else 0)
            if new_dir == t["dir"] and new_dir != 0:
                t["days"] = min(t["days"] + 1, 30)  # 최대 30일 트렌드
            else:
                t["dir"]  = new_dir
                t["days"] = 1 if new_dir != 0 else 0

        # ★ 일별 거래량 계산 및 저장 (호가창용)
        # 유통주식수 = 총발행 - 자사주 - 대주주
        shares_total = stock.get('shares', 1)
        fixed_ratio  = meta.get('treasury_share', 0.0) + meta.get('owner_share', 0.0)
        float_shares = max(1, int(shares_total * (1.0 - fixed_ratio)))

        # ★ 거래량은 delta와 완전 분리 — 회전율로만 결정
        # 현실: 유통주식 기준 회전율 ~1%/일
        # 총발행주식 기준으로 환산 (유통비율 약 50~60% 가정)
        # 대형주: 총발행의 0.5~1.2%/일 (유통기준 ~1%)
        # 중형주: 총발행의 0.8~2.0%/일
        # 소형주: 총발행의 1.5~4.0%/일
        base_turnover = {
            "대형주": random.uniform(0.005, 0.012),
            "중형주": random.uniform(0.008, 0.020),
            "소형주": random.uniform(0.015, 0.040),
        }.get(tier, 0.010)

        # 등락률 클수록 거래량 증가
        abs_rate_pct = abs(stock.get('rate', 0.0))
        vol_mult     = 1.0 + abs_rate_pct * 0.3
        if meta.get('_earnings_just_released'):
            vol_mult *= 3.0
        turnover  = min(0.50, base_turnover * vol_mult)
        total_vol = max(1, int(float_shares * turnover))

        # 투자자별 거래 규모 — 비중 비례
        f_share   = meta.get('foreign_share', 0.0)
        i_share   = meta.get('inst_share', 0.0)
        r_share   = meta.get('retail_share', 0.1)
        share_sum = max(0.01, f_share + i_share + r_share)

        f_vol = int(total_vol * (f_share / share_sum))
        i_vol = int(total_vol * (i_share / share_sum))
        r_vol = total_vol - f_vol - i_vol

        # ★ 방향만 delta 부호에서 결정 — 거래량 크기와 무관
        def delta_to_direction(delta):
            """delta 부호 → 방향 (±1), 중립이면 작은 랜덤"""
            if delta > 0.0001:  return 1
            if delta < -0.0001: return -1
            return 1 if random.random() > 0.5 else -1

        f_dir = delta_to_direction(foreign_delta)
        i_dir = delta_to_direction(inst_delta)
        r_dir = delta_to_direction(retail_delta)

        # 순매수/순매도량 = 거래량의 20~60% (나머지는 양방향 거래)
        f_net = int(f_vol * random.uniform(0.20, 0.60) * f_dir)
        i_net = int(i_vol * random.uniform(0.20, 0.60) * i_dir)
        r_net = int(r_vol * random.uniform(0.20, 0.60) * r_dir)

        # ★ 제로섬 제약 — 합계가 0되도록 delta 가장 약한 주체가 잔여 흡수
        residual = f_net + i_net + r_net
        if residual != 0:
            abs_f = abs(foreign_delta)
            abs_i = abs(inst_delta)
            abs_r = abs(retail_delta)
            if abs_r <= abs_f and abs_r <= abs_i:
                r_net -= residual
            elif abs_i <= abs_f and abs_i <= abs_r:
                i_net -= residual
            else:
                f_net -= residual

        date_str = self.s.current_date.strftime('%Y-%m-%d')
        vol_list = self.s.daily_volume.setdefault(name, [])
        vol_list.append({
            "date":    date_str,
            "foreign": f_net,
            "inst":    i_net,
            "retail":  r_net,
        })
        if len(vol_list) > 252:
            self.s.daily_volume[name] = vol_list[-252:]

    def _apply_convergence(self, stock, is_protected):
        meta = stock['meta']
        name = meta['c_name']

        if name in self.s.pending_events.get("delist", {}):
            p_date = self.s.pending_events["delist"][name]
            if isinstance(p_date, dict): p_date = p_date.get('date')
            if isinstance(p_date, str):  p_date = datetime.strptime(p_date, "%Y-%m-%d")
            days_left = (p_date.date() - self.s.current_date.date()).days
            if 0 < days_left <= 7:
                stock['price'] = int(stock['price'] * 0.983)
                meta['momentum'] -= 0.04

        if name in self.s.pending_events.get("warning", {}):
            w_info = self.s.pending_events["warning"][name]
            w_date = w_info.get('date')
            w_type = w_info.get('type')
            if isinstance(w_date, str): w_date = datetime.strptime(w_date, "%Y-%m-%d")
            if w_date:
                days_left = (w_date.date() - self.s.current_date.date()).days
                if 0 < days_left <= 7:
                    if w_type == 'IN':
                        stock['price'] = int(stock['price'] * 0.988)
                        meta['momentum'] -= 0.02
                    elif w_type == 'OUT':
                        stock['price'] = int(stock['price'] * 1.011)
                        meta['momentum'] += 0.015

        tier_exam = self.s.pending_events.get("tier_exam")
        if tier_exam:
            expires = tier_exam.get("expires", "")
            if isinstance(expires, str) and expires:
                exp_date = datetime.strptime(expires, "%Y-%m-%d")
                days_since = (self.s.current_date.date() - exp_date.date()).days
                if 0 <= days_since <= 7:
                    if name in tier_exam.get("up", []):
                        stock['price'] = int(stock['price'] * 1.007)
                        meta['momentum'] += 0.01
                    elif name in tier_exam.get("down", []):
                        stock['price'] = int(stock['price'] * 0.993)
                        meta['momentum'] -= 0.01

        if meta.get('pending_merge'):
            merge_info = meta['pending_merge']
            m_date = merge_info.get('date')
            if isinstance(m_date, str): m_date = datetime.strptime(m_date, "%Y-%m-%d")
            if m_date:
                days_left = (m_date.date() - self.s.current_date.date()).days
                if 0 < days_left <= 7:
                    stock['price'] = int(stock['price'] * 0.989)
                    meta['momentum'] -= 0.015
                elif days_left <= 0:
                    meta.pop('pending_merge', None)

        if meta.get('pending_industry_change'):
            ind_info = meta['pending_industry_change']
            i_date = ind_info.get('date')
            if isinstance(i_date, str): i_date = datetime.strptime(i_date, "%Y-%m-%d")
            if i_date:
                days_left = (i_date.date() - self.s.current_date.date()).days
                if 0 < days_left <= 7:
                    stock['price'] = int(stock['price'] * 1.004)
                    meta['momentum'] += 0.005
                elif days_left <= 0:
                    meta.pop('pending_industry_change', None)

        if meta.get('pending_split'):
            sp_info = meta['pending_split']
            s_date = sp_info.get('date')
            if isinstance(s_date, str): s_date = datetime.strptime(s_date, "%Y-%m-%d")
            if s_date:
                days_left = (s_date.date() - self.s.current_date.date()).days
                if 0 < days_left <= 7:
                    stock['price'] = int(stock['price'] * 1.007)
                    meta['momentum'] += 0.01
                # ★ days_left <= 0 시 여기서 삭제하지 않음
                # → _handle_stock_split() 에서 실제 분할 처리

        if self.s.pending_events.get("tech_jump"):
            jump_info = self.s.pending_events["tech_jump"]
            p_date    = jump_info['date']
            if isinstance(p_date, str): p_date = datetime.strptime(p_date, "%Y-%m-%d")
            days_left = (p_date.date() - self.s.current_date.date()).days
            if 0 < days_left <= 30 and meta['ind'] in ["IT", "커뮤니케이션"]:
                convergence_factor = (30 - days_left) / 30
                meta['momentum'] += 0.008 * (1 + convergence_factor)

        if self.s.pending_events.get("v_rebound"):
            p_date = self.s.pending_events["v_rebound"]
            if isinstance(p_date, str): p_date = datetime.strptime(p_date, "%Y-%m-%d")
            days_left = (p_date.date() - self.s.current_date.date()).days
            if 0 < days_left <= 30:
                daily_recovery = 0.012 * (1 + (30 - days_left) / 15)
                meta['momentum'] += daily_recovery

    # ─────────────────────────────────────────────
    # 기술 발전에 따른 종목 업그레이드 (기존 유지)
    # ─────────────────────────────────────────────
    def update_company_technology(self):
        current_lv = self.s.max_tech_reached
        # ★ O(n×12) → O(n): 산업별 루프 제거, 단일 패스로 top_by_ind 구성
        top_by_ind = {}
        for s in self.s.stocks:
            ind = s['meta'].get('ind', '')
            if not ind:
                continue
            if ind not in top_by_ind or s['market_cap'] > top_by_ind[ind]['market_cap']:
                top_by_ind[ind] = {'market_cap': s['market_cap'], 'name': s['meta']['c_name']}
        top_by_ind = {ind: v['name'] for ind, v in top_by_ind.items()}

        for stock in self.s.stocks:
            meta     = stock['meta']
            ind      = meta['ind']
            tier     = meta['tier']
            is_group = meta['group_id'] is not None

            if self.s.current_date.month == 12 and self.s.current_date.day == 1:
                jump_premium = 1.15 if self.s.has_paid_news_access else 1.0
                meta['assets']  *= jump_premium
                stock['price']   = int(stock['price'] * jump_premium)

            max_subs = {"대형주": 3, "중형주": 2, "소형주": 1}.get(tier, 1)
            if is_group: max_subs = 3

            current_subs   = [x.strip() for x in meta['sub'].split(',')]
            available_tech = INDUSTRY_LEVELS.get(ind, {}).get(current_lv, [])

            if available_tech and current_subs[0] not in available_tech:
                pivot_chance = {"대형주": 0.85, "중형주": 0.50, "소형주": 0.25}.get(tier, 0.25)
                if is_group: pivot_chance = 0.90
                if random.random() < pivot_chance:
                    new_main = random.choice(available_tech)
                    if new_main not in current_subs:
                        old_sub = current_subs[0] if current_subs else ""
                        current_subs.insert(0, new_main)
                        current_subs = current_subs[:max_subs]
                        meta['sub'] = ", ".join(current_subs)
                        meta['risk_score'] = max(0, meta['risk_score'] - 5)
                        if not meta.get('pending_industry_change'):
                            ind_date = self.s.current_date + timedelta(days=7)
                            meta['pending_industry_change'] = {
                                'date':    ind_date.strftime('%Y-%m-%d'),
                                'old_sub': old_sub,
                                'new_sub': new_main,
                            }
                            if not self.s.silent_mode:
                                if self.s.has_paid_news_access:
                                    self.s.daily_news.append(
                                        f"💎 [업종변경 예고] {meta['c_name']} "
                                        f"{old_sub} → {new_main} 사업 전환 예정 (프리미엄 전용)"
                                    )
                                else:
                                    self.s.daily_news.append(
                                        f"🔄 [업종변경] {meta['c_name']} {old_sub} → {new_main} 전환 완료"
                                    )

            risk = meta.get('risk_score', 0)
            if (risk > 110 or meta.get('continuous_loss_count', 0) > 5) and len(current_subs) > 1:
                removed = current_subs.pop()
                meta['sub']     = ", ".join(current_subs)
                stock['price']  = int(stock['price'] * 0.94)
                meta['risk_score'] = max(0, meta['risk_score'] - 25.0)
                if not self.s.silent_mode:
                    self.s.daily_news.append(f"✂️ [구조조정] {meta['c_name']}이 경영난으로 {removed} 사업을 정리했습니다.")

            # ★ 도태 판단 — 페이즈 기반 (기존 숫자 LV 키 대체)
            from .constants import TECH_PHASE
            cur_phase = getattr(self.s, '_last_processed_phase', '1A')
            ind_data  = INDUSTRY_LEVELS.get(ind, {})

            # 현재 페이즈 + common에 없는 sub는 도태
            cur_phase_subs = set()
            for t_list in ind_data.get(cur_phase, {}).values():
                cur_phase_subs.update(t_list)
            for t_list in ind_data.get('common', {}).values():
                cur_phase_subs.update(t_list)

            # sub_list 기준으로 도태 판단 (없으면 sub 단일값)
            sub_list = meta.get('sub_list') or [meta.get('sub', '')]
            is_obsolete = all(s not in cur_phase_subs for s in sub_list if s)

            if is_obsolete and sub_list:
                if top_by_ind.get(ind) != meta['c_name']:
                    meta['momentum']   = max(-1.5, meta.get('momentum', 0.0) - 0.02)
                    meta['risk_score'] = min(60.0, meta.get('risk_score', 0.0) + 0.05)
                    if random.random() < 0.005:
                        self.s.daily_news.append(
                            f"🏚️ [산업도태] {meta['c_name']}이(가) 시대에 뒤처져 시장 점유율을 잃고 있습니다."
                        )

    # ─────────────────────────────────────────────
    # 경고 시스템 (기존 유지)
    # ─────────────────────────────────────────────
    def check_warning_system(self):
        warning_book = self.s.pending_events.setdefault("warning", {})

        for stock in self.s.stocks:
            meta     = stock['meta']
            name     = meta['c_name']
            hp       = meta.get('hp', 50.0)
            soft_cap = meta.get('hp_soft_cap', 60.0)
            hp_ratio = hp / max(1.0, soft_cap)
            char     = meta.get('char', 'Normal')

            if name in warning_book:
                info        = warning_book[name]
                target_date = info.get('date')
                if isinstance(target_date, str):
                    target_date = datetime.strptime(target_date, "%Y-%m-%d")
                if self.s.current_date.date() >= target_date.date():
                    w_type = info.get('type')
                    if w_type == 'IN':
                        meta['char'] = 'WARNING'
                        self.s.daily_news.append(f"⚠️ [투자경고 지정] {name} 재무 체력 위험 수준 — 투자 유의")
                    elif w_type == 'DANGER':
                        meta['char'] = 'DANGER'
                        self.s.daily_news.append(f"🚨 [상폐위험 지정] {name} 재무 체력 극도로 위험 — 상장폐지 임박")
                    elif w_type == 'OUT':
                        meta['char'] = 'Normal'
                        self.s.daily_news.append(f"✅ [투자경고 해제] {name} 재무 정상화 확인")
                    del warning_book[name]
                continue

            if char == 'Normal' and hp_ratio < 0.40:
                warn_date = self.s.current_date + timedelta(days=7)
                warning_book[name] = {'date': warn_date, 'type': 'IN'}
                if self.s.has_paid_news_access:
                    self.s.daily_news.append(
                        f"💎 [경고 D-7 예보] {name} 재무 체력 {hp_ratio*100:.1f}% 위험선 돌파 — 7일 후 투자경고 지정 예정 (프리미엄 전용)"
                    )
            elif char == 'WARNING' and hp_ratio < 0.15:
                warn_date = self.s.current_date + timedelta(days=7)
                warning_book[name] = {'date': warn_date, 'type': 'DANGER'}
                if self.s.has_paid_news_access:
                    self.s.daily_news.append(
                        f"💎 [상폐위험 D-7 예보] {name} 재무 체력 {hp_ratio*100:.1f}% — 7일 후 상폐위험 지정 예정 (프리미엄 전용)"
                    )
                total_inst = meta.get('foreign_share', 0.0) + meta.get('inst_share', 0.0)
                escape     = total_inst * 0.15
                meta['foreign_share'] = max(0.001, meta.get('foreign_share', 0.0) - escape * 0.6)
                meta['inst_share']    = max(0.001, meta.get('inst_share', 0.0)    - escape * 0.4)
                meta['retail_share']  = min(0.99,  meta.get('retail_share', 0.0)  + escape)
            elif char == 'WARNING' and hp_ratio >= 0.50:
                warn_date = self.s.current_date + timedelta(days=7)
                warning_book[name] = {'date': warn_date, 'type': 'OUT'}
                if self.s.has_paid_news_access:
                    self.s.daily_news.append(
                        f"💎 [경고해제 D-7 예보] {name} 재무 정상화 — 7일 후 투자경고 해제 예정 (프리미엄 전용)"
                    )
                    recover = meta.get('retail_share', 0.0) * 0.10
                    meta['inst_share']    = min(0.99, meta.get('inst_share', 0.0)    + recover * 0.5)
                    meta['foreign_share'] = min(0.99, meta.get('foreign_share', 0.0) + recover * 0.5)
                    meta['retail_share']  = max(0.001, meta.get('retail_share', 0.0) - recover)

    # ─────────────────────────────────────────────
    # 상장폐지 (기존 유지)
    # ─────────────────────────────────────────────
    def check_delisting(self):
        # HP 0이면 무조건 상폐 (종목 수 관계없이)
        # 종목 수 균형은 IPO 가속으로 해결
        MIN_STOCKS_LIMIT = 50  # 절대 최솟값만 유지
        if len(self.s.stocks) <= MIN_STOCKS_LIMIT:
            return

        # ★ HP 0 종목 즉시 상폐 처리 (좀비 방지)
        # pending_events 거치지 않고 바로 delisted_stocks에 추가
        to_delist_now = []
        for stock in self.s.stocks:
            hp_now = stock['meta'].get('hp', 99.0)
            if hp_now <= 0.0:
                to_delist_now.append(stock)

        remaining = len(self.s.stocks)
        for stock in to_delist_now:
            if remaining <= MIN_STOCKS_LIMIT:
                break
            meta = stock['meta']
            name = meta['c_name']
            meta['is_doomed']              = True
            meta['delisted_date']          = self.s.current_date.strftime('%Y-%m-%d')
            meta['is_officially_delisted'] = True
            self.s.delisted_stocks.append(stock)
            if hasattr(self, '_db') and self._db:
                self._db.save_delisted_stock(stock)
            self.s.stocks.remove(stock)
            self.s.pending_events.get("delist", {}).pop(name, None)
            self.s.pending_events.get("warning", {}).pop(name, None)
            remaining -= 1

        delisted_this_turn = []
        is_depression = (
            any("대공황" in str(v) for v in [self.s.current_scenario])
            and "극복" not in self.s.current_scenario
        )
        # ★ HP 0인 종목은 제한 없이 전부 상폐 처리
        # (좀비 기업 방지)
        max_delist   = 999 if not is_depression else 30
        new_reserved = 0
        candidates   = sorted(self.s.stocks, key=lambda x: x['meta'].get('hp', 99.0))
        _LOSS_LIMIT  = {"대형주": 12, "중형주": 10, "소형주": 8}

        for stock in candidates:
            meta = stock['meta']
            name = meta['c_name']

            if name in self.s.pending_events["delist"]:
                info        = self.s.pending_events["delist"][name]
                target_date = info.get('date') if isinstance(info, dict) else info
                if isinstance(target_date, str):
                    target_date = datetime.strptime(target_date, "%Y-%m-%d")
                if self.s.current_date < target_date:
                    continue
                reason = info.get('reason', '재무 파탄') if isinstance(info, dict) else '재무 파탄'
                self.s.daily_news.append(f"💀 [상장폐지 확정] {name} ({reason})")
                meta['delisted_date'] = self.s.current_date.strftime('%Y-%m-%d')
                delisted_this_turn.append(stock)
                del self.s.pending_events["delist"][name]
                self.s.pending_events.get("warning", {}).pop(name, None)
                continue

            ld = meta.get('listed_date_dt')
            if not ld or isinstance(ld, str):
                try:    ld = datetime.strptime(meta['listed_date'], '%Y-%m-%d')
                except: ld = self.s.current_date
                meta['listed_date_dt'] = ld

            hp_now      = meta.get('hp', 50.0)
            is_bankrupt = hp_now <= 0.0

            if is_bankrupt and not meta.get('is_doomed'):
                remaining = len(self.s.stocks) - len(delisted_this_turn) - new_reserved
                if remaining > MIN_STOCKS_LIMIT:
                    delist_date = self.s.current_date
                    reason      = "재무 완전 파탄 (HP 소진)"
                    self.s.pending_events["delist"][name] = {"date": delist_date, "reason": reason}
                    meta['is_doomed'] = True
                    new_reserved += 1
                    if not self.s.silent_mode:
                        self.s.daily_news.append(f"☠️ [즉시상폐] {name} 재무 체력 완전 소진 — 오늘 상장폐지")
                    continue

        for ds in delisted_this_turn:
            if ds in self.s.stocks:
                ds['meta']['delisted_date']          = self.s.current_date.strftime('%Y-%m-%d')
                ds['meta']['is_officially_delisted'] = True
                self.s.delisted_stocks.append(ds)
                if hasattr(self, '_db') and self._db:
                    self._db.save_delisted_stock(ds)
                self.s.stocks.remove(ds)

    # ─────────────────────────────────────────────
    # 그룹 확장 (기존 유지)
    # ─────────────────────────────────────────────
    def handle_group_expansion(self, silent: bool):
        current_lv    = self.s.max_tech_reached
        is_depression = "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario
        is_recovery   = "극복" in self.s.current_scenario or "전후" in self.s.current_scenario

        if is_depression:
            group_ranking = sorted(
                [{"gid": gid, "cap": sum(s['market_cap'] for s in self.s.stocks if s['meta']['group_id'] == gid)}
                 for gid in self.s.groups],
                key=lambda x: x['cap'], reverse=True
            )
            top_3 = {g['gid'] for g in group_ranking[:3]}
            for gid, ginfo in list(self.s.groups.items()):
                members = [s for s in self.s.stocks if s['meta']['group_id'] == gid]
                if not members: continue
                members.sort(key=lambda x: x['market_cap'])
                core = members[-1]
                survival_limit = 5 if gid in top_3 else 3
                if len(members) > survival_limit:
                    for i in range(len(members) - survival_limit):
                        target = members[i]
                        if target in self.s.stocks and target != core:
                            core['meta']['assets']    += target['meta']['assets'] * 0.3
                            core['meta']['risk_score'] = max(0, core['meta']['risk_score'] - 15.0)
                            self.s.stocks.remove(target)
                            if not silent:
                                self.s.daily_news.append(
                                    f"🔪 [피의 구조조정] {ginfo['name']}그룹이 {core['meta']['c_name']}를 살리기 위해 {target['meta']['c_name']}를 정리했습니다."
                                )
            return

        # ★ 페이즈별 그룹사 계열사 수 제한
        phase = getattr(self.s, '_last_processed_phase', '1A')
        limit = {
            "1A": 3, "1B": 4,
            "2A": 5, "2B": 7,
            "3A": 8, "3B": 9,
            "4A": 10, "4B": 10,
        }.get(phase, 3)
        if is_recovery: limit = min(limit + 1, 10)

        if len(self.s.groups) < self.s.max_group_count and random.random() < 0.01:
            candidates = [s for s in self.s.stocks
                          if s['meta']['tier'] == "대형주"
                          and s['meta']['group_id'] is None
                          and s['meta']['risk_score'] < 1.0]
            if candidates:
                target_stock = max(candidates, key=lambda x: x['market_cap'])
                parent_name  = target_stock['meta']['c_name']
                gid          = f"GROUP_{parent_name}"
                self.s.groups[gid] = {"name": parent_name, "active": True}
                target_stock['meta']['group_id'] = gid
                target_stock['meta']['group']    = parent_name
                # ★ 승격 시 계열사 추가도 limit 체크
                if limit > 1:
                    new_ind = random.choice([i for i in MAIN_INDUSTRIES if i != target_stock['meta']['ind']])
                    new_s = self.cm.create_stock_data(None, new_ind, "중", gid)
                    self.s.stocks.append(new_s)
                    if self._db:
                        self._db.save_listing_snapshot(new_s)
                if not silent:
                    self.s.daily_news.append(f"🏢 [그룹승격] {parent_name}이 지주사 체제로 전환합니다!")

        for gid, ginfo in self.s.groups.items():
            members = [s for s in self.s.stocks if s['meta']['group_id'] == gid]
            # ★ limit 초과한 경우 추가 금지 (이전 잔여 코드로 초과된 경우 방지)
            if len(members) >= limit:
                continue
            if random.random() < 0.03:
                existing_inds = [m['meta']['ind'] for m in members]
                # ★ 이미 시장에 동일 그룹명+산업 조합 종목이 있으면 제외
                group_name = ginfo['name']
                used_inds = set(existing_inds)
                for st in self.s.stocks:
                    st_name = st['meta'].get('c_name', '')
                    st_ind  = st['meta'].get('ind', '')
                    # "서한 건강관리" 패턴으로 이미 존재하면 해당 산업 제외
                    if st_name.startswith(f"{group_name} {st_ind}") or \
                       st_name == f"{group_name} {st_ind}":
                        used_inds.add(st_ind)
                avail = [i for i in MAIN_INDUSTRIES if i not in used_inds]
                if avail:
                    new_ind = random.choice(avail)
                    new_s = self.cm.create_stock_data(None, new_ind, "중", gid)
                    self.s.stocks.append(new_s)
                    if self._db:
                        self._db.save_listing_snapshot(new_s)
                    if not silent:
                        self.s.daily_news.append(f"📢 [그룹확장] {ginfo['name']}그룹이 {new_ind} 계열사를 추가했습니다.")

    # ─────────────────────────────────────────────
    # 신규 상장
    # ─────────────────────────────────────────────
    def handle_new_listings(self, silent: bool):
        from .constants import MAIN_INDUSTRIES
        if len(self.s.stocks) >= self.s.MAX_STOCKS:
            return

        is_depression = "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario
        today_str     = self.s.current_date.strftime('%Y-%m-%d')
        cur_count     = len(self.s.stocks)
        bubble_index  = getattr(self.s, 'bubble_index', 0.0)
        cycle         = getattr(self.s, 'cycle_stage', '확장')

        # 대기 중인 상장 처리
        for stock in self.s.pending_listings[:]:
            if stock['meta']['listed_date'] <= today_str:
                self.s.stocks.append(stock)
                self.s.pending_listings.remove(stock)
                # ★ 상장 시점 스냅샷 저장
                if self._db:
                    self._db.save_listing_snapshot(stock)
                if not silent:
                    self.s.daily_news.append(f"🚀 [신규상장] {stock['meta']['c_name']}이 거래를 시작합니다!")

        if self.s.virtual_weekday != 0:
            return

        # 대공황 중엔 후반부만 상장 허용
        if is_depression:
            total_days   = 252 * 10
            elapsed_days = total_days - self.s.scenario_timer
            if (elapsed_days / total_days) < 0.5: return
            if random.random() > 0.1: return

        # ── 종목 수 기반 IPO 속도 조정 ───────────────
        # 400개 이상:  1~3개 (과포화 방지)
        # 300~400개:  2~4개 (정상)
        # 250~300개:  3~5개 (소폭 증가)
        # 200~250개:  4~6개 (경보)
        # 200개 이하: 6~8개 (긴급)
        if cur_count >= 400:
            ipo_min, ipo_max = 1, 3
            mid_ratio = 0.20  # ★ 항상 20% 중형주 유지 (현실 신규상장 비율 반영)
        elif cur_count >= 300:
            ipo_min, ipo_max = 2, 4
            mid_ratio = 0.25
        elif cur_count >= 250:
            ipo_min, ipo_max = 3, 5
            mid_ratio = 0.25
        elif cur_count >= 200:
            ipo_min, ipo_max = 4, 6
            mid_ratio = 0.30
        else:
            ipo_min, ipo_max = 6, 8
            mid_ratio = 0.35  # 긴급 시 중형주 35%

        # 버블/수축기 보정 (단, 종목 수 긴급 구간이면 무시)
        if cur_count >= 250:
            if cycle == "수축" or bubble_index < 50:
                ipo_max = max(ipo_min, ipo_max - 1)
            elif bubble_index > 200:
                ipo_max = min(self.s.MAX_STOCKS - cur_count, ipo_max + 1)

        # ★ 5순위: 산업별 IPO 가중치 계산
        # 포화 산업(현재 수 / 상한 수 비율이 높을수록) IPO 확률 낮아짐
        from .constants import INDUSTRY_MAX_COMPANIES
        lv_now = self.s.max_tech_reached
        ind_counts = {}
        for st in self.s.stocks:
            ind = st['meta'].get('ind', '')
            ind_counts[ind] = ind_counts.get(ind, 0) + 1

        ipo_weights = []
        for ind in MAIN_INDUSTRIES:
            max_co    = INDUSTRY_MAX_COMPANIES.get(ind, {}).get(lv_now, 30)
            cur_co    = ind_counts.get(ind, 0)
            saturation = cur_co / max(1, max_co)
            weight    = max(0.05, 1.0 - saturation)   # 포화될수록 낮아짐, 최소 0.05
            ipo_weights.append(weight)

        ipo_count = random.randint(ipo_min, max(ipo_min, ipo_max))
        for _ in range(ipo_count):
            # 종목 수 다시 체크 (루프 중 MAX 초과 방지)
            if len(self.s.stocks) + len(self.s.pending_listings) >= self.s.MAX_STOCKS:
                break

            # ★ 가중치 기반 산업 선택 (포화 산업은 확률 낮음)
            chosen_ind = random.choices(MAIN_INDUSTRIES, weights=ipo_weights, k=1)[0]

            # 중형주 비율 적용
            tier = "중" if random.random() < mid_ratio else "소"
            new_s = self.cm.create_stock_data(None, chosen_ind, tier)

            # ★ GRI → 공모가 연동 개선
            # 기존: GRI 전체 배율 그대로 적용 → 소형주 공모가 폭등 문제
            # 개선: 산업 평균 PER 기준 역산 + GRI 배율 완화 (최대 2배)
            gri_ratio_ipo = self.s.gri / 1000.0
            if gri_ratio_ipo > 1.0:
                price_boost = min(2.0, 1.0 + (gri_ratio_ipo - 1.0) * 0.3)  # 완화된 배율
                new_s['price'] = int(new_s['price'] * price_boost)
                new_s['market_cap'] = new_s['price'] * new_s['shares']

            listing_date = self.s.current_date + timedelta(days=7)
            new_s['meta']['listed_date'] = listing_date.strftime('%Y-%m-%d')
            self.s.pending_listings.append(new_s)
            if not silent:
                self.s.daily_news.append(
                    f"📅 [상장예고] {new_s['meta']['c_name']} ({new_s['meta']['listed_date']} 상장 예정)"
                )

    # ─────────────────────────────────────────────
    # 자사주 / ★분할(정상화) / 병합 이벤트
    # ─────────────────────────────────────────────
    def apply_stock_event(self, stock: dict, silent: bool = False):
        meta   = stock['meta']
        price  = stock['price']
        sector = SECTOR_MAP.get(meta['ind'], "Value")

        # 자사주 매입
        if (meta.get('momentum', 0) < -0.05
                and stock['market_cap'] < meta['assets'] * 0.7
                and meta['risk_score'] < 1.0):
            buy_ratio = random.uniform(0.01, 0.03)
            meta['treasury_share'] += buy_ratio
            meta['momentum'] += 0.05
            inst_abs   = random.uniform(0.1, 0.35)
            retail_abs = 1.0 - inst_abs
            meta['inst_share']   = max(0.0, meta.get('inst_share', 0.0)   - buy_ratio * inst_abs)
            meta['retail_share'] = max(0.0, meta.get('retail_share', 0.0) - buy_ratio * retail_abs)
            stock['price'] = int(stock['price'] * 1.02)
            if not silent:
                self.s.daily_news.append(f"📢 [자사주매입] {meta['c_name']} 주가 방어 및 지분 흡수")

        # 자사주 매도
        elif stock['market_cap'] > meta['assets'] * 2.0 and meta.get('treasury_share', 0) > 0.05:
            sell_ratio = random.uniform(0.02, 0.05)
            meta['treasury_share'] -= sell_ratio
            meta['momentum'] -= 0.04
            cash_in = stock['price'] * (stock['shares'] * sell_ratio)
            meta['assets'] += cash_in
            tier = meta.get('tier', '소형주')
            if   tier == "대형주": fw, iw = random.uniform(0.4, 0.6), random.uniform(0.2, 0.4)
            elif tier == "중형주": fw, iw = random.uniform(0.1, 0.25), random.uniform(0.3, 0.5)
            else:                   fw, iw = random.uniform(0.0, 0.05), random.uniform(0.1, 0.2)
            rw = max(0.0, 1.0 - fw - iw)
            meta['foreign_share'] = meta.get('foreign_share', 0.0) + sell_ratio * fw
            meta['inst_share']    = meta.get('inst_share', 0.0)    + sell_ratio * iw
            meta['retail_share']  = meta.get('retail_share', 0.0)  + sell_ratio * rw
            total_sum = (meta['treasury_share'] + meta['owner_share']
                         + meta['foreign_share'] + meta['inst_share'] + meta['retail_share'])
            if total_sum > 1.0:
                excess = total_sum - 1.0
                meta['retail_share'] = max(0.0, meta['retail_share'] - excess) \
                    if meta['retail_share'] > 0 else max(0.0, meta['inst_share'] - excess)
            if meta.get('continuous_loss_count', 0) == 0 and meta.get('risk_score', 0) < 1.5:
                stock['price'] = int(stock['price'] * 1.03)
                if not silent: self.s.daily_news.append(f"🟢 [자사주처분] {meta['c_name']} 우수 실적 기반 자금 확보")
            else:
                stock['price'] = int(stock['price'] * 0.94)
                if not silent: self.s.daily_news.append(f"🔴 [자사주처분] {meta['c_name']} 단순 현금화로 인한 수급 부담")

        self._handle_stock_split(stock, silent)

        # 일일 HP 미세 차감
        is_resistant = "대형주" in meta['tier'] or meta['group_id'] is not None
        loss_count   = meta.get('continuous_loss_count', 0)

        if loss_count > 0:
            sens = meta.get('risk_sensitivity', 1.0)
            if is_resistant: sens *= 0.3
            # 일일 HP 차감 완화 (0.02 → 0.01)
            daily_hp_dmg = 0.01 * loss_count * sens
            if self.s.macro["interest_rate"] > 15.0:
                daily_hp_dmg += 0.02
            if price < 1000:
                daily_hp_dmg += 0.02

            hp       = meta.get('hp', 50.0)
            soft_cap = meta.get('hp_soft_cap', 60.0)
            hp_ratio = hp / max(1.0, soft_cap)
            shield   = meta.get('shield', 0.0)

            if hp_ratio < 0.30 and shield > 0:
                shield_dmg = daily_hp_dmg * meta.get('assets', 1.0) * 0.001
                if shield >= shield_dmg:
                    meta['shield'] = round(shield - shield_dmg, 2)
                else:
                    remaining_dmg  = daily_hp_dmg - (shield / max(1.0, meta.get('assets', 1.0)) * 1000)
                    meta['shield'] = 0.0
                    meta['hp']     = round(max(0.0, hp - remaining_dmg), 2)
            else:
                meta['hp'] = round(max(0.0, hp - daily_hp_dmg), 2)
        else:
            if loss_count == 0:
                hp       = meta.get('hp', 50.0)
                soft_cap = meta.get('hp_soft_cap', 60.0)
                # ★ HP 0이면 회복 안 됨 + 주가 100원 미만이면 회복 안 됨
                if hp > 0.0 and price >= 100:
                    meta['hp'] = round(min(soft_cap, hp + 0.02), 2)

        # ★ 절대 주가 기준 HP 차감 (loss_count 관계없이)
        # 주가가 낮을수록 시장 신뢰 상실 → HP 강제 차감
        hp_now = meta.get('hp', 50.0)
        if hp_now > 0.0:
            if   price <= 10:  meta['hp'] = max(0.0, hp_now - 2.0)
            elif price < 50:   meta['hp'] = max(0.0, hp_now - 1.0)
            elif price < 100:  meta['hp'] = max(0.0, hp_now - 0.5)

        # char 상태 판정
        hp_now       = meta.get('hp', 50.0)
        soft_cap     = meta.get('hp_soft_cap', 60.0)
        hp_ratio_now = hp_now / max(1.0, soft_cap)

        if meta.get('delist_timer', 0) > 0:
            meta['char'] = f"EXIT-{meta['delist_timer']}"
        elif hp_now <= 0:
            meta['char'] = "BANKRUPT"
        elif hp_ratio_now < 0.15:
            meta['char'] = "DANGER"
        elif hp_ratio_now < 0.40:
            meta['char'] = "WARNING"
        else:
            meta['char'] = "Normal"

    # ─────────────────────────────────────────────
    # ★ 액면분할 — 섹터별 트리거/비율/쿨다운/횟수 차별화
    # ─────────────────────────────────────────────
    @staticmethod
    def get_tick_size(price: int) -> int:
        """현실 한국 주식 호가 단위"""
        if price < 2_000:        return 1
        elif price < 5_000:      return 5
        elif price < 20_000:     return 10
        elif price < 50_000:     return 50
        elif price < 200_000:    return 100
        elif price < 500_000:    return 500
        else:                    return 1_000

    def _handle_stock_split(self, stock: dict, silent: bool = False):
        meta    = stock['meta']
        price   = int(stock['price'])
        shares  = stock['shares']
        tier    = meta['tier']
        ind     = meta.get('ind', '')
        sector  = SECTOR_MAP.get(ind, "Value")
        self.s.daily_splits = getattr(self.s, 'daily_splits', {})

        # ★ 소형주는 분할 없음
        if "소형" in tier:
            return

        # ★ 섹터별 설정 — 횟수 제한 제거, 쿨다운으로 대체
        # 쿨다운: Growth 5년 / Value 10년 / Defensive 7년 / Theme 3년
        SPLIT_CFG = {
            #            트리거      목표주가  쿨다운   의지확률
            "Growth":    (  500_000,   100_000, 1260,   0.95),  # 50만 트리거, 목표 10만원대
            "Value":     (5_000_000, 1_000_000, 2520,   0.60),  # 500만 트리거, 목표 100만원대
            "Defensive": (3_000_000,   300_000, 1764,   0.35),  # 300만 트리거, 목표 30만원대
            "Cyclical":     (  800_000,    80_000,  756,   0.80),  # 80만 트리거, 목표 8만원대
        }
        cfg = SPLIT_CFG.get(sector, SPLIT_CFG["Value"])
        trigger, target_price, cooldown_days, will_prob = cfg

        # ★ will_to_split: 최초 1회만 결정 (섹터별 의지확률 반영)
        if 'will_to_split' not in meta:
            meta['will_to_split'] = (random.random() < will_prob)

        if not meta['will_to_split']:
            # 주가 구간별 매일 재평가 — 트리거 배율 기준 (섹터별 트리거 차이 흡수)
            # 트리거의 1~1.5배: 거의 안 함 / 4배+: 높은 확률
            over = price - trigger
            if over > 0:
                ratio = price / trigger
                if   ratio >= 10: daily_p = 0.040
                elif ratio >= 4:  daily_p = 0.025
                elif ratio >= 2:  daily_p = 0.012
                elif ratio >= 1.5: daily_p = 0.005
                else:             daily_p = 0.002  # 트리거 직후: 0.2%/일
                if random.random() < daily_p:
                    meta['will_to_split'] = True
            return

        # ★ 쿨다운 체크 (횟수 제한 없음 — 쿨다운으로만 제어)
        cooldown = meta.get('split_cooldown_days', 0)
        if cooldown > 0:
            meta['split_cooldown_days'] = cooldown - 1
            return

        # ── 유통주식수 (거래량 트리거 판단용) ──────────────────────────────
        fixed_ratio  = meta.get('treasury_share', 0.0) + meta.get('owner_share', 0.0)
        float_shares = max(1, int(shares * (1.0 - fixed_ratio)))
        avg_vol      = meta.get('avg_daily_volume', float_shares * 0.003)

        # ★ 트리거 체크
        split_ratio = 0

        # ★ 목표주가 기반 분할 비율 계산 (모든 섹터 공통)
        # 분할 비율 = price // target_price (목표주가로 나눔)
        # 단, 섹터별 실행 확률 차등 + Theme는 고가 시 조건 완화

        if sector == "Cyclical":
            rate_1m = meta.get('rate_1m', stock.get('rate', 0.0))
            overage = price / trigger
            if price >= trigger * 5:
                # 400만원+: 가격만으로 분할 (급등 조건 불필요)
                exec_prob = 0.050 if overage >= 10 else (0.030 if overage >= 5 else 0.015)
                if random.random() > exec_prob:
                    return
            elif price >= trigger and rate_1m >= 30.0:
                # 80만원+ + 월간 30%+: 기존 급등 분할
                if random.random() > 0.03:
                    return
            else:
                return

        elif sector == "Defensive":
            liquidity_starved = (avg_vol < float_shares * 0.0003) and (price >= 1_000_000)
            price_hit = (price >= trigger)
            if not (liquidity_starved or price_hit):
                return
            pass_prob = 0.015 if liquidity_starved else 0.008
            if random.random() > pass_prob:
                return

        elif sector == "Value":
            if price < trigger:
                return
            if avg_vol >= float_shares * 0.0005:
                if random.random() > 0.10:
                    return
            if random.random() > 0.010:
                return

        else:  # Growth
            if price < trigger:
                return
            overage = price / trigger
            if   overage >= 10: exec_prob = 0.050
            elif overage >= 4:  exec_prob = 0.030
            elif overage >= 2:  exec_prob = 0.015
            elif overage >= 1.5: exec_prob = 0.005
            else:                exec_prob = 0.002
            if random.random() > exec_prob:
                return

        # ★ 목표주가 기반 비율 계산
        # 분할 후 주가가 target_price 수준이 되도록
        raw_ratio = price // target_price
        split_ratio = max(2, min(50, raw_ratio))  # 최소 2:1, 최대 50:1

        if split_ratio < 2:
            if price < 1000:
                self._handle_stock_merge(stock, silent)
            return

        # ★ 액면가 하한 체크
        # 전 섹터 100원 하한 (한국 상법 기준)
        par_value = meta.get('par_value', 500)
        new_par   = par_value / split_ratio
        par_floor = 100
        if new_par < par_floor:
            split_ratio = max(2, int(par_value // par_floor))
            new_par = par_value / split_ratio
            if new_par < par_floor or split_ratio < 2:
                return  # 이미 최소 액면가 → 분할 불가

        # ★ D-7 예약
        if not meta.get('pending_split'):
            split_date = self.s.current_date + timedelta(days=7)
            meta['pending_split'] = {
                'date':  split_date.strftime('%Y-%m-%d'),
                'ratio': split_ratio,
            }
            if self.s.has_paid_news_access:
                self.s.daily_news.append(
                    f"💎 [분할예고] {meta['c_name']} 7일 후 1:{split_ratio} 액면분할 예정 "
                    f"(액면가 {par_value}원 → {int(new_par)}원) (프리미엄 전용)"
                )
            return

        elif meta.get('pending_split'):
            p      = meta['pending_split']
            p_date = datetime.strptime(p['date'], '%Y-%m-%d')
            if self.s.current_date.date() < p_date.date():
                return
            split_ratio = p['ratio']
            meta.pop('pending_split', None)

            old_name = meta['c_name']
            new_price = int(stock['price']) // split_ratio
            stock['price']      = new_price
            stock['shares']    *= split_ratio
            stock['market_cap'] = stock['price'] * stock['shares']
            meta['split_count'] = meta.get('split_count', 0) + 1
            meta['par_value']   = int(par_value / split_ratio)  # ★ 액면가 갱신
            meta['split_cooldown_days'] = cooldown_days

            # ★ 분할 후 initial_price 조정 — 분할 전 고가 기준으로 HP 오차 방지
            old_initial = meta.get('initial_price', 0)
            if old_initial > 0:
                meta['initial_price'] = old_initial / split_ratio
            # initial_assets도 함께 조정 (PBR 기준 왜곡 방지)
            old_init_assets = meta.get('initial_assets', 0)
            if old_init_assets > 0:
                meta['initial_assets'] = old_init_assets  # assets 자체는 그대로 (주식수가 늘었으니)

            # ★ 주가 tick_size 정합성 맞추기
            tick = self.get_tick_size(new_price)
            stock['price'] = (new_price // tick) * tick

            self.s.daily_splits[old_name] = 1.0 / float(split_ratio)
            if not silent:
                self.s.daily_news.append(
                    f"✂️ [액면분할] {meta['c_name']} {split_ratio}:1 분할 "
                    f"(액면가 {par_value}원 → {meta['par_value']}원)"
                )
                self.s.daily_news.append(
                    f"  └ 분할가: {stock['price']:,}원 | "
                    f"발행주식수: {stock['shares']/1_000_000:.0f}백만주"
                )

    # ─────────────────────────────────────────────
    # 주식 병합 (별도 분리)
    # ─────────────────────────────────────────────
    def _handle_stock_merge(self, stock: dict, silent: bool = False):
        meta        = stock['meta']
        merge_count = meta.get('merge_count', 0)
        last_merge  = meta.get('last_merge_date', '')
        today_str   = self.s.current_date.strftime('%Y-%m-%d')
        days_since_merge = 999
        if last_merge:
            try:
                days_since_merge = (self.s.current_date - datetime.strptime(last_merge, '%Y-%m-%d')).days
            except Exception:
                pass

        if merge_count < 2 and days_since_merge >= 30 and random.random() < 0.3:
            if not meta.get('pending_merge'):
                merge_date = self.s.current_date + timedelta(days=7)
                meta['pending_merge'] = {
                    'date':  merge_date.strftime('%Y-%m-%d'),
                    'ratio': 10,
                }
                if self.s.has_paid_news_access:
                    self.s.daily_news.append(
                        f"💎 [병합예고] {meta['c_name']} 7일 후 1:10 주식 병합 예정 (프리미엄 전용)"
                    )
            elif meta.get('pending_merge'):
                p      = meta['pending_merge']
                p_date = datetime.strptime(p['date'], '%Y-%m-%d')
                if self.s.current_date.date() >= p_date.date():
                    ratio    = p['ratio']
                    old_name = meta['c_name']
                    stock['price']  *= ratio
                    stock['shares'] //= ratio
                    stock['market_cap'] = stock['price'] * stock['shares']
                    meta['merge_count']     = merge_count + 1
                    meta['last_merge_date'] = today_str
                    self.s.daily_splits[old_name] = float(ratio)
                    hp_gain  = 5.0
                    soft_cap = meta.get('hp_soft_cap', 60.0)
                    meta['hp'] = round(min(soft_cap, meta.get('hp', 0.0) + hp_gain), 2)
                    # ★ 병합 후 initial_price 조정
                    old_initial = meta.get('initial_price', 0)
                    if old_initial > 0:
                        meta['initial_price'] = old_initial * ratio
                    meta.pop('pending_merge', None)
                    self.s.daily_news.append(
                        f"🧩 [AI병합] {meta['c_name']}이 1:{ratio} 병합을 완료했습니다. (병합 {meta['merge_count']}회차 / HP +{hp_gain})"
                    )

    # ─────────────────────────────────────────────
    # 생존 전략 (기존 유지)
    # ─────────────────────────────────────────────
    def _handle_survival_strategy(self, stock: dict):
        meta     = stock['meta']
        hp       = meta.get('hp', 50.0)
        soft_cap = meta.get('hp_soft_cap', 60.0)
        hp_ratio = hp / max(1.0, soft_cap)
        sector   = SECTOR_MAP.get(meta['ind'], "Value")

        if hp_ratio >= 0.40: return

        if meta.get('group_id'):
            gid        = meta['group_id']
            loss_cnt   = meta.get('continuous_loss_count', 0)
            hp_pct     = hp_ratio

            # ★ 그룹 전체 시총 계산
            group_cap  = sum(
                s['market_cap'] for s in self.s.stocks
                if s['meta'].get('group_id') == gid
            )

            # ★ 주가 하락률 계산
            initial_price = meta.get('initial_price', 0)
            cur_price     = stock['price']
            if initial_price > 10:
                price_drop = 1.0 - (cur_price / max(1.0, initial_price))
            else:
                price_drop = 0.0

            # ★ 퇴출 조건 체크 (하나라도 해당하면 버림)
            should_abandon = (
                loss_cnt >= 8                          # 연속 적자 2년 이상
                or price_drop >= 0.90                  # 주가 -90% 이상
                or (hp_pct < 0.20 and loss_cnt >= 4)  # HP 20% 미만 + 연속 적자 4분기
            )

            # ★ 지원 가능 조건 체크
            can_support = (
                loss_cnt < 8
                and price_drop < 0.90
                and group_cap >= 50_000_000_000   # 그룹 시총 500억 이상
            )

            if should_abandon:
                # 그룹이 계열사를 버림
                old_group = meta.get('group', meta['c_name'])
                meta['group_id'] = None
                meta['group']    = None
                if not self.s.silent_mode:
                    self.s.daily_news.append(
                        f"🗑️ [계열사 정리] {old_group}그룹이 {meta['c_name']}을 "
                        f"정리했습니다. (연속적자 {loss_cnt}분기 / 주가하락 {price_drop*100:.1f}%)"
                    )
                # 그룹에서 제외 후 독자 생존 로직으로 넘어감
            elif can_support:
                # 정상 그룹지원
                meta['hp'] = round(min(soft_cap, hp + 5.0), 2)
                if not self.s.silent_mode:
                    self.s.daily_news.append(
                        f"🛡️ [그룹지원] {meta['c_name']}가 그룹사의 자금 지원으로 위기를 넘깁니다."
                    )
                return  # 지원 받았으면 독자 생존 로직 불필요
        else:
            if sector in ["Value", "Defensive"] and meta.get('treasury_share', 0) > 0.02:
                sell_shares_count  = int(stock['shares'] * 0.02)
                meta['treasury_share'] -= 0.02
                meta['assets']         += stock['price'] * sell_shares_count
                meta['hp'] = round(min(soft_cap, hp + 3.0), 2)
                stock['price']      = int(stock['price'] * 0.97)
                stock['market_cap'] = stock['price'] * stock['shares']
                if not self.s.silent_mode:
                    self.s.daily_news.append(f"💸 [위기처분] {meta['c_name']}가 자사주를 매각하여 운영 자금을 확보했습니다.")
            else:
                raise_count = meta.get('capital_raise_count', 0)
                if raise_count >= 3:
                    return
                new_shares     = int(stock['shares'] * 0.15)
                capital_raised = new_shares * (stock['price'] * 0.8)
                stock['shares']   += new_shares
                meta['assets']    += capital_raised
                meta['hp'] = round(min(soft_cap, hp + 2.0), 2)
                meta['capital_raise_count'] = raise_count + 1
                stock['price']      = int(stock['price'] * 0.85)
                stock['market_cap'] = stock['price'] * stock['shares']
                if not self.s.silent_mode:
                    self.s.daily_news.append(f"💉 [유상증자] {meta['c_name']}가 생존을 위해 증자를 단행했습니다. (가치 희석)")
    # ─────────────────────────────────────────────
    # ★ 신규: 컨센서스 선행매매 adj
    # ─────────────────────────────────────────────
    def _calc_consensus_adj(self, name: str, cur_date, tier: str, sector: str) -> float:
        """
        실적 발표 D-30~D-7: 기관이 컨센서스 방향으로 서서히 선행매매
        실적 발표 후: 소문에 사고 뉴스에 팔기 패턴 (기관 차익실현)
        """
        consensus = self.s.earnings_consensus.get(name)
        if not consensus:
            return 0.0

        direction   = consensus.get('direction', 0)
        confidence  = consensus.get('confidence', 0.5)

        # 실적 예정일 체크 (pending_events earnings에서 조회)
        earnings_ev = self.s.pending_events.get('earnings', {}).get(name)
        if not earnings_ev:
            return 0.0

        announce_date = earnings_ev.get('date') or earnings_ev.get('announce_date')
        if not announce_date:
            return 0.0
        if isinstance(announce_date, str):
            try:
                from datetime import datetime as _dt
                announce_date = _dt.strptime(announce_date, '%Y-%m-%d')
            except Exception:
                return 0.0

        days_to_announce = (announce_date.date() - cur_date.date()).days

        # D-30 ~ D-7: 기관 선행매매 (서서히 포지션 쌓기)
        if 7 <= days_to_announce <= 30:
            intensity = (30 - days_to_announce) / 23.0   # 가까울수록 강해짐
            base_adj  = direction * confidence * intensity * 0.0008
            # 대형주는 선행매매 효과 더 강함 (기관 비중 높음)
            if "대형" in tier:   base_adj *= 1.5
            elif "소형" in tier: base_adj *= 0.3
            return base_adj

        # D-7 ~ D-0: 본격 선행매매
        elif 0 < days_to_announce < 7:
            intensity = (7 - days_to_announce) / 7.0
            base_adj  = direction * confidence * intensity * 0.0015
            if "대형" in tier:   base_adj *= 1.5
            elif "소형" in tier: base_adj *= 0.3
            return base_adj

        # 발표 직후 (D+1 ~ D+3): 소문에 사고 뉴스에 팔기
        elif -3 <= days_to_announce <= 0:
            # 기관이 이미 선취매했으므로 발표 후 차익실현 → 반대 방향
            return -direction * confidence * 0.0010

        return 0.0

    # ─────────────────────────────────────────────
    # ★ 신규: 테마 군집 adj
    # ─────────────────────────────────────────────
    def _calc_cluster_adj(self, stock: dict, sector: str,
                          ind_avg_rate: dict = None) -> float:
        """
        같은 산업 내 다른 종목들의 평균 등락에 일부 동조
        ind_avg_rate: 메인 루프 밖에서 사전 계산된 산업별 평균 등락률 캐시 (O(1) 조회)
        """
        meta = stock['meta']
        ind  = meta.get('ind', '')
        if not ind:
            return 0.0

        if ind_avg_rate is not None:
            avg_rate = ind_avg_rate.get(ind, 0.0)
        else:
            same_ind_rates = [
                s.get('rate', 0.0)
                for s in self.s.stocks
                if s['meta'].get('ind') == ind and s['meta']['c_name'] != meta['c_name']
            ]
            if not same_ind_rates:
                return 0.0
            avg_rate = sum(same_ind_rates) / len(same_ind_rates)

        cluster_strength = {
            "Cyclical":     0.15,
            "Growth":    0.10,
            "Value":     0.06,
            "Defensive": 0.04,
        }.get(sector, 0.08)

        return (avg_rate / 100.0) * cluster_strength

    # ─────────────────────────────────────────────
    # ★ 신규: 유동성 함정 adj
    # ─────────────────────────────────────────────
    def _calc_liquidity_adj(self, stock: dict, tier: str) -> float:
        """
        소형주: 거래량 희박 → 조금만 사도 급등, 조금만 팔아도 급락
        대형주: 유동성 풍부 → 완충 효과
        """
        if "대형" in tier:
            return 0.0   # 대형주는 유동성 충분

        meta   = stock['meta']
        name   = meta['c_name']
        shares = max(1, stock.get('shares', 1))

        # 유통주식수 계산
        fixed  = meta.get('treasury_share', 0.0) + meta.get('owner_share', 0.0)
        float_ratio = max(0.05, 1.0 - fixed)

        # 개인 비중 높을수록 유동성 함정 강화
        retail = meta.get('retail_share', 0.3)

        if "소형" in tier:
            # 소형주: 개인 비중 50%+ 이고 유통물량 적으면 변동성 증폭
            if retail > 0.50 and float_ratio < 0.50:
                rate = stock.get('rate', 0.0)
                # 오를 때 더 오르고, 내릴 때 더 내리는 효과
                return (rate / 100.0) * 0.08
            elif retail > 0.40:
                rate = stock.get('rate', 0.0)
                return (rate / 100.0) * 0.04
        elif "중형" in tier:
            if retail > 0.45 and float_ratio < 0.55:
                rate = stock.get('rate', 0.0)
                return (rate / 100.0) * 0.03

        return 0.0

    # ─────────────────────────────────────────────
    # ★ 신규: 대주주 행동 adj
    # ─────────────────────────────────────────────
    def _calc_major_holder_adj(self, name: str, meta: dict, tier: str) -> float:
        """
        대주주 매수 → 호재 / 대주주 매도 → 악재
        랜덤하게 공시 이벤트 발생, 며칠간 지속
        """
        action_info = self.s.major_holder_action.get(name)

        # 신규 대주주 행동 발생 (낮은 확률)
        if not action_info:
            prob = 0.0003 if "대형" in tier else (0.0005 if "중형" in tier else 0.0008)
            if random.random() < prob:
                owner_share = meta.get('owner_share', 0.3)
                hp_ratio    = meta.get('hp', 50.0) / max(1.0, meta.get('hp_soft_cap', 60.0))

                # HP 낮으면 대주주 매도 확률 높음, 높으면 매수 확률 높음
                sell_prob = 0.3 + (1.0 - hp_ratio) * 0.4
                action    = "sell" if random.random() < sell_prob else "buy"
                ratio     = random.uniform(0.005, 0.02)   # 지분의 0.5~2%
                days      = random.randint(3, 10)

                self.s.major_holder_action[name] = {
                    "action":   action,
                    "ratio":    ratio,
                    "days_left": days,
                }
                # 뉴스 발행
                if not self.s.silent_mode:
                    if action == "buy":
                        self.s.daily_news.append(
                            f"📢 [대주주 공시] {name} 대주주가 자사주 {ratio*100:.1f}% 추가 매입 공시"
                        )
                    else:
                        self.s.daily_news.append(
                            f"📢 [대주주 공시] {name} 대주주가 보유 지분 {ratio*100:.1f}% 매도 공시"
                        )
                action_info = self.s.major_holder_action[name]

        if not action_info:
            return 0.0

        # 진행 중인 대주주 행동 적용
        action   = action_info['action']
        days_left = action_info['days_left']

        if days_left <= 0:
            del self.s.major_holder_action[name]
            return 0.0

        self.s.major_holder_action[name]['days_left'] -= 1

        # 대형주는 대주주 행동 영향 작음 (분산된 지분 구조)
        intensity = {"대형주": 0.3, "중형주": 0.7, "소형주": 1.0}.get(tier, 0.7)

        if action == "buy":
            return +0.0008 * intensity
        else:
            return -0.0010 * intensity

    # ─────────────────────────────────────────────
    # ★ 신규: 신용잔고 반대매매 adj
    # ─────────────────────────────────────────────
    def _calc_margin_call_adj(self, name: str, stock: dict, tier: str, sector: str) -> float:
        """
        개인이 빚내서 산 종목(신용잔고) → 하락 시 반대매매 → 추가 하락
        소형 테마주에서 가장 강하게 나타남
        """
        # 대형주는 신용잔고 반대매매 거의 없음
        if "대형" in tier:
            return 0.0

        # 신용잔고 초기화 (신규 종목)
        if name not in self.s.margin_balance:
            # 소형 테마주는 기본 신용잔고 높게 시작
            if "소형" in tier and sector == "Cyclical":
                self.s.margin_balance[name] = random.uniform(0.05, 0.15)
            elif "소형" in tier:
                self.s.margin_balance[name] = random.uniform(0.02, 0.08)
            else:
                self.s.margin_balance[name] = random.uniform(0.01, 0.05)

        margin = self.s.margin_balance[name]
        rate   = stock.get('rate', 0.0)

        # 주가 상승 시: 신용잔고 소폭 증가 (개인 레버리지 추가)
        if rate > 2.0:
            self.s.margin_balance[name] = min(0.30, margin + 0.002)
            return 0.0

        # 주가 하락 시: 신용잔고 반대매매 발동
        if rate < -3.0:
            # -3% 이하: 반대매매 시작
            call_intensity = min(0.20, abs(rate) / 100.0 * 2.0) * margin
            # 신용잔고 소진
            self.s.margin_balance[name] = max(0.0, margin - call_intensity * 0.5)
            # 추가 하락 압력
            return -call_intensity * 0.5
        elif rate < -1.5:
            call_intensity = margin * 0.02
            self.s.margin_balance[name] = max(0.0, margin - call_intensity)
            return -call_intensity * 0.3

        return 0.0