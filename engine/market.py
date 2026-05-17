"""
engine/market.py
StockMarket: 주가 변동(apply_price_change), 상장폐지, 그룹 확장,
신규 상장, 자사주/분할/병합 이벤트 처리.
UI 코드 금지.
"""
import random
import math
from datetime import datetime, timedelta
from .constants import SECTOR_MAP, MAIN_INDUSTRIES, INDUSTRY_LEVELS


class StockMarket:
    def __init__(self, state, economy, company_mgr):
        self.s    = state
        self.eco  = economy
        self.cm   = company_mgr

    # ─────────────────────────────────────────────
    # 주가 변동 (핵심 엔진)
    # ─────────────────────────────────────────────
    def apply_price_change(self) -> float:
        interest_rate  = self.s.macro["interest_rate"]
        total_market_cap = 0
        resistance = 1.0
        if self.s.gri > 50000:
            resistance = 1.0 / (1.0 + math.log10(self.s.gri / 50000) * 1.5)

        is_normal_depression = (
            "대공황" in self.s.current_scenario
            and "V" not in self.s.current_scenario
            and "극복" not in self.s.current_scenario
        )

        daily_target = 0.0003
        is_protected = False
        cpi_impact   = self.s.macro["cpi"] / 100.0

        if is_normal_depression:
            is_protected = self.s.scenario_timer > -252
            daily_target = -0.008 if is_protected else (-0.025 - cpi_impact * 0.5)
            if self.s.gri < 700:
                daily_target *= 0.3
        elif "극복" in self.s.current_scenario:
            daily_target = 0.018 + 0.01 / max(1, cpi_impact)
        elif "대공황" in self.s.current_scenario:
            daily_target = -0.015 - cpi_impact * 0.2

        panic_factor = 1.0
        # panic_factor: gri가 최고점 대비 크게 하락했을 때만 적용
        # 절대값(7000) 기준 → 시작 gri=1000이라 항상 걸리는 버그
        # 해결: gri가 역대 최고점의 50% 미만으로 떨어진 경우에만 적용
        peak_gri = getattr(self.s, 'peak_gri', self.s.gri)
        if self.s.gri > peak_gri:
            self.s.peak_gri = self.s.gri
            peak_gri = self.s.gri
        gri_ratio = self.s.gri / max(1.0, peak_gri)
        if   gri_ratio < 0.50: panic_factor = 0.93
        elif gri_ratio < 0.75: panic_factor = 0.97

        fear_index      = self.s.macro.get("fear_index", 10.0)
        fear_multiplier = 1.0
        if fear_index >= 50.0 and "대공황" in self.s.current_scenario:
            fear_multiplier = 1.8 if self.s.has_paid_news_access else 1.1

        for stock in self.s.stocks:
            meta      = stock['meta']
            name      = meta['c_name']
            old_price = float(stock['price'])

            # ── 수렴 엔진 ────────────────────────
            self._apply_convergence(stock, is_protected)

            # ── 기본 변동성 ──────────────────────
            risk       = meta.get('risk_score', 0)
            tier       = meta['tier']
            loss_count = meta.get('continuous_loss_count', 0)
            loss_penalty = -(loss_count * 0.02) if loss_count > 0 else 0.0

            cur_day    = self.s.current_date.day
            cur_month  = self.s.current_date.month
            report_day = meta.get('report_day', 15)
            is_pre_ann = (cur_month in [3, 6, 9, 12]
                          and 0 < report_day - cur_day <= 7)

            vol_multiplier = (1.6 if self.s.has_paid_news_access else 1.15) if is_pre_ann else 1.0

            if   "대형주" in tier: vol = 0.012 * vol_multiplier; decay = 0.85; gm = 1.35 * resistance
            elif "중형주" in tier: vol = 0.045 * vol_multiplier; decay = 0.6;  gm = 1.45 * resistance
            else:                   vol = 0.180 * vol_multiplier; decay = 0.2;  gm = 1.80 * resistance

            denom        = 400 if is_protected else 200
            risk_pressure = -((risk - 50) / denom) if risk > 50 else 0.0

            meta['momentum'] = (meta.get('momentum', 0.0) * decay
                                + random.uniform(-vol, vol)
                                + risk_pressure
                                + loss_penalty)

            adj_efficiency = meta['efficiency'] * gm
            int_impact     = 0.002 if is_protected else 0.01
            perf = (adj_efficiency + daily_target + meta['momentum']
                    - interest_rate * int_impact
                    - risk / 1000) / 252

            perf = self.eco.apply_macro_sector_sensitivity(stock, perf)
            perf = self.eco.apply_foreign_inst_shock(stock, perf)

            meta['assets'] *= (1 + perf)
            safe_assets    = max(10000.0, meta['assets'])
            target_price   = int(safe_assets / max(100, stock['shares']))

            # HP 기반 패닉 계수
            # HP 30% 미만일 때만 패널티 작동 (평상시엔 1.0 유지)
            hp_ratio = meta.get('hp', 50.0) / max(1.0, meta.get('hp_soft_cap', 60.0))
            if hp_ratio < 0.30:
                # WARNING 구간: HP 비율에 비례해서 0.7~1.0 사이로 패널티
                hp_panic = 0.7 + (hp_ratio / 0.30) * 0.3
            elif hp_ratio <= 0.0:
                hp_panic = 0.5   # HP 완전 소진: 최대 패널티
            else:
                hp_panic = 1.0   # 정상 구간: 패널티 없음

            raw_price  = max(10, int(target_price
                                     * random.uniform(0.98, 1.02)
                                     * panic_factor
                                     * hp_panic
                                     * fear_multiplier))

            max_up   = int(old_price * 1.3)
            max_down = int(old_price * 0.7)
            stock['price']  = max(max_down, min(max_up, raw_price))
            stock['rate']   = round((stock['price'] / old_price - 1) * 100, 2) if old_price > 0 else 0.0
            stock['market_cap'] = stock['price'] * stock['shares']
            total_market_cap   += stock['market_cap']

            self._handle_survival_strategy(stock)

        # GRI 갱신
        if getattr(self.s, 'initial_market_total_cap', 0) <= 0:
            self.s.initial_market_total_cap = total_market_cap if total_market_cap > 0 else 1.0
            raw_gri = 1000.0
        else:
            raw_gri = (total_market_cap / self.s.initial_market_total_cap) * 1000.0

        weight = 0.005
        if is_normal_depression:
            if raw_gri < 1500: weight = 0.02;  raw_gri = max(800, raw_gri)
            else:               weight = 0.12
        elif "극복" in self.s.current_scenario:
            weight = 0.1

        self.s.gri = self.s.gri * (1 - weight) + raw_gri * weight
        return total_market_cap

    def _apply_convergence(self, stock, is_protected):
        """수렴 엔진: 상폐 예약 / 테크 도약 / 대공황 V반등 선반영"""
        meta = stock['meta']
        name = meta['c_name']

        # 상폐 D-7
        if name in self.s.pending_events.get("delist", {}):
            p_date = self.s.pending_events["delist"][name]
            if isinstance(p_date, dict): p_date = p_date.get('date')
            if isinstance(p_date, str):  p_date = datetime.strptime(p_date, "%Y-%m-%d")
            days_left = (p_date.date() - self.s.current_date.date()).days
            if 0 < days_left <= 7:
                stock['price'] = int(stock['price'] * random.uniform(0.80, 0.86))
                meta['momentum'] -= 0.25

        # 테크 도약 D-30
        if self.s.pending_events.get("tech_jump"):
            jump_info = self.s.pending_events["tech_jump"]
            p_date    = jump_info['date']
            if isinstance(p_date, str): p_date = datetime.strptime(p_date, "%Y-%m-%d")
            days_left = (p_date.date() - self.s.current_date.date()).days
            if 0 < days_left <= 30 and meta['ind'] in ["IT", "커뮤니케이션"]:
                convergence_factor = (30 - days_left) / 30
                meta['momentum'] += 0.008 * (1 + convergence_factor)

        # V반등 D-30
        if self.s.pending_events.get("v_rebound"):
            p_date = self.s.pending_events["v_rebound"]
            if isinstance(p_date, str): p_date = datetime.strptime(p_date, "%Y-%m-%d")
            days_left = (p_date.date() - self.s.current_date.date()).days
            if 0 < days_left <= 30:
                daily_recovery = 0.012 * (1 + (30 - days_left) / 15)
                meta['momentum'] += daily_recovery

    # ─────────────────────────────────────────────
    # 기술 발전에 따른 종목 업그레이드
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

            # 12월 1일 테크 점프 구간
            if self.s.current_date.month == 12 and self.s.current_date.day == 1:
                jump_premium = 1.15 if self.s.has_paid_news_access else 1.0
                meta['assets']  *= jump_premium
                stock['price']   = int(stock['price'] * jump_premium)

            max_subs      = {"대형주": 3, "중형주": 2, "소형주": 1}.get(tier, 1)
            if is_group: max_subs = 3

            current_subs   = [x.strip() for x in meta['sub'].split(',')]
            available_tech = INDUSTRY_LEVELS.get(ind, {}).get(current_lv, [])

            if available_tech and current_subs[0] not in available_tech:
                pivot_chance = {"대형주": 0.85, "중형주": 0.50, "소형주": 0.25}.get(tier, 0.25)
                if is_group: pivot_chance = 0.90
                if random.random() < pivot_chance:
                    new_main = random.choice(available_tech)
                    if new_main not in current_subs:
                        current_subs.insert(0, new_main)
                        current_subs = current_subs[:max_subs]
                        meta['sub']     = ", ".join(current_subs)
                        stock['price']  = int(stock['price'] * 1.08)
                        meta['risk_score'] = max(0, meta['risk_score'] - 5)

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
                    penalty = random.uniform(0.015, 0.035)
                    stock['price']      = int(stock['price'] * (1 - penalty))
                    meta['risk_score'] += 0.4
                    if random.random() < 0.005:
                        self.s.daily_news.append(f"🏚️ [산업도태] {meta['c_name']}이(가) 시대에 뒤처져 시장 점유율을 잃고 있습니다.")

    # ─────────────────────────────────────────────
    # 경고(WARNING) 진입/해제 7일 선반영 시스템
    # ─────────────────────────────────────────────
    def check_warning_system(self):
        """
        매일 HP 비율을 감지하여 투자경고 진입/해제를 7일 전에 예약.
        예약 시 선반영 주가 변동 + 프리미엄 예고 뉴스 발송.
        D-Day에 char 라벨 조용히 변경.
        """
        warning_book = self.s.pending_events.setdefault("warning", {})

        for stock in self.s.stocks:
            meta     = stock['meta']
            name     = meta['c_name']
            hp       = meta.get('hp', 50.0)
            soft_cap = meta.get('hp_soft_cap', 60.0)
            hp_ratio = hp / max(1.0, soft_cap)
            char     = meta.get('char', 'Normal')

            # ── D-Day 처리: 예약 날짜 도달 시 라벨 변경 ─────────
            if name in warning_book:
                info        = warning_book[name]
                target_date = info.get('date')
                if isinstance(target_date, str):
                    from datetime import datetime as dt2
                    target_date = dt2.strptime(target_date, "%Y-%m-%d")

                if self.s.current_date.date() >= target_date.date():
                    w_type = info.get('type')
                    if w_type == 'IN':
                        meta['char'] = 'WARNING'
                        if not self.s.silent_mode:
                            # 무료: D-Day 당일 뉴스
                            self.s.daily_news.append(
                                f"⚠️ [투자경고 지정] {name} 재무 체력 위험 수준 — 투자 유의"
                            )
                    elif w_type == 'OUT':
                        meta['char'] = 'Normal'
                        if not self.s.silent_mode:
                            self.s.daily_news.append(
                                f"✅ [투자경고 해제] {name} 재무 정상화 확인"
                            )
                    del warning_book[name]
                continue

            # ── 경고 진입 감지 (Normal → WARNING 예약) ───────────
            if char == 'Normal' and hp_ratio < 0.30:
                from datetime import timedelta
                warn_date = self.s.current_date + timedelta(days=7)
                warning_book[name] = {'date': warn_date, 'type': 'IN'}

                if not self.s.silent_mode:
                    # 프리미엄: D-7 예고 뉴스
                    if self.s.has_paid_news_access:
                        self.s.daily_news.append(
                            f"💎 [경고 D-7 예보] {name} 재무 체력 {hp_ratio*100:.1f}% 위험선 돌파 "
                            f"— 7일 후 투자경고 지정 예정 (프리미엄 전용)"
                        )
                    # 선반영: 주가 즉시 -8%, 기관/외인 15% 개인에게 강제 이탈
                    stock['price'] = int(stock['price'] * 0.92)
                    meta['momentum'] -= 0.15
                    total_inst = meta.get('foreign_share', 0.0) + meta.get('inst_share', 0.0)
                    escape     = total_inst * 0.15
                    meta['foreign_share'] = max(0.001, meta.get('foreign_share', 0.0) - escape * 0.6)
                    meta['inst_share']    = max(0.001, meta.get('inst_share', 0.0)    - escape * 0.4)
                    meta['retail_share']  = min(0.99,  meta.get('retail_share', 0.0)  + escape)

            # ── 경고 해제 감지 (WARNING → Normal 예약) ───────────
            elif char == 'WARNING' and hp_ratio >= 0.30:
                from datetime import timedelta
                warn_date = self.s.current_date + timedelta(days=7)
                warning_book[name] = {'date': warn_date, 'type': 'OUT'}

                if not self.s.silent_mode:
                    # 프리미엄: D-7 예고 뉴스
                    if self.s.has_paid_news_access:
                        self.s.daily_news.append(
                            f"💎 [경고해제 D-7 예보] {name} 재무 정상화 "
                            f"— 7일 후 투자경고 해제 예정 (프리미엄 전용)"
                        )
                    # 선반영: 주가 즉시 +8%, 기관/외인 선취매
                    stock['price'] = int(stock['price'] * 1.08)
                    meta['momentum'] += 0.10
                    recover  = meta.get('retail_share', 0.0) * 0.10
                    meta['inst_share']    = min(0.99, meta.get('inst_share', 0.0)    + recover * 0.5)
                    meta['foreign_share'] = min(0.99, meta.get('foreign_share', 0.0) + recover * 0.5)
                    meta['retail_share']  = max(0.001, meta.get('retail_share', 0.0) - recover)

    # ─────────────────────────────────────────────
    # 상장폐지
    # ─────────────────────────────────────────────
    def check_delisting(self):
        MIN_STOCKS_LIMIT = 60
        if len(self.s.stocks) <= MIN_STOCKS_LIMIT:
            return

        delisted_this_turn = []
        is_depression = (
            any("대공황" in str(v) for v in [self.s.current_scenario])
            and "극복" not in self.s.current_scenario
        )
        max_delist = 30 if is_depression else 1
        new_reserved = 0

        # HP 낮은 순으로 정렬 (위험 종목 우선 처리)
        candidates = sorted(self.s.stocks, key=lambda x: x['meta'].get('hp', 99.0))

        # 체급별 연속 적자 상폐 기준
        _LOSS_LIMIT = {"대형주": 6, "중형주": 5, "소형주": 4}
        MIN_AGE_DAYS = 504  # 2년 (504거래일)

        for stock in candidates:
            meta = stock['meta']
            name = meta['c_name']

            # ── 예약 상폐 D-Day 처리 ────────────────────────────
            if name in self.s.pending_events["delist"]:
                info        = self.s.pending_events["delist"][name]
                target_date = info.get('date') if isinstance(info, dict) else info
                if isinstance(target_date, str):
                    target_date = datetime.strptime(target_date, "%Y-%m-%d")

                if self.s.current_date < target_date:
                    continue

                reason = info.get('reason', '재무 파탄') if isinstance(info, dict) else '재무 파탄'
                if not self.s.silent_mode:
                    # 무료: 당일 상폐 확정 뉴스
                    self.s.daily_news.append(f"💀 [상장폐지 확정] {name} ({reason})")

                meta['delisted_date'] = self.s.current_date.strftime('%Y-%m-%d')
                delisted_this_turn.append(stock)
                del self.s.pending_events["delist"][name]
                # 예약 장부 경고/경보 데이터도 정리
                self.s.pending_events.get("warning", {}).pop(name, None)
                continue

            # ── 상폐 조건 판정 ───────────────────────────────────
            ld = meta.get('listed_date_dt')
            if not ld or isinstance(ld, str):
                try:    ld = datetime.strptime(meta['listed_date'], '%Y-%m-%d')
                except: ld = self.s.current_date
                meta['listed_date_dt'] = ld

            age_days   = (self.s.current_date - ld).days
            hp_now     = meta.get('hp', 50.0)
            loss_count = meta.get('continuous_loss_count', 0)
            tier       = meta.get('tier', '소형주')
            loss_limit = _LOSS_LIMIT.get(tier, 4)

            # 즉시 상폐: HP = 0
            is_bankrupt = hp_now <= 0.0

            # 유예 상폐: 연속 적자 N회 + 상장 2년 이상
            is_zombie = (loss_count >= loss_limit and age_days >= MIN_AGE_DAYS)

            # 대공황 시 조건 완화
            if is_depression:
                is_zombie = loss_count >= max(2, loss_limit - 2) and age_days >= MIN_AGE_DAYS

            if (is_bankrupt or is_zombie) and not meta.get('is_doomed'):
                if new_reserved < max_delist:
                    remaining = len(self.s.stocks) - len(delisted_this_turn) - new_reserved
                    if remaining > MIN_STOCKS_LIMIT:
                        if is_bankrupt:
                            # HP=0: 당일 장 마감 후 즉시 상폐
                            delist_date = self.s.current_date
                            reason      = "재무 완전 파탄 (HP 소진)"
                        else:
                            # 연속 적자: 7일 유예
                            delist_date = self.s.current_date + timedelta(days=7)
                            reason      = f"연속 적자 {loss_count}분기" + (" (대공황 가속)" if is_depression else "")

                        self.s.pending_events["delist"][name] = {"date": delist_date, "reason": reason}
                        meta['is_doomed'] = True
                        new_reserved += 1

                        if not self.s.silent_mode:
                            if is_bankrupt:
                                # HP=0: 즉시상폐는 무료/프리미엄 동일
                                self.s.daily_news.append(f"☠️ [즉시상폐] {name} 재무 체력 완전 소진 — 오늘 상장폐지")
                            else:
                                # 7일 유예: 프리미엄만 예고 뉴스
                                if self.s.has_paid_news_access:
                                    self.s.daily_news.append(
                                        f"💎 [상폐예고] {name} 연속 적자 {loss_count}분기 "
                                        f"— 7일 후 상장폐지 예정 (프리미엄 전용)"
                                    )
                                # 선반영: 주가 즉시 -12%, 기관/외인 이탈
                                stock['price'] = int(stock['price'] * 0.88)
                                meta['momentum'] -= 0.30
                                total_inst = meta.get('foreign_share', 0.0) + meta.get('inst_share', 0.0)
                                escape = total_inst * 0.15
                                meta['foreign_share'] = max(0.001, meta.get('foreign_share', 0.0) - escape * 0.5)
                                meta['inst_share']    = max(0.001, meta.get('inst_share', 0.0)    - escape * 0.5)
                                meta['retail_share']  = min(0.99,  meta.get('retail_share', 0.0)  + escape)
                        continue

        for ds in delisted_this_turn:
            if ds in self.s.stocks:
                ds['meta']['delisted_date']         = self.s.current_date.strftime('%Y-%m-%d')
                ds['meta']['is_officially_delisted'] = True
                self.s.delisted_stocks.append(ds)
                self.s.stocks.remove(ds)

    # ─────────────────────────────────────────────
    # 그룹 확장
    # ─────────────────────────────────────────────
    def handle_group_expansion(self, silent: bool):
        current_lv   = self.s.max_tech_reached
        is_depression = "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario
        is_recovery   = "극복" in self.s.current_scenario  or "재건" in self.s.current_scenario

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
                            core['meta']['assets']     += target['meta']['assets'] * 0.3
                            core['meta']['risk_score']  = max(0, core['meta']['risk_score'] - 15.0)
                            self.s.stocks.remove(target)
                            if not silent:
                                self.s.daily_news.append(
                                    f"🔪 [피의 구조조정] {ginfo['name']}그룹이 {core['meta']['c_name']}를 살리기 위해 {target['meta']['c_name']}를 정리하고 자본을 수혈했습니다."
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
                gid = f"GROUP_{parent_name}"
                self.s.groups[gid] = {"name": parent_name, "active": True}
                target_stock['meta']['group_id'] = gid
                target_stock['meta']['group']    = parent_name
                new_ind = random.choice([i for i in MAIN_INDUSTRIES if i != target_stock['meta']['ind']])
                self.s.stocks.append(self.cm.create_stock_data(None, new_ind, "소", gid))
                if not silent:
                    self.s.daily_news.append(f"🏢 [그룹승격] {parent_name}이 지주사 체제로 전환합니다!")

        for gid, ginfo in self.s.groups.items():
            members = [s for s in self.s.stocks if s['meta']['group_id'] == gid]
            if len(members) < limit and random.random() < 0.03:
                existing_inds = [m['meta']['ind'] for m in members]
                avail = [i for i in MAIN_INDUSTRIES if i not in existing_inds]
                if avail:
                    new_ind = random.choice(avail)
                    self.s.stocks.append(self.cm.create_stock_data(None, new_ind, "소", gid))
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

        for stock in self.s.pending_listings[:]:
            if stock['meta']['listed_date'] <= today_str:
                self.s.stocks.append(stock)
                self.s.pending_listings.remove(stock)
                if not silent:
                    self.s.daily_news.append(f"🚀 [신규상장] {stock['meta']['c_name']}이 거래를 시작합니다!")

        if self.s.virtual_weekday == 0:
            if is_depression:
                total_days   = 252 * 10
                elapsed_days = total_days - self.s.scenario_timer
                if (elapsed_days / total_days) < 0.5: return
                if random.random() > 0.1: return

            for _ in range(random.randint(2, 5)):
                new_s = self.cm.create_stock_data(
                    random.choice(NAME_DB),
                    random.choice(MAIN_INDUSTRIES)
                )
                listing_date = self.s.current_date + timedelta(days=7)
                new_s['meta']['listed_date'] = listing_date.strftime('%Y-%m-%d')
                self.s.pending_listings.append(new_s)
                if not silent:
                    self.s.daily_news.append(
                        f"📅 [상장예고] {new_s['meta']['c_name']} ({new_s['meta']['listed_date']} 상장 예정)"
                    )

    # ─────────────────────────────────────────────
    # 자사주 / 분할 / 병합 이벤트
    # ─────────────────────────────────────────────
    def apply_stock_event(self, stock: dict, silent: bool = False):
        meta  = stock['meta']
        price = stock['price']
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

        # ── 일일 HP 미세 차감 (연속 적자 중인 종목 압박) ─────────
        is_resistant = "대형주" in meta['tier'] or meta['group_id'] is not None
        loss_count   = meta.get('continuous_loss_count', 0)

        if loss_count > 0:
            # 연속 적자 중: 매일 소량 HP 차감 (분기 실적과 별개)
            sens     = meta.get('risk_sensitivity', 1.0)
            if is_resistant: sens *= 0.3
            daily_hp_dmg = 0.05 * loss_count * sens

            # 고금리 추가 압박
            if self.s.macro["interest_rate"] > 15.0:
                daily_hp_dmg += 0.03

            # 주가 1000원 미만 압박
            if price < 1000:
                daily_hp_dmg += 0.05

            hp       = meta.get('hp', 50.0)
            soft_cap = meta.get('hp_soft_cap', 60.0)
            hp_ratio = hp / max(1.0, soft_cap)
            shield   = meta.get('shield', 0.0)

            # HP 30% 미만이면 쉴드 발동
            if hp_ratio < 0.30 and shield > 0:
                shield_dmg = daily_hp_dmg * meta.get('assets', 1.0) * 0.001
                if shield >= shield_dmg:
                    meta['shield'] = round(shield - shield_dmg, 2)
                else:
                    meta['shield'] = 0.0
                    meta['hp']     = round(max(0.0, hp - daily_hp_dmg), 2)
            else:
                meta['hp'] = round(max(0.0, hp - daily_hp_dmg), 2)
        else:
            # 흑자/정상: HP 미세 회복 (하루 +0.01)
            hp       = meta.get('hp', 50.0)
            soft_cap = meta.get('hp_soft_cap', 60.0)
            meta['hp'] = round(min(soft_cap, hp + 0.01), 2)

        # ── char 상태 판정 (HP 기준) ───────────────────────────
        hp_now   = meta.get('hp', 50.0)
        soft_cap = meta.get('hp_soft_cap', 60.0)
        hp_ratio_now = hp_now / max(1.0, soft_cap)

        if meta.get('delist_timer', 0) > 0:
            meta['char'] = f"EXIT-{meta['delist_timer']}"
        elif hp_now <= 0:
            meta['char'] = "BANKRUPT"
        elif hp_ratio_now < 0.30:
            meta['char'] = "WARNING"
        else:
            meta['char'] = "Normal" 

    def _handle_stock_split(self, stock: dict, silent: bool = False):
        meta    = stock['meta']
        price   = stock['price']
        shares  = stock['shares']
        tier    = meta['tier']
        sector  = SECTOR_MAP.get(meta['ind'], "Value")
        self.s.daily_splits = getattr(self.s, 'daily_splits', {})

        if not meta.get('will_to_split', True):
            if price < 10_000_000: return
            if random.random() > 0.01: return

        split_ratio = 0

        if "중형주" in tier or "대형주" in tier:
            if price >= 150000:
                if   shares < 100_000_000: split_ratio = 10
                elif shares < 500_000_000: split_ratio = 5
                else:                       split_ratio = 2

        if split_ratio == 0:
            hard_limit = 10_000_000 if sector == "Value" else 5_000_000
            if price >= hard_limit:
                split_ratio = 10 if price < hard_limit * 5 else 50
            elif price >= hard_limit * 0.2 and shares < 50_000_000:
                if random.random() < 0.2: split_ratio = 5

        if split_ratio > 0:
            old_name = meta['c_name']
            stock['price']  //= split_ratio
            stock['shares']  *= split_ratio
            meta['split_count'] = meta.get('split_count', 0) + 1
            self.s.daily_splits[old_name] = 1.0 / float(split_ratio)  # 과거 주가 ÷ split_ratio
            if not silent:
                self.s.daily_news.append(f"✂️ [액면분할] {meta['c_name']}이 {split_ratio}:1 분할을 실시합니다.")
                self.s.daily_news.append(f"  └ 현재가: {stock['price']:,}원 | 발행주식수: {stock['shares'] / 1e8:.1f}억 주")

        elif price < 1000:
            # 병합 횟수 제한 (최대 3회) + 쿨다운 (30일)
            merge_count = meta.get('merge_count', 0)
            last_merge  = meta.get('last_merge_date', '')
            today_str   = self.s.current_date.strftime('%Y-%m-%d')
            days_since_merge = 999
            if last_merge:
                from datetime import datetime
                try:
                    days_since_merge = (self.s.current_date - datetime.strptime(last_merge, '%Y-%m-%d')).days
                except Exception:
                    pass

            if merge_count < 2 and days_since_merge >= 30 and random.random() < 0.3:
                ratio    = 10
                old_name = meta['c_name']
                stock['price']  *= ratio
                stock['shares'] //= ratio
                meta['merge_count']     = merge_count + 1
                meta['last_merge_date'] = today_str
                self.s.daily_splits[old_name] = float(ratio)  # 과거 주가 × ratio

                # 병합 시 HP 소량 회복 (최대 soft_cap까지)
                hp_gain  = 5.0
                soft_cap = meta.get('hp_soft_cap', 60.0)
                meta['hp'] = round(min(soft_cap, meta.get('hp', 0.0) + hp_gain), 2)

                if not silent:
                    self.s.daily_news.append(
                        f"🧩 [AI병합] {meta['c_name']}이 상장 유지를 위해 1:{ratio} 병합을 단행했습니다. "
                        f"(병합 {meta['merge_count']}회차 / HP +{hp_gain})"
                    )

    def _handle_survival_strategy(self, stock: dict):
        meta     = stock['meta']
        hp       = meta.get('hp', 50.0)
        soft_cap = meta.get('hp_soft_cap', 60.0)
        hp_ratio = hp / max(1.0, soft_cap)
        sector   = SECTOR_MAP.get(meta['ind'], "Value")

        # HP 40% 이상이면 생존 전략 불필요
        if hp_ratio >= 0.40: return

        if meta.get('group_id'):
            # 그룹사 지원 → HP 회복
            meta['hp'] = round(min(soft_cap, hp + 5.0), 2)
            if not self.s.silent_mode:
                self.s.daily_news.append(f"🛡️ [그룹지원] {meta['c_name']}가 그룹사의 자금 지원으로 위기를 넘깁니다.")
        else:
            if sector in ["Value", "Defensive"] and meta.get('treasury_share', 0) > 0.02:
                sell_shares_count  = int(stock['shares'] * 0.02)
                meta['treasury_share'] -= 0.02
                meta['assets']         += stock['price'] * sell_shares_count
                meta['hp'] = round(min(soft_cap, hp + 3.0), 2)
                stock['price']          = int(stock['price'] * 0.97)
                if not self.s.silent_mode:
                    self.s.daily_news.append(f"💸 [위기처분] {meta['c_name']}가 자사주를 매각하여 운영 자금을 확보했습니다.")
            else:
                new_shares     = int(stock['shares'] * 0.15)
                capital_raised = new_shares * (stock['price'] * 0.8)
                stock['shares']   += new_shares
                meta['assets']    += capital_raised
                meta['hp'] = round(min(soft_cap, hp + 2.0), 2)
                stock['price']     = int(stock['price'] * 0.85)
                if not self.s.silent_mode:
                    self.s.daily_news.append(f"💉 [유상증자] {meta['c_name']}가 생존을 위해 증자를 단행했습니다. (가치 희석)")