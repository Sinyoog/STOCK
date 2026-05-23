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
    "Growth":    50.0,
    "Value":     20.0,
    "Defensive": 30.0,
    "Theme":     40.0,
}

# ★ 신용등급별 이자비용 가중치
_CREDIT_COST = {
    "AA":  1.0,
    "BB":  1.3,
    "CCC": 1.8,
}


class StockMarket:
    def __init__(self, state, economy, company_mgr):
        self.s   = state
        self.eco = economy
        self.cm  = company_mgr

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
                "확장": +0.00060,   # 상향: 변동성 드래그 상쇄
                "정점": +0.00025,
                "수축": -0.00005,   # 완화: 거의 횡보
                "저점": +0.00010,
            }.get(cycle, +0.00035)

        # ★ 확장기 초반(2000~2005) 성장 모멘텀 보너스
        if cur_date.year <= 2005 and cycle == "확장":
            market_drift += 0.00020

        prev_rate  = getattr(self.s, '_prev_macro_snapshot', {}).get('interest_rate', interest_rate)
        rate_delta = interest_rate - prev_rate
        if   rate_delta >  0.1: market_drift -= 0.00020
        elif rate_delta < -0.1: market_drift += 0.00020
        market_drift += (sentiment - 50) / 50 * 0.00008

        # ★ 버블 drift 보정 — 상승 중일 때만 억제 적용
        # 이미 하락 중이면 건드리지 않음 (폭락 방지)
        if market_drift > 0:
            if   bubble_index >= 280: market_drift *= -0.3
            elif bubble_index >= 250: market_drift *= -0.1
            elif bubble_index >= 200: market_drift *=  0.3
            elif bubble_index >= 150: market_drift *=  0.7

        peak_gri    = getattr(self.s, 'peak_gri', self.s.gri)
        if self.s.gri > peak_gri: self.s.peak_gri = peak_gri = self.s.gri
        gri_ratio   = self.s.gri / max(1.0, peak_gri)
        panic_factor = 0.97 if gri_ratio < 0.60 else (0.99 if gri_ratio < 0.80 else 1.0)  # 완화

        fear_mult = 1.0
        if self.s.macro.get("fear_index", 10) >= 50 and is_depression:
            fear_mult = 1.3

        tech_upgrade_year = getattr(self.s, '_tech_upgrade_year', 1999)

        # ★ 산업별 경쟁도 갱신
        self._update_industry_competition()

        # ★ 시장 전체 PER 계산용 (버블 지수 갱신용)
        for stock in self.s.stocks:
            meta = stock['meta']
            name = meta['c_name']
            hist = self.s.earnings_history.get(name, {})
            annual_ni = self._calc_annual_net_income(name, hist)
            if annual_ni > 0:
                total_net_income += annual_ni

        for stock in self.s.stocks:
            meta      = stock['meta']
            name      = meta['c_name']
            old_price = float(stock['price'])
            tier      = meta.get('tier', '소형주')
            sector    = SECTOR_MAP.get(meta.get('ind', ''), 'Value')

            # 체급별 변동성 (소형주 0.020 → 0.015로 축소)
            if   "대형" in tier: vol = 0.008; tier_mult = 0.8
            elif "중형" in tier: vol = 0.012; tier_mult = 1.0
            else:                vol = 0.015; tier_mult = 1.2

            eff   = meta.get('efficiency', 0.05) * tier_mult
            alpha = (eff - 0.05) * 0.05

            # 섹터/테크 조정
            sector_adj = 0.0
            if lv >= 2 and sector == "Growth":
                sector_adj += 0.05 / 252
            elif lv >= 3 and sector == "Value":
                sector_adj -= 0.03 / 252

            years_since_lv_up = cur_date.year - tech_upgrade_year
            if 0 <= years_since_lv_up <= 3:
                if   sector == "Growth":    sector_adj += 0.05 / 252
                elif sector == "Defensive": sector_adj += 0.01 / 252

            cycle_sector = {
                ("확장", "Growth"):    +0.04 / 252,   # 상향
                ("확장", "Value"):     +0.02 / 252,   # 확장기 Value도 상승
                ("확장", "Defensive"): -0.01 / 252,   # 완화: -0.02 → -0.01
                ("수축", "Defensive"): +0.02 / 252,
                ("수축", "Growth"):    -0.01 / 252,
                ("저점", "Value"):     +0.02 / 252,
                ("저점", "Growth"):    +0.01 / 252,   # 저점에서 성장주도 소폭 회복
            }.get((cycle, sector), 0.0)
            sector_adj += cycle_sector

            # ★ 산업 경쟁 패널티
            ind_count = self.s.industry_competition.get(meta.get('ind', ''), 1)
            if ind_count > 20:
                competition_penalty = min(0.02, (ind_count - 20) * 0.001) / 252
                sector_adj -= competition_penalty

            # 실적 신호
            earnings_adj = 0.0
            hist   = self.s.earnings_history.get(name, {})
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
                        earnings_adj  = min(0.0003, (last_ni / assets) * 0.5) / 252
                        # total_net_income은 첫 번째 루프에서만 계산 (이중 계산 방지)
                    else:
                        # 4분기 이상 실적 데이터 있을 때만 적자 압력 (초반 보호)
                        hist_q_count = sum(len(qd) for qd in hist.values())
                        if hist_q_count >= 4:
                            revenue      = max(1.0, last_q_data[last_q].get('revenue', assets * 0.05))
                            loss_ratio   = abs(last_ni) / revenue
                            earnings_adj = max(-0.0005, -loss_ratio * 0.3) / 252
                            if loss_cnt >= 3:
                                earnings_adj *= 1.5

            # HP 패닉 + HP → 주가 하락 압력
            hp       = meta.get('hp', 50.0)
            soft_cap = meta.get('hp_soft_cap', 60.0)
            hp_ratio = hp / max(1.0, soft_cap)
            shield   = meta.get('shield', 0.0)

            if   hp_ratio <= 0.0:  hp_panic = 0.70
            elif hp_ratio < 0.15:  hp_panic = 0.85
            elif hp_ratio < 0.40:  hp_panic = 0.95
            else:                  hp_panic = 1.0

            # ★ HP → 주가 하락 압력 (HP 낮을수록 주가 하락)
            hp_pressure = 0.0
            if   hp_ratio < 0.15: hp_pressure = -0.003
            elif hp_ratio < 0.30: hp_pressure = -0.001
            elif hp_ratio < 0.50: hp_pressure = -0.0003

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
            ) * panic_factor * hp_panic * fear_mult

            pump = meta.pop('_hp_overflow_pump', 0.0)
            daily_return += pump

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
                if per > per_limit * 1.5:
                    val_penalty = -min(0.005, (per - per_limit * 1.5) / per_limit * 0.003)
                    daily_return += val_penalty
                elif per > per_limit:
                    val_penalty = -min(0.002, (per - per_limit) / per_limit * 0.001)
                    daily_return += val_penalty
                elif per < per_limit * 0.5:
                    daily_return += 0.0002
            elif annual_ni < 0:
                hist_ni_count = len([x for yv in hist.values() for x in yv.values()])
                if hist_ni_count >= 4:
                    loss_cnt     = meta.get('continuous_loss_count', 0)
                    loss_ratio   = abs(annual_ni) / max(1.0, assets_v)
                    base_penalty = -min(0.003, loss_ratio * 0.3)
                    loss_mult    = min(2.0, 1.0 + loss_cnt * 0.1)
                    daily_return += base_penalty * loss_mult

            # ★ 주가 → HP 차감 (-80~95% 설계, 방어막 미적용)
            # initial_price 없으면 52주 신고가 또는 assets/주식수로 추정
            initial_price = meta.get('initial_price', 0)
            if initial_price <= 10:
                # 기존 세이브 파일 대응: 52주 신고가 또는 assets 기반 추정
                p52h = meta.get('price_52w_high', 0)
                if p52h > 10:
                    initial_price = p52h
                else:
                    # assets / 주식수로 추정
                    shares = max(1, stock.get('shares', 1))
                    initial_price = meta.get('assets', old_price * shares) / shares
                initial_price = max(old_price, initial_price)

            if initial_price > 10:
                price_drop = 1.0 - (old_price / max(1.0, initial_price))

                if   price_drop > 0.95: hp_price_drain = 5.0
                elif price_drop > 0.85: hp_price_drain = 2.0
                elif price_drop > 0.70: hp_price_drain = 0.5
                elif price_drop > 0.50: hp_price_drain = 0.1
                else:                   hp_price_drain = 0.0

                if hp_price_drain > 0:
                    cur_hp = meta.get('hp', 50.0)
                    # 방어막 미적용: 직접 HP 차감
                    meta['hp'] = max(0.0, cur_hp - hp_price_drain)

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

            # ★ 계절성 보정
            daily_return += self._get_seasonal_adj(cur_date, sector, tier)

            # ★ 52주 신고가 모멘텀
            if old_price > meta.get('price_52w_high', old_price) * 1.001:
                daily_return += 0.0003   # 신고가 돌파 모멘텀
            elif old_price < meta.get('price_52w_low', old_price) * 0.999:
                daily_return -= 0.0003   # 신저가 하향 압력

            if   "대형" in tier: cap = 0.08
            elif "중형" in tier: cap = 0.12
            else:                cap = 0.12

            # ★ 고주가 종목 추가 변동성 제한 (SKT형 급락 방지)
            if old_price >= 1_000_000:
                cap = min(cap, 0.05)   # 주가 100만원 이상: 일 5% 상한
            elif old_price >= 200_000:
                cap = min(cap, 0.07)   # 주가 20만원 이상: 일 7% 상한

            # ★ 확장기에는 하루 하락 하한선 적용 (무조건 하락 방지)
            if cycle == "확장" and not is_depression:
                daily_return = max(-0.03, daily_return)
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
                min(init_assets * 100, new_assets)  # 상한선: 초기값의 100배
            )

            # ★ 부채 갱신 (금리 연동 이자비용)
            self._update_debt(stock, interest_rate)

            rate_raw            = (new_price / old_price - 1) * 100
            stock['price']      = int(new_price)
            stock['rate']       = round(rate_raw, 2) if math.isfinite(rate_raw) else 0.0
            stock['market_cap'] = stock['price'] * stock['shares']
            total_market_cap   += stock['market_cap']

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
        import math as _math
        tier_weights = {"대형주": 3.0, "중형주": 1.5, "소형주": 0.2}  # 변동성 드래그 방지
        w_sum = 0.0; wr_sum = 0.0
        total_w_cap = sum(
            tier_weights.get(s['meta'].get('tier', '소형주'), 0.5) * s['market_cap']
            for s in self.s.stocks
        )

        for stock in self.s.stocks:
            r = stock.get('rate', 0.0)
            if not _math.isfinite(r): continue

            # ★ 신규 상장 30일 미만 종목 GRI 계산 제외 (상장 러시로 인한 하락 방지)
            try:
                ld = stock['meta'].get('listed_date_dt')
                if not ld:
                    ld = __import__('datetime').datetime.strptime(
                        stock['meta']['listed_date'], '%Y-%m-%d')
                if (cur_date - ld).days < 30:
                    continue
            except Exception:
                pass

            w = tier_weights.get(stock['meta'].get('tier', '소형주'), 0.2)

            # ★ 단일 종목 GRI 기여 상한 10% (고주가 종목 왜곡 방지)
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
        # lv_target: 2045년 Lv3 기준 목표 25,000~30,000 역산
        # Lv1(~2015): 연 8%, Lv2(2015~2035): 연 10%, Lv3(2035~): 연 9%
        lv_target = {
            1: 1000 * (1.08 ** years_elapsed),
            2: 1000 * (1.08 ** 15) * (1.10 ** max(0, years_elapsed - 15)),
            3: 1000 * (1.08 ** 15) * (1.10 ** 20) * (1.09 ** max(0, years_elapsed - 35)),
            4: 1000 * (1.08 ** 15) * (1.10 ** 20) * (1.09 ** 25) * (1.13 ** max(0, years_elapsed - 60)),
        }.get(lv, 1000.0)

        anchor = self.s.gri / max(1.0, lv_target)
        # anchor 브레이크: 오버슈팅 방지 (단계적 강화)
        if   anchor > 5.0: weighted_avg_rate -= 0.0050
        elif anchor > 3.0: weighted_avg_rate -= 0.0025
        elif anchor > 2.0: weighted_avg_rate -= 0.0012
        elif anchor > 1.5: weighted_avg_rate -= 0.0005
        elif anchor < 0.3: weighted_avg_rate += 0.0020
        elif anchor < 0.5: weighted_avg_rate += 0.0012
        elif anchor < 0.7: weighted_avg_rate += 0.0006

        # ★ 초반 2년 추가 보정 (상장 러시 + 데이터 부족으로 인한 하락 방지)
        if cur_date.year <= 2002 and anchor < 0.8:
            weighted_avg_rate += 0.0008

        # ★ prev_gri는 GRI 갱신 전에 저장 (등락률 계산용)
        self.s.prev_gri = self.s.gri

        raw_gri = self.s.gri * (1.0 + weighted_avg_rate)
        if not _math.isfinite(raw_gri): raw_gri = self.s.gri
        self.s.gri = max(100.0, raw_gri)

        # ★ GDP 연간 성장 (매년 1월 1일)
        if cur_date.month == 1 and cur_date.day == 1:
            gdp_growth = getattr(self.s, 'gdp_growth_rate', 0.05)
            # 기술 레벨별 GDP 성장률 보정
            lv_growth = {1: 0.05, 2: 0.07, 3: 0.06, 4: 0.09}.get(lv, 0.05)
            self.s.gdp = getattr(self.s, 'gdp', 600_000_000_000_000.0) * (1 + lv_growth)

        # ★ 버핏 지수 갱신
        gdp = getattr(self.s, 'gdp', 600_000_000_000_000.0)
        self.s.buffett_index = (total_market_cap / max(1.0, gdp)) * 100

        # ★ 버블 지수: GRI / lv_target 비율 기반으로 재계산
        # prev_gri는 이미 위에서 저장됨
        prev_gri_v = getattr(self.s, 'prev_gri', self.s.gri)
        gri_change = (self.s.gri - prev_gri_v) / max(1.0, prev_gri_v)

        years_elapsed_bi = max(0, cur_date.year - 2000)
        lv_target_bi = {
            1: 1000 * (1.08 ** years_elapsed_bi),
            2: 1000 * (1.08 ** 15) * (1.10 ** max(0, years_elapsed_bi - 15)),
            3: 1000 * (1.08 ** 15) * (1.10 ** 20) * (1.09 ** max(0, years_elapsed_bi - 35)),
            4: 1000 * (1.08 ** 15) * (1.10 ** 20) * (1.09 ** 25) * (1.13 ** max(0, years_elapsed_bi - 60)),
        }.get(lv, 1000.0)

        # T3 유지: 목표값 낮게 (박스권)
        if "T3 유지" in self.s.current_scenario:
            lv_target_bi *= 0.6
        # 대공황 극복: 목표값 T3보다 낮게
        elif "극복" in self.s.current_scenario:
            lv_target_bi *= 0.5

        # 버블 = (GRI / lv_target - 1.0) × 200
        # GRI가 목표치 2배 → 버블 200, 목표치와 같으면 0, 목표 미만이면 음수→0
        gri_ratio_bi  = self.s.gri / max(1.0, lv_target_bi)
        target_bubble = max(0.0, (gri_ratio_bi - 1.0) * 200.0)

        # 실제 버블 지수를 목표값으로 서서히 수렴 (급변 방지)
        bi          = getattr(self.s, 'bubble_index', 0.0)
        bubble_diff = target_bubble - bi
        # 하루 최대 변화폭: +2.0 / -3.0 (하락이 더 빠르게)
        bubble_delta_final = max(-3.0, min(2.0, bubble_diff * 0.05))
        self.s.bubble_index = max(0.0, min(300.0, bi + bubble_delta_final))

        # 이하 버핏 지수 연동 추가 보정
        buffett = getattr(self.s, 'buffett_index', 0.0)
        if buffett > 150 and self.s.bubble_index < 200:
            self.s.bubble_index = min(300.0, self.s.bubble_index + 0.3)

        # ★ 버핏 지수 추가 보정 (이미 위에서 bubble_index에 직접 반영)
        # bubble_delta는 새 방식에서 사용 안 함 (bubble_delta_final로 대체)

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

        # ★ 고금리 + 고부채 → HP 추가 차감
        debt_ratio = meta.get('debt_ratio', 0.5)
        if interest_rate >= 7.0 and debt_ratio >= 1.5:
            extra_hp_dmg = (interest_rate - 7.0) * debt_ratio * 0.002
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

        # 1월 효과: 소형주 보너스
        if month == 1 and "소형" in tier:
            adj += 0.0003

        # 4월/10월: 실적시즌 변동성 확대 (노이즈 추가는 apply_price_change에서)
        # 여기선 Growth 섹터 소폭 보너스만
        if month in [4, 10] and sector == "Growth":
            adj += 0.0001

        # 12월: 방어주/배당주 소폭 상승
        if month == 12 and sector == "Defensive":
            adj += 0.0002

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
        if cycle == "확장" and "대형" in tier: foreign_delta += 0.0008
        elif cycle in ("수축", "저점"):        foreign_delta -= 0.0010
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
        tech_upgrade_year = getattr(self.s, '_tech_upgrade_year', 1999)
        if 0 <= self.s.current_date.year - tech_upgrade_year <= 2 and sector == "Growth":
            inst_delta += 0.003

        retail_delta = 0.0
        if   rate < -0.05: retail_delta += 0.010
        elif rate < -0.02: retail_delta += 0.004
        elif rate >  0.05: retail_delta -= 0.005
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
                elif days_left <= 0:
                    meta.pop('pending_split', None)

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
        top_by_ind = {}
        for ind in MAIN_INDUSTRIES:
            ind_stocks = [s for s in self.s.stocks if s['meta']['ind'] == ind]
            if ind_stocks:
                top_by_ind[ind] = max(ind_stocks, key=lambda x: x['market_cap'])['meta']['c_name']

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

            t1_subs = INDUSTRY_LEVELS.get(ind, {}).get(1, [])
            if current_lv > 1 and current_subs[0] in t1_subs:
                if top_by_ind.get(ind) != meta['c_name']:
                    meta['momentum'] = max(-1.5, meta.get('momentum', 0.0) - 0.02)
                    meta['risk_score'] = min(60.0, meta.get('risk_score', 0.0) + 0.05)
                    if random.random() < 0.005:
                        self.s.daily_news.append(f"🏚️ [산업도태] {meta['c_name']}이(가) 시대에 뒤처져 시장 점유율을 잃고 있습니다.")

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
                self.s.stocks.remove(ds)

    # ─────────────────────────────────────────────
    # 그룹 확장 (기존 유지)
    # ─────────────────────────────────────────────
    def handle_group_expansion(self, silent: bool):
        current_lv    = self.s.max_tech_reached
        is_depression = "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario
        is_recovery   = "극복" in self.s.current_scenario or "재건" in self.s.current_scenario

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

        limit = {1: 3, 2: 5, 3: 8, 4: 10}.get(current_lv, 3)
        if is_recovery: limit = 8

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
                new_ind = random.choice([i for i in MAIN_INDUSTRIES if i != target_stock['meta']['ind']])
                self.s.stocks.append(self.cm.create_stock_data(None, new_ind, "중", gid))
                if not silent:
                    self.s.daily_news.append(f"🏢 [그룹승격] {parent_name}이 지주사 체제로 전환합니다!")

        for gid, ginfo in self.s.groups.items():
            members = [s for s in self.s.stocks if s['meta']['group_id'] == gid]
            if len(members) < limit and random.random() < 0.03:
                existing_inds = [m['meta']['ind'] for m in members]
                avail = [i for i in MAIN_INDUSTRIES if i not in existing_inds]
                if avail:
                    new_ind = random.choice(avail)
                    self.s.stocks.append(self.cm.create_stock_data(None, new_ind, "중", gid))
                    if not silent:
                        self.s.daily_news.append(f"📢 [그룹확장] {ginfo['name']}그룹이 {new_ind} 계열사를 추가했습니다.")

    # ─────────────────────────────────────────────
    # 신규 상장
    # ─────────────────────────────────────────────
    def handle_new_listings(self, silent: bool):
        from .constants import NAME_DB
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
            mid_ratio = 0.0   # 중형주 비율
        elif cur_count >= 300:
            ipo_min, ipo_max = 2, 4
            mid_ratio = 0.0
        elif cur_count >= 250:
            ipo_min, ipo_max = 3, 5
            mid_ratio = 0.0
        elif cur_count >= 200:
            ipo_min, ipo_max = 4, 6
            mid_ratio = 0.0
        else:
            ipo_min, ipo_max = 6, 8
            mid_ratio = 0.3   # 긴급 시 중형주 30% 섞기

        # 버블/수축기 보정 (단, 종목 수 긴급 구간이면 무시)
        if cur_count >= 250:
            if cycle == "수축" or bubble_index < 50:
                ipo_max = max(ipo_min, ipo_max - 1)
            elif bubble_index > 200:
                ipo_max = min(self.s.MAX_STOCKS - cur_count, ipo_max + 1)

        ipo_count = random.randint(ipo_min, ipo_max)
        for _ in range(ipo_count):
            # 종목 수 다시 체크 (루프 중 MAX 초과 방지)
            if len(self.s.stocks) + len(self.s.pending_listings) >= self.s.MAX_STOCKS:
                break

            # 중형주 비율 적용
            tier = "중" if random.random() < mid_ratio else "소"
            new_s = self.cm.create_stock_data(
                random.choice(NAME_DB),
                random.choice(MAIN_INDUSTRIES),
                tier
            )
            # ★ GRI → 공모가 연동 (GRI 높을수록 공모가 높게)
            gri_ratio_ipo = self.s.gri / 1000.0  # 기준 GRI 1000
            if gri_ratio_ipo > 1.0:
                price_boost = min(3.0, gri_ratio_ipo)  # 최대 3배
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
                # ★ HP 0이면 회복 안 됨 (좀비 방지)
                if hp > 0.0:
                    meta['hp'] = round(min(soft_cap, hp + 0.02), 2)

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
    # ★ 액면분할 (정상화)
    # ─────────────────────────────────────────────
    def _handle_stock_split(self, stock: dict, silent: bool = False):
        meta    = stock['meta']
        price   = stock['price']
        shares  = stock['shares']
        tier    = meta['tier']
        sector  = SECTOR_MAP.get(meta['ind'], "Value")
        self.s.daily_splits = getattr(self.s, 'daily_splits', {})

        # ★ 소형주는 분할 없음
        if "소형" in tier:
            return

        # ★ will_to_split=False면 매우 드물게만 분할
        if not meta.get('will_to_split', True):
            if price < 25_000_000:
                return
            if random.random() > 0.005:
                return

        # ★ 생애 최대 5회 제한
        split_count = meta.get('split_count', 0)
        if split_count >= 5:
            return

        # ★ 쿨다운: 756 거래일(3년) 이상 경과해야 분할 가능
        cooldown = meta.get('split_cooldown_days', 0)
        if cooldown > 0:
            meta['split_cooldown_days'] = cooldown - 1
            return

        # ★ 트리거 조건 (체급별 차등)
        split_ratio = 0
        if "대형" in tier:
            trigger = 5_000_000   # 500만원
        else:  # 중형주
            trigger = 2_000_000   # 200만원

        if price >= trigger:
            # ★ 분할 비율 (주가 구간별)
            if price >= trigger * 10:      # 10배 이상
                split_ratio = 10
            elif price >= trigger * 2:     # 2~10배
                split_ratio = 5
            else:                          # 1~2배
                split_ratio = 2

            # ★ will_to_split=True + 주가 2500만원 이상이면 50:1 가능
            if meta.get('will_to_split', True) and price >= 25_000_000:
                split_ratio = 50

            # ★ 매일 5% 확률로 트리거 (0.5% → 5%, 주가 너무 높아지는 거 방지)
            if random.random() > 0.05:
                return

        if split_ratio == 0:
            # 병합 조건 (주가 1000원 미만)
            if price < 1000:
                self._handle_stock_merge(stock, silent)
            return

        # ★ D-7 예약
        if not meta.get('pending_split'):
            split_date = self.s.current_date + timedelta(days=7)
            meta['pending_split'] = {
                'date':  split_date.strftime('%Y-%m-%d'),
                'ratio': split_ratio,
            }
            if self.s.has_paid_news_access:
                self.s.daily_news.append(
                    f"💎 [분할예고] {meta['c_name']} 7일 후 1:{split_ratio} 액면분할 예정 (프리미엄 전용)"
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
            stock['price']  //= split_ratio
            stock['shares']  *= split_ratio
            stock['market_cap'] = stock['price'] * stock['shares']
            meta['split_count'] = split_count + 1

            # ★ 쿨다운 초기화 (252 거래일 = 1년으로 단축)
            meta['split_cooldown_days'] = 252

            self.s.daily_splits[old_name] = 1.0 / float(split_ratio)
            if not silent:
                self.s.daily_news.append(f"✂️ [액면분할] {meta['c_name']}이 {split_ratio}:1 분할을 실시합니다.")
                self.s.daily_news.append(f"  └ 현재가: {stock['price']:,}원 | 발행주식수: {stock['shares'] / 1e8:.1f}억 주")

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
            meta['hp'] = round(min(soft_cap, hp + 5.0), 2)
            if not self.s.silent_mode:
                self.s.daily_news.append(f"🛡️ [그룹지원] {meta['c_name']}가 그룹사의 자금 지원으로 위기를 넘깁니다.")
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