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
from collections import defaultdict
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
        elif "대공황V" in self.s.current_scenario or "고난과 부활" in self.s.current_scenario:
            _gri_ratio_v = self.s.gri / max(1.0, getattr(self.s, 'peak_gri', 1000.0))
            if   _gri_ratio_v < 0.15:  market_drift = +0.0012
            elif _gri_ratio_v < 0.30:  market_drift = +0.0010
            elif _gri_ratio_v < 0.50:  market_drift = +0.0008
            else:                       market_drift = +0.0006
        elif "극복" in self.s.current_scenario:
            market_drift = +0.0006
        elif getattr(self.s, '_pandemic_liquidity_active', False):
            # 팬데믹 유동성 장세: 각국 QE → 상승 drift
            market_drift = +0.0008
        elif '팬데믹' in self.s.current_scenario and '극복' not in self.s.current_scenario:
            # ★ [수정] 팬데믹 진행 중: 음수 drift (버그: 이 분기가 없어서 확장 기본drift 적용됐음)
            # 초반 충격 이후에도 계속 약한 하락 압력 유지 (유동성 장세 전환 전까지)
            market_drift = random.uniform(-0.0008, -0.0002)
        elif any(x in self.s.current_scenario for x in ['전쟁', '분쟁']) or                 getattr(self.s, 'war_event', {}).get('phase') == '진행중':
            # ★ [수정] 전쟁/분쟁 중 drift 음수 (버그: 전쟁 4년 +226%)
            # 현실: 전쟁 중 주식시장은 불확실성으로 하락 or 횡보
            # 대규모전쟁: 강한 하락 / 지역분쟁: 약한 하락
            _war_type = getattr(self.s, 'war_event', {}).get('type', '')
            if '대규모' in _war_type or '전쟁' in self.s.current_scenario:
                market_drift = random.uniform(-0.0010, -0.0003)
            else:  # 지역분쟁
                market_drift = random.uniform(-0.0004, +0.0001)
        else:
            market_drift = {
                "확장": +0.00048,   # +0.00080 → +0.00048 (연 ~12%, 기존 ~20%)
                "정점": +0.00018,   # +0.00030 → +0.00018 (연 ~4.5%)
                "수축": -0.00005,
                "저점": +0.00009,   # +0.00015 → +0.00009
            }.get(cycle, +0.00024)

        # ★ 초반 성장률 억제 — 연도 하드코딩 제거, 게임 시작 기준 상대 연수로
        _start_year = getattr(self.s, 'start_date', cur_date).year
        _years_elapsed = cur_date.year - _start_year
        if _years_elapsed <= 2:
            market_drift = min(market_drift, 0.00020)   # 상승 상한: 연 ~5%
            market_drift = max(market_drift, -0.00020)  # ★ 하락 하한: 상승 상한과 대칭 (기존 -0.00010 → -0.00020)
        elif _years_elapsed <= 5:
            market_drift = min(market_drift, 0.00030)   # 상승 상한: 연 ~7.5%
            market_drift = max(market_drift, -0.00030)  # ★ 하락 하한: 상승 상한과 대칭 (기존 -0.00015 → -0.00030)

        # ★ [신규] 초반 소형/중형주 drift 하한 — GRI 희석 방지
        # 소형/중형주가 초반에 쏟아지면서 -1%+/일씩 빠져 GRI를 끌어내리는 문제 해소
        # 일별 하한: -0.5% (연 약 -12%). 이보다 더 빠지는 건 개별 악재가 있을 때만
        _is_early_game = _years_elapsed <= 3
        _early_cap_floor = -0.005  # 일별 -0.5% 하한 (2~3년 공통)

        prev_rate  = getattr(self.s, '_prev_macro_snapshot', {}).get('interest_rate', interest_rate)
        rate_delta = interest_rate - prev_rate
        # ★ [수정] 금리 변화 → GRI drift 연동 강화
        # 현실: 금리 인하 → 할인율 하락 → PER 확장 → 주가 상승 (가장 강력한 연동)
        # 기존: ±0.00020으로 너무 약했음 (금리 1%p 인하가 하루 +0.02%에 불과)
        if   rate_delta >= 0.50:  market_drift -= 0.00080   # 빅스텝 인상(0.5%p): 강한 하락 압력
        elif rate_delta >= 0.25:  market_drift -= 0.00040   # 일반 인상(0.25%p)
        elif rate_delta >  0.10:  market_drift -= 0.00020
        elif rate_delta <= -0.50: market_drift += 0.00100   # 빅스텝 인하: 강한 상승 압력 (유동성 장세)
        elif rate_delta <= -0.25: market_drift += 0.00060   # 일반 인하(0.25%p)
        elif rate_delta < -0.10:  market_drift += 0.00020
        market_drift += (sentiment - 50) / 50 * 0.00008

        # ★ 버핏지수 과열 시 market_drift 하락 압력
        # 현실: 버핏지수 높으면 외국인 이탈, 고평가 인식 → 시총(분자)이 내려옴
        # GDP 가속이 아니라 주가 자체에 브레이크
        _buffett_now = getattr(self.s, 'buffett_index', 0.0)
        if _buffett_now > 150:
            # 버핏 150%+: 연 최대 -3%p 하락 압력 (일별 -0.012%)
            _buffett_penalty = -min(0.00012, (_buffett_now - 150) / 100 * 0.00005)
            market_drift += _buffett_penalty
        elif _buffett_now > 120:
            # 버핏 120%+: 약한 압력
            _buffett_penalty = -min(0.00004, (_buffett_now - 120) / 30 * 0.00002)
            market_drift += _buffett_penalty

        # ★ 버블 drift 보정 — 상승 중일 때만 억제 적용
        # 이미 하락 중이면 건드리지 않음 (폭락 방지)
        # [수정] elif 중복 버그 제거 (200, 150 구간이 두 번 정의됨 → 뒤 것은 dead code)
        if market_drift > 0:
            if   bubble_index >= 300: market_drift *= -0.15  # 극단 버블: 반전 압력
            elif bubble_index >= 250: market_drift *=  0.05  # 버블 250+: 거의 0
            elif bubble_index >= 200: market_drift *=  0.25  # 버블 200+: 약한 성장
            elif bubble_index >= 150: market_drift *=  0.50  # 버블 150+: 절반 성장

        peak_gri    = getattr(self.s, 'peak_gri', self.s.gri)
        if self.s.gri > peak_gri: self.s.peak_gri = peak_gri = self.s.gri
        gri_ratio   = self.s.gri / max(1.0, peak_gri)
        panic_factor = 0.97 if gri_ratio < 0.60 else (0.99 if gri_ratio < 0.80 else 1.0)  # 완화

        fear_mult = 1.0
        if self.s.macro.get("fear_index", 10) >= 50 and is_depression:
            fear_mult = 1.3

        tech_upgrade_year = getattr(self.s, '_tech_upgrade_years', {1: 1999}).get(lv) or 1999

        # ★ [신규] 종목별 거래량 비율(vol_ratio) 사전 계산 — 주가 영향용
        # 현실: 평소 대비 거래량이 폭발한 날 → 방향성 강화, 수렴 느려짐
        # vol_ratio = 오늘 거래량 / 최근 20일 평균거래량
        _vol_ratio_map: dict = {}
        for _s in self.s.stocks:
            _n    = _s['meta']['c_name']
            _vols = self.s.daily_volume.get(_n, [])
            if len(_vols) >= 5:
                # 최근 20일(최대) 총거래량(abs) 평균
                _recent = _vols[-20:]
                _avg = sum(abs(_v.get('foreign', 0)) + abs(_v.get('inst', 0)) + abs(_v.get('retail', 0))
                           for _v in _recent) / len(_recent)
                _today_v = _vols[-1] if _vols else {}
                _today = abs(_today_v.get('foreign', 0)) + abs(_today_v.get('inst', 0)) + abs(_today_v.get('retail', 0))
                _vol_ratio_map[_n] = (_today / max(1, _avg)) if _avg > 0 else 1.0
            else:
                _vol_ratio_map[_n] = 1.0

        # ★ 산업별 경쟁도 갱신
        self._update_industry_competition()

        # ★ 산업별 평균 등락률 사전 계산 — _calc_cluster_adj O(n²) → O(n) 최적화
        _ind_rates: defaultdict = defaultdict(list)
        for _s in self.s.stocks:
            _ind = _s['meta'].get('ind', '')
            if _ind:
                _ind_rates[_ind].append(_s.get('rate', 0.0))
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
        for _t in self.s.active_themes:
            _t['elapsed'] += 1
            if _t['elapsed'] >= _t['duration'] and _t['type'] == 'bull':
                self.s._theme_cooldown[_t['ind']] = cur_date.year
        self.s.active_themes = [
            _t for _t in self.s.active_themes
            if _t['elapsed'] < _t['duration']
        ]

        # 테마 강도 계산 함수 (루프 안에서 재사용)
        def _calc_theme_intensity(theme: dict) -> float:
            """테마 진행 단계에 따라 강도 반환 (사인 곡선 기반)"""
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

            # ★ [수정] 변동성 현실화 — 코스피 기준
            # 코스피 대형주 일 변동성: 0.8~1.2%, 중형주: 1.2~2.0%, 소형주: 1.5~2.5%
            # 기존 소형주 4.0%는 코스닥 단타 종목 수준 → 1.8%로 조정
            foreign_share = meta.get('foreign_share', 0.1)
            inst_share    = meta.get('inst_share',    0.15)
            retail_share  = meta.get('retail_share',  0.5)

            if   "대형" in tier: base_vol = 0.010; tier_mult = 0.8
            elif "중형" in tier: base_vol = 0.016; tier_mult = 1.0   # 기존 0.022 → 0.016
            else:                base_vol = 0.020; tier_mult = 1.2   # 기존 0.040 → 0.020

            vol_multiplier = 1.0 + (retail_share * 1.8) - (inst_share * 1.0) - (foreign_share * 0.8)
            vol_multiplier = max(0.4, min(3.5, vol_multiplier))   # 상한 2.5 → 3.5
            vol = base_vol * vol_multiplier

            # ★ 섹터별 변동성 추가 조정
            if sector == "Cyclical":    vol *= 1.5   # 1.4 → 1.5
            elif sector == "Growth":    vol *= 1.3   # 1.2 → 1.3
            elif sector == "Defensive": vol *= 0.8

            # ★ [신규] 이벤트 기반 변동성 스파이크
            # 현실: 어닝 서프라이즈, 테마 뉴스, 외국인 대량 매수 당일은 vol이 3~10배
            _vol_spike = 1.0
            if meta.get('_earnings_just_released'):
                _vol_spike = 4.0 if "소형" in tier else (2.5 if "중형" in tier else 1.8)
            elif meta.get('_theme_spike'):   # dispatcher에서 설정 가능한 플래그
                _vol_spike = random.uniform(2.0, 5.0) if "소형" in tier else 1.5
            # 외국인/기관 추세 5일 이상 → 모멘텀 장세, vol 소폭 상승
            _f_days = self.s.investor_trends.get(name, {}).get('foreign', {}).get('days', 0)
            if _f_days >= 5 and "대형" not in tier:
                _vol_spike = max(_vol_spike, 1.5)
            vol *= _vol_spike

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
            # [수정] Growth 베이스 7%→5%. 커뮤니케이션이 Defensive로 이동했으므로
            #        Growth 섹터는 IT+건강관리만. Defensive 베이스는 유지.
            sector_adj = {
                "Growth":    0.030 / 252,   # 5% → 3%
                "Value":     0.024 / 252,   # 4% → 2.4%
                "Defensive": 0.018 / 252,   # 3% → 1.8%
                "Cyclical":  0.030 / 252,   # 5% → 3%
            }.get(sector, 0.018 / 252)

            # ★ 산업별 추가 보정
            if ind == "IT":
                sector_adj += 0.012 / 252   # 2% → 1.2%
            elif ind == "건강관리":
                sector_adj += 0.006 / 252   # 1% → 0.6%

            # ★ 레벨별 섹터 보너스
            if lv >= 4 and sector == "Growth":
                sector_adj += 0.030 / 252   # 5% → 3%
            elif lv >= 3 and sector == "Growth":
                sector_adj += 0.018 / 252   # 3% → 1.8%
            elif lv >= 2 and sector == "Growth":
                sector_adj += 0.018 / 252   # 3% → 1.8%
            elif lv >= 2 and sector == "Cyclical":
                sector_adj += 0.012 / 252   # 2% → 1.2%
            if lv >= 3 and sector == "Value":
                sector_adj -= 0.012 / 252   # 2% → 1.2%

            years_since_lv_up = cur_date.year - tech_upgrade_year
            if 0 <= years_since_lv_up <= 3:
                if   sector == "Growth":    sector_adj += 0.024 / 252  # 4% → 2.4%
                elif sector == "Cyclical":  sector_adj += 0.018 / 252  # 3% → 1.8%
                elif sector == "Defensive": sector_adj += 0.006 / 252  # 1% → 0.6%

            # ★ PHASE_SECTOR_COEFF는 economy.py에서 이미 적용
            # market.py에서 중복 적용하지 않음

            cycle_sector = {
                # ★ 확장기
                ("확장", "Growth"):    +0.060 / 252,   # 10% → 6%
                ("확장", "Value"):     +0.036 / 252,   # 6% → 3.6%
                ("확장", "Defensive"): -0.018 / 252,   # -3% → -1.8%
                ("확장", "Cyclical"):  +0.042 / 252,   # 7% → 4.2%
                # ★ 정점기
                ("정점", "Defensive"): +0.030 / 252,   # 5% → 3%
                ("정점", "Growth"):    -0.006 / 252,   # -1% → -0.6%
                ("정점", "Cyclical"):  -0.012 / 252,   # -2% → -1.2%
                ("정점", "Value"):     +0.012 / 252,   # 2% → 1.2%
                # ★ 수축기
                ("수축", "Defensive"): +0.048 / 252,   # 8% → 4.8%
                ("수축", "Growth"):    -0.012 / 252,   # -2% → -1.2%
                ("수축", "Value"):     -0.0024 / 252,
                ("수축", "Cyclical"):  -0.018 / 252,   # -3% → -1.8%
                # ★ 저점기
                ("저점", "Value"):     +0.060 / 252,   # 10% → 6%
                ("저점", "Growth"):    +0.030 / 252,   # 5% → 3%
                ("저점", "Defensive"): +0.024 / 252,   # 4% → 2.4%
                ("저점", "Cyclical"):  +0.018 / 252,   # 3% → 1.8%
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

                if _t['type'] == 'bull':
                    _base = 0.30 * _tier_theme_mult
                    # ★ [수정] 소형주 테마 adj 연간 상한
                    # 기존: 소형주 0.30*3.5=1.05 (연 105%) → 비현실적
                    # 수정: 소형주 연 50%, 중형주 연 35%, 대형주 연 20%
                    _theme_annual_cap = {'대형주': 0.20, '중형주': 0.35, '소형주': 0.50}.get(tier, 0.35)
                    _base = min(_base, _theme_annual_cap)
                    _theme_adj += _intensity * _base / 252
                else:
                    _base = 0.20 * _tier_theme_mult
                    _theme_annual_cap = {'대형주': 0.15, '중형주': 0.25, '소형주': 0.40}.get(tier, 0.25)
                    _base = min(_base, _theme_annual_cap)
                    _theme_adj -= _intensity * _base / 252

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

                # ★ PER 패널티 — 현실화
                # 실제 시장: 성장주 PER 100~200배도 수년 유지 가능 (테슬라, 카카오 등)
                # 패널티는 장기적 수렴 압력이지 단기 폭락이면 안 됨
                # 기존 최대 -5%/일은 PER 970배 종목을 연 -1260%로 만듦 → 비현실적
                # 수정: 최대 -0.5%/일 (연 -70% 수준이 상한)
                _per_tier_mult = {'대형주': 0.5, '중형주': 0.8, '소형주': 1.5}.get(tier, 0.8)
                if per > eff_limit * 3.0:
                    val_penalty = -min(0.005, (per - eff_limit) / eff_limit * 0.0008) * _per_tier_mult
                elif per > eff_limit * 2.5:
                    val_penalty = -min(0.003, (per - eff_limit) / eff_limit * 0.0005) * _per_tier_mult
                elif per > eff_limit * 1.5:
                    val_penalty = -min(0.0015, (per - eff_limit) / eff_limit * 0.0003) * _per_tier_mult
                elif per > eff_limit:
                    val_penalty = -min(0.0005, (per - eff_limit) / eff_limit * 0.0001) * _per_tier_mult
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

            # ★ [신규] [6] 거래량→주가 피드백 adj
            # 현실: 거래량이 평소 대비 클수록 방향성 강화
            # 거래량 폭발 = 확신의 증거 → 그 방향으로 추가 모멘텀
            daily_return += self._calc_volume_momentum_adj(name, daily_return, _vol_ratio_map)

            # ★ [신규] [7] 공매도 adj
            # 현실: short_interest 높은 종목 → 하락 압력 상시 존재
            #        주가 급등 시 숏커버 → 추가 급등(쇼트스퀴즈)
            daily_return += self._calc_short_selling_adj(name, meta, stock, tier, sector)

            # ★ 계절성 보정
            daily_return += self._get_seasonal_adj(cur_date, sector, tier)

            # ★ 52주 신고가 모멘텀
            if old_price > meta.get('price_52w_high', old_price) * 1.001:
                daily_return += 0.0003   # 신고가 돌파 모멘텀
            elif old_price < meta.get('price_52w_low', old_price) * 0.999:
                daily_return -= 0.0003   # 신저가 하향 압력

            # ★ 전 종목 ±30% (현실 상한가/하한가)
            cap = 0.30

            # ★ [신규] 초반 소형/중형주 하락 하한 — GRI 희석 방지
            # 게임 시작 3년 내 소형/중형주가 매일 -1%+ 빠져 GRI를 끌어내리는 문제 해소
            # 일별 -1.5% 하한: 개별 악재 없이 구조적으로 빠지는 것만 차단 (자연 하위25% 수준)
            if _is_early_game and ("소형" in tier or "중형" in tier):
                daily_return = max(-0.015, daily_return)

            # ★ 서킷브레이커: GRI 하루 -5% 이상이면 당일 변동폭 절반
            if _circuit_breaker:
                cap = 0.15

            # ★ [수정] 확장기 하락 하한선: 소형주/이벤트 종목은 제외
            # 기존 -5% 캡이 소형주 급락, 상한가 종목의 다음날 급락 등을 막았음
            # 대형주 확장기에만 -8%로 완화 적용 (소형/중형은 자유롭게)
            if cycle == "확장" and not is_depression:
                if "대형" in tier:
                    daily_return = max(-0.08, daily_return)   # -0.05 → -0.08 (대형만)
                # 중형/소형은 하락 제한 없음 → 상한가/하한가 자연 발생 가능
            daily_return = max(-cap, min(cap, daily_return))

            new_price = old_price * (1.0 + daily_return)
            new_price = max(10.0, new_price)
            if not math.isfinite(new_price): new_price = old_price

            # ★ 개별 종목 시총 상한 제거
            # 현실 주식시장에는 시총 하드캡이 없음
            # 과열 제어는 PER 압력 / 버핏지수 패널티 / anchor 브레이크로 처리
            _phase_now = getattr(self.s, '_last_processed_phase', '1A')
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

            # assets 상한: 초기값 30배 유지 (시총 연동 제거 — earnings 폭발 루프 방지)
            meta['assets'] = max(
                init_assets * 0.1,
                min(init_assets * 30, new_assets)
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
                # [수정] 절대 상한 하향: ROE 200% 같은 비현실적 수치 방지
                # 현실 최고 수준 기업(애플, 엔비디아): ROE 50~100%
                # efficiency 0.60은 ROE 수백 % 가능 → 0.30으로 제한
                _ABS_CAP = {
                    "Growth": 0.30, "Cyclical": 0.22,
                    "Value": 0.18,  "Defensive": 0.14,
                }.get(sector, 0.20)

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

        # ── 1위 집중도 패널티 — 주가에 직접 적용 ───────
        # 현실: 삼성전자도 최대 25~30% / 42% 같은 극단적 집중은 현실에 없음
        # 집중도 30% 초과 시 해당 종목 주가에 하락 압력 적용
        if self.s.stocks:
            _total_mc = sum(s['market_cap'] for s in self.s.stocks)
            _top1 = max(self.s.stocks, key=lambda s: s['market_cap'])
            _top1_ratio = _top1['market_cap'] / max(1, _total_mc)
            if _top1_ratio > 0.30:
                # 30% 초과분에 비례해서 주가 하락 압력
                # 40%면 -0.1%/일, 50%면 -0.2%/일 수준
                _penalty = min(0.003, (_top1_ratio - 0.30) * 0.01)
                _old = _top1['price']
                _top1['price'] = max(10, int(_old * (1.0 - _penalty)))
                _top1['market_cap'] = _top1['price'] * _top1['shares']
                # total_market_cap도 보정
                total_market_cap -= (_old - _top1['price']) * _top1['shares']

        # ── GRI 갱신 — 수정 기준시총 방식 (현실 코스피와 동일) ───────
        # 공식: GRI = (현재 시총 / 수정 기준시총) × 1000
        # 수정 기준시총: 신규상장/유상증자 시 기준시총도 같이 올려줌
        #               → 순수 주가 상승분만 GRI에 반영
        #               상폐 시: 기준시총에서 해당 종목 상폐 직전 시총 제거
        # 이렇게 하면 상폐 잡주가 GRI를 깎아먹는 왜곡이 사라짐
        import math as _math

        # 수정 기준시총 초기화 (게임 시작 시 최초 1회)
        if not hasattr(self.s, '_adj_base_cap') or self.s._adj_base_cap <= 0:
            self.s._adj_base_cap = float(self.s.initial_market_total_cap or total_market_cap or 1.0)

        # ★ 기준시총 현실 코스피 종목 수 차이 보정
        # 현실 코스피: 940개 종목, 기준시총 연간 증가율 약 14%
        # (신규상장+유상증자+주식수증가 합산, 실증 역산값)
        # 게임: 최대 400개 → 실제 반영 비율 400/940 = 42.6%
        # 나머지 57.4%는 기준시총에 가상으로 반영 (GRI 스케일 현실화)
        # 주가/시총/버핏지수는 완전히 그대로, GRI 숫자만 조정됨
        _real_market_growth_annual = 0.14          # 현실 기준시총 연간 증가율 (역산값)
        _game_coverage   = min(1.0, len(self.s.stocks) / 940.0)
        _missing_annual  = _real_market_growth_annual * (1.0 - _game_coverage)
        _daily_base_adj  = (1.0 + _missing_annual) ** (1.0 / 252) - 1.0
        self.s._adj_base_cap *= (1.0 + _daily_base_adj)

        # 현재 GRI = 현재시총 / 수정기준시총 × 1000
        _adj_base   = max(1.0, self.s._adj_base_cap)
        raw_gri_cap = (total_market_cap / _adj_base) * 1000.0

        # GRI 하한
        import math as _math
        _floor_absolute = 50.0
        _peak_for_floor = getattr(self.s, 'peak_gri', raw_gri_cap)
        _is_dep_scenario = ('대공황' in self.s.current_scenario)
        if _is_dep_scenario:
            _floor_relative = _peak_for_floor * 0.40
            _floor = max(_floor_absolute, _floor_relative)
        else:
            _floor = _floor_absolute

        self.s.prev_gri = self.s.gri
        self.s.gri = max(_floor, raw_gri_cap)
        if not _math.isfinite(self.s.gri):
            self.s.gri = self.s.prev_gri

        # peak_gri 갱신
        peak_gri = getattr(self.s, 'peak_gri', self.s.gri)
        if self.s.gri > peak_gri:
            self.s.peak_gri = self.s.gri

        # ★ 절대 경과일 카운터 갱신
        self.s._total_days_elapsed = getattr(self.s, "_total_days_elapsed", 0) + 1

        # ★ 위기 후 저점 추적
        _trough = getattr(self.s, "_gri_trough_after_crisis", self.s.gri)
        if self.s.gri < _trough:
            self.s._gri_trough_after_crisis = self.s.gri

        # ★ [수정] GDP 분기별 갱신 (매 분기 첫날) — 현실: GDP는 분기별 발표
        # 한국 GDP 현실 기준:
        #   확장기: +4~7% / 정점: +2~4% / 수축: -1~+2% / 저점: -3~+1%
        #   대공황: -6~-12% / LV2: 성장률 상향 / LV3+: 점차 안정화
        is_quarter_start = (cur_date.month in [1, 4, 7, 10]) and (cur_date.day == 1)
        if is_quarter_start:
            lv_base_growth = {1: 0.055, 2: 0.065, 3: 0.045, 4: 0.025}.get(lv, 0.055)

            # ★ GDP 성장률 현실화 — 한국 실제 기준
            # LV1(2000년대): 확장기 연 5~7% / LV2(2010년대): 연 4~6% / LV3+: 연 3~5%
            # 정점: 연 2~4% / 수축: 연 -1~+2% / 저점: 연 -3~+1% / 대공황: 연 -6~-12%
            _lv_gdp_bonus = {1: 0.000, 2: 0.003, 3: 0.005, 4: 0.004}.get(lv, 0.0)
            if is_depression:
                q_growth = random.uniform(-0.030, -0.015)
            else:
                q_growth = {
                    "확장": random.uniform(0.013, 0.018),   # 분기 +1.3~1.8% (연 +5~7%)
                    "정점": random.uniform(0.005, 0.010),   # 분기 +0.5~1.0% (연 +2~4%)
                    "수축": random.uniform(-0.003, 0.005),  # 분기 -0.3~+0.5%
                    "저점": random.uniform(-0.008, 0.003),  # 분기 -0.8~+0.3%
                }.get(cycle, lv_base_growth / 4)
            q_growth += _lv_gdp_bonus

            # ★ [신규] 호황/악재 시나리오 GDP 추가 보정
            scenario = self.s.current_scenario
            boom = getattr(self.s, 'boom_event', {})
            if boom.get('phase') == '진행중':
                btype = boom.get('type', '')
                if btype in ('수출호황', '반도체슈퍼사이클'):
                    q_growth += 0.005   # 수출호황 → GDP 추가 상승
                elif btype in ('유동성장세', '정부부양'):
                    q_growth += 0.003
            if any(x in scenario for x in ['전쟁', '팬데믹', '스태그']):
                q_growth -= 0.005

            prev_gdp = getattr(self.s, 'gdp', 600_000_000_000_000.0)

            # GDP 성장률 현실화 — 버핏지수 추격 가속 없음
            # 현실: GDP는 실물경제 기준으로만 성장, 시총과 무관
            # 버핏지수 정상화는 GDP가 올라가는 게 아니라 시총 drift 압력으로 처리
            self.s.gdp = prev_gdp * (1 + q_growth)
            self.s.gdp_growth_rate = q_growth * 4

            if not self.s.silent_mode:
                ann_pct = q_growth * 4 * 100
                q_num   = {1: 'Q4', 4: 'Q1', 7: 'Q2', 10: 'Q3'}.get(cur_date.month, '')
                if ann_pct < -2:
                    self.s.daily_news.append(
                        f"📉 [GDP 발표] {cur_date.year}년 {q_num} GDP 성장률 {ann_pct:+.1f}% "
                        f"(역성장 — 버핏지수 상승 압력)"
                    )
                elif ann_pct < 2:
                    self.s.daily_news.append(
                        f"📊 [GDP 발표] {cur_date.year}년 {q_num} GDP 성장률 {ann_pct:+.1f}% (저성장)"
                    )
                else:
                    self.s.daily_news.append(
                        f"📈 [GDP 발표] {cur_date.year}년 {q_num} GDP 성장률 {ann_pct:+.1f}%"
                    )

        # ★ 버핏 지수 갱신
        # ★ [수정] 400종목 보정: 게임은 400개 종목 기준이므로 시총이 코스피보다 낮음
        # 코스피 940개 중 상위 대형주가 시총 60%를 차지 → 게임 대형주 시총은 유사
        # 그러나 중소형 풀이 얇아서 전체 시총이 코스피 대비 구조적으로 낮음
        # 버핏지수를 그대로 쓰면 항상 저평가 → 보정 계수 적용
        # 보정: 코스피 940개 / 게임 현재 종목수 의 로그 비율로 완만하게 상향
        import math as _math_bi
        _cur_stock_count = max(1, len(self.s.stocks))
        _kospi_equiv     = 940
        # 종목 수가 적을수록 시총이 덜 집계됨 → 버핏지수 상향 보정
        # 400종목 기준: log(940/400)/log(940/1) ≈ 보정 약 1.25배
        # 종목 수가 많아질수록 보정 줄어듦 (400개 도달 시 ~1.20배)
        # ★ [수정] 보정 범위 축소 (버그3: 버핏지수 인위 상승 방지)
        _stock_count_adj = (_math_bi.log(_kospi_equiv) / _math_bi.log(max(2, _cur_stock_count))) ** 0.25
        _stock_count_adj = max(1.00, min(1.15, _stock_count_adj))  # 1.0~1.15배 (기존 1.1~1.4)

        gdp = getattr(self.s, 'gdp', 600_000_000_000_000.0)
        self.s.buffett_index = (total_market_cap / max(1.0, gdp)) * 100 * _stock_count_adj

        # ★ [수정] 버핏 지수 → 버블 지수 변환 현실화
        # 한국 코스피 버핏지수 역사적 범위:
        #   2000년대 초: ~40~60% (저평가)
        #   2007~2008: ~100~120% (과열)
        #   2020~2021: ~130~150% (버블)
        #   정상 범위: 70~100%
        # 기존 설정이 너무 낮은 버핏지수에서도 버블 압력을 줬음
        # → 80% 이하는 완전 정상, 120% 이상부터 버블 신호로 조정
        buffett = getattr(self.s, 'buffett_index', 0.0)

        if buffett < 60:
            target_bubble = buffett * 0.2                            # 0~12 (저평가)
        elif buffett < 100:
            target_bubble = 12 + (buffett - 60) * 0.7               # 12~40 (정상)
        elif buffett < 130:
            target_bubble = 40 + (buffett - 100) * 1.6              # 40~88 (주의)
        elif buffett < 170:
            target_bubble = 88 + (buffett - 130) * 1.8              # 88~160 (경고)
        elif buffett < 230:
            target_bubble = 160 + (buffett - 170) * 1.5             # 160~250 (위험)
        else:
            target_bubble = min(300.0, 250 + (buffett - 230) * 1.43)  # 230%+: 극단

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
        # ★ [수정] natural_decay 강화 — 버블 270 고착 방지
        # 기존: bi=270일 때 decay=-1.5, diff*0.05=+1.5 → 상쇄되어 고착
        # 수정: decay를 더 강하게 해서 고버블 시 반드시 하락 압력
        if bi >= 270:
            natural_decay = -3.0   # -1.5 → -3.0
        elif bi >= 240:
            natural_decay = -2.0   # -1.0 → -2.0
        elif bi >= 200:
            natural_decay = -1.0   # -0.5 → -1.0
        elif bi >= 150:
            natural_decay = -0.3   # -0.15 → -0.3
        else:
            natural_decay = 0.0

        bubble_delta_final = max(-6.0, min(2.0, bubble_diff * 0.05 + natural_decay))
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

        # 자산에서 이자 차감 — 하한: 초기 자산의 10% (earnings.py와 일관성)
        _init_assets_floor = meta.get('initial_assets', meta['assets']) * 0.10
        meta['assets'] = max(_init_assets_floor, meta['assets'] - daily_interest)

        # ★ 부채비율 독립 HP 차감 (금리 무관 — 부채 자체의 구조적 위험)
        # 부채비율 200% 이상이면 매일 소량 HP 차감
        # 현실: 이자보상배율 악화, 자금조달 비용 상승, 신용경색 리스크
        debt_ratio = meta.get('debt_ratio', 0.5)
        tier       = meta.get('tier', '소형주')

        # ★ [수정] 부채비율 HP 차감 완화
        # 기존: 200%+ 매일 -0.015 → 연 -3.78 HP. earnings.py와 중복 차감 → 폭주
        # 코스피 현실: 부채비율 200%는 위험하지만 즉시 망하지 않음 (대우조선해양 수년 버팀)
        # 수정: 차감량 절반 + 500% 이상 극단값만 강하게
        if debt_ratio >= 5.0:       # 500%+: 실질 자본잠식
            debt_hp_dmg = 0.03      # 기존 0.08 → 0.03
        elif debt_ratio >= 3.0:     # 300%+: 심각
            debt_hp_dmg = 0.015     # 기존 0.04 → 0.015
        elif debt_ratio >= 2.0:     # 200%+: 위험
            debt_hp_dmg = 0.005     # 기존 0.015 → 0.005
        else:
            debt_hp_dmg = 0.0

        # 대형주는 자금조달 능력이 있으므로 50% 완화
        if "대형" in tier:
            debt_hp_dmg *= 0.5

        if debt_hp_dmg > 0:
            meta['hp'] = max(0.0, meta.get('hp', 50.0) - debt_hp_dmg)

        # ★ 금리 + 고부채 → HP 추가 차감 (기존 7% → 3%로 완화)
        # Lv1 정상 금리(3~4%)에서도 고부채 기업 타격 가능하도록
        # ★ [수정] 금리+고부채 추가 차감 완화
        # 기존: 금리 3% + 부채 1.5 → 매일 -0.009 → 연 -2.27 HP (부채비율 차감과 중복)
        # 수정: 임계값 상향(5.0%+) + 계수 절반
        if interest_rate >= 5.0 and debt_ratio >= 2.0:
            extra_hp_dmg = (interest_rate - 5.0) * debt_ratio * 0.001
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
            "대형주": {"foreign": (0.05, 0.55), "inst": (0.10, 0.40), "retail": (0.05, 0.45)},
            "중형주": {"foreign": (0.01, 0.30), "inst": (0.05, 0.30), "retail": (0.15, 0.65)},
            "소형주": {"foreign": (0.00, 0.10), "inst": (0.01, 0.18), "retail": (0.25, 0.85)},
        }
        lim = LIMITS.get(tier, LIMITS["소형주"])

        foreign_delta = 0.0
        # ★ [수정] 수급 delta를 현실적 규모로 확대
        # 현실: 대형주 외국인 비중이 1~3개월 사이 5~10%p 이동은 흔함
        # 일별 최대 변동: 대형주 ±0.5~1%, 중형 ±0.3~0.6%, 소형 ±0.1~0.3%
        cycle_base = {
            "확장": +0.0008, "정점": 0.0,
            "수축": -0.0015, "저점": -0.0008,
        }.get(cycle, 0.0)
        if "대형" in tier:
            foreign_delta += cycle_base + random.gauss(0, 0.0035)   # 0.0012 → 0.0035
        elif "중형" in tier:
            foreign_delta += cycle_base * 0.5 + random.gauss(0, 0.0020)  # 0.0008 → 0.0020
        else:
            foreign_delta += random.gauss(0, 0.0006)  # 소형주: 외국인 거의 없음

        foreign_delta -= rate_delta * 0.0015   # 0.0008 → 0.0015 (금리 민감도 ↑)
        if macro.get('exchange_rate', 1100) > 1400: foreign_delta -= 0.0020   # 0.0008 → 0.0020
        elif macro.get('exchange_rate', 1100) < 1050: foreign_delta += 0.0015 # 0.0005 → 0.0015
        if meta.get('_earnings_just_released'):
            lc = meta.get('continuous_loss_count', 0)
            foreign_delta += -0.018 if lc >= 2 else (-0.008 if lc == 1 else +0.012)  # 2~3배 확대
        if "소형" in tier: foreign_delta *= 0.15
        if "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario:
            foreign_delta -= 0.0040   # 0.0015 → 0.0040

        # ★ 외국인 수급 지수 반영 (강도 상향)
        flow_idx = getattr(self.s, 'foreign_flow_index', 0.0)
        foreign_delta += flow_idx * 0.00015   # 0.00005 → 0.00015

        # ★ [신규] 외국인 연속 매수/매도 모멘텀 (추세 지속성)
        # 현실: 외국인은 한 번 방향 잡으면 수주~수개월 지속하는 경향
        f_trend = self.s.investor_trends.get(meta['c_name'], {}).get('foreign', {})
        f_trend_days = f_trend.get('days', 0)
        f_trend_dir  = f_trend.get('dir', 0)
        if f_trend_days >= 5 and f_trend_dir != 0:
            # 5일 이상 연속 추세 → 방향 강화 (최대 10일치 누적, 이후 수렴)
            momentum_boost = min(f_trend_days, 15) * 0.0002 * f_trend_dir
            foreign_delta += momentum_boost

        inst_delta = 0.0
        # ★ [수정] 기관: 외국인과 독립적 판단 강화 (반대 성향 완화)
        # 현실: 외국인과 기관이 같은 방향으로 움직이는 날도 많음
        if "대형" in tier:
            # 외국인 강매수 시 기관 차익실현 성향은 유지하되, 독립 노이즈 크게
            inst_delta += -foreign_delta * 0.25 + random.gauss(0, 0.0030)  # -0.4 → -0.25, 0.0010 → 0.0030
        elif "중형" in tier:
            inst_delta += random.gauss(0, 0.0020)   # 0.0008 → 0.0020
        else:
            inst_delta += random.gauss(0, 0.0012)   # 0.0005 → 0.0012

        if meta.get('_earnings_just_released'):
            lc = meta.get('continuous_loss_count', 0)
            inst_delta += -0.025 if lc >= 2 else (-0.010 if lc == 1 else +0.015)  # 2배 확대
        hp_r = meta.get('hp', 50.0) / max(1.0, meta.get('hp_soft_cap', 60.0))
        if hp_r < 0.15: inst_delta -= 0.010   # 0.004 → 0.010
        elif hp_r < 0.30: inst_delta -= 0.004  # 0.0015 → 0.004
        if bubble_index > 200: inst_delta -= 0.003   # 0.001 → 0.003
        elif bubble_index > 150: inst_delta -= 0.0015 # 0.0005 → 0.0015
        cm = self.s.current_date.month; cd = self.s.current_date.day
        if cm in [3, 6, 9, 12] and cd >= 25:
            inst_delta += -0.008 if meta.get('continuous_loss_count', 0) > 0 else +0.005
        tech_upgrade_year = getattr(self.s, '_tech_upgrade_years', {1: 1999}).get(
            self.s.max_tech_reached) or 1999
        if 0 <= self.s.current_date.year - tech_upgrade_year <= 2 and sector == "Growth":
            inst_delta += 0.008   # 0.003 → 0.008

        # ★ [신규] 기관 연속 추세 모멘텀
        i_trend = self.s.investor_trends.get(meta['c_name'], {}).get('inst', {})
        i_trend_days = i_trend.get('days', 0)
        i_trend_dir  = i_trend.get('dir', 0)
        if i_trend_days >= 3 and i_trend_dir != 0:
            inst_delta += min(i_trend_days, 10) * 0.00025 * i_trend_dir

        retail_delta = 0.0
        # ★ [수정] 개인: 역매매 성향 + 노이즈 (규모 현실화)
        if "대형" in tier:
            retail_delta += -(foreign_delta + inst_delta) * 0.3 + random.gauss(0, 0.0025)  # 0.0008 → 0.0025
        else:
            retail_delta += random.gauss(0, 0.0045)   # 0.0015 → 0.0045

        if   rate < -0.05: retail_delta += 0.025   # 0.010 → 0.025 급락 시 강한 저점매수
        elif rate < -0.02: retail_delta += 0.010   # 0.004 → 0.010
        elif rate >  0.05: retail_delta -= 0.012   # 0.005 → 0.012 급등 시 강한 차익실현
        elif rate >  0.02: retail_delta -= 0.005   # 0.002 → 0.005
        sentiment = getattr(self.s, 'sentiment', 50.0)
        if sentiment > 75 and bubble_index > 150: retail_delta += 0.005   # 0.002 → 0.005
        elif sentiment < 25: retail_delta -= 0.003  # 0.001 → 0.003

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

        # ★ [수정] 거래량 계산 현실화
        # 현실: 평소 대비 어닝/테마/급등 당일은 5~50배 거래량 폭발
        base_turnover = {
            "대형주": random.uniform(0.005, 0.012),
            "중형주": random.uniform(0.010, 0.025),   # 0.008~0.020 → 0.010~0.025
            "소형주": random.uniform(0.020, 0.060),   # 0.015~0.040 → 0.020~0.060
        }.get(tier, 0.010)

        # ★ [수정] 등락률 기반 거래량 배율 현실화
        # 기존: 1.0 + rate * 0.3 (최대 ~10배)
        # 수정: 등락률 크기에 따라 지수적으로 증가
        abs_rate_pct = abs(stock.get('rate', 0.0))
        if abs_rate_pct >= 25:
            vol_mult = random.uniform(15.0, 50.0)   # 상/하한가 근접: 15~50배
        elif abs_rate_pct >= 15:
            vol_mult = random.uniform(6.0, 20.0)    # 급등락: 6~20배
        elif abs_rate_pct >= 8:
            vol_mult = random.uniform(3.0, 8.0)     # 큰 등락: 3~8배
        elif abs_rate_pct >= 3:
            vol_mult = random.uniform(1.5, 4.0)     # 소폭: 1.5~4배
        else:
            vol_mult = random.uniform(0.7, 1.5)     # 보합: 평소 수준

        # ★ 어닝 발표일: 추가 거래량 폭발
        if meta.get('_earnings_just_released'):
            vol_mult *= random.uniform(2.0, 5.0)

        # ★ 소형주 테마 스파이크
        if meta.get('_theme_spike') and "소형" in tier:
            vol_mult *= random.uniform(3.0, 10.0)

        turnover  = min(0.80, base_turnover * vol_mult)   # 상한 0.50 → 0.80
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
    # 상장폐지 (완화 버전)
    # ─────────────────────────────────────────────
    def check_delisting(self):
        MIN_STOCKS_LIMIT = 50

        if len(self.s.stocks) <= MIN_STOCKS_LIMIT:
            return

        is_depression = (
            any("대공황" in str(v) for v in [self.s.current_scenario])
            and "극복" not in self.s.current_scenario
        )

        # ★ [수정] 연간 상폐 상한 — 400종목 기준 현실화
        # 코스피 940개 기준 연 30~40개(3.2~4.3%) → 400개 기준 비례 적용
        # 정상: 400 × 3.2% ≈ 13개 / 대공황: 400 × 5% ≈ 20개
        # 단, HP=0 종목은 상한 예외 처리 (좀비주 누적 방지)
        _cur_year    = self.s.current_date.year
        _annual_key  = f"_delist_count_{_cur_year}"
        _annual_used = getattr(self.s, _annual_key, 0)
        # ★ [수정] 연간 상폐 상한 상향 (버그: 10원 좀비 누적 방지)
        # 코스피 현실: 연 40~50개 상폐, 400종목 기준 비례 상향
        _annual_max  = 30 if is_depression else 20   # 20/13 → 30/20

        if _annual_used >= _annual_max:
            # 연간 상한 초과 — pending만 처리하고 신규 상폐 없음
            _process_pending_only = True
        else:
            _process_pending_only = False

        # ★ [수정] HP=0 종목은 연간 상한 무관하게 반드시 워크아웃 큐 등록 (좀비 누적 방지)
        # 10원 고착 종목(is_doomed 예정)은 grace 기간 단축
        for stock in list(self.s.stocks):
            hp_now = stock['meta'].get('hp', 99.0)
            name   = stock['meta']['c_name']
            if hp_now <= 0.0 and not stock['meta'].get('is_doomed'):
                remaining = len(self.s.stocks)
                if remaining <= MIN_STOCKS_LIMIT:
                    break
                # [수정] 10원 고착이면 유예 단축 (5~10일), 아니면 기존 유예
                _stuck = stock['meta'].get('_price_low_days', 0)
                _grace_days = random.randint(5, 10) if _stuck >= 30 else random.randint(63, 105)
                delist_date = self.s.current_date + timedelta(days=_grace_days)
                self.s.pending_events["delist"][name] = {
                    "date":   delist_date,
                    "reason": "재무 완전 파탄 (워크아웃 실패)"
                }
                stock['meta']['is_doomed'] = True
                _annual_used += 1
                setattr(self.s, _annual_key, _annual_used)
                if not self.s.silent_mode:
                    self.s.daily_news.append(
                        f"⚠️ [워크아웃 개시] {name} HP 소진 — "
                        f"{delist_date.strftime('%Y-%m-%d')} 상폐 예정 (회생 가능성 있음)"
                    )

        delisted_this_turn = []
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

            if _process_pending_only:
                continue

            ld = meta.get('listed_date_dt')
            if not ld or isinstance(ld, str):
                try:    ld = datetime.strptime(meta['listed_date'], '%Y-%m-%d')
                except: ld = self.s.current_date
                meta['listed_date_dt'] = ld

            hp_now      = meta.get('hp', 50.0)
            is_bankrupt = hp_now <= 0.0

            if is_bankrupt and not meta.get('is_doomed'):
                    # ★ [수정] 즉시상폐 → 유예 큐로 리다이렉트 (워크아웃 처리로 이미 처리됨)
                    remaining = len(self.s.stocks) - len(delisted_this_turn) - new_reserved
                    if remaining > MIN_STOCKS_LIMIT and _annual_used < _annual_max:
                        _grace_days = random.randint(63, 105)
                        delist_date = self.s.current_date + timedelta(days=_grace_days)
                        self.s.pending_events["delist"][name] = {
                            "date":   delist_date,
                            "reason": "재무 완전 파탄 (워크아웃 실패)"
                        }
                        meta['is_doomed'] = True
                        new_reserved += 1
                        _annual_used += 1
                        setattr(self.s, _annual_key, _annual_used)
                        if not self.s.silent_mode:
                            self.s.daily_news.append(
                                f"⚠️ [워크아웃 개시] {name} HP 소진 — "
                                f"{delist_date.strftime('%Y-%m-%d')} 상폐 예정"
                            )
                    continue

        for ds in delisted_this_turn:
            if ds in self.s.stocks:
                ds['meta']['delisted_date']          = self.s.current_date.strftime('%Y-%m-%d')
                ds['meta']['is_officially_delisted'] = True
                self.s.delisted_stocks.append(ds)
                if hasattr(self, '_db') and self._db:
                    self._db.save_delisted_stock(ds)
                # ★ 상폐 → 수정 기준시총에서 해당 종목 상폐 직전 시총 제거
                # 현실 코스피와 동일: 상폐 종목이 GRI를 깎지 않도록 기준시총도 같이 줄임
                _delist_cap = float(ds.get('market_cap', 0))
                if _delist_cap > 0:
                    self.s._adj_base_cap = max(1.0, getattr(self.s, '_adj_base_cap', _delist_cap) - _delist_cap)
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
                # ★ 신규상장 → 수정 기준시총에 해당 종목 시총 추가
                # 현실 코스피와 동일: 신규상장은 GRI에 영향 없음 (순수 주가 상승분만 반영)
                _ipo_cap = float(stock.get('market_cap', 0))
                if _ipo_cap > 0:
                    self.s._adj_base_cap = getattr(self.s, '_adj_base_cap', 0.0) + _ipo_cap
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
        # ★ [수정] 초반 종목 수 구간 세분화 — 50개 미만은 별도 완속 처리
        # 기존: 200개 이하 = 6~8개/주 → 50개에서도 6~8개 쏟아져 소형주 희석 폭락
        # 수정: 100개 미만은 2~3개/주, 100~150개는 3~5개/주로 천천히 키움
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
        elif cur_count >= 150:
            ipo_min, ipo_max = 3, 5   # ★ 신규: 150~200 구간
            mid_ratio = 0.35
        elif cur_count >= 100:
            ipo_min, ipo_max = 2, 4   # ★ 신규: 100~150 구간
            mid_ratio = 0.40          # ★ 중형주 비율 높임 (소형주 희석 방지)
        else:
            ipo_min, ipo_max = 1, 3   # ★ 수정: 100개 미만은 1~3개 (기존 6~8개)
            mid_ratio = 0.50          # ★ 초반은 중형주 위주로 상장

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
            # ★ [수정] price < 1000 차감 제거 — 신규 상장 소형주 초기가가 낮아서
            # 정상적인 종목도 매일 깎이는 문제 (1000원 미만이면 전체 소형주 해당)

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

        # ★ 절대 주가 기준 HP 차감
        # ★ [수정] 완화 — 주가 저가가 매일 폭발적으로 HP를 깎아 상폐 2000개 유발
        # 현실: 코스피 저가주도 즉시 퇴출 아님, 관리종목 지정 후 수개월 유예
        # 10원: -0.5/일 (soft_cap 60 기준 120일 내 HP=0, 4개월)
        # 10~50원: -0.2/일 (300일, 약 1년)
        # 50~100원: -0.05/일 (완만한 압박)
        hp_now = meta.get('hp', 50.0)
        if hp_now > 0.0:
            if   price <= 10:  meta['hp'] = max(0.0, hp_now - 0.8)   # 0.5→0.8: 75일 내 HP 소진
            elif price < 50:   meta['hp'] = max(0.0, hp_now - 0.25)
            elif price < 100:  meta['hp'] = max(0.0, hp_now - 0.05)
        # ★ [수정] 저가 좀비주 복합 조건 — 10원 정확히 아닌 저가+HP 기준 (버그: 10~27 왕복)
        # 50원 미만 저가에서 매일 누적, 10원 초과해도 50원 미만이면 카운터 유지
        _is_cheap = price < 50
        if _is_cheap:
            _stuck_days = meta.get('_price_low_days', 0) + 1
            meta['_price_low_days'] = _stuck_days
            # [수정] 30일(1개월) 이상 50원 미만 + HP 50% 미만: 강제 HP 0
            # 현실 코스피: 주가 50원 미만 + 30일 지속 → 관리종목, 이후 상폐
            # 기존 126일(6개월)은 너무 느려서 좀비주 137개 누적됨
            _hp_low = meta.get('hp', 50.0) / max(1.0, meta.get('hp_soft_cap', 60.0)) < 0.50
            if _stuck_days >= 30 and _hp_low and not meta.get('is_doomed'):
                meta['hp'] = 0.0
        else:
            meta['_price_low_days'] = 0

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
            if retail > 0.50 and float_ratio < 0.50:
                rate = stock.get('rate', 0.0)
                # ★ [수정] 유동성 함정 상한: ±2% (기존 무한 증폭 → 억제)
                return max(-0.02, min(0.02, (rate / 100.0) * 0.08))
            elif retail > 0.40:
                rate = stock.get('rate', 0.0)
                return max(-0.015, min(0.015, (rate / 100.0) * 0.04))
        elif "중형" in tier:
            if retail > 0.45 and float_ratio < 0.55:
                rate = stock.get('rate', 0.0)
                return max(-0.008, min(0.008, (rate / 100.0) * 0.03))

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

    # ─────────────────────────────────────────────
    # ★ [신규] 거래량 → 주가 피드백 adj
    # ─────────────────────────────────────────────
    def _calc_volume_momentum_adj(self, name: str, current_return: float,
                                   vol_ratio_map: dict) -> float:
        """
        거래량이 평소보다 클수록 현재 방향성을 강화.

        현실 근거:
        - 거래량 폭발 + 상승 = 강한 매수 수요 → 추가 상승 모멘텀
        - 거래량 폭발 + 하락 = 패닉셀 / 공매도 집중 → 추가 하락
        - 거래량 극도 저조 + 상승 = 유동성 부족, 신뢰 낮은 상승 → 약한 adj
        - 이른바 "거래량은 주가에 선행한다" 원칙

        vol_ratio 기준:
        - 1.0 이하: 평소 이하 거래 → adj 없음(중립)
        - 2~5배: 소폭 방향 강화
        - 5~15배: 중간 강화 (테마/뉴스 장)
        - 15배+: 강한 강화 (상한가 급등, 패닉셀)
        """
        vol_ratio = vol_ratio_map.get(name, 1.0)

        # 거래량이 평소 이하면 adj 없음
        if vol_ratio <= 1.2:
            return 0.0

        # 방향: 현재 daily_return 부호
        direction = 1.0 if current_return > 0 else -1.0

        # 강도: vol_ratio에 따라 로그 스케일 adj
        # vol_ratio=3 → 0.003, vol_ratio=10 → 0.007, vol_ratio=30 → 0.012
        import math as _math
        strength = min(0.015, _math.log(vol_ratio, 3) * 0.004)

        # 소형주일수록 거래량 영향 큼 (유동성 낮아 가격 충격 큼)
        # 단, tier는 호출 시점에서 meta로 접근
        return strength * direction

    # ─────────────────────────────────────────────
    # ★ [신규] 공매도 adj
    # ─────────────────────────────────────────────
    def _calc_short_selling_adj(self, name: str, meta: dict,
                                 stock: dict, tier: str, sector: str) -> float:
        """
        공매도(Short Selling) 효과 시뮬레이션.

        현실 메커니즘:
        1. short_interest(공매도 잔고비율): 유통주식 대비 공매도 비율
           - 높을수록 하락 베팅이 많다는 의미 → 상시 하락 압력
        2. 숏커버(Short Squeeze):
           - 공매도 많은 종목이 급등하면 숏포지션 청산 강제
           - → 추가 매수 발생 → 더 큰 급등 (감마 스퀴즈)
        3. 공매도 증가 조건:
           - 버블지수 높음, PER 고평가, HP 낮음, 수축/저점기
        4. 공매도 감소 조건:
           - 주가 급락 (숏커버), 어닝 서프라이즈, 호재 이벤트

        코스피 현실:
        - 대형주 short_interest: 0.5~3%
        - 중형주: 1~5%
        - 소형주 테마주: 0.5~2% (대차가 어려워 낮음)
        - 버블/고평가 종목: 최대 10~15%
        """
        # short_interest 초기화
        if 'short_interest' not in meta:
            if "대형" in tier:
                meta['short_interest'] = random.uniform(0.005, 0.025)
            elif "중형" in tier:
                meta['short_interest'] = random.uniform(0.008, 0.040)
            else:
                meta['short_interest'] = random.uniform(0.003, 0.015)

        si         = meta['short_interest']
        rate       = stock.get('rate', 0.0)
        bubble     = getattr(self.s, 'bubble_index', 0.0)
        cycle      = getattr(self.s, 'cycle_stage', '확장')
        hp_ratio   = meta.get('hp', 50.0) / max(1.0, meta.get('hp_soft_cap', 60.0))

        adj = 0.0

        # ── 1. 상시 하락 압력 (공매도 잔고 존재 자체) ──────────
        # 현실: 공매도 잔고가 많으면 매도 오버행이 지속됨
        adj -= si * 0.08   # SI 5% → 일별 -0.004 (연 -1%)

        # ── 2. 숏커버(쇼트스퀴즈) — 급등 시 강제 청산 ──────────
        # 현실: 하루 5% 이상 급등 시 손절/강제청산 발동
        if rate >= 8.0 and si >= 0.02:
            # 숏포지션 강제청산 → 추가 매수 → 급등 가속
            squeeze_power = si * random.uniform(0.5, 1.5)
            squeeze_adj   = min(0.05, squeeze_power * 0.8)
            adj += squeeze_adj
            # 숏커버 후 잔고 감소
            meta['short_interest'] = max(0.001, si * 0.70)
            if not self.s.silent_mode and squeeze_adj > 0.02:
                self.s.daily_news.append(
                    f"⚡ [숏스퀴즈] {name} 공매도 청산 러시! "
                    f"공매도잔고 {si*100:.1f}% → 강제 매수 급등"
                )
        elif rate >= 4.0 and si >= 0.01:
            # 소규모 숏커버
            meta['short_interest'] = max(0.001, si * 0.92)
            adj += si * 0.15

        # ── 3. 공매도 증가 조건 ──────────────────────────────
        # 고평가 + 버블 + 수축기 = 공매도 세력 유입
        if bubble > 180 and cycle in ('수축', '저점') and hp_ratio < 0.6:
            si_increase = random.uniform(0.0002, 0.0008)
            meta['short_interest'] = min(0.15, si + si_increase)

        # HP 낮은 종목 (펀더멘털 악화) = 공매도 타깃
        elif hp_ratio < 0.30 and "대형" not in tier:
            si_increase = random.uniform(0.0003, 0.0010)
            meta['short_interest'] = min(0.15, si + si_increase)

        # ── 4. 공매도 자연 감소 (숏포지션 만기/청산) ──────────
        # 공매도는 무한정 유지할 수 없음 — 매일 소폭 감소
        else:
            meta['short_interest'] = max(0.001, si * 0.998)

        # ── 5. 공매도 금지 시나리오 (한국 특유) ──────────────
        # 금융위기/대공황 시 한국은 공매도 금지를 자주 시행
        scenario = self.s.current_scenario
        if any(x in scenario for x in ['대공황', '금융위기', '외부충격', '긴축 쇼크']):
            # 공매도 금지 효과: 하락 압력 완화, 단 인위적 가격 왜곡
            adj *= 0.2   # 공매도 효과 80% 감쇠
            meta['short_interest'] = max(0.001, meta['short_interest'] * 0.90)

        return adj