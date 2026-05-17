"""
events/dispatcher.py
EventDispatcher: next_day 때 이벤트 처리 순서를 중앙에서 관리.
새 이벤트 추가 시 이 파일만 수정하면 됩니다.
UI 코드 금지.
"""
import random
from datetime import timedelta


class EventDispatcher:
    """
    하루치 게임 진행을 순서대로 실행합니다.
    엔진 하위 모듈들(economy, market, earnings)을 조율하는 역할만 합니다.
    """

    def __init__(self, state, economy, market, earnings, persistence, company_mgr):
        self.s    = state
        self.eco  = economy
        self.mkt  = market
        self.ear  = earnings
        self.db   = persistence
        self.cm   = company_mgr

    # ─────────────────────────────────────────────
    # 하루 진행 (핵심 진입점)
    # ─────────────────────────────────────────────
    def next_day(self, silent: bool = False) -> bool:
        # 1. 날짜 업데이트
        self.s.current_date   += timedelta(days=1)
        self.s.virtual_weekday = (self.s.virtual_weekday + 1) % 7
        self.s.is_market_open  = self.s.virtual_weekday < 5

        # 2. 일일 상태 초기화
        self.s.daily_news        = []
        self.s.daily_delist_count = 0
        self.s.daily_splits      = {}
        self.s.silent_mode       = silent

        if self.s.scenario_timer > 0:
            self.s.scenario_timer -= 1

        # 3. 기술 레벨 확인 (내부에서 pending_events["tech_jump"] 처리)
        # → 주말 포함 매일 실행 (테크 도약 뉴스는 주말에도 발송)
        lv = self.eco.get_tech_level()

        # 3-1. 테크3 도달 후 분기점 시나리오 결정 뉴스
        self._check_branching_point_news(silent)

        # 3-2. 대공황 극복 예고 뉴스
        self._check_recovery_news(silent)

        # 4. 실적 스케줄 처리 (장 열림 여부와 무관하게 수치 확정은 매일 실행)
        self.ear.process_earnings_schedule(silent)

        # 5. 장이 열린 날에만 경제 연산
        if self.s.is_market_open:
            self.mkt.handle_group_expansion(silent)
            self.eco.update_macro_logic()
            self.mkt.apply_price_change()
            self.mkt.update_company_technology()
            # 경고 진입/해제 7일 선반영 시스템
            self.mkt.check_warning_system()
            # 6월/12월 티어 심사 D-7 예고 + D-Day 실행
            self._check_tier_exam(silent)

            # DB 저장
            date_str   = self.s.current_date.strftime('%Y-%m-%d')
            db_records = []
            for stock in self.s.stocks:
                self.mkt.apply_stock_event(stock, silent)
                db_records.append((date_str, stock['meta']['c_name'],
                                   int(stock['price']), int(stock['market_cap'])))

            # 분할/병합 발생 시 과거 주가 DB 보정
            for name, ratio in self.s.daily_splits.items():
                self.db.update_adjusted_price(name, ratio)

            self.db.insert_stock_records(db_records)
            self.mkt.check_delisting()

            # 지수 업데이트
            wsi_multiplier = {1: 1.5, 2: 2.2, 3: 3.5, 4: 6.0}.get(lv, 3.5)
            self.s.wsi = self.s.gri * wsi_multiplier * random.uniform(0.98, 1.02)

            if self.s.virtual_weekday == 0:
                self.mkt.handle_new_listings(silent)
            # reassign_tiers_by_cap 제거:
            # 티어 변경은 이제 _check_tier_exam(6월/12월 심사)과
            # _check_immediate_demotion(즉시강등)으로만 처리

        else:
            if not silent:
                self.s.daily_news.append(
                    f"💤 [휴장] {self.s.current_date.strftime('%Y-%m-%d')} 주말입니다."
                )

        return True

    # ─────────────────────────────────────────────
    # UI용 패킷 반환
    # ─────────────────────────────────────────────
    def get_ui_packet(self) -> dict:
        weekdays = ['월', '화', '수', '목', '금', '토', '일']
        return {
            "date":     f"{self.s.current_date.strftime('%Y-%m-%d')} ({weekdays[self.s.virtual_weekday]})",
            "level":    self.s.max_tech_reached,
            "gri":      self.s.gri,
            "wsi":      self.s.wsi,
            "scenario": self.s.current_scenario,
            "macro":    self.s.macro,
            "stocks": [
                {
                    "name":  s['meta']['c_name'],
                    "price": int(s['price']),
                    "rate":  s.get('rate', 0.0),
                    "meta":  s['meta'],
                }
                for s in self.s.stocks
            ],
        }

    def next_day_process(self) -> dict:
        """UI의 '다음 날' 버튼 콜백용"""
        self.next_day(silent=True)
        self.record_current_state()
        return self.get_ui_packet()

    # ─────────────────────────────────────────────
    # 상태 기록 (메모리 최근 5일치)
    # ─────────────────────────────────────────────
    def record_current_state(self):
        from engine.constants import SECTOR_MAP  # 순환 import 방지용 지연 import
        snapshot = {
            "date":        self.s.current_date.strftime('%Y-%m-%d'),
            "tech_level":  self.s.max_tech_reached,
            "gri":         self.s.gri,
            "wsi":         self.s.wsi,
            "oil":         self.s.macro["oil_price"],
            "interest":    self.s.macro["interest_rate"],
            "cpi":         self.s.macro["cpi"],
            "exchange_rate": self.s.macro["exchange_rate"],
            "price_index": self.s.base_item_price,
            "scenario":    self.s.current_scenario,
            "stocks": {
                s['meta']['c_name']: {
                    "tier":           s['meta']['tier'],
                    "ind":            s['meta']['ind'],
                    "sub":            s['meta']['sub'],
                    "char":           s['meta']['char'],
                    "group":          s['meta']['group'],
                    "sector":         SECTOR_MAP.get(s['meta']['ind'], "Value"),
                    "price":          s['price'],
                    "rate":           s['rate'],
                    "shares":         s['shares'],
                    "assets":         s['meta']['assets'],
                    "risk":           s['meta']['risk_score'],
                    "treasury_share": s['meta'].get('treasury_share', 0.0),
                    "owner_share":    s['meta'].get('owner_share', 0.0),
                    "foreign_share":  s['meta'].get('foreign_share', 0.0),
                    "inst_share":     s['meta'].get('inst_share', 0.0),
                    "retail_share":   s['meta'].get('retail_share', 0.0),
                }
                for s in self.s.stocks
            },
        }
        self.s.history_records.append(snapshot)
        if len(self.s.history_records) > 5:
            self.s.history_records.pop(0)

    # ─────────────────────────────────────────────
    # 분기점 시나리오 뉴스 (테크3 도달 후)
    # ─────────────────────────────────────────────
    def _check_branching_point_news(self, silent: bool):
        """테크3 도달 이후 분기점 시나리오가 결정됐을 때 프리미엄 예고 뉴스 발송"""
        if self.s.max_tech_reached < 3:
            return
        if self.s.world_line == "Decided":
            return
        # reserved_scenario가 설정됐는데 아직 뉴스를 안 보낸 경우
        if not self.s.reserved_scenario:
            return
        if getattr(self.s, '_branch_news_sent', False):
            return

        self.s._branch_news_sent = True
        scenario = self.s.reserved_scenario
        cy = self.s.current_date.year

        # 프리미엄 전용 — 분기점 결정 즉시 (30일 전 예고)
        if self.s.has_paid_news_access and not silent:
            self.s.daily_news.append(
                f"💎 [분기점 예고] {cy}년, 문명의 갈림길이 결정되었습니다! "
                f"2050~2060년 이후 시나리오: '{scenario}' (프리미엄 전용 정보)"
            )

    # ─────────────────────────────────────────────
    # 대공황 극복 예고 뉴스
    # ─────────────────────────────────────────────
    def _check_recovery_news(self, silent: bool):
        """대공황 극복 시나리오 전환 시 D-30 프리미엄 예고 + D-0 무료 뉴스"""
        pending = self.s.pending_events.get("recovery")
        if not pending:
            return

        from datetime import datetime
        target_date = pending.get("date")
        if isinstance(target_date, str):
            target_date = datetime.strptime(target_date, "%Y-%m-%d")

        cy = self.s.current_date.year
        days_left = (target_date.date() - self.s.current_date.date()).days

        # D-30: 프리미엄 예고 (예약 직후 1회)
        if not pending.get("notified"):
            pending["notified"] = True
            if self.s.has_paid_news_access and not silent:
                date_str = target_date.strftime('%Y년 %m월 %d일')
                self.s.daily_news.append(
                    f"💎 [극복 예고] {cy}년, {date_str}에 대공황 극복이 선언됩니다! "
                    f"경제 회복 국면이 시작될 예정입니다. (프리미엄 전용)"
                )

        # D-0: 극복 확정 — 주말 포함 무조건 뉴스 발송
        if self.s.current_date.date() >= target_date.date():
            self.s.current_scenario = pending.get("scenario", "✨ 대공황V (고난과 부활)")
            self.s.daily_news.append(
                f"🌅 [대공황 극복] {cy}년, 마침내 대공황을 극복했습니다! "
                f"경제 재건이 시작됩니다."
            )
            self.s.pending_events.pop("recovery", None)

    # ─────────────────────────────────────────────
    # 6월/12월 티어 심사 시스템
    # ─────────────────────────────────────────────
    def _check_tier_exam(self, silent: bool):
        """
        6월/12월 첫 거래일: 티어 심사 실행
        D-7: 프리미엄 예고 + 선반영
        D-0: 티어 변경 확정 + 전체 뉴스
        """
        cur_date  = self.s.current_date
        cur_month = cur_date.month
        cur_day   = cur_date.day

        # 심사월 D-7: 5월 마지막 주 or 11월 마지막 주
        # 심사일: 6월 1일 or 12월 1일 (첫 거래일)
        is_exam_day    = cur_month in [6, 12] and cur_day == 1
        is_preview_day = (cur_month == 5  and cur_day == 24) or                          (cur_month == 11 and cur_day == 24)

        # ── D-7 심사 예고 (프리미엄 전용) ───────────────────────
        if is_preview_day and not silent:
            up_list, down_list = self._evaluate_tier_candidates()

            if self.s.has_paid_news_access and (up_list or down_list):
                up_str   = ", ".join([f"{n}({f}→{t})" for n, f, t in up_list[:5]]) or "없음"
                down_str = ", ".join([f"{n}({f}→{t})" for n, f, t in down_list[:5]]) or "없음"
                self.s.daily_news.append(
                    f"💎 [티어심사 D-7 예보] 7일 후 정기 티어 심사 예정"
                    f"  승급 예정: {up_str}"
                    f"  강등 예정: {down_str} (프리미엄 전용)"
                )

            # 선반영: 승급 예정 +5%, 강등 예정 -5%
            names_up   = {n for n, _, _ in up_list}
            names_down = {n for n, _, _ in down_list}
            for stock in self.s.stocks:
                name = stock['meta']['c_name']
                if name in names_up:
                    stock['price'] = int(stock['price'] * 1.05)
                    stock['meta']['momentum'] = stock['meta'].get('momentum', 0.0) + 0.08
                elif name in names_down:
                    stock['price'] = int(stock['price'] * 0.95)
                    stock['meta']['momentum'] = stock['meta'].get('momentum', 0.0) - 0.08

        # ── D-0 심사 확정 ────────────────────────────────────────
        if is_exam_day:
            self._execute_tier_exam(silent)

        # ── 강등은 즉시 (심사일 무관) ────────────────────────────
        self._check_immediate_demotion(silent)

    def _evaluate_tier_candidates(self):
        """심사 결과 사전 평가 — 승급/강등 후보 리스트 반환"""
        up_list   = []
        down_list = []

        # 시총 기준선
        CAP_LARGE = 10_000_000_000_000   # 10조
        CAP_MID   =    300_000_000_000   # 3000억

        # 체급별 승급 조건 (시총 초과 유지일수)
        HOLD_DAYS = {"소형주": 90, "중형주": 180}

        for stock in self.s.stocks:
            meta  = stock['meta']
            name  = meta['c_name']
            tier  = meta['tier']
            mc    = stock['market_cap']
            hp    = meta.get('hp', 50.0)
            sc    = meta.get('hp_soft_cap', 60.0)
            hp_r  = hp / max(1.0, sc)

            # 상장일 확인
            ld = meta.get('listed_date_dt')
            if not ld:
                try:    ld = __import__('datetime').datetime.strptime(meta['listed_date'], '%Y-%m-%d')
                except: continue
            age_days = (self.s.current_date - ld).days

            # 최근 흑자 횟수
            history  = self.s.earnings_history.get(name, {})
            surplus_count = sum(
                1 for yr in list(history.keys())[-2:]
                for q_data in history[yr].values()
                if q_data.get('is_surplus', False)
            )

            # ── 승급 조건 ───────────────────────────────────────
            cap_hold = meta.get('cap_exceed_days', 0)

            if tier == '소형주' and mc >= CAP_MID:
                meta['cap_exceed_days'] = cap_hold + 1
                if (cap_hold >= HOLD_DAYS['소형주'] and
                        surplus_count >= 2 and
                        hp_r >= 0.50 and
                        age_days >= 504):
                    up_list.append((name, '소형주', '중형주'))
            elif tier == '중형주' and mc >= CAP_LARGE:
                meta['cap_exceed_days'] = cap_hold + 1
                if (cap_hold >= HOLD_DAYS['중형주'] and
                        surplus_count >= 3 and
                        hp_r >= 0.60 and
                        age_days >= 504 and
                        meta.get('continuous_loss_count', 0) == 0):
                    up_list.append((name, '중형주', '대형주'))
            else:
                # 기준 미달이면 카운트 리셋
                meta['cap_exceed_days'] = 0

            # ── 강등 조건 (즉시 강등용 판별) ───────────────────
            cap_below = meta.get('cap_below_days', 0)

            if tier == '대형주' and mc < CAP_LARGE:
                meta['cap_below_days'] = cap_below + 1
                if cap_below >= 30:
                    down_list.append((name, '대형주', '중형주'))
            elif tier == '중형주' and mc < CAP_MID:
                meta['cap_below_days'] = cap_below + 1
                if cap_below >= 30:
                    down_list.append((name, '중형주', '소형주'))
            else:
                meta['cap_below_days'] = 0

        return up_list, down_list

    def _execute_tier_exam(self, silent: bool):
        """심사 D-Day: 승급 확정 처리"""
        up_list, _ = self._evaluate_tier_candidates()

        # HP 스펙 매핑
        _HP_SPEC = {
            '대형주': {'soft_cap': 100.0, 'sensitivity': 0.1, 'shield_ratio': 0.015},
            '중형주': {'soft_cap': 80.0,  'sensitivity': 0.5, 'shield_ratio': 0.003},
            '소형주': {'soft_cap': 60.0,  'sensitivity': 1.2, 'shield_ratio': 0.0},
        }

        for name, from_tier, to_tier in up_list:
            stock = next((s for s in self.s.stocks if s['meta']['c_name'] == name), None)
            if not stock: continue
            meta  = stock['meta']
            spec  = _HP_SPEC[to_tier]

            # 티어 변경
            meta['tier'] = to_tier

            # HP 비율 보존하며 soft_cap 갱신
            old_cap  = meta.get('hp_soft_cap', 60.0)
            old_hp   = meta.get('hp', old_cap)
            hp_ratio = old_hp / max(1.0, old_cap)
            meta['hp_soft_cap']      = spec['soft_cap']
            meta['hp']               = round(min(spec['soft_cap'], hp_ratio * spec['soft_cap']), 2)
            meta['risk_sensitivity'] = spec['sensitivity']

            # 쉴드 부여
            new_shield_max = stock['market_cap'] * spec['shield_ratio']
            meta['shield'] = round(min(new_shield_max, meta.get('shield', 0.0) + new_shield_max * 0.5), 2)

            # 카운트 리셋
            meta['cap_exceed_days'] = 0

            if not silent:
                arrow = "🔼" if to_tier == '대형주' else "📈"
                self.s.daily_news.append(
                    f"{arrow} [정기심사 승급] {name}: {from_tier} → {to_tier} 확정"
                )

    def _check_immediate_demotion(self, silent: bool):
        """강등 조건 충족 시 즉시 강등 (심사일 무관)"""
        CAP_LARGE = 10_000_000_000_000
        CAP_MID   =    300_000_000_000

        _HP_SPEC = {
            '중형주': {'soft_cap': 80.0,  'sensitivity': 0.5, 'shield_ratio': 0.003},
            '소형주': {'soft_cap': 60.0,  'sensitivity': 1.2, 'shield_ratio': 0.0},
        }

        for stock in self.s.stocks:
            meta  = stock['meta']
            name  = meta['c_name']
            tier  = meta['tier']
            mc    = stock['market_cap']
            hp    = meta.get('hp', 50.0)
            sc    = meta.get('hp_soft_cap', 60.0)
            hp_r  = hp / max(1.0, sc)
            loss  = meta.get('continuous_loss_count', 0)

            to_tier   = None
            reason    = ""

            if tier == '대형주':
                cap_below = meta.get('cap_below_days', 0)
                if   cap_below >= 30:          to_tier = '중형주'; reason = f"시총 기준 미달 {cap_below}일"
                elif loss >= 3:                to_tier = '중형주'; reason = f"연속 적자 {loss}분기"
                elif hp_r < 0.30:              to_tier = '중형주'; reason = f"재무 체력 위험 ({hp_r*100:.0f}%)"

            elif tier == '중형주':
                cap_below = meta.get('cap_below_days', 0)
                if   cap_below >= 30:          to_tier = '소형주'; reason = f"시총 기준 미달 {cap_below}일"
                elif loss >= 3:                to_tier = '소형주'; reason = f"연속 적자 {loss}분기"
                elif hp_r < 0.30:              to_tier = '소형주'; reason = f"재무 체력 위험 ({hp_r*100:.0f}%)"

            if to_tier and meta.get('tier') != to_tier:
                old_tier = meta['tier']
                spec     = _HP_SPEC[to_tier]
                meta['tier'] = to_tier

                # HP 비율 보존
                old_cap  = meta.get('hp_soft_cap', sc)
                hp_ratio = hp / max(1.0, old_cap)
                meta['hp_soft_cap']      = spec['soft_cap']
                meta['hp']               = round(min(spec['soft_cap'], hp_ratio * spec['soft_cap']), 2)
                meta['risk_sensitivity'] = spec['sensitivity']
                if to_tier == '소형주':
                    meta['shield'] = 0.0
                else:
                    meta['shield'] = min(meta.get('shield', 0.0),
                                        stock['market_cap'] * spec['shield_ratio'])
                meta['cap_below_days'] = 0

                if not silent:
                    self.s.daily_news.append(
                        f"📉 [긴급강등] {name}: {old_tier} → {to_tier} ({reason})"
                    )