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
        # ★ 시나리오 변경 감지 — dispatcher 자체 변수로 관리 (state 리셋에 영향 안 받음)
        if not hasattr(self, '_logged_scenario'):
            self._logged_scenario = None
        _prev_scenario = self._logged_scenario

        # ★ 최초 실행 시 초기 상태 강제 기록
        if _prev_scenario is None and hasattr(self.db, 'log_scenario_change'):
            date_str = self.s.current_date.strftime('%Y-%m-%d')
            self.db.log_scenario_change(
                date_str     = date_str,
                scenario     = self.s.current_scenario,
                gri          = self.s.gri,
                bubble       = getattr(self.s, 'bubble_index', 0.0),
                macro        = self.s.macro,
                war_event    = getattr(self.s, 'war_event', {}),
                note         = "게임 시작",
                market_stats = self._collect_market_stats(),
            )
            self._logged_scenario = self.s.current_scenario
            _prev_scenario = self.s.current_scenario

        if self.s.scenario_timer > 0:
            self.s.scenario_timer -= 1

        # 3. 기술 레벨 확인 (내부에서 pending_events["tech_jump"] 처리)
        # → 주말 포함 매일 실행 (테크 도약 뉴스는 주말에도 발송)
        lv = self.eco.get_tech_level()

        # 3-1. 대공황 극복 예고 뉴스
        self._check_recovery_news(silent)

        # 4. 실적 스케줄 처리 (장 열림 여부와 무관하게 수치 확정은 매일 실행)
        self.ear.process_earnings_schedule(silent)

        # 5. 장이 열린 날에만 경제 연산
        if self.s.is_market_open:
            self.mkt.handle_group_expansion(silent)
            # ★ update_macro_logic 전에 전일 값 보존 — UI 등락률 표시 전용
            # (_prev_macro_snapshot은 economy.py 내부에서 덮어쓰므로 별도 키 사용)
            self.s._ui_prev_macro = dict(self.s.macro)
            self.s._ui_prev_macro["buffett_index"] = getattr(self.s, 'buffett_index', 0.0)
            self.eco.update_macro_logic()
            # ★ 6순위: 공급망 패널티 적용
            self.eco.apply_supply_chain_penalty()
            # ★ 7순위: 호재 시나리오 체크 (신규)
            self._check_boom_event(silent)
            # ★ 7.5순위: 수출 규제 이벤트 체크
            self._check_export_sanction(silent)
            # ★ 8순위: 외부 충격 이벤트 체크 (매년 1월 1일)
            self._check_external_shock(silent)
            # ★ 9순위: 전쟁/분쟁 이벤트 체크
            self._check_war_event(silent)
            # ★ 10순위: 팬데믹 이벤트 체크
            self._check_pandemic_event(silent)
            # ★ 10.5순위: 구조적 경제 충격 (긴축/스태그/기술버블)
            self._check_structural_shift(silent)
            # ★ 10.6순위: 신규 시나리오 체크
            self._check_new_scenarios(silent)
            # ★ 10.65순위: 경기 정상화 / 금융 완화 사이클
            self._check_recovery_cycle(silent)
            # ★ 10.7순위: 환율 위기 체크
            self._check_exchange_crisis(silent)
            # ★ 10.9순위: 박스권 횡보 감지
            self._check_boxrange(silent)
            # ★ 11순위: 대공황 자연 발생 트리거 체크 (신규)
            self._check_depression(silent)
            # ★ 11.5순위: 대공황V 종료 타이머 + 위기 정책 자동 발동
            self._tick_depression_v(silent)
            self._apply_crisis_policy(silent)
            # ★ 12순위: 페이즈 전환 체크 (신규)
            self._check_phase_transition(silent)
            self.mkt.apply_price_change()
            self.mkt.update_company_technology()
            # 경고 진입/해제 7일 선반영 시스템
            self.mkt.check_warning_system()
            # ★ 산업 패권 시스템 (월 1일 체크)
            self._check_dividend(silent)
            self._check_industry_dominance(silent)
            # ★ 광기 지수 업데이트 (분기 1일 체크)
            self._update_mania_index(silent)
            # ★ LV4 복합 조건 체크 (LV3일 때만)
            if self.s.max_tech_reached == 3:
                self._check_lv4_conditions(silent)

            # DB 저장 (모든 INSERT를 모은 뒤 flush_daily_db로 commit 1회)
            date_str   = self.s.current_date.strftime('%Y-%m-%d')
            # GRI 일별 저장 (commit 없음)
            self.db.insert_gri_record(
                date_str,
                self.s.gri,
                getattr(self.s, 'bubble_index', 0.0)
            )
            self.db.insert_macro_record(
                date_str, self.s.macro,
                getattr(self.s, 'buffett_index', 0.0)
            )   # ★ 거시경제 + 버핏지수 일별 저장
            db_records    = []
            vol_records   = []
            for stock in self.s.stocks:
                # ★ 거시경제 섹터 민감도 적용 (유가/환율/금리 → 섹터별 주가)
                rate = stock.get('rate', 0.0)
                adjusted_rate = self.eco.apply_macro_sector_sensitivity(stock, rate)
                if adjusted_rate != rate:
                    adj_delta = (adjusted_rate - rate) / 100.0
                    stock['price'] = max(10, int(stock['price'] * (1 + adj_delta)))
                    stock['market_cap'] = stock['price'] * stock['shares']
                self.mkt.apply_stock_event(stock, silent)
                name = stock['meta']['c_name']
                db_records.append((date_str, name,
                                   int(stock['price']), int(stock['market_cap'])))
                # 투자자 거래량 배치 수집 — state.daily_volume 마지막 항목에서 읽기
                _vol_today = self.s.daily_volume.get(name, [])
                _vt = _vol_today[-1] if _vol_today else {}
                vol_records.append((
                    date_str, name,
                    int(_vt.get('foreign', 0)),
                    int(_vt.get('inst',    0)),
                    int(_vt.get('retail',  0)),
                ))

            # 분할/병합 발생 시 과거 주가 DB 보정
            for name, ratio in self.s.daily_splits.items():
                self.db.update_adjusted_price(name, ratio)

            # ★ [최적화] silent 모드(치트키)에서 주가·거래량 저장 주기 조절
            # 치트키 고속 진행 시 매일 400행 INSERT → DB 과부하 주범
            # silent=True 이면 월 1일에만 저장 (약 20배 감소), False면 매일 저장
            _save_today = (not silent) or (self.s.current_date.day == 1)
            if _save_today:
                self.db.insert_stock_records(db_records)
                self.db.insert_investor_volume_batch(vol_records)
            # ★ 하루치 전체를 commit 1회로 마무리
            self.db.flush_daily_db()
            self.mkt.check_delisting()

            # 지수 업데이트

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

        # ★ 티어 심사: 장 열림 여부와 무관하게 매일 체크
        # (3/6/9/12월 1일이 주말이면 다음 첫 거래일에 실행)
        self._check_tier_exam(silent)

        # ★ 매일 고점/저점/지속일수 업데이트
        if hasattr(self.db, 'update_scenario_log_daily'):
            self.db.update_scenario_log_daily(self.s.gri)

        # ★ 시나리오/테크/페이즈 변경 감지 — market_stats는 필요할 때 1회만 수집
        cur_scenario   = self.s.current_scenario
        _tech_jump_log = getattr(self.s, '_tech_jump_log', None)
        _phase_jump_log= getattr(self.s, '_phase_jump_log', None)
        _need_log      = (
            cur_scenario != _prev_scenario
            or _tech_jump_log is not None
            or _phase_jump_log is not None
        )

        if _need_log and hasattr(self.db, 'log_scenario_change'):
            # ★ [최적화] market_stats 하루 1회만 수집 (시나리오·테크·페이즈 공유)
            market_stats = self._collect_market_stats()
            # 대공황 트리거 판단용 캐시 업데이트
            self.s._last_market_stats = market_stats
            war      = getattr(self.s, 'war_event', {})
            bubble   = getattr(self.s, 'bubble_index', 0.0)

            # ── ① 시나리오 변경 로그 ──────────────────────
            if cur_scenario != _prev_scenario:
                _date_str = self.s.current_date.strftime('%Y-%m-%d')
                _trigger_note = getattr(self.s, '_last_scenario_trigger_note', '')
                if _trigger_note:
                    note = _trigger_note
                    self.s._last_scenario_trigger_note = ''
                else:
                    note = f"{_prev_scenario} → {cur_scenario}" if _prev_scenario else ""
                self.db.log_scenario_change(
                    date_str     = _date_str,
                    scenario     = cur_scenario,
                    gri          = self.s.gri,
                    bubble       = bubble,
                    macro        = self.s.macro,
                    war_event    = war,
                    note         = note,
                    market_stats = market_stats,
                )
                self._logged_scenario = cur_scenario

            # ── ② 테크 레벨 전환 로그 ────────────────────
            if _tech_jump_log:
                self.db.log_scenario_change(
                    date_str     = _tech_jump_log['date'],
                    scenario     = f"🚀 [테크 도약] LV{_tech_jump_log['lv']-1} → LV{_tech_jump_log['lv']} ({_tech_jump_log['lv_name']})",
                    gri          = self.s.gri,
                    bubble       = bubble,
                    macro        = self.s.macro,
                    war_event    = war,
                    note         = "LV 전환",
                    market_stats = market_stats,
                )
                self.s._tech_jump_log = None

            # ── ③ 페이즈 전환 로그 (1A→1B, 2A→2B 등) ────
            if _phase_jump_log:
                self.db.log_scenario_change(
                    date_str     = _phase_jump_log['date'],
                    scenario     = f"📡 [페이즈 전환] LV{_phase_jump_log['lv']}: {_phase_jump_log['from_phase']} → {_phase_jump_log['to_phase']} ({_phase_jump_log['phase_name']})",
                    gri          = self.s.gri,
                    bubble       = bubble,
                    macro        = self.s.macro,
                    war_event    = war,
                    note         = f"페이즈 전환 ({_phase_jump_log['from_phase']}→{_phase_jump_log['to_phase']})",
                    market_stats = market_stats,
                )
                self.s._phase_jump_log = None

        return True

    # ─────────────────────────────────────────────
    # UI용 패킷 반환
    # ─────────────────────────────────────────────
    def get_ui_packet(self) -> dict:
        weekdays = ['월', '화', '수', '목', '금', '토', '일']
        s = self.s
        return {
            "date":     f"{s.current_date.strftime('%Y-%m-%d')} ({weekdays[s.virtual_weekday]})",
            "level":    s.max_tech_reached,
            "gri":      s.gri,
            "scenario": s.current_scenario,
            "macro":    s.macro,
            # ★ [신규] UI에서 바로 쓸 수 있는 확장 필드
            "bubble_index":    getattr(s, 'bubble_index', 0.0),
            "buffett_index":   getattr(s, 'buffett_index', 0.0),
            "cycle_stage":     getattr(s, 'cycle_stage', '확장'),
            "sentiment":       getattr(s, 'sentiment', 50.0),
            "gdp_growth_rate": getattr(s, 'gdp_growth_rate', 0.05),
            "boom_event":      getattr(s, 'boom_event', {}),
            "foreign_flow_index": getattr(s, 'foreign_flow_index', 0.0),
            "market_mania_index": getattr(s, 'market_mania_index', 1.0),
            "phase":           getattr(s, '_last_processed_phase', '1A'),
            "stocks": [
                {
                    "name":  st['meta']['c_name'],
                    "price": int(st['price']),
                    "rate":  st.get('rate', 0.0),
                    "meta":  st['meta'],
                }
                for st in s.stocks
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
            # ★ 전쟁/팬데믹 진행 중이면 시나리오 덮어쓰기 방지
            war      = getattr(self.s, 'war_event', {})
            pandemic = getattr(self.s, 'pandemic_event', {})
            is_war_active      = war.get('phase') == '진행중'
            is_pandemic_active = pandemic.get('phase') == '진행중'

            if not is_war_active and not is_pandemic_active:
                self.s.current_scenario = pending.get("scenario", "✨ 대공황V (고난과 부활)")

            self.s.daily_news.append(
                f"🌅 [외부충격 극복] {cy}년, 경제가 회복 국면에 접어들었습니다!"
            )
            self.s.pending_events.pop("recovery", None)

    # ─────────────────────────────────────────────
    # 6월/12월 티어 심사 시스템
    # ─────────────────────────────────────────────
    def _check_tier_exam(self, silent: bool):
        """
        3/6/9/12월 첫 거래일: 비율 기반 전체 티어 재배정
        - 1일이 주말이면 다음 월요일(첫 거래일)에 자동 실행
        D-7: 프리미엄 예고
        D-0: 전체 종목 비율(대형15/중형45/소형40) 기준 재배정 확정
        """
        cur_date  = self.s.current_date
        cur_month = cur_date.month
        cur_day   = cur_date.day
        weekday   = self.s.virtual_weekday  # 0=월 … 4=금, 5=토, 6=일

        # ── 심사일 판정 ──────────────────────────────────────────
        # 3/6/9/12월의 첫 거래일 = 1일이 평일이면 1일, 주말이면 다음 월요일
        # 즉, 해당 월에서 요일이 0(월)~4(금)인 첫 날
        is_exam_month   = cur_month in [3, 6, 9, 12]
        is_preview_month = cur_month in [2, 5, 8, 11]

        # 심사일: 해당 분기 월의 1~3일 중 첫 번째 평일
        last_exam       = getattr(self.s, '_last_tier_exam_yearmonth', None)
        this_quarter    = f'{cur_date.year}_{cur_month}'
        is_exam_day    = is_exam_month and cur_day <= 3 and weekday <= 4 and \
                         last_exam != this_quarter
        is_preview_day = is_preview_month and cur_day == 24

        # ── D-7 심사 예고 (프리미엄 전용) ───────────────────────
        if is_preview_day and not silent:
            up_list, down_list = self._evaluate_tier_candidates()
            if self.s.has_paid_news_access and (up_list or down_list):
                up_str   = ", ".join([f"{n}({f}→{t}, {r}위)" for n, f, t, r in up_list[:5]]) or "없음"
                down_str = ", ".join([f"{n}({f}→{t}, {r}위)" for n, f, t, r in down_list[:5]]) or "없음"
                self.s.daily_news.append(
                    f"💎 [티어심사 D-7 예보] 7일 후 정기 티어 심사 예정 "
                    f"승급 예정: {up_str} / 강등 예정: {down_str} (프리미엄 전용)"
                )
            self.s.pending_events["tier_exam"] = {
                "up":      [n for n, _, _, _ in up_list],
                "down":    [n for n, _, _, _ in down_list],
                "expires": self.s.current_date.strftime('%Y-%m-%d'),
            }

        # ── D-0 심사 확정: 비율 기반 전체 재배정 ────────────────
        if is_exam_day:
            self._execute_full_tier_rebalance(silent)
            # 이번 분기 심사 완료 기록 (세이브/로드 후에도 유지됨)
            self.s._last_tier_exam_yearmonth = f'{cur_date.year}_{cur_month}'

        # ── 즉시강등: 현저한 이탈 (심사일 무관, 매일) ───────────
        self._check_immediate_demotion(silent)

    def _get_tier_caps(self) -> tuple:
        """
        비율 기반 동적 경계값.
        대형주 상위 15% / 중형주 다음 45% / 소형주 나머지 40%.
        반환: (대형주_최하위_시총, 중형주_최하위_시총)
        """
        stocks = self.s.stocks
        if len(stocks) < 3:
            return (3_000_000_000_000, 100_000_000_000)
        caps  = sorted((s['market_cap'] for s in stocks), reverse=True)
        total = len(caps)
        l_idx = max(0, int(total * 0.15) - 1)
        m_idx = max(l_idx + 1, int(total * 0.60) - 1)
        return caps[l_idx], caps[m_idx]

    def _evaluate_tier_candidates(self):
        """
        분기 심사 사전 평가 — 시총 순위 기반으로 승급/강등 후보 리스트 반환.
        HOLD_DAYS 조건 없음: 분기 심사일에 순위 기준으로 즉시 판정.
        건너뛰기 승급/강등 허용 (소형→대형 직행 등).
        """
        stocks = self.s.stocks
        if len(stocks) < 3:
            return [], []

        # 시총 순위 기반 신규 티어 계산
        sorted_stocks = sorted(stocks, key=lambda x: x['market_cap'], reverse=True)
        total       = len(sorted_stocks)
        large_limit = max(1, int(total * 0.15))
        mid_limit   = max(1, int(total * 0.60))

        # 순위 → 신규 티어 매핑
        rank_to_tier = {}
        for i, stock in enumerate(sorted_stocks):
            name = stock['meta']['c_name']
            if i < large_limit:
                rank_to_tier[name] = ('대형주', i + 1)
            elif i < mid_limit:
                rank_to_tier[name] = ('중형주', i + 1)
            else:
                rank_to_tier[name] = ('소형주', i + 1)

        up_list   = []
        down_list = []
        tier_rank = {'소형주': 0, '중형주': 1, '대형주': 2}

        for stock in stocks:
            meta     = stock['meta']
            name     = meta['c_name']
            cur_tier = meta['tier']
            if name not in rank_to_tier:
                continue
            new_tier, rank = rank_to_tier[name]
            if new_tier == cur_tier:
                continue
            if tier_rank[new_tier] > tier_rank[cur_tier]:
                up_list.append((name, cur_tier, new_tier, rank))
            else:
                down_list.append((name, cur_tier, new_tier, rank))

        return up_list, down_list

    def _execute_full_tier_rebalance(self, silent: bool):
        """
        정기심사 D-0: 전체 종목을 시총 순위 기준으로 비율 재배정.
        대형주 상위 15% / 중형주 다음 45% / 소형주 나머지 40%
        """
        stocks = self.s.stocks
        if len(stocks) < 3:
            return

        _HP_SPEC = {
            '대형주': {'soft_cap': 100.0, 'sensitivity': 0.1, 'shield_ratio': 0.015},
            '중형주': {'soft_cap': 80.0,  'sensitivity': 0.5, 'shield_ratio': 0.003},
            '소형주': {'soft_cap': 60.0,  'sensitivity': 1.2, 'shield_ratio': 0.0},
        }

        sorted_stocks = sorted(stocks, key=lambda x: x['market_cap'], reverse=True)
        total       = len(sorted_stocks)
        large_limit = max(1, int(total * 0.15))
        mid_limit   = max(1, int(total * 0.60))

        promoted = []
        demoted  = []

        for i, stock in enumerate(sorted_stocks):
            meta = stock['meta']
            old_tier = meta['tier']

            if i < large_limit:
                new_tier = '대형주'
            elif i < mid_limit:
                new_tier = '중형주'
            else:
                new_tier = '소형주'

            if new_tier == old_tier:
                continue

            spec    = _HP_SPEC[new_tier]
            old_cap = meta.get('hp_soft_cap', spec['soft_cap'])
            old_hp  = meta.get('hp', old_cap)
            hp_ratio = old_hp / max(1.0, old_cap)

            meta['tier']             = new_tier
            meta['hp_soft_cap']      = spec['soft_cap']
            meta['hp']               = round(min(spec['soft_cap'], hp_ratio * spec['soft_cap']), 2)
            meta['risk_sensitivity'] = spec['sensitivity']

            if new_tier == '소형주':
                meta['shield'] = 0.0
            elif old_tier == '소형주':
                meta['shield'] = round(stock['market_cap'] * spec['shield_ratio'], 2)
            else:
                meta['shield'] = round(min(
                    meta.get('shield', 0.0),
                    stock['market_cap'] * spec['shield_ratio']
                ), 2)

            meta['cap_exceed_days'] = 0
            meta['cap_below_days']  = 0

            # 승격/강등 분류 (순위 정보 포함)
            tier_rank = {'소형주': 0, '중형주': 1, '대형주': 2}
            if tier_rank[new_tier] > tier_rank[old_tier]:
                promoted.append(f"{meta['c_name']}({old_tier}→{new_tier}, {i+1}위)")
            else:
                demoted.append(f"{meta['c_name']}({old_tier}→{new_tier}, {i+1}위)")

        if not silent:
            cy = self.s.current_date.year
            qtr = {3: '1분기', 6: '2분기', 9: '3분기', 12: '4분기'}.get(
                self.s.current_date.month, '')
            self.s.daily_news.append(
                f"📋 [{cy} {qtr} 티어 정기심사] 전체 {total}개 종목 재배정 완료 — "
                f"대형주 {large_limit}개 / 중형주 {mid_limit - large_limit}개 / "
                f"소형주 {total - mid_limit}개"
            )
            if promoted and len(promoted) <= 10:
                self.s.daily_news.append(f"🔼 [승격] {', '.join(promoted)}")
            if demoted and len(demoted) <= 10:
                self.s.daily_news.append(f"🔽 [강등] {', '.join(demoted)}")

    def _execute_tier_exam(self, silent: bool):
        """기존 D-0 개별 심사 (하위 호환용, _execute_full_tier_rebalance로 대체됨)"""
        self._execute_full_tier_rebalance(silent)

    def _execute_tier_exam_legacy(self, silent: bool):
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
        """
        즉시강등: 해당 티어 최하위 시총의 50% 미만이면 당일 강등.
        정기심사를 기다리기엔 너무 명백한 이탈 케이스만 처리.
        예: 대형주 최하위 5조인데 내 시총 2.5조 미만 → 즉시 중형주
        """
        stocks = self.s.stocks
        if len(stocks) < 3:
            return

        # ★ [최적화] 티어별 최하위 시총 — 단일 패스 O(N)
        large_min = float('inf')
        mid_min   = float('inf')
        for _s in stocks:
            _mc   = _s['market_cap']
            _tier = _s['meta']['tier']
            if _tier == '대형주' and _mc < large_min:
                large_min = _mc
            elif _tier == '중형주' and _mc < mid_min:
                mid_min = _mc
        large_min = 0 if large_min == float('inf') else large_min
        mid_min   = 0 if mid_min   == float('inf') else mid_min

        # 즉시강등 임계: 최하위의 50%
        THRESHOLD = 0.50

        _HP_SPEC = {
            '중형주': {'soft_cap': 80.0,  'sensitivity': 0.5, 'shield_ratio': 0.003},
            '소형주': {'soft_cap': 60.0,  'sensitivity': 1.2, 'shield_ratio': 0.0},
        }

        for stock in stocks:
            meta  = stock['meta']
            name  = meta['c_name']
            tier  = meta['tier']
            mc    = stock['market_cap']
            hp    = meta.get('hp', 50.0)
            sc    = meta.get('hp_soft_cap', 60.0)
            hp_r  = hp / max(1.0, sc)
            loss  = meta.get('continuous_loss_count', 0)

            to_tier = None
            reason  = ""

            if tier == '대형주' and large_min > 0:
                if mc < large_min * THRESHOLD:
                    to_tier = '중형주'
                    reason  = f"시총 {mc//100_000_000:.0f}억 (대형주 최하위 {large_min//100_000_000:.0f}억의 {mc/large_min*100:.0f}%)"
                elif loss >= 6:
                    to_tier = '중형주'; reason = f"연속 적자 {loss}분기"
                elif hp_r < 0.15:
                    to_tier = '중형주'; reason = f"재무 체력 위험 ({hp_r*100:.0f}%)"

            elif tier == '중형주' and mid_min > 0:
                if mc < mid_min * THRESHOLD:
                    to_tier = '소형주'
                    reason  = f"시총 {mc//100_000_000:.0f}억 (중형주 최하위 {mid_min//100_000_000:.0f}억의 {mc/mid_min*100:.0f}%)"
                elif loss >= 5:
                    to_tier = '소형주'; reason = f"연속 적자 {loss}분기"
                elif hp_r < 0.15:
                    to_tier = '소형주'; reason = f"재무 체력 위험 ({hp_r*100:.0f}%)"

            if to_tier and meta['tier'] != to_tier:
                old_tier = meta['tier']
                spec     = _HP_SPEC[to_tier]
                meta['tier'] = to_tier

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
                        f"📉 [즉시강등] {name}: {old_tier} → {to_tier} ({reason})"
                    )
    # ─────────────────────────────────────────────
    # ★ 외부 충격 이벤트 (GRI 분산 충격으로 개선)
    # ─────────────────────────────────────────────
    def _check_external_shock(self, silent: bool):
        """
        [개선] GRI 즉시 충격 상한 -15%, 나머지 60%는 _scenario_drift_penalty로 분산
        [개선] 버블지수 높을수록 확률 상승 (고평가 시장이 외부 충격에 취약)
        """
        import random as _rnd
        cur = self.s.current_date

        if cur.month != 1 or cur.day > 7:
            return

        scenario = self.s.current_scenario
        if any(x in scenario for x in ["대공황", "전쟁", "분쟁", "팬데믹", "외부충격"]):
            return

        last_shock_year = getattr(self.s, '_last_external_shock_year', 0)
        if last_shock_year == cur.year:
            return

        years_since_start = cur.year - self.s.start_date.year
        if years_since_start >= 3 and len(self.s.stocks) >= 200:
            self.s._market_fully_formed = True
        if not getattr(self.s, '_market_fully_formed', False):
            return

        years_since_shock = cur.year - last_shock_year if last_shock_year > 0 else 10
        if years_since_shock < 3:
            return

        # ★ [수정] GRI 회복률 체크 — 직전 저점 대비 70% 미만 회복이면 새 충격 차단
        # 현실: 아직 전 위기에서 못 벗어난 시장에 연속으로 충격이 오지 않음
        _gri_trough = getattr(self.s, '_gri_trough_after_crisis', self.s.gri)
        _gri_peak_before = getattr(self.s, '_gri_peak_before_crisis', self.s.gri)
        if _gri_peak_before > 0:
            _recovery_ratio = (self.s.gri - _gri_trough) / max(1.0, _gri_peak_before - _gri_trough)
            if _recovery_ratio < 0.70:
                return  # 아직 70% 회복 못 함 → 새 충격 차단

        # ★ [수정] 버블 50 이상일 때만 외부충격 발동
        # 현실: 시장이 과열되지 않은 상태에서 패닉이 와도 충격이 작음
        bubble = getattr(self.s, 'bubble_index', 0.0)
        if bubble < 50:
            return

        # ★ [수정] 이미 전고점 대비 -20% 이상 조정 중이면 차단 (이미 충격 흡수 중)
        _peak = getattr(self.s, 'peak_gri', self.s.gri)
        if self.s.gri < _peak * 0.80:
            return

        base_prob   = min(0.12, years_since_shock * 0.015)
        # 버블 100 이상부터 확률 가중 (최대 1.5배)
        bubble_mult = 1.0 + min(0.5, max(0.0, (bubble - 100) / 200))
        final_prob  = min(0.15, base_prob * bubble_mult)

        if _rnd.random() > final_prob:
            return

        shock_type = _rnd.choices(
            ["글로벌 금융위기", "글로벌 침체 동조", "일시적 패닉"],
            weights=[0.15, 0.35, 0.50]
        )[0]

        self.s._last_external_shock_year = cur.year
        self.s._last_crisis_year         = cur.year

        # ★ [신규] 회복률 추적용 고점/저점 기록
        self.s._gri_peak_before_crisis = self.s.gri
        self.s._gri_trough_after_crisis = self.s.gri

        # ★ 발동 조건 기록 (발동 확정 직후)
        self.s._last_scenario_trigger_note = (
            f"외부충격 | {shock_type} | "
            f"확률:{final_prob*100:.1f}% | 버블:{bubble:.0f} | "
            f"직전충격:{last_shock_year}년({years_since_shock}년경과)"
        )

        # ★ [수정] 낙폭 현실화
        # 현실: 금융위기(2008) 코스피 -54%, 침체 -20~30%, 패닉 -10~15%
        # 코스피 역사상 최악은 IMF(-70%), 그 이하는 없음
        if shock_type == "글로벌 금융위기":
            intensity     = _rnd.uniform(0.20, 0.35)   # 기존 0.30~0.50 → 0.20~0.35
            duration_days = _rnd.randint(252, 756)      # 기존 504~1260 → 252~756
            scenario_name = "📉 글로벌 금융위기 (외부 충격)"
            edu_text      = "(글로벌 경기 동조화 → 동반 하락)"
        elif shock_type == "글로벌 침체 동조":
            intensity     = _rnd.uniform(0.10, 0.20)   # 기존 0.15~0.30 → 0.10~0.20
            duration_days = _rnd.randint(126, 378)      # 기존 252~504 → 126~378
            scenario_name = "📉 글로벌 침체 동조 (외부 충격)"
            edu_text      = "(해외 경기침체 → 수출 감소 → 실적 악화)"
        else:
            intensity     = _rnd.uniform(0.05, 0.12)   # 기존 0.08~0.18 → 0.05~0.12
            duration_days = _rnd.randint(21, 63)
            scenario_name = "📉 일시적 시장 패닉 (외부 충격)"
            edu_text      = "(단기 패닉 → 빠른 회복 가능)"

        # ── GRI 충격 분산 적용 ────────────────────────
        # 즉시 충격: 40%, 최대 -15% 상한
        # 나머지 60%: _scenario_drift_penalty로 기간 분산
        immediate    = min(0.15, intensity * 0.40)
        deferred     = intensity * 0.60
        daily_penalty = -(deferred / max(1, duration_days))

        self.s.gri = max(50.0, self.s.gri * (1.0 - immediate))  # ★ 플로어 100→50
        self.s._scenario_drift_penalty = daily_penalty

        self.s.current_scenario = scenario_name
        self.s.scenario_timer   = duration_days

        recovery_date = cur + timedelta(days=duration_days)
        self.s.pending_events["recovery"] = {
            "date":     recovery_date,
            "scenario": f"✨ {shock_type} 극복 (회복 국면)",
            "notified": False,
        }

        if not silent:
            self.s.daily_news.append(
                f"🌏 [외부 충격] {cur.year}년 {shock_type} 발생! "
                f"즉시 GRI -{immediate*100:.0f}% + {duration_days//252}년간 추가 하락 압력 "
                f"{edu_text}"
            )
            self.s.daily_news.append(
                f"  └ 회복 예정: {recovery_date.strftime('%Y-%m-%d')} "
                f"| 버블지수 {bubble:.0f} 반영"
            )
    # ─────────────────────────────────────────────
    # ★ 전쟁/분쟁 이벤트
    # ─────────────────────────────────────────────
    def _check_war_event(self, silent: bool):
        """
        랜덤 전쟁/분쟁 이벤트.
        - 지역분쟁: 연 8% 확률, 6개월~2년 지속
        - 대규모전쟁: 연 3% 확률, 2~5년 지속
        - 대공황 중 / 이미 전쟁 중이면 발생 안 함
        - 지역별 원자재 충격 차별화
        - 재건 섹터 연동
        """
        cur = self.s.current_date

        # ★ 전쟁 진행 중이면 매일 timer 차감 (월 체크 전에 먼저 처리)
        war = getattr(self.s, 'war_event', {})
        if war.get('phase') == '진행중':
            timer = war.get('timer', 0)
            if timer <= 0:
                self.s.war_event['phase'] = '종전'
                self._on_war_end(silent)
            else:
                self.s.war_event['timer'] = timer - 1
                # ★ 중동 전쟁: 유가 점진 상승 (주 1회 체크)
                if war.get('oil_pressure') and cur.weekday() == 0:
                    oil_now = self.s.macro.get('oil_price', 30.0)
                    # 유가가 전쟁 중 최대 2.5배까지 점진 상승
                    # 매주 0.3~0.8% 상승 (연간 약 15~40%)
                    if oil_now < 200.0:
                        self.s.macro['oil_price'] = oil_now * random.uniform(1.003, 1.008)
            return  # 전쟁 중엔 새 전쟁 발생 안 함

        # 종전 후 재건 중이면 timer 차감 + 원자재 점진 하락
        if war.get('phase') == '종전':
            recon_timer = war.get('recon_timer', 0)
            if recon_timer <= 0:
                self.s.war_event = {}  # 완전 종료
                if not silent:
                    self.s.daily_news.append("✅ [재건 완료] 전후 재건이 마무리되었습니다.")
            else:
                self.s.war_event['recon_timer'] = recon_timer - 1
                # ★ 원자재 점진 정상화 (주 1회, 재건 기간 동안)
                if war.get('commodity_recovery') and cur.weekday() == 0:
                    region = war.get('recon_region', '')
                    if region == '중동':
                        oil_now = self.s.macro.get('oil_price', 30.0)
                        # 유가 매주 0.3~0.7% 하락 → 재건 1~3년간 서서히 정상화
                        self.s.macro['oil_price'] = max(30.0, oil_now * random.uniform(0.993, 0.997))
                    elif region == '동유럽':
                        grain = self.s.macro.get('grain_price', 250.0)
                        metal = self.s.macro.get('metal_price', 1800.0)
                        self.s.macro['grain_price'] = max(250.0, grain * random.uniform(0.994, 0.998))
                        self.s.macro['metal_price'] = max(1800.0, metal * random.uniform(0.994, 0.998))
                    elif region == '아프리카':
                        metal = self.s.macro.get('metal_price', 1800.0)
                        self.s.macro['metal_price'] = max(1800.0, metal * random.uniform(0.994, 0.998))
            return

        # ★ 새 전쟁 발생 체크 — 매년 1월 1~7일 (연 1회 고정)
        # [버그 수정] 기존: random.randint(1,7) 매 호출 → 월별 1/7 통과
        # [수정] 1월에만 체크, 쿨다운/조건 기반 발동
        if cur.month != 1 or cur.day > 7:
            return

        # 대공황 중엔 전쟁 없음
        if '대공황' in self.s.current_scenario and '극복' not in self.s.current_scenario:
            return

        # 시장 형성 전엔 없음
        if not getattr(self.s, '_market_fully_formed', False):
            return

        # ── 지역별 쿨다운 차등 체크 ──────────────
        # 중동/아프리카: 만성 분쟁 지역 → 짧은 쿨다운
        # 동남아/동유럽: 공급망 핵심 → 긴 쿨다운
        # ★ [수정] 지역 쿨다운 강화 (버그: 아프리카 20년에 4번)
        _REGION_COOLDOWN = {
            '중동':     5,   # 3→5년
            '아프리카': 8,   # 4→8년
            '동유럽':   12,  # 8→12년
            '동남아':   15,  # 10→15년
        }
        # 마지막 전쟁 지역별 연도 기록
        _last_war_by_region = getattr(self.s, '_last_war_by_region', {})

        # 전체 악재 쿨다운: 2년 (어느 지역이든 전쟁 직후 2년은 없음)
        last_crisis = getattr(self.s, '_last_crisis_year', 0)
        if last_crisis and cur.year - last_crisis < 2:
            return

        # ── 지역별 긴장도 → 가중 확률 ────────────
        # ★ 인과관계 수정: 전쟁이 먼저, 원자재는 결과
        # 중동: 지정학적 만성 긴장 → 항상 높은 기본 가중치 (유가와 무관)
        # 동유럽: 경기침체/수축기에 강대국 충돌 위험 상승
        # 동남아: SOX 높을수록 반도체 공급망 견제 위험 (기술패권 갈등)
        # 아프리카: 금속 수요/가격 높을수록 자원 이권 분쟁
        macro  = self.s.macro
        sox    = macro.get('semi_index', 1000.0)
        grain  = macro.get('grain_price', 250.0)
        metal  = macro.get('metal_price', 1800.0)
        cycle  = getattr(self.s, 'cycle_stage', '확장')

        base_weights = {
            '중동':    3.0,   # 항상 높음 (지정학적 만성 긴장 지역)
            '동유럽':  1.0 + (1.5 if cycle in ('수축', '저점') else 0.0),  # 경기침체기 위험
            '동남아':  1.0 + max(0.0, (sox - 1000) / 2000),   # SOX 높을수록 위험
            '아프리카': 1.0 + max(0.0, (metal - 2000) / 2000), # 금속가 높을수록 위험
        }

        # 지역별 가중치 (쿨다운 중인 지역은 가중치 0)
        region_weight = {}
        for rgn, base_w in base_weights.items():
            last_yr  = _last_war_by_region.get(rgn, 0)
            cooldown = _REGION_COOLDOWN.get(rgn, 5)
            if last_yr and cur.year - last_yr < cooldown:
                region_weight[rgn] = 0.0  # 쿨다운 중
            else:
                region_weight[rgn] = base_w

        # 모든 지역이 쿨다운이면 패스
        if sum(region_weight.values()) <= 0:
            return

        # ★ [수정] 버블 고점에서 전쟁 트리거 차단 (버그: 버블 270에서도 전쟁 발발)
        # 현실: 시장 과열과 전쟁 발발은 독립적이지만
        # 게임에서 버블 고점 전쟁은 수혜 섹터 부스트와 합쳐져 GRI 폭발
        _bubble_now = getattr(self.s, 'bubble_index', 0.0)
        if _bubble_now >= 200:
            return  # 버블 200+ 상태에서 신규 전쟁 차단

        roll = random.random()
        if roll < 0.015:
            war_type = '대규모전쟁'
            duration = random.randint(504, 1008)   # 최대 4년 → 3년으로 단축
        elif roll < 0.050:
            war_type = '지역분쟁'
            duration = random.randint(126, 378)    # 최대 2년 → 1.5년으로 단축
        else:
            return

        region = random.choices(
            list(region_weight.keys()),
            weights=list(region_weight.values())
        )[0]

        # ── 원자재 충격 즉시 적용 ─────────────────
        # 중동: 유가 단계적 상승 (즉시 1.4배 + 전쟁 기간 중 추가 압력)
        COMMODITY_SHOCK = {
            '중동':    {'oil_price':   1.4},
            '동유럽':  {'grain_price': 1.6, 'metal_price': 1.3},
            '동남아':  {'semi_index':  0.72, 'metal_price': 1.4},  # ★ [수정] 0.65→0.72
            '아프리카': {'metal_price': 1.5},
        }
        shocks = COMMODITY_SHOCK.get(region, {})
        for key, mult in shocks.items():
            if key in self.s.macro:
                if key == 'semi_index':
                    # ★ [수정] SOX 충격에 절대 하한 보장 (버그4)
                    _sox_before = self.s.macro[key]
                    _phase_now  = getattr(self.s, '_last_processed_phase', '1A')
                    _sox_floor_war = {
                        '1A': 50.0, '1B': 150.0, '2A': 500.0, '2B': 1500.0,
                        '3A': 5000.0, '3B': 12000.0, '4A': 30000.0, '4B': 99999.0,
                    }.get(_phase_now, 100.0)
                    self.s.macro[key] = max(_sox_floor_war, _sox_before * mult)
                else:
                    self.s.macro[key] *= mult

        # ★ 중동 전쟁: 유가 추가 압력 예약 (전쟁 기간 내내 유가 상승)
        if region == '중동':
            # 전쟁 기간 중 유가를 매일 소폭 추가 상승시키는 플래그
            self.s.war_event = self.s.war_event if self.s.war_event else {}
            self.s.war_event['oil_pressure'] = True   # economy.py에서 참조

        # ── GRI 충격 분산 적용 ────────────────────────
        raw_impact = {'지역분쟁': 0.10, '대규모전쟁': 0.25}[war_type]
        immediate  = min(0.12, raw_impact * 0.50)
        deferred   = raw_impact * 0.50
        self.s.gri = max(50.0, self.s.gri * (1.0 - immediate))  # ★ 플로어 100→50
        self.s._scenario_drift_penalty = -(deferred / max(1, duration))

        # ★ 발동 조건 기록
        _war_shocks = ', '.join(f"{k}×{v:.1f}" for k, v in shocks.items())
        self.s._last_scenario_trigger_note = (
            f"{war_type} 발발 | 지역:{region} | 원자재:{_war_shocks} | "
            f"버블{getattr(self.s,'bubble_index',0):.0f} 금리{self.s.macro.get('interest_rate',0):.2f}%"
        )

        # 전쟁 상태 저장
        self.s.war_event = {
            'type':         war_type,
            'region':       region,
            'timer':        duration,
            'phase':        '진행중',
            'notified':     False,
            'oil_pressure': (region == '중동'),  # 중동 유가 압력 플래그
        }

        # 시나리오 반영
        scenario_map = {
            ('지역분쟁',   '중동'):    '🔫 중동 분쟁 (유가 충격)',
            ('지역분쟁',   '동유럽'):  '🔫 동유럽 분쟁 (곡물/금속 충격)',
            ('지역분쟁',   '동남아'):  '🔫 동남아 분쟁 (반도체 공급 차질)',
            ('지역분쟁',   '아프리카'):'🔫 아프리카 분쟁 (금속 공급 차질)',
            ('대규모전쟁', '중동'):    '💣 중동 전쟁 (유가 폭등)',
            ('대규모전쟁', '동유럽'):  '💣 동유럽 전쟁 (곡물/금속 폭등)',
            ('대규모전쟁', '동남아'):  '💣 동남아 전쟁 (반도체 위기)',
            ('대규모전쟁', '아프리카'):'💣 아프리카 전쟁 (자원 전쟁)',
        }
        self.s.current_scenario = scenario_map.get((war_type, region), f'🔫 {region} {war_type}')
        self.s.scenario_timer   = duration

        # ★ 쿨다운 기록 (지역별 + 전체)
        self.s._last_crisis_year = cur.year
        _last_war_by_region[region] = cur.year
        self.s._last_war_by_region  = _last_war_by_region

        if not silent:
            commodity_str = ', '.join(f"{k} x{v:.1f}" for k, v in shocks.items())
            extra = " (전쟁 기간 내내 유가 추가 상승 예정)" if region == '중동' else ""
            self.s.daily_news.append(
                f"⚔️ [{war_type} 발생] {cur.year}년 {region} {war_type} 발발! "
                f"원자재 충격: {commodity_str} | GRI -{immediate*100:.0f}%{extra}"
            )
            self.s.daily_news.append(
                f"  └ 예상 지속: 약 {duration//252}년 {(duration%252)//21}개월 "
                f"| 산업재/소재/유틸 섹터 수혜 예정"
            )

    def _on_war_end(self, silent: bool):
        """종전 처리 — 산업재/소재/유틸리티에 임시 버프 주입 (재건 섹터 대체)"""
        war      = getattr(self.s, 'war_event', {})
        region   = war.get('region', '')
        war_type = war.get('type', '')

        recon_days = random.randint(252, 756)  # 1~3년 재건

        # 재건 상태로 전환
        self.s.war_event['phase']       = '종전'
        self.s.war_event['recon_timer'] = recon_days
        self.s.war_event['oil_pressure'] = False  # 유가 압력 해제

        # ★ 원자재 점진 정상화 예약
        # 종전 후 재건 기간 동안 원자재가 서서히 하락
        # economy.py update_macro_logic()에서 참조하는 플래그
        self.s.war_event['commodity_recovery'] = True
        self.s.war_event['recon_region'] = region

        # ★ 재건 섹터 대신 기존 산업에 임시 버프 주입
        # [수정] 기존값(0.015/0.010/0.008)은 일별 기준으로 연 +378%/+252%/+201% — 현실과 동떨어짐
        # 현실 재건 수혜: 연 +8~15% 추가 수익. 일별로 환산하면 0.015/0.010/0.008 / 252
        # 재건 기간이 252~756일(1~3년)이므로 기간 내내 적용 → 연 수혜 수준으로 설정
        from datetime import timedelta
        expire_dt = self.s.current_date + timedelta(days=recon_days)
        self.s.temp_sector_buff = {
            "산업재":   (0.00005, expire_dt),   # 연 +1.3% 추가 (재건 수혜 현실적)
            "소재":     (0.00003, expire_dt),   # 연 +0.8%
            "유틸리티": (0.00002, expire_dt),   # 연 +0.5%
        }

        # ★ 동남아 전쟁 종전 시 SOX 점진 회복 (즉시 +15% 점프 제거 → 자연 수렴으로)
        # [수정] semi_index * 1.15 즉시 주입은 GRI 급등의 원인 — 제거
        if region == '동남아':
            # SOX는 _update_commodity_prices()의 자연 수렴 로직이 회복을 처리하도록 둠
            # 단, 전쟁 중 0.97배/일로 눌렸던 것을 해제하는 효과만 있으면 충분
            pass

        # 재건 시나리오로 전환
        self.s.current_scenario = f'🏗️ {region} 전후 재건 (산업재/소재 강세)'
        self._trigger_scenario_themes('재건')
        self.s.scenario_timer   = recon_days

        if not silent:
            self.s.daily_news.append(
                f"🕊️ [종전] {region} {war_type} 종료! "
                f"재건 국면 돌입 — 재건/산업재/소재 섹터 수혜 예상"
            )
            if region == '중동':
                self.s.daily_news.append(
                    f"  └ 중동 유가 압력 해제 — 유가 점진적 하락 예상"
                )

    # ─────────────────────────────────────────────
    # ★ 팬데믹 이벤트
    # ─────────────────────────────────────────────
    def _check_pandemic_event(self, silent: bool):
        """
        랜덤 팬데믹 이벤트.
        - 연 2% 확률 발생
        - 비대면/IT/건강관리 수혜
        - 여행/오프라인/자유소비재 타격
        - GRI -20~35% 후 V자 반등
        - 전쟁 중 / 대공황 중엔 발생 안 함
        """
        cur = self.s.current_date

        # 매년 랜덤 월 체크
        # ★ [수정] 팬데믹 진행 중 처리는 매일 실행 (기존: day>7 return으로 월 7회만 차감 → 5년 지속)
        pandemic = getattr(self.s, 'pandemic_event', {})
        if pandemic.get('phase') == '진행중':
            timer = pandemic.get('timer', 0)

            # ★ [신규] 유동성 반등 처리 (6~9개월 후 자동 발동)
            _liq = self.s.pending_events.get('pandemic_liquidity', {})
            if _liq and not _liq.get('activated') and cur >= _liq.get('date', cur):
                # ★ [수정] 버블 100 이상에서 유동성 장세 GRI 부스트 차단 (버그: 버블 200 팬데믹 +47%)
                _bubble_liq = getattr(self.s, 'bubble_index', 0.0)
                self.s._pandemic_liquidity_active = True
                self.s.pending_events['pandemic_liquidity']['activated'] = True
                if _bubble_liq < 100:
                    _boost = random.uniform(0.05, 0.15)
                    self.s.gri = self.s.gri * (1.0 + _boost)
                    if not silent:
                        self.s.daily_news.append(
                            f"💉 [팬데믹 유동성 장세] 각국 양적완화 투입 → "
                            f"GRI +{_boost*100:.0f}% 반등. IT/헬스케어 수혜 본격화"
                        )
                else:
                    # 버블 과열 시 drift만 전환, 즉시 부스트 없음
                    if not silent:
                        self.s.daily_news.append(
                            f"💉 [팬데믹 유동성 장세] 양적완화 투입 — 단, 고버블({_bubble_liq:.0f}) 상태로 GRI 부스트 제한"
                        )

            if timer <= 0:
                self.s.pandemic_event = {'phase': '종료'}
                self.s._pandemic_liquidity_active = False
                from datetime import timedelta
                rec_date = cur + timedelta(days=random.randint(63, 126))
                self.s.pending_events['recovery'] = {
                    'date':     rec_date,
                    'scenario': '✨ 팬데믹 극복 (V자 반등)',
                    'notified': False,
                }
                if not silent:
                    self.s.daily_news.append(
                        f"💊 [팬데믹 종식] 위기가 진정됩니다. V자 반등 기대."
                    )
            else:
                self.s.pandemic_event['timer'] = timer - 1
            return  # 팬데믹 중엔 새 팬데믹 없음

        # ★ [수정] 신규 팬데믹 발생 체크는 월초에만
        if cur.day > 7:
            return

        # 전쟁 중 / 대공황 중엔 팬데믹 없음
        war = getattr(self.s, 'war_event', {})
        if war.get('phase') == '진행중':
            return
        if '대공황' in self.s.current_scenario and '극복' not in self.s.current_scenario:
            return
        if not getattr(self.s, '_market_fully_formed', False):
            return

        # 발생 확률 체크 — 매년 1월 1~7일에만 (연 1회 체크)
        # ★ 팬데믹 확률 상향: 1.5% → 4% (26년간 기댓값 약 1회)
        if cur.month != 1 or cur.day > 7:
            return

        # ★ 팬데믹 전용 쿨다운: 15년 (글로벌 팬데믹은 100년에 1~2회 수준)
        last_pandemic = getattr(self.s, '_last_pandemic_year', 0)
        if last_pandemic and cur.year - last_pandemic < 15:
            return

        # ★ 악재 쿨다운: 전쟁/위기 후 2년 이내엔 팬데믹 없음
        last_crisis = getattr(self.s, '_last_crisis_year', 0)
        if last_crisis and cur.year - last_crisis < 2:
            return

        if random.random() > 0.04:
            return

        # ★ 팬데믹 발생 시 쿨다운 기록
        self.s._last_crisis_year  = cur.year
        self.s._last_pandemic_year = cur.year

        # ★ [수정] 팬데믹 현실화 — 코로나형 구조
        # 현실: 초반 -20~30% 충격 → 6개월 후 유동성 투입 → 오히려 급등
        # (코로나: 2020년 3월 -35% → 연말까지 +80%)
        intensity = random.uniform(0.15, 0.25)   # 기존 0.20~0.35 → 0.15~0.25 (초기 충격 완화)
        duration  = random.randint(365, 730)

        # GRI 즉시 충격 (초반 공포)
        self.s.gri = max(50.0, self.s.gri * (1.0 - intensity))

        # ★ 유동성 반등 예약 — 6~9개월 후 boom_event 자동 발동
        # 현실: 각국 정부 유동성 투입 → 비대면/IT/헬스케어 장세
        _liquidity_delay = random.randint(126, 189)  # 6~9개월
        from datetime import timedelta
        _liquidity_date = self.s.current_date + timedelta(days=_liquidity_delay)
        self.s.pending_events['pandemic_liquidity'] = {
            'date': _liquidity_date,
            'activated': False,
        }

        # 팬데믹 상태 저장
        if not hasattr(self.s, 'pandemic_event'):
            self.s.pandemic_event = {}
        self.s.pandemic_event = {
            'phase': '진행중',
            'timer': duration,
            'intensity': intensity,
            'liquidity_triggered': False,
        }

        # ★ 발동 조건 기록
        self.s._last_scenario_trigger_note = (
            f"팬데믹 발동 | 강도:{intensity*100:.0f}% | "
            f"연 4% 확률 | 직전위기:{getattr(self.s,'_last_crisis_year',0)}년 | "
            f"GRI:{self.s.gri:.0f} 버블:{getattr(self.s,'bubble_index',0):.0f}"
        )

        # 시나리오 전환
        self.s.current_scenario = '🦠 글로벌 팬데믹 (비대면 전환)'
        self.s.scenario_timer   = duration
        self._trigger_scenario_themes('팬데믹')

        # 원자재 충격 (공급망 차질)
        self.s.macro['semi_index'] = self.s.macro.get('semi_index', 1000.0) * 0.75
        self.s.macro['metal_price']   = self.s.macro.get('metal_price', 100.0) * 0.85

        if not silent:
            self.s.daily_news.append(
                f"🦠 [팬데믹 발생] {cur.year}년 글로벌 팬데믹 발발! "
                f"GRI -{intensity*100:.0f}% 충격 | 비대면/IT/건강관리 수혜 예상"
            )
            self.s.daily_news.append(
                f"  └ 여행/오프라인/자유소비재 타격 | 예상 지속: "
                f"약 {duration//252}년 {(duration%252)//21}개월"
            )

    # ─────────────────────────────────────────────
    # ★ 시장 통계 수집 (시나리오 로그용)
    # ─────────────────────────────────────────────
    def _collect_market_stats(self) -> dict:
        """PER/섹터별/산업별 등락률 수집 — O(n) 단일 패스"""
        from engine.constants import SECTOR_MAP
        stocks = self.s.stocks
        if not stocks:
            return {}

        # 단일 루프로 전부 수집
        per_sum  = {'대형주': 0.0, '중형주': 0.0, '소형주': 0.0}
        per_cnt  = {'대형주': 0,   '중형주': 0,   '소형주': 0}
        sec_sum  = {}; sec_cnt = {}
        ind_sum  = {}; ind_cnt = {}

        eh = self.s.earnings_history  # 참조만

        for s in stocks:
            meta   = s['meta']
            name   = meta.get('c_name', '')
            tier   = meta.get('tier', '소형주')
            ind    = meta.get('ind', '')
            sector = SECTOR_MAP.get(ind, 'Value')
            rate   = s.get('rate', 0.0)

            # 섹터/산업 등락
            sec_sum[sector] = sec_sum.get(sector, 0.0) + rate
            sec_cnt[sector] = sec_cnt.get(sector, 0) + 1
            if ind:
                ind_sum[ind] = ind_sum.get(ind, 0.0) + rate
                ind_cnt[ind] = ind_cnt.get(ind, 0) + 1

            # PER — 최근 1년치만 빠르게 합산
            hist = eh.get(name)
            if hist:
                recent_yr = max(hist.keys())
                annual_ni = sum(
                    q.get('net_income', 0)
                    for q in hist[recent_yr].values()
                ) * 4
                mc = s.get('market_cap', 0)
                if annual_ni > 0 and mc > 0:
                    per = mc / annual_ni
                    if per < 500 and tier in per_sum:
                        per_sum[tier] += per
                        per_cnt[tier] += 1

        def wavg(s, c): return s/c if c > 0 else 0

        def wi(k): return wavg(ind_sum.get(k, 0), ind_cnt.get(k, 0))
        return {
            'per_large':      wavg(per_sum['대형주'], per_cnt['대형주']),
            'per_mid':        wavg(per_sum['중형주'], per_cnt['중형주']),
            'per_small':      wavg(per_sum['소형주'], per_cnt['소형주']),
            'sector_growth':  wavg(sec_sum.get('Growth',    0), sec_cnt.get('Growth',    0)),
            'sector_value':   wavg(sec_sum.get('Value',     0), sec_cnt.get('Value',     0)),
            'sector_cyclical': wavg(sec_sum.get('Cyclical', 0), sec_cnt.get('Cyclical',  0)),
            'ind_it':         wi('IT'),
            'ind_health':     wi('건강관리'),
            'ind_energy':     wi('에너지'),
            'ind_finance':    wi('금융'),
            'ind_industry':   wi('산업재'),
            'ind_material':   wi('소재'),
            'ind_realestate': wi('부동산'),
            'ind_util':       wi('유틸리티'),
            'ind_consumer':   wi('자유소비재'),
            'ind_staple':     wi('필수소비재'),
            'ind_comm':       wi('커뮤니케이션'),
        }

    # ─────────────────────────────────────────────
    # ★ 호재 시나리오 (경제 지표 조건 기반으로 전면 개선)
    # ─────────────────────────────────────────────
    def _check_boom_event(self, silent: bool):
        """
        [개선] 매달 랜덤 → 경제 지표 조건 충족 시 확률 발동

        기존 3종: 수출호황 / 유동성장세 / 내수붐
        신규 3종: 혁신기술붐 / 외국인대규모유입 / 정부경기부양
        """
        cur = self.s.current_date

        # ── 진행 중인 호재 tick ───────────────────────
        boom = getattr(self.s, 'boom_event', {})
        if boom.get('phase') == '진행중':
            boom['timer'] = boom.get('timer', 0) - 1
            if boom['timer'] <= 0:
                self.s.boom_event       = {}
                self.s.current_scenario = "정상 성장"
                self.s._last_boom_year  = cur.year
                # ★ [신규] 월 단위 쿨다운 기록
                self.s._last_boom_month   = cur.month
                self.s._last_boom_year_m  = cur.year
                if not silent:
                    self.s.daily_news.append("📊 [호황 종료] 경기 호황이 마무리됩니다.")
            return

        # ── 공통 차단 조건 ────────────────────────────
        scenario = self.s.current_scenario
        if any(x in scenario for x in ["전쟁", "분쟁", "대공황", "팬데믹", "외부충격",
                                        "긴축", "스태그", "버블붕괴", "환율위기",
                                        "공급망 대란", "신흥국 위기"]):
            return
        if not getattr(self.s, '_market_fully_formed', False):
            return
        last_crisis = getattr(self.s, '_last_crisis_year', 0)
        if last_crisis and cur.year - last_crisis < 1:
            return
        last_boom = getattr(self.s, '_last_boom_year', 0)
        # ★ [수정] 호황 시나리오 쿨다운 강화: 2년 → 3년
        # 현실: 같은 종류의 슈퍼사이클이 3년 내 재발하기 어려움
        if last_boom and cur.year - last_boom < 3:
            return

        # ★ [신규] 동일 월 내 호황 중복 발생 차단
        # cur.day == 1 조건은 유지하되, 최근 6개월 내 다른 호황도 체크
        _last_boom_month = getattr(self.s, '_last_boom_month', 0)
        _last_boom_year_m = getattr(self.s, '_last_boom_year_m', 0)
        _months_since_boom = (cur.year - _last_boom_year_m) * 12 + (cur.month - _last_boom_month)
        if _last_boom_month > 0 and _months_since_boom < 6:
            return   # 6개월 내 호황 시나리오 재발 차단

        if cur.day != 1:
            return

        # ── 경제 지표 참조 ────────────────────────────
        macro     = self.s.macro
        rate      = macro.get('interest_rate', 4.0)
        cpi       = macro.get('cpi', 2.0)
        sox       = macro.get('semi_index', 1000.0)
        exchange  = macro.get('exchange_rate', 1100.0)
        ff        = getattr(self.s, 'foreign_flow_index', 0.0)
        cycle     = getattr(self.s, 'cycle_stage', '확장')
        cur_phase = getattr(self.s, '_last_processed_phase', '1A')
        prev_snap = getattr(self.s, '_prev_macro_snapshot', {})
        prev_rate = prev_snap.get('interest_rate', rate)
        metal     = macro.get('metal_price', 2000.0)   
        grain     = macro.get('grain_price', 300.0)
        oil_now  = macro.get('oil_price', 30.0)

        # anchor 계산
        lv  = self.s.max_tech_reached
        ye  = max(0, cur.year - 2000)
        # ★ [수정] market.py와 동일한 lv_base 사용 (버그: 별도 1.075 → 항상 저평가 판정 → boom 과잉)
        lv_base = {
            1: 1000 * (1.038 ** ye),
            2: 1000 * (1.038**15) * (1.045**max(0, ye-15)),
            3: 1000 * (1.038**15) * (1.045**20) * (1.032**max(0, ye-35)),
            4: 1000 * (1.038**15) * (1.045**20) * (1.032**25) * (1.022**max(0, ye-60)),
        }.get(lv, 1000.0)
        anchor = self.s.gri / max(1.0, lv_base)

        roll = random.random()

        # ① 수출 호황 — SOX 강세 + 고환율 + 확장기
        sox_strong  = sox >= 1200.0
        high_fx     = exchange >= 1200.0
        expanding   = (cycle == '확장')
        export_prob = 0.030 if (sox_strong and high_fx and expanding) else 0.005

        if roll < export_prob:
            # ★ [수정] 수출호황: 1~2년 (기존 1~3년에서 단축)
            duration = random.randint(126, 504)
            self.s.boom_event = {'type': '수출호황', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "📈 수출 호황 (반도체/수출 슈퍼사이클)"
            self._trigger_scenario_themes('수출호황')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            cond = []
            if sox_strong: cond.append(f"SOX{sox:.0f}")
            if high_fx:    cond.append(f"환율{exchange:.0f}원")
            self.s._last_scenario_trigger_note = (
                f"수출호황 | {' / '.join(cond)} | 확장기:{expanding} | "
                f"CPI:{cpi:.2f}% 금리:{rate:.2f}%"
            )
            if not silent:
                self.s.daily_news.append(
                    f"📈 [수출 호황] {cur.year}년 반도체·수출 슈퍼사이클 진입! "
                    f"({' / '.join(cond)}) IT/산업재/소재 강세 예상"
                )
            return

        # ② 유동성 장세 — 금리 인하 사이클 + 외국인 이탈 없음
        rate_cutting = (rate < prev_rate) and (rate <= 3.5)
        ff_ok        = ff >= -10.0
        if rate_cutting and ff_ok and roll < (export_prob + 0.040):
            # ★ [수정] 유동성장세: 6개월~1.5년 (기존 1~2년에서 단축)
            duration = random.randint(126, 378)
            self.s.boom_event = {'type': '유동성장세', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "💰 유동성 장세 (저금리 + 외국인 유입)"
            self._trigger_scenario_themes('유동성장세')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            self.s._last_scenario_trigger_note = (
                f"유동성장세 | 금리인하사이클:{rate:.2f}% | ff:{ff:.0f} | "
                f"사이클:{cycle} | SOX:{sox:.0f}"
            )
            if not silent:
                self.s.daily_news.append(
                    f"💰 [유동성 장세] {cur.year}년 금리 인하 사이클({rate:.1f}%) + "
                    f"외국인 자금 유입 — 전 섹터 상승 모멘텀"
                )
            return

        # ③ 내수 소비 붐 — 저금리 + 확장기 + CPI 안정
        domestic_prob = 0.025 if (rate <= 3.0 and cpi <= 3.0 and expanding) else 0.004
        if roll < (export_prob + 0.040 + domestic_prob):
            # ★ [수정] 내수붐: 4~12개월 (기존 6~18개월에서 단축)
            duration = random.randint(84, 252)
            self.s.boom_event = {'type': '내수붐', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🛒 내수 소비 붐"
            self._trigger_scenario_themes('내수붐')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            self.s._last_scenario_trigger_note = (
                f"내수붐 | 금리:{rate:.2f}%(≤3%) CPI:{cpi:.2f}%(≤3%) | 확장기:{expanding}"
            )
            if not silent:
                self.s.daily_news.append(
                    f"🛒 [내수 붐] {cur.year}년 저금리({rate:.1f}%) + CPI 안정({cpi:.1f}%) "
                    f"→ 소비 심리 회복! 필수소비재/자유소비재 강세"
                )
            return

        # ④ 혁신 기술 붐 — 페이즈 2A~3B + SOX 급등 + 버블 과열 아님
        tech_ok = cur_phase in ('2A', '2B', '3A', '3B')
        if tech_ok and sox >= 1500.0 and anchor < 1.3 and \
           roll < (export_prob + 0.040 + domestic_prob + 0.020):
            # ★ [수정] 혁신기술붐: 6~18개월 (기존 1~2년 유지)
            duration = random.randint(126, 378)
            self.s.boom_event = {'type': '혁신기술붐', 'phase': '진행중', 'timer': duration}
            phase_nm = {'2A': '모바일', '2B': '클라우드/플랫폼',
                        '3A': 'AI 상용화', '3B': '양자/바이오'}.get(cur_phase, cur_phase)
            self.s.current_scenario = f"🤖 혁신 기술 붐 ({phase_nm})"
            self._trigger_scenario_themes('혁신기술붐')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            if not silent:
                self.s.daily_news.append(
                    f"🤖 [혁신 기술 붐] {cur.year}년 {phase_nm} 시대 기술 혁신 가속! "
                    f"SOX {sox:.0f} — IT/건강관리 집중 강세"
                )
            return

        # ⑤ 외국인 대규모 유입 — 외국인 반전 + 원화 강세 + 저평가
        prev_ff      = getattr(self.s, '_prev_foreign_flow', ff)
        ff_rebound   = (ff > prev_ff + 10) and (ff >= 20)
        fx_falling   = exchange < prev_snap.get('exchange_rate', exchange) * 0.99
        if ff_rebound and fx_falling and anchor < 1.0 and \
           roll < (export_prob + 0.040 + domestic_prob + 0.020 + 0.025):
            # ★ [수정] 외국인유입: 4~12개월
            duration = random.randint(84, 252)
            self.s.boom_event = {'type': '외국인유입', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🌏 외국인 대규모 유입 (원화 강세 + 저평가)"
            self._trigger_scenario_themes('외국인유입')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            if not silent:
                self.s.daily_news.append(
                    f"🌏 [외국인 대규모 유입] {cur.year}년 원화 강세({exchange:.0f}원) + "
                    f"저평가 인식 — 대형주 집중 수혜"
                )
            return

        # ⑥ 정부 경기 부양 — 수축/저점 + GRI 저평가 + 위기 후 정책 여력
        recession    = cycle in ('수축', '저점')
        policy_ready = (cur.year - getattr(self.s, '_last_crisis_year', 0)) >= 1
        if recession and anchor < 0.85 and policy_ready and \
           roll < (export_prob + 0.040 + domestic_prob + 0.020 + 0.025 + 0.040):
            # ★ [수정] 정부부양: 4~10개월 (기존 6~12개월에서 단축)
            duration = random.randint(84, 210)
            self.s.boom_event = {'type': '정부부양', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🏛️ 정부 경기 부양 (재정 확대)"
            self._trigger_scenario_themes('정부부양')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            boost = random.uniform(0.03, 0.05)
            self.s.gri = self.s.gri * (1.0 + boost)
            if not silent:
                self.s.daily_news.append(
                    f"🏛️ [정부 경기 부양] {cur.year}년 재정 확대 정책 발표! "
                    f"경기 수축({cycle}) 대응 — 산업재/인프라 수혜 "
                    f"(즉시 GRI +{boost*100:.1f}%)"
                )
            return

        # ⑦ 반도체 슈퍼사이클 — SOX 급등 + IT 섹터 집중
        sox_boom = sox >= 2000.0 and cur_phase in ('2A', '2B', '3A', '3B')
        if sox_boom and anchor < 1.5 and \
           roll < (export_prob + 0.040 + domestic_prob + 0.020 + 0.025 + 0.040 + 0.015):
            # ★ [수정] 반도체슈퍼사이클: 6~18개월 (기존 1~2.5년에서 단축)
            duration = random.randint(126, 378)
            self.s.boom_event = {'type': '반도체슈퍼사이클', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "💾 반도체 슈퍼사이클 (SOX 폭등)"
            self._trigger_scenario_themes('반도체슈퍼사이클')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            if not silent:
                self.s.daily_news.append(
                    f"💾 [반도체 슈퍼사이클] {cur.year}년 SOX {sox:.0f} — "
                    f"AI/모바일 수요 폭발로 반도체 공급 부족! "
                    f"IT/소재 집중 수혜, 단 버블 경고"
                )
            return

        # ⑧ 원자재 슈퍼사이클 — 금속/곡물 동반 강세 + 에너지 상승
        commodity_boom = (metal >= 3000.0 and oil_now >= 80.0 and grain >= 400.0)
        if commodity_boom and cycle == '확장' and \
           roll < (export_prob + 0.040 + domestic_prob + 0.020 + 0.025 + 0.040 + 0.015 + 0.015):
            # ★ [수정] 원자재슈퍼사이클: 1~2년 (기존 1.5~3년에서 단축)
            duration = random.randint(252, 504)
            self.s.boom_event = {'type': '원자재슈퍼사이클', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "⛏️ 원자재 슈퍼사이클 (에너지/금속 강세)"
            self._trigger_scenario_themes('원자재슈퍼사이클')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            if not silent:
                self.s.daily_news.append(
                    f"⛏️ [원자재 슈퍼사이클] {cur.year}년 에너지·금속·곡물 동반 강세! "
                    f"유가 ${oil_now:.0f} / 구리 ${metal:,.0f} — "
                    f"에너지/소재/산업재 수혜, IT/소비재 비용 압박"
                )
            return

        cum_prob = export_prob + 0.040 + domestic_prob + 0.020 + 0.025 + 0.040 + 0.015 + 0.015

        # ⑨ FTA 체결 / 무역 확대 — 수출 구조 개선, 특정 산업 수혜
        # 조건: 확장기 + 환율 안정 + 수출 경쟁력 있음
        fta_ok = (expanding and exchange <= 1300 and sox >= 1200)
        fta_prob = 0.018 if fta_ok else 0.003
        if roll < (cum_prob + fta_prob):
            duration = random.randint(126, 378)
            self.s.boom_event = {'type': 'FTA무역확대', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🤝 FTA 체결 (무역 자유화 · 수출 확대)"
            self._trigger_scenario_themes('FTA무역확대')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            self.s._last_boom_year   = cur.year
            if not silent:
                self.s.daily_news.append(
                    f"🤝 [FTA 체결] {cur.year}년 주요국과 자유무역협정 발효! "
                    f"IT/산업재/소재 수출 확대 — 원화 강세 + 경상수지 개선 기대"
                )
            return

        cum_prob += fta_prob

        # ⑩ 그린/인프라 투자 붐 — 정부 대규모 인프라·에너지전환 정책
        # 조건: LV2+ + 수축기 탈출 + 정부 재정 여력
        infra_ok = (lv >= 2 and cycle in ('확장', '저점') and
                    (cur.year - getattr(self.s, '_last_crisis_year', 0)) >= 2)
        infra_prob = 0.015 if infra_ok else 0.003
        if roll < (cum_prob + infra_prob):
            duration = random.randint(252, 504)
            self.s.boom_event = {'type': '인프라투자붐', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🏗️ 그린·인프라 투자 붐 (에너지전환·건설)"
            self._trigger_scenario_themes('인프라투자붐')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            self.s._last_boom_year   = cur.year
            if not silent:
                self.s.daily_news.append(
                    f"🏗️ [인프라 투자 붐] {cur.year}년 정부 대규모 그린·인프라 투자 발표! "
                    f"산업재/소재/유틸리티 강세 — 에너지 전환 수요 폭발"
                )
            return

        cum_prob += infra_prob

        # ⑪ 이머징 자금 유입 붐 — 선진국 저금리 → 이머징 시장 자금 대이동
        # 조건: LV1~2 + 금리 낮음 + 외국인 수급 개선 중
        emerging_ok = (lv <= 2 and rate <= 2.5 and
                       getattr(self.s, 'foreign_flow_index', 0.0) >= 0)
        emerging_prob = 0.020 if emerging_ok else 0.004
        if roll < (cum_prob + emerging_prob):
            duration = random.randint(126, 252)
            self.s.boom_event = {'type': '이머징붐', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🌐 이머징 자금 유입 붐 (글로벌 저금리)"
            self._trigger_scenario_themes('이머징붐')
            self.s.scenario_timer = duration
            self.s._last_boom_month  = cur.month
            self.s._last_boom_year_m = cur.year
            self.s._last_boom_year   = cur.year
            # 즉각 외국인 수급 개선
            self.s.foreign_flow_index = min(60.0,
                getattr(self.s, 'foreign_flow_index', 0.0) + 25.0)
            if not silent:
                self.s.daily_news.append(
                    f"🌐 [이머징 자금 유입] {cur.year}년 글로벌 저금리 기조로 "
                    f"외국인 자금 대규모 유입! 원화 강세 + 대형주 집중 수혜"
                )
            return

        cum_prob += emerging_prob

        # ⑨ 부동산 버블 — 저금리 장기화 + 부동산 시총 비중 급등
        realestate_cap = sum(
            s['market_cap'] for s in self.s.stocks
            if s['meta'].get('ind') == '부동산'
        )
        total_cap = sum(s['market_cap'] for s in self.s.stocks) or 1
        re_ratio  = realestate_cap / total_cap
        re_bubble = (rate <= 2.5 and re_ratio >= 0.12 and cycle == '확장')
        if re_bubble and roll < (cum_prob + 0.010):
            duration = random.randint(252, 504)
            self.s.boom_event = {'type': '부동산버블', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🏠 부동산 버블 (저금리 유동성 집중)"
            self._trigger_scenario_themes('부동산버블')
            self.s.scenario_timer = duration
            if not silent:
                self.s.daily_news.append(
                    f"🏠 [부동산 버블] {cur.year}년 저금리({rate:.1f}%) 장기화로 "
                    f"부동산 시총 비중 {re_ratio*100:.1f}%! "
                    f"부동산/금융 단기 수혜 — 버블 붕괴 위험 누적"
                )
            return

        cum_prob += 0.010

        # ⑩ 공급망 대란 — 전쟁 없이도 물류 마비 (항만 파업/자연재해/지정학)
        # 팬데믹 이후 or 긴장고조 상황에서 발생
        supply_ok = (
            not any(x in self.s.current_scenario for x in ["전쟁", "분쟁", "팬데믹"]) and
            (sox < 800.0 or grain >= 500.0 or metal >= 4000.0)  # 원자재 이상 징후
        )
        if supply_ok and roll < (cum_prob + 0.012):
            duration = random.randint(126, 378)
            self.s.boom_event = {'type': '공급망대란', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🚢 공급망 대란 (물류 마비)"
            self._trigger_scenario_themes('공급망대란')
            self.s.scenario_timer = duration
            # 원자재 충격
            self.s.macro['semi_index']  = self.s.macro.get('semi_index', 1000.0) * 0.85
            self.s.macro['metal_price'] = self.s.macro.get('metal_price', 1800.0) * 1.20
            if not silent:
                self.s.daily_news.append(
                    f"🚢 [공급망 대란] {cur.year}년 글로벌 물류 마비! "
                    f"항만 파업·지정학 긴장으로 반도체/부품 공급 차질 — "
                    f"IT/산업재 단기 타격, 물류/소재 수혜"
                )
            return

        cum_prob += 0.012

        # ⑪ 신흥국 위기 — 외국인 대규모 이탈 + 환율 급등
        # 달러 강세 사이클에서 신흥국 자본 이탈
        em_crisis = (
            ff <= -40.0 and
            macro.get('exchange_rate', 1100.0) >= 1400.0 and
            rate >= 5.0
        )
        if em_crisis and roll < (cum_prob + 0.015):
            duration = random.randint(126, 378)
            # 이건 악재 시나리오 — boom_event 대신 직접 scenario 설정
            exchange = macro.get('exchange_rate', 1100.0)
            self.s.current_scenario = f"🌏 신흥국 위기 (달러 강세·자본 이탈)"
            self.s.scenario_timer   = duration
            self.s._last_crisis_year = cur.year
            drift = -random.uniform(0.05, 0.12) / max(1, duration)
            self.s._scenario_drift_penalty = drift
            self._trigger_scenario_themes('신흥국위기')
            if not silent:
                self.s.daily_news.append(
                    f"🌏 [신흥국 위기] {cur.year}년 달러 강세({exchange:.0f}원)·외국인 이탈! "
                    f"수출주 단기 수혜, 내수/금융 타격 — IMF 우려 확산"
                )
            return


    # ─────────────────────────────────────────────
    # ★ 수출 규제 이벤트
    # ─────────────────────────────────────────────
    def _check_export_sanction(self, silent: bool):
        """
        수출 규제 이벤트 체크 및 진행.
        현실 기준:
          반도체 수출 규제: 10년에 1~2회
          배터리 소재 제한: 10년에 1회
          방산/콘텐츠 규제: 산발적
        구조: 단기충격(6~12개월) → 중장기수혜(1~2년) → 종료
        """
        import random
        from engine.constants import EXPORT_SANCTION_TYPES

        cur = self.s.current_date
        if not getattr(self.s, '_market_fully_formed', False):
            return

        # 초기화
        if not hasattr(self.s, 'export_sanctions'):
            self.s.export_sanctions = {}

        # 진행 중인 규제 tick
        for sid in list(self.s.export_sanctions.keys()):
            state = self.s.export_sanctions[sid]
            s_def = EXPORT_SANCTION_TYPES.get(sid, {})
            state['timer'] = state.get('timer', 0) - 1

            if state['timer'] <= 0:
                phase = state.get('phase', '')
                if phase == '단기충격':
                    # 단기충격 종료 → 중장기수혜 시작
                    state['phase'] = '중장기수혜'
                    state['timer'] = s_def.get('duration_long', 378)
                    if not silent:
                        targets = '/'.join(s_def.get('target_inds', []))
                        self.s.daily_news.append(
                            f"🔄 [{s_def['emoji']} 공급망 재편] {s_def['name']} 충격 완화 — "
                            f"{targets} 섹터 대체 공급망 수혜 시작"
                        )
                elif phase == '중장기수혜':
                    # 완전 종료
                    del self.s.export_sanctions[sid]
                    if not silent:
                        self.s.daily_news.append(
                            f"✅ [{s_def['emoji']} 규제 종료] {s_def['name']} 효과 완전 소멸"
                        )
            continue

        # 매달 1일 신규 발동 체크
        if cur.day != 1:
            return

        # 전쟁/대공황/팬데믹 중엔 추가 규제 없음 (이미 충분한 충격)
        scenario = self.s.current_scenario
        if any(x in scenario for x in ["대공황", "팬데믹", "전쟁"]):
            return

        # 수출 호황 후 1~3년 내 규제 확률 상승 (현실: 호황 → 견제)
        last_boom = getattr(self.s, '_last_boom_year', 0)
        years_since_boom = cur.year - last_boom if last_boom else 99
        boom_factor = 2.0 if 1 <= years_since_boom <= 3 else 1.0

        # 이미 진행 중인 규제가 있으면 신규 발동 억제
        if len(self.s.export_sanctions) >= 2:
            return

        # 규제 쿨다운: 직전 규제 후 2년
        last_sanction = getattr(self.s, '_last_sanction_year', 0)
        if last_sanction and cur.year - last_sanction < 2:
            return

        # 각 규제 유형별 월 발동 확률
        _SANCTION_PROB = {
            "semiconductor":     0.008,   # 월 0.8% → 연 약 9%
            "battery_material":  0.005,   # 월 0.5% → 연 약 6%
            "defense_restriction": 0.003,
            "content_ban":       0.004,
        }

        for sid, base_prob in _SANCTION_PROB.items():
            if sid in self.s.export_sanctions:
                continue  # 이미 진행 중

            prob = base_prob * boom_factor
            if random.random() < prob:
                s_def = EXPORT_SANCTION_TYPES[sid]
                self.s.export_sanctions[sid] = {
                    'phase': '단기충격',
                    'timer': s_def['duration_short'],
                }
                self.s._last_sanction_year = cur.year

                targets = '/'.join(s_def['target_inds'])
                if not silent:
                    self.s.daily_news.append(
                        f"{s_def['emoji']} [{s_def['name']}] 해외 주요국이 한국 {targets} 산업에 "
                        f"수출 규제를 발동했습니다. 단기 실적 타격 예상 — "
                        f"중장기적으로는 공급망 재편 수혜 가능"
                    )
                    # 시나리오 뉴스에도 추가
                    self.s.daily_news.append(
                        f"📊 [산업 영향] {targets} 섹터 수출 규제 단기 충격 "
                        f"({s_def['duration_short']//21}주) → "
                        f"이후 공급망 재편 수혜 ({s_def['duration_long']//21}주)"
                    )
                break  # 한 번에 하나씩만

    # ─────────────────────────────────────────────
    # ★ 대공황 자연 발생 트리거 (신규)
    # ─────────────────────────────────────────────
    def _check_depression(self, silent: bool):
        """
        복합 경제 지표가 임계값을 30일 이상 초과하면 대공황 자연 발생.
        기존 forced 방식 대체.
        """
        # 이미 대공황이면 회복 조건 체크
        if "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario:
            self._tick_depression_recovery(silent)
            return

        # 대공황 진입 불가 조건
        if not getattr(self.s, '_market_fully_formed', False):
            return
        if self.s.boom_event.get('phase') == '진행중':
            return

        # ★ 대공황 쿨다운: 직전 대공황 후 15년 이내 재발 없음 (기존 10년)
        # 현실: 1929년 대공황 후 다음 시스템급 위기는 2008년(79년 후)
        last_depression = getattr(self.s, '_last_depression_year', 0)
        if last_depression and self.s.current_date.year - last_depression < 15:
            return

        # 트리거 조건 점수 계산
        bi        = self.s.bubble_index
        threshold = getattr(self.s, '_depression_threshold', 200)
        rate      = self.s.macro.get('interest_rate', 4.0)
        cpi       = self.s.macro.get('cpi', 2.0)
        ff        = getattr(self.s, 'foreign_flow_index', 0.0)

        # ★ [최적화] PER 참조 — 당일 캐시(_last_market_stats) 우선, 없으면 수집
        # _check_depression은 장 마감 후 호출되므로 당일 캐시가 대부분 유효
        stats   = getattr(self.s, '_last_market_stats', None) or self._collect_market_stats()
        per_l   = stats.get('per_large', 0)

        trigger_score = 0
        if bi >= threshold:           trigger_score += 3   # 핵심 조건
        if bi >= threshold * 0.80:    trigger_score += 1
        if per_l >= 70:               trigger_score += 1
        if rate >= 7.0:               trigger_score += 1
        if cpi >= 6.0:                trigger_score += 1
        if ff <= -70:                 trigger_score += 1
        # GRI 고점 대비 -35% 이상
        peak = getattr(self.s, 'peak_gri', self.s.gri)
        if self.s.gri < peak * 0.65:  trigger_score += 2

        # ★ 광기 지수가 높으면 트리거 점수 추가 (광기 = 취약한 기반)
        mmi = getattr(self.s, 'market_mania_index', 1.0)
        from engine.constants import MANIA_INDEX_THRESHOLDS
        if mmi >= MANIA_INDEX_THRESHOLDS["붕괴전"]:
            trigger_score += 3   # 붕괴 직전: 대공황 훨씬 쉽게 터짐
        elif mmi >= MANIA_INDEX_THRESHOLDS["광기"]:
            trigger_score += 2
        elif mmi >= MANIA_INDEX_THRESHOLDS["버블"]:
            trigger_score += 1

        # ★ [수정] 누적 임계값 상향: 4→6 (기존에 버블 낮아도 쉽게 터지던 문제 해소)
        # 광기 지수 완화 조건도 더 엄격하게
        _trigger_threshold = 6
        if mmi >= MANIA_INDEX_THRESHOLDS["광기"]:
            _trigger_threshold = 4
        elif mmi >= MANIA_INDEX_THRESHOLDS["버블"]:
            _trigger_threshold = 5

        if trigger_score >= _trigger_threshold:
            self.s.depression_trigger_count = getattr(self.s, 'depression_trigger_count', 0) + 1
        else:
            self.s.depression_trigger_count = max(
                0, getattr(self.s, 'depression_trigger_count', 0) - 1
            )

        # 광기가 심할수록 발동 기간 단축 (30~40일)
        _sustain_days = 40
        if mmi >= MANIA_INDEX_THRESHOLDS["붕괴전"]:
            _sustain_days = 20
        elif mmi >= MANIA_INDEX_THRESHOLDS["광기"]:
            _sustain_days = 30

        if getattr(self.s, 'depression_trigger_count', 0) >= _sustain_days:
            self.s.depression_active         = True
            self.s.depression_trigger_count  = 0
            self.s.current_scenario          = "💀 대공황 (시스템 붕괴)"
            self._trigger_scenario_themes('대공황')
            # 자연 발생 대공황: 2~4년
            self.s.scenario_timer            = 252 * random.randint(2, 4)
            self.s._last_crisis_year         = self.s.current_date.year
            self.s._last_depression_year     = self.s.current_date.year  # ★ 쿨다운 기록
            # ★ 대공황 발생 시 광기 지수 리셋
            self.s.market_mania_index        = 1.0
            # ★ 발동 조건 기록
            _conds = []
            if bi >= threshold:     _conds.append(f"버블{bi:.0f}(임계{threshold})")
            if per_l >= 70:         _conds.append(f"PER대{per_l:.0f}x")
            if rate >= 7.0:         _conds.append(f"금리{rate:.2f}%")
            if cpi >= 6.0:          _conds.append(f"CPI{cpi:.2f}%")
            if ff <= -70:           _conds.append(f"외국인ff{ff:.0f}")
            if mmi >= 1.8:          _conds.append(f"광기{mmi:.2f}")
            self.s._last_scenario_trigger_note = "대공황 트리거: " + " / ".join(_conds)
            if not silent:
                self.s.daily_news.append(
                    f"💀 [대공황 발생] 복합 경제 위기가 임계점을 돌파했습니다! "
                    f"광기지수 {mmi:.2f} — 버블 붕괴, 금리, 외국인 이탈 동시 폭발."
                )

    def _tick_depression_recovery(self, silent: bool):
        """대공황 중 회복 조건 체크 — 조건 충족 시 대공황V 전환"""
        bi   = self.s.bubble_index
        rate = self.s.macro.get('interest_rate', 4.0)
        ff   = getattr(self.s, 'foreign_flow_index', 0.0)
        peak = getattr(self.s, 'peak_gri', self.s.gri)
        gri_drop = self.s.gri / max(1, peak)

        # ★ 타이머 기반 강제 회복 (현실: 대공황도 결국 끝남)
        # scenario_timer가 0 이하면 조건 무관 강제 회복
        timer = getattr(self.s, 'scenario_timer', 0)
        if timer <= 0:
            self.s.depression_active      = False
            self.s.recovery_trigger_count = 0
            self.s.current_scenario       = "✨ 대공황V (고난과 부활)"
            if not silent:
                self.s.daily_news.append(
                    "🌅 [대공황 종료] 긴 침체 끝에 경제가 회복 국면에 접어들었습니다."
                )
            return

        recovery_score = 0
        if bi <= 60:          recovery_score += 1
        if rate <= 3.5:       recovery_score += 1
        if ff >= 10:          recovery_score += 1
        if gri_drop <= 0.65:  recovery_score += 1

        self.s.recovery_trigger_count = getattr(self.s, 'recovery_trigger_count', 0)
        if recovery_score >= 2:
            self.s.recovery_trigger_count += 1
        else:
            self.s.recovery_trigger_count = max(0, self.s.recovery_trigger_count - 1)

        if self.s.recovery_trigger_count >= 30:
            self.s.depression_active       = False
            self.s.recovery_trigger_count  = 0
            self.s.current_scenario        = "✨ 대공황V (고난과 부활)"
            # ★ [신규] 대공황V 진입 시 종료 타이머 설정 (1~2년)
            self.s._depression_v_timer = 252 * random.randint(1, 2)
            if not silent:
                self.s.daily_news.append(
                    "🌅 [회복 신호] 경제 지표가 바닥을 확인했습니다. "
                    "대공황 극복 국면에 진입합니다!"
                )
    # ─────────────────────────────────────────────
    # ★ 대공황V 종료 타이머
    # ─────────────────────────────────────────────
    def _tick_depression_v(self, silent: bool):
        """
        대공황V 진입 후 1~2년이면 정상 성장으로 자동 전환.
        현실: V자 반등은 1~3년 안에 마무리됨.
        """
        if "대공황V" not in self.s.current_scenario:
            return

        timer = getattr(self.s, '_depression_v_timer', 0)
        if timer <= 0:
            # 타이머 없이 진입한 경우 (구버전 세이브) → 즉시 설정
            self.s._depression_v_timer = 252 * random.randint(1, 2)
            return

        self.s._depression_v_timer = timer - 1

        if self.s._depression_v_timer <= 0:
            self.s.current_scenario = "정상 성장"
            self.s._depression_v_timer = 0
            # ★ V자 종료 후 호황 시나리오 확률 대폭 상향 (실제 반등 반영)
            self.s.sentiment = min(75.0, getattr(self.s, 'sentiment', 50.0) + 20.0)
            self.s.foreign_flow_index = max(
                getattr(self.s, 'foreign_flow_index', 0.0),
                20.0
            )
            if not silent:
                self.s.daily_news.append(
                    "🌈 [대공황 극복 완료] V자 반등이 마무리되었습니다. "
                    "경제가 정상 성장 궤도로 복귀합니다!"
                )

    # ─────────────────────────────────────────────
    # ★ 위기 자동 정책 대응
    # ─────────────────────────────────────────────
    def _apply_crisis_policy(self, silent: bool):
        """
        악재 발생 시 중앙은행/정부의 자동 정책 대응.

        현실 메커니즘:
        - 대공황: 긴급 금리 인하 + QE (유동성 공급) + 재정 지출
        - 팬데믹: 제로금리 + 재난지원금 효과 (소비 부양)
        - 전쟁: 방산/에너지 예산 확대
        - 환율위기: 긴급 금리 인상 + 외환 방어

        매 분기(3개월) 1회 실행.
        """
        cur = self.s.current_date
        if cur.month not in [1, 4, 7, 10] or cur.day != 1:
            return

        macro    = self.s.macro
        scenario = self.s.current_scenario
        rate     = macro.get('interest_rate', 4.0)
        lv       = self.s.max_tech_reached

        # ── 대공황 정책 ───────────────────────────
        is_depression = "대공황" in scenario and "극복" not in scenario
        if is_depression:
            # 금리 인하 (분기마다 0.25%p, 하한 0.1%)
            if rate > 0.5:
                new_rate = max(0.1, rate - 0.25)
                macro['interest_rate'] = new_rate
                if not silent:
                    self.s.daily_news.append(
                        f"🏦 [긴급 금통위] 대공황 대응 — 기준금리 긴급 인하 "
                        f"{rate:.2f}% → {new_rate:.2f}% (비상 조치)"
                    )
            # QE 효과: 외국인 수급 소폭 개선
            ff_now = getattr(self.s, 'foreign_flow_index', 0.0)
            self.s.foreign_flow_index = min(0.0, ff_now + 3.0)  # 음수 완화만 (플러스 아님)
            return

        # ── 대공황V 정책 (회복기 부양) ──────────
        if "대공황V" in scenario:
            if rate > 1.0:
                new_rate = max(0.5, rate - 0.25)
                macro['interest_rate'] = new_rate
            return

        # ── 팬데믹 정책 ──────────────────────────
        pandemic = getattr(self.s, 'pandemic_event', {})
        if pandemic.get('phase') == '진행중':
            pan_timer = pandemic.get('timer', 0)
            pan_intensity = pandemic.get('intensity', 0.25)
            # 초기 대응만 (처음 2분기): 금리 인하
            if pan_timer > 300 and rate > 1.0:
                new_rate = max(0.25, rate - 0.50)
                macro['interest_rate'] = new_rate
                if not silent:
                    self.s.daily_news.append(
                        f"🏦 [팬데믹 긴급대응] 기준금리 0.50%p 인하 "
                        f"{rate:.2f}% → {new_rate:.2f}% + 재난지원금 편성"
                    )
            # ★ [수정] efficiency 부스트 제거 — sector_sensitivity에서 이미 처리됨
            # 중복 부스트가 4년 누적되어 GRI 폭등 유발
            return

        # ── 전쟁 정책 ────────────────────────────
        war = getattr(self.s, 'war_event', {})
        if war.get('phase') == '진행중':
            # 외국인 이탈 가속만 완화 (efficiency 부스트 제거)
            ff_now = getattr(self.s, 'foreign_flow_index', 0.0)
            self.s.foreign_flow_index = max(-80.0, ff_now - 2.0)
            return

        # ── 스태그플레이션/긴축 쇼크 정책 ────────
        if "스태그" in scenario or "긴축 쇼크" in scenario:
            # 금리 인상 사이클 — 이미 economy.py에서 처리되지만 추가 가속
            cpi = macro.get('cpi', 2.0)
            if cpi > 5.0 and rate < 6.0:
                macro['interest_rate'] = min(6.0, rate + 0.25)
            return

    # ─────────────────────────────────────────────
    # ★ 페이즈 전환 체크 (신규)
    # 페이즈가 바뀌었을 때 각 종목의 사업 전환/도태 처리
    # ─────────────────────────────────────────────
    def _check_phase_transition(self, silent: bool):
        """
        현재 페이즈와 _last_processed_phase를 비교해
        페이즈가 바뀐 경우에만 사업 전환/도태 처리 실행.
        """
        from engine.constants import (
            INDUSTRY_LEVELS, TIER_BUSINESS_SPEC, TECH_PHASE
        )

        cur_phase = self.eco.get_current_phase()
        last_phase = getattr(self.s, '_last_processed_phase', '1A')

        if cur_phase == last_phase:
            return  # 페이즈 변화 없음 → 스킵

        # ★ [버그수정] 페이즈 역행 차단 — 단방향만 허용
        # get_current_phase()의 bonus가 매일 달라져서 A↔B 진동하던 문제 해결
        # 페이즈는 한번 넘어가면 절대 되돌아갈 수 없음
        _PHASE_ORDER = {'1A':1,'1B':2,'2A':3,'2B':4,'3A':5,'3B':6,'4A':7,'4B':8}
        if _PHASE_ORDER.get(cur_phase, 0) <= _PHASE_ORDER.get(last_phase, 0):
            return  # 역행 또는 동일 → 차단

        # ── 페이즈 전환 확정 ──────────────────────
        self.s._last_processed_phase = cur_phase
        cy = self.s.current_date.year

        lv = self.s.max_tech_reached
        phase_name = next(
            (p["name"] for p in TECH_PHASE.get(lv, []) if p["id"] == cur_phase),
            cur_phase
        )

        # ★ 시나리오 로그용 페이즈 전환 플래그 세팅 (LV 전환과 동일한 방식)
        self.s._phase_jump_log = {
            'from_phase': last_phase,
            'to_phase':   cur_phase,
            'phase_name': phase_name,
            'lv':         lv,
            'date':       self.s.current_date.strftime('%Y-%m-%d'),
        }

        if not silent:
            self.s.daily_news.append(
                f"🔄 [시대 전환] {cy}년, 경제 패러다임이 [{last_phase}] → [{cur_phase} {phase_name}]로 전환됩니다!"
            )

        # ── ★ 페이즈 전환 호재 효과 ──────────────
        # GRI 즉각 부양 + sentiment 상승 + foreign_flow 개선
        _PHASE_BOOST = {
            '1B': (0.03, 65,  0),   # 인터넷 성숙기
            '2A': (0.05, 70, 20),   # 모바일 혁명
            '2B': (0.04, 68, 15),   # 클라우드/플랫폼
            '3A': (0.08, 75, 30),   # AI 상용화 ← 강력
            '3B': (0.06, 73, 25),   # 양자/바이오
            '4A': (0.12, 80, 40),   # 기술 특이점 ← 최강
            '4B': (0.10, 78, 35),   # 포스트 휴먼
        }
        gri_boost, new_sentiment, ff_boost = _PHASE_BOOST.get(cur_phase, (0, 50, 0))

        if gri_boost > 0:
            # 버블 과열 시 효과 감소
            bubble = getattr(self.s, 'bubble_index', 0.0)
            if bubble >= 150:
                gri_boost *= 0.3
            elif bubble >= 100:
                gri_boost *= 0.6

            self.s.gri = self.s.gri * (1.0 + gri_boost)
            self.s.sentiment = max(self.s.sentiment, float(new_sentiment))
            if ff_boost > 0:
                self.s.foreign_flow_index = min(
                    100.0,
                    getattr(self.s, 'foreign_flow_index', 0.0) + ff_boost
                )
            if not silent:
                self.s.daily_news.append(
                    f"📈 [기술 호재] {cur_phase} 시대 진입! "
                    f"시장 즉각 반응 +{gri_boost*100:.0f}% "
                    f"(심리지수 {new_sentiment}, 외국인 수급 +{ff_boost})"
                )

        # ★ 페이즈 전환 시 테마 쿨다운 무시하고 강제 발동
        self._trigger_phase_themes(cur_phase, last_phase, ignore_cooldown=True)

        # ── 각 종목 사업 전환/도태 처리 ──────────
        transition_log = []  # 뉴스용 로그

        for stock in self.s.stocks:
            meta = stock['meta']
            ind  = meta.get('ind', '')
            tier_raw = meta.get('tier_raw', '소')

            # tier_raw 없으면 tier 문자열에서 추출
            if not tier_raw:
                t = meta.get('tier', '소형주')
                if '대형주' == t:
                    # group_id 있으면 대1, 없으면 대
                    tier_raw = '대1' if meta.get('group_id') else '대'
                elif '중형주' == t:
                    tier_raw = '중'
                else:
                    tier_raw = '소'

            spec = TIER_BUSINESS_SPEC.get(tier_raw, TIER_BUSINESS_SPEC['소'])
            ind_data = INDUSTRY_LEVELS.get(ind, {})

            # 현재 페이즈 풀 (대/중/소 전체 합산)
            cur_phase_data = ind_data.get(cur_phase, {})
            cur_all_subs = set()
            for t_list in cur_phase_data.values():
                cur_all_subs.update(t_list)

            # common 풀
            common_data = ind_data.get('common', {})
            common_all = set()
            for t_list in common_data.values():
                common_all.update(t_list)

            # 현재 sub_list
            sub_list = meta.get('sub_list', [meta.get('sub', '')])
            if not sub_list:
                sub_list = [meta.get('sub', '')]

            new_sub_list = []
            dropped = []
            added = []

            for sub in sub_list:
                is_common   = sub in common_all
                is_current  = sub in cur_all_subs

                if is_common or is_current:
                    # 유지 가능한 사업
                    new_sub_list.append(sub)
                else:
                    # 도태 사업
                    roll = random.random()
                    if roll < spec['follow_prob']:
                        # 새 사업으로 교체
                        new_sub = self._pick_new_sub(ind, tier_raw, cur_phase, new_sub_list, ind_data)
                        if new_sub:
                            new_sub_list.append(new_sub)
                            dropped.append(sub)
                            added.append(new_sub)
                        else:
                            new_sub_list.append(sub)  # 교체 실패 시 유지
                    elif roll < spec['follow_prob'] + spec['survive_prob']:
                        # 낮은 효율로 버팀 — efficiency 패널티
                        meta['efficiency'] = max(
                            0.005,
                            meta.get('efficiency', 0.05) * (1.0 - spec['obsolete_penalty'])
                        )
                        new_sub_list.append(sub)
                    else:
                        # 상폐 경로 — efficiency 강한 패널티
                        meta['efficiency'] = max(
                            0.005,
                            meta.get('efficiency', 0.05) * (1.0 - spec['obsolete_penalty'] * 3)
                        )
                        new_sub_list.append(sub)  # 아직 버리진 않음, 패널티만

            # 사업 추가 (max_subs 미만이고 add_prob 충족 시)
            if (len(new_sub_list) < spec['max_subs']
                    and random.random() < spec['add_prob']):
                new_sub = self._pick_new_sub(ind, tier_raw, cur_phase, new_sub_list, ind_data)
                if new_sub:
                    new_sub_list.append(new_sub)
                    added.append(new_sub)

            # sub_list 업데이트
            meta['sub_list'] = new_sub_list[:spec['max_subs']]
            # 주력 사업(sub)은 sub_list 첫 번째로
            if new_sub_list:
                meta['sub'] = new_sub_list[0]

            # 뉴스 로그 (대형주만, 조용하지 않을 때)
            if not silent and tier_raw in ('대1', '대') and (dropped or added):
                name = meta.get('c_name', '')
                if dropped:
                    transition_log.append(f"{name}: {', '.join(dropped[:2])} 사업 철수")
                if added:
                    transition_log.append(f"{name}: {', '.join(added[:2])} 신규 진출")

        # 전환 요약 뉴스 (최대 5개)
        if not silent and transition_log:
            for msg in transition_log[:5]:
                self.s.daily_news.append(f"🏭 [사업 재편] {msg}")

    # ─────────────────────────────────────────────
    # ★ 테마 모멘텀 시스템
    # ─────────────────────────────────────────────

    def _add_theme(self, ind: str, theme_type: str, peak: float,
                   duration: int, source: str):
        """
        테마 추가. 이미 같은 산업의 같은 방향 테마가 있으면 스킵.
        bull 테마는 쿨다운 체크.
        """
        cur_year = self.s.current_date.year

        # bull 테마 쿨다운: 마지막 bull 테마 종료 후 3년 이내 재발생 금지
        if theme_type == 'bull':
            last_year = self.s._theme_cooldown.get(ind, 0)
            if last_year and cur_year - last_year < 3:
                return

        # 이미 같은 산업+방향 테마 존재 시 스킵
        for t in self.s.active_themes:
            if t['ind'] == ind and t['type'] == theme_type:
                return

        self.s.active_themes.append({
            'ind':      ind,
            'type':     theme_type,
            'peak':     peak,
            'duration': duration,
            'elapsed':  0,
            'source':   source,
        })

    def _trigger_phase_themes(self, cur_phase: str, last_phase: str,
                               ignore_cooldown: bool = False):
        """
        페이즈 전환 시 수혜/피해 산업 테마 발생.
        ignore_cooldown=True 시 쿨다운 무시 (페이즈 전환은 특수 이벤트).
        """
        # 페이즈별 bull/bear 테마 정의
        # peak: 테마 최고 강도 (0~1)
        # duration: 영업일 기준 (252 = 1년)
        _PHASE_THEMES = {
            '1B': {  # 인터넷 성숙기
                'bull': [('IT', 0.45, 504), ('커뮤니케이션', 0.35, 378)],
                'bear': [],
            },
            '2A': {  # 모바일 전성기
                'bull': [('IT', 0.55, 504), ('건강관리', 0.35, 378),
                         ('자유소비재', 0.30, 252)],
                'bear': [('에너지', 0.25, 252)],
            },
            '2B': {  # 클라우드/플랫폼
                'bull': [('IT', 0.60, 630), ('산업재', 0.40, 378),
                         ('소재', 0.35, 378)],
                'bear': [],
            },
            '3A': {  # AI 상용화
                'bull': [('IT', 0.70, 756), ('건강관리', 0.50, 504),
                         ('금융', 0.35, 378)],
                'bear': [('산업재', 0.30, 252)],
            },
            '3B': {  # 양자/바이오
                'bull': [('건강관리', 0.75, 756), ('소재', 0.50, 504),
                         ('에너지', 0.45, 504)],
                'bear': [('커뮤니케이션', 0.25, 252)],
            },
            '4A': {  # 특이점
                'bull': [('IT', 0.80, 1008), ('에너지', 0.70, 756)],
                'bear': [('금융', 0.40, 504), ('산업재', 0.35, 378)],
            },
            '4B': {  # 포스트 휴먼
                'bull': [('IT', 0.90, 1260), ('에너지', 0.80, 1008),
                         ('건강관리', 0.70, 756)],
                'bear': [('금융', 0.50, 630), ('부동산', 0.40, 504)],
            },
        }

        themes = _PHASE_THEMES.get(cur_phase, {})
        for ind, peak, duration in themes.get('bull', []):
            if ignore_cooldown:
                # 페이즈 전환 시 쿨다운 무시하고 강제 발동
                self.s._theme_cooldown.pop(ind, None)
            self._add_theme(ind, 'bull', peak, duration, 'phase')
        for ind, peak, duration in themes.get('bear', []):
            self._add_theme(ind, 'bear', peak, duration, 'phase')

    def _trigger_scenario_themes(self, scenario_type: str):
        """
        시나리오 발생 시 연동 테마 발생.
        dispatcher의 각 이벤트 체크 메서드에서 호출.
        """
        _SCENARIO_THEMES = {
            '수출호황': {
                'bull': [('IT', 0.50, 378), ('소재', 0.40, 378),
                         ('산업재', 0.35, 252)],
                'bear': [],
            },
            '유동성장세': {
                'bull': [('IT', 0.45, 252), ('자유소비재', 0.40, 252),
                         ('부동산', 0.35, 252)],
                'bear': [],
            },
            '내수붐': {
                'bull': [('필수소비재', 0.45, 252), ('자유소비재', 0.50, 252),
                         ('커뮤니케이션', 0.35, 252)],
                'bear': [],
            },
            '팬데믹': {
                'bull': [('IT', 0.50, 504), ('건강관리', 0.55, 378),
                         ('필수소비재', 0.30, 252)],
                'bear': [('자유소비재', 0.45, 378), ('부동산', 0.30, 252)],
            },
            # ★ [수정] 전쟁/재건 테마 강도 축소 (버그: 전쟁 중 GRI drift 음수인데 테마가 상쇄)
            '전쟁': {
                'bull': [('에너지', 0.25, 189), ('소재', 0.20, 189)],
                'bear': [('IT', 0.30, 252), ('자유소비재', 0.25, 189)],
            },
            '재건': {
                'bull': [('산업재', 0.30, 252), ('소재', 0.25, 252)],
                'bear': [],
            },
            '대공황': {
                'bull': [],
                # 대공황: 모든 섹터 bear 테마 (강도 낮게 — 시장 자체가 하락)
                'bear': [('IT', 0.35, 504), ('금융', 0.40, 504),
                         ('자유소비재', 0.35, 378)],
            },
            # ── 신규 호재 ─────────────────────────────
            '혁신기술붐': {
                'bull': [('IT', 0.65, 504), ('건강관리', 0.50, 378),
                         ('커뮤니케이션', 0.35, 252)],
                'bear': [('에너지', 0.20, 252), ('유틸리티', 0.15, 126)],
            },
            '외국인유입': {
                'bull': [('IT', 0.40, 252), ('금융', 0.35, 252),
                         ('자유소비재', 0.30, 252)],
                'bear': [],
            },
            '정부부양': {
                'bull': [('산업재', 0.55, 252), ('소재', 0.40, 252),
                         ('IT', 0.30, 126)],
                'bear': [],
            },
            # ── 신규 악재 ─────────────────────────────
            '기술버블붕괴': {
                # IT/성장주 폭락, Value/Defensive 자금 유입
                'bull': [('필수소비재', 0.35, 378), ('유틸리티', 0.30, 378),
                         ('에너지', 0.25, 252)],
                'bear': [('IT', 0.60, 504), ('건강관리', 0.45, 378),
                         ('커뮤니케이션', 0.40, 378)],
            },
            '스태그플레이션': {
                'bull': [('에너지', 0.40, 504), ('소재', 0.35, 378)],
                'bear': [('자유소비재', 0.50, 504), ('IT', 0.35, 378),
                         ('부동산', 0.40, 504)],
            },
            '긴축쇼크': {
                'bull': [('금융', 0.35, 252), ('필수소비재', 0.25, 252)],
                'bear': [('IT', 0.45, 378), ('자유소비재', 0.35, 252),
                         ('부동산', 0.40, 378)],
            },
            '환율위기': {
                # 수출주 단기 수혜, 내수/소비재 타격
                'bull': [('IT', 0.35, 252), ('산업재', 0.30, 252)],
                'bear': [('필수소비재', 0.40, 378), ('자유소비재', 0.45, 378),
                         ('유틸리티', 0.30, 252)],
            },
            # ── 신규 시나리오 테마 ─────────────────────
            '반도체슈퍼사이클': {
                'bull': [('IT', 0.75, 630), ('소재', 0.45, 378),
                         ('산업재', 0.35, 252)],
                'bear': [('유틸리티', 0.15, 126)],
            },
            '원자재슈퍼사이클': {
                'bull': [('에너지', 0.70, 504), ('소재', 0.65, 504),
                         ('산업재', 0.40, 378)],
                'bear': [('IT', 0.25, 252), ('자유소비재', 0.30, 252)],
            },
            '부동산버블': {
                'bull': [('부동산', 0.75, 378), ('금융', 0.50, 378),
                         ('자유소비재', 0.30, 252)],
                'bear': [('유틸리티', 0.20, 126)],
            },
            '공급망대란': {
                'bull': [('소재', 0.45, 252), ('에너지', 0.35, 252)],
                'bear': [('IT', 0.40, 252), ('산업재', 0.35, 252),
                         ('자유소비재', 0.30, 252)],
            },
            '신흥국위기': {
                'bull': [('IT', 0.40, 252), ('산업재', 0.30, 252)],  # 수출주 수혜
                'bear': [('금융', 0.55, 378), ('부동산', 0.50, 378),
                         ('필수소비재', 0.35, 252), ('자유소비재', 0.40, 252)],
            },
            # ── 신규 5종 시나리오 테마 ─────────────
            '기업실적장세': {
                # PER 낮음 + 이익 성장 → 전 섹터 고른 상승
                # Value株가 먼저 반응, Growth는 후반부 합류
                'bull': [('금융', 0.50, 378), ('산업재', 0.45, 378),
                         ('소재', 0.40, 252), ('자유소비재', 0.40, 252),
                         ('IT', 0.35, 252)],
                'bear': [],
            },
            '인플레이션충격': {
                # CPI 폭등 → 실물 자산 수혜, 성장주/소비재 타격
                'bull': [('에너지', 0.60, 504), ('소재', 0.55, 504),
                         ('부동산', 0.35, 378)],
                'bear': [('IT', 0.40, 378), ('자유소비재', 0.55, 504),
                         ('필수소비재', 0.30, 252), ('커뮤니케이션', 0.30, 252)],
            },
            '기술패권경쟁': {
                # 반도체 수출 제한 → IT 단기 타격, 소재/장비 중장기 수혜
                # 국산화 투자 → 소재/산업재 수혜
                'bull': [('소재', 0.55, 504), ('산업재', 0.45, 378),
                         ('에너지', 0.30, 252)],
                'bear': [('IT', 0.55, 504), ('커뮤니케이션', 0.35, 378)],
            },
            '부동산버블붕괴': {
                # 담보 가치 하락 → 금융/부동산 집중 타격
                # 안전자산(필수소비재/유틸) 방어
                'bull': [('필수소비재', 0.40, 504), ('유틸리티', 0.35, 504),
                         ('건강관리', 0.25, 252)],
                'bear': [('부동산', 0.80, 630), ('금융', 0.65, 504),
                         ('자유소비재', 0.45, 378), ('산업재', 0.30, 252)],
            },
            '구조적저성장': {
                'bull': [('유틸리티', 0.40, 756), ('필수소비재', 0.35, 756)],
                'bear': [('IT', 0.30, 756), ('자유소비재', 0.35, 756),
                         ('건강관리', 0.20, 504)],
            },
            # ── 신규 2종 (경기 회복 계열) ──────────
            '경기정상화': {
                # 위기 후 전 섹터 균형 회복 — 특정 쏠림 없이 실적 기반
                'bull': [('금융', 0.40, 378), ('산업재', 0.38, 378),
                         ('자유소비재', 0.35, 252), ('IT', 0.30, 252)],
                'bear': [],
            },
            '금융완화': {
                # 저금리 신용 확장 → 금융/부동산 선행, 성장주 합류
                'bull': [('금융', 0.55, 504), ('부동산', 0.50, 504),
                         ('IT', 0.40, 378), ('자유소비재', 0.35, 252)],
                'bear': [('필수소비재', 0.15, 252)],  # 방어주 소외
            },
        }

        # 대공황 중엔 bull 테마 강제 소멸
        if scenario_type == '대공황':
            self.s.active_themes = [
                t for t in self.s.active_themes if t['type'] != 'bull'
            ]

        themes = _SCENARIO_THEMES.get(scenario_type, {})
        for ind, peak, duration in themes.get('bull', []):
            self._add_theme(ind, 'bull', peak, duration, 'scenario')
        for ind, peak, duration in themes.get('bear', []):
            self._add_theme(ind, 'bear', peak, duration, 'scenario')

    # ─────────────────────────────────────────────
    # ★ 10.5순위: 구조적 경제 충격
    # 긴축 쇼크 / 스태그플레이션 / 기술 버블 붕괴
    # ─────────────────────────────────────────────
    def _check_structural_shift(self, silent: bool):
        """
        외생 충격이 아닌 내생적 경제 지표에서 발생하는 악재.
        GRI 즉시 충격 없음 — drift 패널티로 서서히 반영.

        ③ 기술 버블 붕괴: Growth PER 80배+ + 버블 150+ + 금리 인상
        ② 스태그플레이션: CPI 5%+ + GDP 역성장 동시 3개월 이상
        ① 긴축 쇼크: CPI 4%+ + 금리 인상 사이클 2개월 이상
        """
        cur      = self.s.current_date
        scenario = self.s.current_scenario

        if any(x in scenario for x in ["전쟁", "분쟁", "대공황", "팬데믹",
                                        "긴축", "스태그", "버블붕괴", "환율위기",
                                        "외부충격"]):
            return
        if not getattr(self.s, '_market_fully_formed', False):
            return
        if cur.day != 1:
            return

        macro     = self.s.macro
        cpi       = macro.get('cpi', 2.0)
        rate      = macro.get('interest_rate', 4.0)
        prev_snap = getattr(self.s, '_prev_macro_snapshot', {})
        prev_rate = prev_snap.get('interest_rate', rate)
        rate_rising = rate > prev_rate
        stats    = getattr(self.s, '_last_market_stats', {})
        per_l    = stats.get('per_large', 0.0)
        bubble   = getattr(self.s, 'bubble_index', 0.0)
        gdp_gr   = getattr(self.s, 'gdp_growth_rate', 0.03)
        cycle    = getattr(self.s, 'cycle_stage', '확장')

        # ③ 기술 버블 붕괴
        # ★ 광기 지수가 높을수록 트리거 확률 상승 (펀더멘탈 괴리 기반)
        mmi = getattr(self.s, 'market_mania_index', 1.0)
        from engine.constants import MANIA_INDEX_THRESHOLDS
        bubble_threshold = MANIA_INDEX_THRESHOLDS["버블"]
        mania_threshold  = MANIA_INDEX_THRESHOLDS["광기"]

        # 기본 조건: PER 80배+ + 버블 150+ + 금리 인상
        # 광기 지수가 높으면 조건 완화 (더 쉽게 터짐)
        base_bubble_prob = 0.15
        if mmi >= MANIA_INDEX_THRESHOLDS["붕괴전"]:
            base_bubble_prob = 0.60   # 붕괴 직전: 60% 확률
        elif mmi >= mania_threshold:
            base_bubble_prob = 0.35   # 광기 구간: 35%
        elif mmi >= bubble_threshold:
            base_bubble_prob = 0.20   # 버블 구간: 20%

        # 광기 구간이면 PER 조건 완화 (주가가 먼저 앞서가는 게 이미 반영됨)
        per_condition = (per_l >= 80) if mmi < bubble_threshold else (per_l >= 60)
        bubble_condition = (bubble >= 150) if mmi < mania_threshold else (bubble >= 100)

        if per_condition and bubble_condition and rate_rising and random.random() < base_bubble_prob:
            duration = random.randint(252, 504)
            gri_drop = random.uniform(0.10, 0.20)
            # ★ 광기가 심할수록 충격 크게
            if mmi >= MANIA_INDEX_THRESHOLDS["붕괴전"]:
                gri_drop = random.uniform(0.25, 0.45)
            elif mmi >= mania_threshold:
                gri_drop = random.uniform(0.18, 0.30)
            immediate = gri_drop * 0.30
            self.s.gri = max(50.0, self.s.gri * (1.0 - immediate))  # ★ 플로어 100→50
            self.s._scenario_drift_penalty = -(gri_drop * 5.0 / max(1, duration))  # [수정]
            self.s.current_scenario = "💻 기술 버블 붕괴 (성장주 디레이팅)"
            self.s.scenario_timer   = duration
            self.s._last_crisis_year = cur.year
            # ★ 버블 붕괴 시 광기 지수 리셋 (시장 정화)
            self.s.market_mania_index = 1.0
            self.s._last_scenario_trigger_note = (
                f"기술버블붕괴 | PER대:{per_l:.0f}x | 버블:{bubble:.0f} | "
                f"광기:{mmi:.2f} | 금리인상중:{rate_rising} | "
                f"즉시충격:{immediate*100:.0f}%"
            )
            self._trigger_scenario_themes('기술버블붕괴')
            if not silent:
                self.s.daily_news.append(
                    f"💻 [기술 버블 붕괴] {cur.year}년 성장주 밸류에이션 붕괴! "
                    f"PER {per_l:.0f}배 + 버블지수 {bubble:.0f} + 광기지수 {mmi:.2f} → "
                    f"즉시 -{immediate*100:.0f}% | IT/성장주 집중 하락"
                )
            return

        # ② 스태그플레이션
        stag_cnt = getattr(self.s, '_stagflation_counter', 0)
        if cpi >= 5.0 and gdp_gr <= 0.01:
            self.s._stagflation_counter = stag_cnt + 1
        else:
            self.s._stagflation_counter = max(0, stag_cnt - 1)

        if getattr(self.s, '_stagflation_counter', 0) >= 3 and random.random() < 0.25:
            duration = random.randint(252, 756)
            gri_drop = random.uniform(0.05, 0.15)
            # ★ [수정] 스태그플레이션 penalty 대폭 강화 (anchor 부스트 상쇄)
            # 현실: 1970년대 S&P 10년 횡보, 실질 -50% 수준
            self.s._scenario_drift_penalty = -(gri_drop * 8.0 / max(1, duration))
            self.s.current_scenario = "🔥 스태그플레이션 (물가↑ 성장↓)"
            self.s.scenario_timer   = duration
            self.s._stagflation_counter = 0
            self.s._last_crisis_year    = cur.year
            self.s._last_scenario_trigger_note = (
                f"스태그플레이션 | CPI:{cpi:.2f}% GDP역성장:{gdp_gr*100:.1f}% | "
                f"3개월 이상 동시충족 | 금리:{rate:.2f}%"
            )
            self._trigger_scenario_themes('스태그플레이션')
            if not silent:
                self.s.daily_news.append(
                    f"🔥 [스태그플레이션] {cur.year}년 물가({cpi:.1f}%) + "
                    f"GDP 역성장({gdp_gr*100:.1f}%) 동시 발생! "
                    f"전 섹터 장기 부진 — 에너지/소재 상대적 방어"
                )
            return

        # ① 긴축 쇼크
        tight_cnt = getattr(self.s, '_tightening_counter', 0)
        if cpi >= 4.0 and rate_rising and cycle == '확장':
            self.s._tightening_counter = tight_cnt + 1
        else:
            self.s._tightening_counter = max(0, tight_cnt - 2)

        if getattr(self.s, '_tightening_counter', 0) >= 2 and random.random() < 0.20:
            duration = random.randint(126, 378)
            gri_drop = random.uniform(0.05, 0.12)
            # ★ [수정] 긴축쇼크 penalty 강화
            self.s._scenario_drift_penalty = -(gri_drop * 6.0 / max(1, duration))
            self.s.current_scenario = f"📊 긴축 쇼크 (금리 {rate:.1f}% / CPI {cpi:.1f}%)"
            self.s.scenario_timer   = duration
            self.s._tightening_counter = 0
            self.s._last_scenario_trigger_note = (
                f"긴축쇼크 | CPI:{cpi:.2f}% 금리:{rate:.2f}% 상승중 | "
                f"사이클:{cycle} | 누적카운터2회 이상"
            )
            self._trigger_scenario_themes('긴축쇼크')
            if not silent:
                self.s.daily_news.append(
                    f"📊 [긴축 쇼크] {cur.year}년 금리 인상({rate:.1f}%) + "
                    f"물가({cpi:.1f}%) 압박 → 성장주 밸류에이션 하락 "
                    f"(Value/Defensive 상대적 방어)"
                )

    # ─────────────────────────────────────────────
    # ★ 10.6순위: 신규 시나리오 (5종)
    # ─────────────────────────────────────────────
    def _check_new_scenarios(self, silent: bool):
        """
        신규 시나리오 5종 발동 체크 (매달 1일).

        ① 기업 실적 장세  — PER 낮음 + 실적 성장 + 확장기
        ② 인플레이션 충격 — CPI 급등 + 성장 유지 (스태그와 구분)
        ③ 기술 패권 경쟁  — SOX 강세 + 수출규제 + LV2 이상
        ④ 부동산 버블 붕괴 — 부동산버블 시나리오 이후 금리 인상
        ⑤ 구조적 저성장   — LV3+ + GRI 장기 정체 + 고령화(선행지수 만성 저하)
        """
        cur      = self.s.current_date
        scenario = self.s.current_scenario

        if cur.day != 1:
            return
        if not getattr(self.s, '_market_fully_formed', False):
            return
        # 강한 시나리오 진행 중엔 발동 안 함
        if any(x in scenario for x in ["대공황", "전쟁", "분쟁", "팬데믹",
                                        "기술 패권", "인플레이션 충격",
                                        "기업 실적 장세", "부동산 버블 붕괴",
                                        "구조적 저성장"]):
            return

        macro    = self.s.macro
        rate     = macro.get('interest_rate', 4.0)
        cpi      = macro.get('cpi', 2.0)
        sox      = macro.get('semi_index', 1000.0)
        cycle    = getattr(self.s, 'cycle_stage', '확장')
        ff       = getattr(self.s, 'foreign_flow_index', 0.0)
        lv       = self.s.max_tech_reached
        last_crisis = getattr(self.s, '_last_crisis_year', 0)
        last_boom   = getattr(self.s, '_last_boom_year', 0)
        # ★ [최적화] PER 참조 — 당일 캐시 우선, 없으면 실시간 수집
        # (월 1일 호출이므로 빈도 낮지만, 같은 날 수집된 캐시가 있으면 재사용)
        stats    = getattr(self.s, '_last_market_stats', None) or self._collect_market_stats()
        per_l    = stats.get('per_large', 15.0)
        per_m    = stats.get('per_mid', 15.0)
        gdp_gr   = getattr(self.s, 'gdp_growth_rate', 0.03)
        leading  = getattr(self.s, 'leading_index', 0.0)
        prev_snap = getattr(self.s, '_prev_macro_snapshot', {})
        prev_rate = prev_snap.get('interest_rate', rate)

        # ★ [수정] anchor 사전 계산 (기업실적장세 조건에서 참조)
        _ye_anchor = max(0, cur.year - 2000)
        _lv_anchor = self.s.max_tech_reached
        _lv_base_anchor = {
            1: 1000 * (1.038 ** _ye_anchor),
            2: 1000 * (1.038**15) * (1.045**max(0, _ye_anchor-15)),
            3: 1000 * (1.038**15) * (1.045**20) * (1.032**max(0, _ye_anchor-35)),
            4: 1000 * (1.038**15) * (1.045**20) * (1.032**25) * (1.022**max(0, _ye_anchor-60)),
        }.get(_lv_anchor, 1000.0)
        anchor = self.s.gri / max(1.0, _lv_base_anchor)

        roll = random.random()

        # ① 기업 실적 장세
        # 조건: PER 대형주 낮음(저평가) + 실적 성장 + 확장기 + 위기 후 안정기
        # 현실: 2003~2007, 2013~2015 코스피 실적 장세
        # ★ [수정] 기업실적장세 조건 강화 (버그2: 연속 boom → GRI 폭등)
        earn_boom_ok = (
            per_l <= 12.0 and       # 15.0 → 12.0 (더 엄격한 저평가 기준)
            per_m <= 15.0 and       # 18.0 → 15.0
            gdp_gr >= 0.04 and      # 0.03 → 0.04 (GDP 성장률 더 강해야 발동)
            cycle in ('확장', '정점') and
            anchor < 0.90 and       # GRI가 기준선 90% 미만일 때만 (신규: 이미 과열이면 차단)
            (cur.year - last_crisis) >= 3 and   # 2년 → 3년
            (cur.year - last_boom) >= 2         # 1년 → 2년
        )
        earn_prob = 0.035 if earn_boom_ok else 0.003
        if roll < earn_prob:
            duration = random.randint(252, 630)
            self.s.boom_event = {'type': '기업실적장세', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "📊 기업 실적 장세 (이익 성장 주도)"
            self._trigger_scenario_themes('기업실적장세')
            self.s.scenario_timer = duration
            self.s._last_boom_year = cur.year
            self.s._last_scenario_trigger_note = (
                f"기업실적장세 | PER대:{per_l:.1f}x PER중:{per_m:.1f}x | "
                f"GDP:{gdp_gr*100:.1f}% | 사이클:{cycle} | 직전위기:{last_crisis}년"
            )
            if not silent:
                self.s.daily_news.append(
                    f"📊 [기업 실적 장세] {cur.year}년 저PER({per_l:.0f}x) + 실적 성장 → "
                    f"펀더멘털 주도 상승! Value/Growth 전 섹터 고른 강세"
                )
            return

        cum = earn_prob

        # ② 인플레이션 충격 (스태그와 구분: 성장은 유지되나 물가만 폭등)
        # 조건: CPI 4%+ + 확장기 유지 + 금리 인상 중 + 스태그 아님
        # 현실: 2021~2022 코로나 이후 공급망 인플레
        lv_target_cpi = {1: 2.0, 2: 2.5, 3: 4.0, 4: 1.0}.get(lv, 2.0)
        infla_ok = (
            cpi >= lv_target_cpi + 2.0 and   # 목표 CPI 2%p 초과
            gdp_gr >= 0.02 and               # 성장은 유지
            cycle in ('확장', '정점') and     # 스태그(수축기) 아님
            rate > prev_rate                 # 금리 인상 중
        )
        infla_cnt = getattr(self.s, '_inflation_shock_counter', 0)
        if infla_ok:
            self.s._inflation_shock_counter = infla_cnt + 1
        else:
            self.s._inflation_shock_counter = max(0, infla_cnt - 1)

        infla_prob = 0.0
        if getattr(self.s, '_inflation_shock_counter', 0) >= 2:
            infla_prob = 0.25

        if roll < (cum + infla_prob):
            duration = random.randint(189, 504)
            gri_drop = random.uniform(0.06, 0.14)
            self.s._scenario_drift_penalty = -(gri_drop * 6.0 / max(1, duration))  # [수정] 인플레충격
            self.s.current_scenario = f"🔥 인플레이션 충격 (CPI {cpi:.1f}%)"
            self.s.scenario_timer   = duration
            self.s._last_crisis_year = cur.year
            self.s._inflation_shock_counter = 0
            self._trigger_scenario_themes('인플레이션충격')
            self.s._last_scenario_trigger_note = (
                f"인플레이션충격 | CPI:{cpi:.2f}%(목표+{cpi-lv_target_cpi:.1f}%p) | "
                f"GDP:{gdp_gr*100:.1f}% 성장유지 | 금리인상중:{rate:.2f}%"
            )
            # 인플레 충격: 금리 추가 압력 + 실질 소비 위축
            macro['interest_rate'] = min(10.0, rate + random.uniform(0.25, 0.75))
            if not silent:
                self.s.daily_news.append(
                    f"🔥 [인플레이션 충격] {cur.year}년 CPI {cpi:.1f}% 급등! "
                    f"성장은 유지되나 실질 구매력 급락 — "
                    f"에너지/소재 수혜, 소비재/성장주 타격"
                )
            return

        cum += infla_prob

        # ③ 기술 패권 경쟁
        # 조건: LV2 이상 + SOX 강세 + 수출호황 이후 + 대국간 긴장
        # 현실: 2018~2020 미중 반도체 전쟁
        phase = getattr(self.s, '_last_processed_phase', '1A')
        _SOX_LONG_TARGET = {
            '1A': 500, '1B': 1500, '2A': 5000, '2B': 15000,
            '3A': 50000, '3B': 120000, '4A': 300000, '4B': 999999,
        }
        sox_tgt = _SOX_LONG_TARGET.get(phase, 1000.0)
        tech_war_ok = (
            lv >= 2 and
            sox >= sox_tgt * 1.5 and         # SOX 목표 150% 이상 (반도체 강세)
            (cur.year - last_boom) >= 1 and  # 호재 직후엔 없음
            phase in ('2A', '2B', '3A', '3B')
        )
        tech_war_prob = 0.020 if tech_war_ok else 0.002
        if roll < (cum + tech_war_prob):
            duration = random.randint(378, 756)
            gri_drop = random.uniform(0.05, 0.12)
            self.s._scenario_drift_penalty = -(gri_drop * 5.0 / max(1, duration))  # [수정] 기술패권
            self.s.current_scenario = "⚔️ 기술 패권 경쟁 (반도체 수출 제한)"
            self.s.scenario_timer   = duration
            self.s._last_crisis_year = cur.year
            self._trigger_scenario_themes('기술패권경쟁')
            # ★ [수정] SOX 즉시 충격 — 하한 보장 (버그4: SOX 104 폭락 방지)
            # 트리거 조건: sox가 long_target의 50% 이상일 때만 강한 충격
            _sox_tgt_now = sox_tgt if 'sox_tgt' in dir() else macro.get('semi_index', 1000.0)
            _sox_ratio_now = sox / max(1.0, _sox_tgt_now)
            if _sox_ratio_now >= 0.5:
                _shock_rate = random.uniform(0.65, 0.80)  # 20~35% 하락
            else:
                _shock_rate = random.uniform(0.82, 0.92)  # 이미 낮으면 8~18%만
            macro['semi_index'] = max(sox_tgt * 0.15, sox * _shock_rate)
            self.s._last_scenario_trigger_note = (
                f"기술패권경쟁 | SOX:{sox:.0f}(목표{sox_tgt:.0f}의{sox/sox_tgt*100:.0f}%) | "
                f"LV:{lv} 페이즈:{phase} | 직전호재:{last_boom}년"
            )
            if not silent:
                self.s.daily_news.append(
                    f"⚔️ [기술 패권 경쟁] {cur.year}년 주요국 반도체 수출 제한 발동! "
                    f"SOX -{(1-macro['semi_index']/sox)*100:.0f}% 즉각 충격 — "
                    f"IT 단기 타격, 소재/장비 중장기 수혜"
                )
            return

        cum += tech_war_prob

        # ④ 부동산 버블 붕괴
        # 조건: 부동산버블 시나리오 이후 금리 인상 + 부동산 시총 비중 급감
        # 현실: 2008 미국 서브프라임, 2022 한국 부동산 조정
        re_cap = sum(
            s['market_cap'] for s in self.s.stocks
            if s['meta'].get('ind') == '부동산'
        )
        total_cap = sum(s['market_cap'] for s in self.s.stocks) or 1
        re_ratio  = re_cap / total_cap
        re_burst_ok = (
            re_ratio >= 0.08 and            # 부동산 비중 8% 이상
            rate >= 4.0 and                 # 금리 4% 이상
            cycle in ('수축', '저점') and   # 경기 하강 중
            per_l <= 20.0                   # 주식 시장은 아직 과열 아님
        )
        re_burst_cnt = getattr(self.s, '_re_burst_counter', 0)
        if re_burst_ok:
            self.s._re_burst_counter = re_burst_cnt + 1
        else:
            self.s._re_burst_counter = max(0, re_burst_cnt - 1)

        re_burst_prob = 0.0
        if getattr(self.s, '_re_burst_counter', 0) >= 2:
            re_burst_prob = 0.20

        if roll < (cum + re_burst_prob):
            duration = random.randint(378, 756)
            gri_drop = random.uniform(0.10, 0.20)
            immediate = gri_drop * 0.30
            self.s.gri = max(50.0, self.s.gri * (1.0 - immediate))  # ★ 플로어 100→50
            self.s._scenario_drift_penalty = -(gri_drop * 5.0 / max(1, duration))  # [수정]
            self.s.current_scenario = "🏚️ 부동산 버블 붕괴 (자산 디레버리징)"
            self.s.scenario_timer   = duration
            self.s._last_crisis_year = cur.year
            self.s._re_burst_counter = 0
            self._trigger_scenario_themes('부동산버블붕괴')
            self.s._last_scenario_trigger_note = (
                f"부동산버블붕괴 | 부동산비중:{re_ratio*100:.1f}% | "
                f"금리:{rate:.2f}% | 사이클:{cycle} | 즉시충격:{immediate*100:.0f}%"
            )
            if not silent:
                self.s.daily_news.append(
                    f"🏚️ [부동산 버블 붕괴] {cur.year}년 부동산 자산 가치 급락! "
                    f"고금리({rate:.1f}%) + 경기 하강 → 담보 가치 하락 — "
                    f"금융/부동산 집중 타격, 필수소비재 상대 방어"
                )
            return

        cum += re_burst_prob

        # ⑤ 구조적 저성장
        # 조건: LV3 이상 + 선행지수 만성 저하 + GRI 장기 정체 + 대공황 아님
        # 현실: 일본 잃어버린 30년, 유럽 장기 침체
        _lv3_raw = getattr(self.s, '_tech_upgrade_years', {}).get(3, None)
        _lv3_start = _lv3_raw if isinstance(_lv3_raw, int) else 9999
        years_in_lv3 = cur.year - _lv3_start if _lv3_start < 9999 else 0
        hist20 = getattr(self.s, '_gri_history_20', [])
        gri_stagnant = False
        if len(hist20) >= 20:
            gri_20d_chg = (hist20[-1] - hist20[0]) / max(1.0, hist20[0])
            gri_stagnant = abs(gri_20d_chg) < 0.02   # 20일간 2% 미만 변동

        secular_ok = (
            lv >= 3 and
            years_in_lv3 >= 10 and          # LV3 진입 후 10년 이상
            leading <= -0.1 and             # 선행지수 만성 하락
            gri_stagnant and
            (cur.year - last_crisis) >= 3
        )
        secular_cnt = getattr(self.s, '_secular_stagnation_counter', 0)
        if secular_ok:
            self.s._secular_stagnation_counter = secular_cnt + 1
        else:
            self.s._secular_stagnation_counter = max(0, secular_cnt - 1)

        secular_prob = 0.0
        if getattr(self.s, '_secular_stagnation_counter', 0) >= 60:  # 60일 지속
            secular_prob = 0.30

        if roll < (cum + secular_prob):
            duration = random.randint(504, 1512)  # 2~6년
            self.s._scenario_drift_penalty = -random.uniform(0.001, 0.003) / 252
            self.s.current_scenario = "📉 구조적 저성장 (성숙 경제 장기 침체)"
            self.s.scenario_timer   = duration
            self.s._secular_stagnation_counter = 0
            self._trigger_scenario_themes('구조적저성장')
            self.s._last_scenario_trigger_note = (
                f"구조적저성장 | LV3진입후{years_in_lv3}년 | "
                f"선행지수:{leading:.2f} | GRI20일변동:{gri_20d_chg*100:.1f}%"
            )
            if not silent:
                self.s.daily_news.append(
                    f"📉 [구조적 저성장] {cur.year}년 성숙 경제 장기 침체 진입! "
                    f"AI/기술 혁신에도 실물 경제 성장 정체 — "
                    f"배당/방어주 부각, 성장주 장기 소외 경고"
                )

    # ─────────────────────────────────────────────
    # ★ 10.65순위: 경기 정상화 / 금융 완화 사이클 (신규 2종)
    # ─────────────────────────────────────────────
    def _check_recovery_cycle(self, silent: bool):
        """
        신규 시나리오 2종:

        ⑥ 경기 정상화 — 위기 후 안정 회복기. 전 섹터 완만 상승
        ⑦ 금융 완화 사이클 — 금리 인하 + 신용 확장. 금융/부동산 선행
        """
        cur      = self.s.current_date
        scenario = self.s.current_scenario

        if cur.day != 1:
            return
        if not getattr(self.s, '_market_fully_formed', False):
            return
        if any(x in scenario for x in ["대공황", "전쟁", "분쟁", "팬데믹",
                                        "정상화", "금융 완화"]):
            return

        macro       = self.s.macro
        rate        = macro.get('interest_rate', 4.0)
        cpi         = macro.get('cpi', 2.0)
        cycle       = getattr(self.s, 'cycle_stage', '확장')
        ff          = getattr(self.s, 'foreign_flow_index', 0.0)
        lv          = self.s.max_tech_reached
        last_crisis = getattr(self.s, '_last_crisis_year', 0)
        last_boom   = getattr(self.s, '_last_boom_year', 0)
        prev_snap   = getattr(self.s, '_prev_macro_snapshot', {})
        prev_rate   = prev_snap.get('interest_rate', rate)
        lv_target_cpi = {1: 2.0, 2: 2.5, 3: 4.0, 4: 1.0}.get(lv, 2.0)
        roll = random.random()

        # ⑥ 경기 정상화
        # 조건: 위기 후 1~3년 경과 + CPI 안정 + 확장기 진입 + 이전 시나리오가 위기 계열
        norm_ok = (
            1 <= (cur.year - last_crisis) <= 4 and
            cpi <= lv_target_cpi + 0.5 and
            cycle in ('확장', '정점') and
            ff >= -10 and
            (cur.year - last_boom) >= 1
        )
        norm_prob = 0.040 if norm_ok else 0.003
        if roll < norm_prob:
            duration = random.randint(189, 504)
            self.s.boom_event = {'type': '경기정상화', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🌅 경기 정상화 (위기 후 회복)"
            self._trigger_scenario_themes('경기정상화')
            self.s.scenario_timer  = duration
            self.s._last_boom_year = cur.year
            self.s._last_scenario_trigger_note = (
                f"경기정상화 | 직전위기후{cur.year - last_crisis}년 | "
                f"CPI:{cpi:.2f}% 금리:{rate:.2f}% | ff:{ff:.0f} 사이클:{cycle}"
            )
            if not silent:
                self.s.daily_news.append(
                    f"🌅 [경기 정상화] {cur.year}년 위기 후 안정 회복 진입! "
                    f"CPI {cpi:.1f}% 안정 + 경기 확장 — 전 섹터 완만한 균형 상승 "
                    f"(특정 섹터 쏠림 없이 실적 기반)"
                )
            return

        # ⑦ 금융 완화 사이클
        # 조건: 금리 인하 추세 + CPI 안정 + 신용 확장 환경
        # 현실: 2008 이후 제로금리 시대, 2019 예방적 인하
        rate_easing = rate < prev_rate and rate <= 3.5
        easing_ok = (
            rate_easing and
            cpi <= lv_target_cpi + 0.5 and
            cycle in ('확장', '저점', '수축') and  # 저점에서 인하 시작
            (cur.year - last_crisis) >= 1
        )
        easing_cnt = getattr(self.s, '_easing_counter', 0)
        if easing_ok:
            self.s._easing_counter = easing_cnt + 1
        else:
            self.s._easing_counter = max(0, easing_cnt - 1)

        easing_prob = 0.0
        if getattr(self.s, '_easing_counter', 0) >= 3:  # 3개월 인하 추세
            easing_prob = 0.030

        if roll < (norm_prob + easing_prob):
            duration = random.randint(252, 630)
            self.s.boom_event = {'type': '금융완화', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🏦 금융 완화 사이클 (저금리 신용 확장)"
            self._trigger_scenario_themes('금융완화')
            self.s.scenario_timer  = duration
            self.s._last_boom_year = cur.year
            self.s._easing_counter = 0
            self.s._last_scenario_trigger_note = (
                f"금융완화 | 금리인하추세:{rate:.2f}%(3개월+) | "
                f"CPI:{cpi:.2f}% | 사이클:{cycle}"
            )
            if not silent:
                self.s.daily_news.append(
                    f"🏦 [금융 완화 사이클] {cur.year}년 저금리({rate:.1f}%) 신용 확장 진입! "
                    f"금융/부동산 선행 수혜, 이후 성장주 합류 — "
                    f"대출·투자 심리 개선으로 자산 가격 상승"
                )

    # ─────────────────────────────────────────────
    # ★ 10.7순위: 환율 위기
    # ─────────────────────────────────────────────
    def _check_exchange_crisis(self, silent: bool):
        """
        외국인 자금 이탈(-60 이하) + 환율 1400원+ 조건 3개월 지속 시 발동.
        수입 원가 급등 → 내수 타격, 수출주 단기 수혜.
        금리 강제 인상 압력 동반.
        """
        cur      = self.s.current_date
        scenario = self.s.current_scenario

        if any(x in scenario for x in ["환율위기", "대공황", "전쟁", "팬데믹"]):
            return
        if not getattr(self.s, '_market_fully_formed', False):
            return
        if cur.day != 1:
            return

        macro    = self.s.macro
        exchange = macro.get('exchange_rate', 1100.0)
        ff       = getattr(self.s, 'foreign_flow_index', 0.0)

        fx_cnt = getattr(self.s, '_fx_crisis_counter', 0)
        if ff <= -60 and exchange >= 1400.0:
            self.s._fx_crisis_counter = fx_cnt + 1
        else:
            self.s._fx_crisis_counter = max(0, fx_cnt - 1)
            return

        if getattr(self.s, '_fx_crisis_counter', 0) < 3:
            return
        if random.random() > 0.40:
            return

        duration = random.randint(126, 378)
        gri_drop = random.uniform(0.08, 0.18)
        self.s._scenario_drift_penalty = -(gri_drop * 6.0 / max(1, duration))  # [수정]
        self.s.current_scenario = f"💱 환율 위기 (원/달러 {exchange:.0f}원)"
        self.s.scenario_timer   = duration
        self.s._fx_crisis_counter = 0
        self.s._last_crisis_year  = cur.year

        # 환율 방어 긴급 금리 인상
        macro['interest_rate'] = min(8.0, macro['interest_rate'] + random.uniform(0.5, 1.5))
        self._trigger_scenario_themes('환율위기')

        if not silent:
            self.s.daily_news.append(
                f"💱 [환율 위기] {cur.year}년 원/달러 {exchange:.0f}원! "
                f"외국인 이탈(수급 {ff:.0f}) 지속 — "
                f"금리 {macro['interest_rate']:.1f}%로 긴급 인상"
            )
            self.s.daily_news.append(
                f"  └ 수입 원가 급등 → 내수/소비재 타격 "
                f"| IT/수출주 단기 환율 수혜"
            )

    # ─────────────────────────────────────────────
    # ★ 10.9순위: 박스권 횡보
    # ─────────────────────────────────────────────
    def _check_boxrange(self, silent: bool):
        """
        GRI anchor 0.85~1.15 구간에서 60일 이상 머물 때 횡보 진입.
        drift를 ±0.00003으로 수렴 → 자연스러운 장기 횡보 구현.
        강한 시나리오 발생 시 자동 해제.
        """
        cur      = self.s.current_date
        scenario = self.s.current_scenario

        # ★ [수정] 게임 시작 후 최소 1년(252일)은 박스권 진입 불가
        # 문제: 시작 직후 GRI가 lv_base(1000) 근처라 60일만 지나면 무조건 박스권 발동
        _start_date = getattr(self.s, 'start_date', cur)
        _days_since_start = (cur - _start_date).days
        if _days_since_start < 252:
            self.s._boxrange_counter = 0
            return

        # 강한 시나리오 진행 중엔 박스권 없음
        if any(x in scenario for x in ["전쟁", "분쟁", "대공황", "팬데믹",
                                        "수출 호황", "유동성 장세", "혁신 기술",
                                        "외부충격", "긴축", "스태그", "버블붕괴"]):
            self.s._boxrange_counter = 0
            if getattr(self.s, '_in_boxrange', False):
                self.s._in_boxrange   = False
                self.s._boxrange_active = False
                if scenario == "📦 박스권 횡보":
                    self.s.current_scenario = "정상 성장"
            return

        lv  = self.s.max_tech_reached
        ye  = max(0, cur.year - 2000)
        # ★ [수정] market.py와 동일한 lv_base 사용 (버그: 별도 1.075 → 항상 저평가 판정 → boom 과잉)
        lv_base = {
            1: 1000 * (1.038 ** ye),
            2: 1000 * (1.038**15) * (1.045**max(0, ye-15)),
            3: 1000 * (1.038**15) * (1.045**20) * (1.032**max(0, ye-35)),
            4: 1000 * (1.038**15) * (1.045**20) * (1.032**25) * (1.022**max(0, ye-60)),
        }.get(lv, 1000.0)
        anchor = self.s.gri / max(1.0, lv_base)
        in_range = (0.85 <= anchor <= 1.15)

        box_cnt = getattr(self.s, '_boxrange_counter', 0)
        if in_range:
            self.s._boxrange_counter = box_cnt + 1
        else:
            self.s._boxrange_counter = max(0, box_cnt - 5)
            if getattr(self.s, '_in_boxrange', False):
                self.s._in_boxrange     = False
                self.s._boxrange_active = False
                if scenario == "📦 박스권 횡보":
                    self.s.current_scenario = "정상 성장"
                if not silent:
                    self.s.daily_news.append(
                        f"📊 [박스권 탈출] GRI가 횡보 구간을 벗어났습니다. "
                        f"(anchor {anchor:.2f})"
                    )
            return

        if getattr(self.s, '_boxrange_counter', 0) >= 60 and \
           not getattr(self.s, '_in_boxrange', False):
            self.s._in_boxrange     = True
            self.s._boxrange_active = True
            self.s.current_scenario = "📦 박스권 횡보"
            if not silent:
                gri_lo = lv_base * 0.85
                gri_hi = lv_base * 1.15
                self.s.daily_news.append(
                    f"📦 [박스권 진입] 시장이 GRI {gri_lo:.0f}~{gri_hi:.0f} "
                    f"구간에서 장기 횡보 중 — 개별 종목 선별 중요."
                )
        elif getattr(self.s, '_in_boxrange', False):
            self.s._boxrange_active = True

    def _pick_new_sub(self, ind: str, tier_raw: str,
                       phase: str, existing: list, ind_data: dict) -> str:
        """
        현재 페이즈 + 티어에서 기존 sub_list에 없는 새 사업 하나 선택.
        없으면 인접 티어, 그래도 없으면 common에서 선택.
        """
        # 티어 매핑
        tier_key = "대" if tier_raw in ('대1', '대') else tier_raw

        candidates = []
        # 1순위: 현재 페이즈 해당 티어
        candidates = [
            s for s in ind_data.get(phase, {}).get(tier_key, [])
            if s not in existing
        ]
        # 2순위: 현재 페이즈 인접 티어
        if not candidates:
            for t in ['중', '소', '대']:
                if t == tier_key:
                    continue
                candidates = [
                    s for s in ind_data.get(phase, {}).get(t, [])
                    if s not in existing
                ]
                if candidates:
                    break
        # 3순위: common
        if not candidates:
            for t_list in ind_data.get('common', {}).values():
                candidates += [s for s in t_list if s not in existing]

        return random.choice(candidates) if candidates else ""

    # ─────────────────────────────────────────────
    # ★ 산업 패권 시스템
    # 특정 산업이 시총 비중 임계값을 3년 이상 유지하면 패권 선언
    # 패권 산업 → 외국인 자금 추가 유입 + 광기 지수 가속
    # ─────────────────────────────────────────────
    def _check_industry_dominance(self, silent: bool):
        """
        매달 1일: 산업별 시총 비중 계산 → 패권 후보 판정 → 패권 선언/해제
        """
        from engine.constants import INDUSTRY_DOMINANCE
        cur = self.s.current_date
        if cur.day != 1:
            return

        stocks = self.s.stocks
        if not stocks:
            return

        # 산업별 시총 합산
        total_cap = sum(s['market_cap'] for s in stocks)
        if total_cap <= 0:
            return

        ind_caps = {}
        for s in stocks:
            ind = s['meta'].get('ind', '')
            if ind:
                ind_caps[ind] = ind_caps.get(ind, 0) + s['market_cap']

        threshold = INDUSTRY_DOMINANCE["dominance_threshold"]
        req_years = INDUSTRY_DOMINANCE["dominance_years"]
        max_dom   = INDUSTRY_DOMINANCE["max_dominance"]

        dom_counter = getattr(self.s, '_dominance_counter', {})
        cur_dominant = getattr(self.s, 'dominant_industry', None)

        # 각 산업 비중 체크
        new_dominant = None
        new_level    = None

        for ind, cap in ind_caps.items():
            ratio = cap / total_cap
            ratio = min(ratio, max_dom)  # 상한 적용

            if ratio >= threshold:
                dom_counter[ind] = dom_counter.get(ind, 0) + 1
            else:
                dom_counter[ind] = max(0, dom_counter.get(ind, 0) - 1)

            # 패권 강도 결정
            if ratio >= 0.35:
                level = "강"
            elif ratio >= 0.28:
                level = "중"
            elif ratio >= threshold:
                level = "약"
            else:
                continue

            # 3년 이상 유지 시 패권 선언
            if dom_counter.get(ind, 0) >= req_years * 12:  # 월 단위
                new_dominant = ind
                new_level    = level

        # 패권 변화 처리
        old_dominant = getattr(self.s, 'dominant_industry', None)
        self.s._dominance_counter = dom_counter

        if new_dominant != old_dominant:
            if new_dominant:
                self.s.dominant_industry = new_dominant
                self.s.dominance_level   = new_level
                self.s.dominance_years   = dom_counter.get(new_dominant, 0) // 12
                if not silent:
                    ratio = ind_caps.get(new_dominant, 0) / total_cap
                    self.s.daily_news.append(
                        f"👑 [산업 패권] {new_dominant} 산업이 시총 {ratio*100:.1f}% 점유로 "
                        f"패권 산업으로 부상! (강도: {new_level}) "
                        f"외국인 자금 집중 유입 예상 — 광기 지수 가속 경고"
                    )
            else:
                self.s.dominant_industry = None
                self.s.dominance_level   = None
                self.s.dominance_years   = 0
                if not silent and old_dominant:
                    self.s.daily_news.append(
                        f"⚖️ [패권 해체] {old_dominant} 산업 독주 시대 종료 — "
                        f"시장 분산 구조로 전환"
                    )
        elif new_dominant:
            # 패권 유지 중: 강도 업데이트
            self.s.dominance_level = new_level
            self.s.dominance_years = dom_counter.get(new_dominant, 0) // 12

        # 패권 산업에 외국인 자금 추가 유입 적용
        if self.s.dominant_industry and self.s.dominance_level:
            ff_boost = INDUSTRY_DOMINANCE["ff_boost_per_level"].get(
                self.s.dominance_level, 0.0
            )
            # 월 단위 ff 조정 (너무 빠른 변화 방지)
            self.s.foreign_flow_index = min(
                100.0,
                getattr(self.s, 'foreign_flow_index', 0.0) + ff_boost / 12
            )

    # ─────────────────────────────────────────────
    # ★ 광기 지수 업데이트
    # 실적 발표 분기(2/5/8/11월 1일)에 PER 괴리 기반으로 계산
    # ─────────────────────────────────────────────
    def _update_mania_index(self, silent: bool):
        """
        분기 실적 발표 시점에 시장 광기 지수 업데이트.
        mmi = 시가총액가중 평균 (현재PER / 섹터정상PER)
        """
        from engine.constants import SECTOR_MAP, NORMAL_PER_BY_SECTOR, MANIA_INDEX_THRESHOLDS
        cur = self.s.current_date
        # 분기 실적 발표 월 1일에만 계산
        if cur.month not in [2, 5, 8, 11] or cur.day != 1:
            return

        stocks = self.s.stocks
        if not stocks:
            return

        total_weighted = 0.0
        total_weight   = 0.0
        eh = self.s.earnings_history

        for s in stocks:
            meta   = s['meta']
            name   = meta.get('c_name', '')
            ind    = meta.get('ind', '')
            sector = SECTOR_MAP.get(ind, 'Value')
            mc     = s.get('market_cap', 0)
            if mc <= 0:
                continue

            # 연간 순이익 계산
            hist = eh.get(name, {})
            if not hist:
                continue
            recent_yr  = sorted(hist.keys())[-1]
            annual_ni  = sum(
                q.get('net_income', 0) for q in hist[recent_yr].values()
            ) * 4
            if annual_ni <= 0:
                continue

            per = mc / max(1.0, annual_ni)
            normal_per = NORMAL_PER_BY_SECTOR.get(sector, 15.0)
            per_ratio  = per / normal_per  # 1.0 = 정상

            total_weighted += per_ratio * mc
            total_weight   += mc

        if total_weight <= 0:
            return

        new_mmi = total_weighted / total_weight

        # 히스토리 관리
        history = getattr(self.s, '_mania_history', [])
        history.append(new_mmi)
        if len(history) > 20:
            history.pop(0)
        self.s._mania_history = history
        self.s.market_mania_index = new_mmi

        # ★ [신규] GRI 성장속도 → 광기지수 가속
        # 현실: 시장이 빠르게 오를수록 투기 심리 자기강화 → 버블 형성
        # GRI 1년 상승률이 50% 이상이면 광기지수 추가 부스트
        _gri_hist = getattr(self.s, '_gri_history_252', [])  # 1년치 GRI
        if not hasattr(self.s, '_gri_history_252'):
            self.s._gri_history_252 = []
        self.s._gri_history_252.append(self.s.gri)
        if len(self.s._gri_history_252) > 252:
            self.s._gri_history_252.pop(0)
        _gri_hist = self.s._gri_history_252

        _growth_boost = 0.0
        if len(_gri_hist) >= 126:  # 6개월 이상 데이터
            _gri_6m_growth = (_gri_hist[-1] - _gri_hist[-126]) / max(1.0, _gri_hist[-126])
            if   _gri_6m_growth >= 1.00:  _growth_boost = 0.30   # 6개월 +100%: 닷컴 버블급
            elif _gri_6m_growth >= 0.50:  _growth_boost = 0.15   # 6개월 +50%
            elif _gri_6m_growth >= 0.30:  _growth_boost = 0.08   # 6개월 +30%
            elif _gri_6m_growth >= 0.15:  _growth_boost = 0.03   # 6개월 +15%

        self.s.market_mania_index = min(5.0, new_mmi + _growth_boost)

        # 패권 산업이 있으면 광기 지수 추가 가속
        if self.s.dominant_industry and self.s.dominance_level:
            from engine.constants import INDUSTRY_DOMINANCE
            mania_boost = INDUSTRY_DOMINANCE["mania_boost_per_level"].get(
                self.s.dominance_level, 0.0
            )
            self.s.market_mania_index = min(5.0, self.s.market_mania_index + mania_boost)

        # 버블 사이클 카운터 업데이트
        # 광기 지수가 "버블" 구간(1.8)을 찍었다가 "정상(1.2 이하)"으로 내려오면 1 카운트
        bubble_threshold = MANIA_INDEX_THRESHOLDS["버블"]
        normal_threshold = 1.2
        if len(history) >= 4:
            was_bubble = any(m >= bubble_threshold for m in history[-8:-4])
            is_normal  = all(m <= normal_threshold for m in history[-4:])
            last_burst = getattr(self.s, '_last_bubble_burst_year', 0)
            if was_bubble and is_normal and cur.year > last_burst:
                self.s._bubble_cycle_count = getattr(self.s, '_bubble_cycle_count', 0) + 1
                self.s._last_bubble_burst_year = cur.year
                if not silent:
                    self.s.daily_news.append(
                        f"📉 [버블 사이클 완료] 시장 광기가 정상화됐습니다. "
                        f"버블 경험 누적: {self.s._bubble_cycle_count}회 "
                        f"(LV4 조건: 2회 필요)"
                    )

        # 광기 지수 경고
        if not silent:
            if self.s.market_mania_index >= MANIA_INDEX_THRESHOLDS["붕괴전"]:
                self.s.daily_news.append(
                    f"🚨 [광기 최고조] 시장 광기 지수 {self.s.market_mania_index:.2f} — "
                    f"작은 악재 하나에 -30% 폭락 가능. 버블 붕괴 임박!"
                )
            elif self.s.market_mania_index >= MANIA_INDEX_THRESHOLDS["광기"]:
                self.s.daily_news.append(
                    f"⚠️ [시장 광기] 광기 지수 {self.s.market_mania_index:.2f} — "
                    f"펀더멘탈 대비 극도 고평가. 대공황 트리거 민감도 상승"
                )
            elif self.s.market_mania_index >= MANIA_INDEX_THRESHOLDS["버블"]:
                if self.s.has_paid_news_access:
                    self.s.daily_news.append(
                        f"💎 [버블 경고] 시장 광기 지수 {self.s.market_mania_index:.2f} — "
                        f"실적 대비 주가 과열 구간 진입 (프리미엄 전용)"
                    )

    # ─────────────────────────────────────────────
    # ★ LV4 복합 조건 체크
    # 모든 조건이 동시에 충족될 때만 LV4 전환 가능
    # ─────────────────────────────────────────────
    def _check_lv4_conditions(self, silent: bool):
        """
        LV3에서만 호출. 복합 조건 충족 일수를 누적.
        조건이 깨지면 즉시 리셋.
        """
        from engine.constants import LV4_UNLOCK_CONDITIONS
        cur = self.s.current_date

        conditions = LV4_UNLOCK_CONDITIONS
        sox    = self.s.macro.get('semi_index', 1000.0)
        stocks = self.s.stocks
        if not stocks:
            return

        total_cap  = sum(s['market_cap'] for s in stocks)
        bio_cap    = sum(
            s['market_cap'] for s in stocks
            if s['meta'].get('ind') == '건강관리'
        )
        bio_ratio  = bio_cap / max(1, total_cap)
        bubble_cnt = getattr(self.s, '_bubble_cycle_count', 0)

        # 조건 충족 여부
        cond_sox    = sox >= conditions["sox_threshold"]
        cond_bio    = bio_ratio >= conditions["bio_cap_ratio"]
        cond_bubble = bubble_cnt >= conditions["bubble_cycle_count"]
        cond_gri    = self.s.gri >= conditions["gri_threshold"]

        all_met = cond_sox and cond_bio and cond_bubble and cond_gri

        # 상태 캐시 저장
        self.s._lv4_condition_status = {
            "SOX":    f"{'✓' if cond_sox else '✗'} {sox:.0f}/{conditions['sox_threshold']:.0f}",
            "바이오": f"{'✓' if cond_bio else '✗'} {bio_ratio*100:.1f}%/{conditions['bio_cap_ratio']*100:.0f}%",
            "버블":   f"{'✓' if cond_bubble else '✗'} {bubble_cnt}/{conditions['bubble_cycle_count']}회",
            "GRI":    f"{'✓' if cond_gri else '✗'} {self.s.gri:.0f}/{conditions['gri_threshold']:.0f}",
        }

        if all_met:
            self.s._lv4_condition_days = getattr(self.s, '_lv4_condition_days', 0) + 1

            sustain = conditions["sustain_days"]
            if not silent and self.s._lv4_condition_days == 1:
                self.s.daily_news.append(
                    f"🌟 [특이점 조건 달성] LV4 전환 조건이 모두 충족되었습니다! "
                    f"{sustain}일간 유지 시 특이점 도달 — "
                    f"SOX/바이오/버블사이클/GRI 전부 클리어"
                )
            elif not silent and self.s._lv4_condition_days % 63 == 0:
                remaining = sustain - self.s._lv4_condition_days
                self.s.daily_news.append(
                    f"⏳ [특이점 카운트다운] LV4 전환까지 약 {remaining}일 남았습니다."
                )
        else:
            # 조건 미충족 시 리셋
            if self.s._lv4_condition_days > 0 and not silent:
                self.s.daily_news.append(
                    f"❌ [특이점 조건 이탈] 조건 미충족으로 LV4 카운트다운 리셋 "
                    f"({self.s._lv4_condition_days}일 → 0일)"
                )
            self.s._lv4_condition_days = 0

    # ─────────────────────────────────────────────
    # ★ 배당 시스템
    # ─────────────────────────────────────────────
    def _check_dividend(self, silent: bool):
        """
        매년 3월 첫 거래일: 전년도 순이익 기반 배당 결정 + 뉴스 공시
        매년 4월 첫 거래일: 배당락 처리 (주가 하락) + 배당금 플레이어 지급
        """
        from engine.constants import DIVIDEND_PAYOUT_RATIO, SECTOR_MAP, DIVIDEND_MIN_HP

        cur   = self.s.current_date
        month = cur.month
        day   = cur.day

        # ── 중복 실행 방지 플래그 ─────────────────────────────
        # 3월/4월 배당 처리는 연도×월 기준 1회만 실행
        _div_done_key = f"_div_done_{cur.year}_{month}"
        if getattr(self.s, _div_done_key, False):
            return

        # 3월 또는 4월 첫 거래일 판정 (1~7일 중 첫 평일 — 가상 요일 기준)
        if day > 7 or self.s.virtual_weekday >= 5:
            return

        # ── 3월: 배당 결정 ───────────────────────────────────────
        if month == 3:
            prev_year = str(cur.year - 1)
            decided = []
            for stock in self.s.stocks:
                meta = stock['meta']
                name = meta['c_name']

                # 전년도 연간 순이익 합산
                hist = self.s.earnings_history.get(name, {})
                yr_data = hist.get(prev_year, {})
                if not yr_data:
                    meta['div_per_share'] = 0
                    meta['div_yield'] = 0.0
                    continue

                annual_ni = sum(q.get('net_income', 0) for q in yr_data.values())

                # 배당 조건: 흑자 + HP 충분
                hp = meta.get('hp', 50.0)
                if annual_ni <= 0 or hp < DIVIDEND_MIN_HP:
                    meta['div_per_share'] = 0
                    meta['div_yield'] = 0.0
                    continue

                # 섹터별 배당성향
                sector = SECTOR_MAP.get(meta.get('ind', ''), 'Value')
                tier   = meta.get('tier', '소형주')
                payout = DIVIDEND_PAYOUT_RATIO.get(sector, {}).get(tier, 0.10)

                # HP 낮으면 배당성향 감소
                hp_ratio = hp / max(1.0, meta.get('hp_soft_cap', 60.0))
                if hp_ratio < 0.6:
                    payout *= 0.5

                total_div = annual_ni * payout
                shares    = stock['shares']
                dps       = int(total_div / max(1, shares))  # 주당배당금

                # 호가단위 기준 반올림
                if dps < 10:
                    dps = 0
                else:
                    dps = max(10, round(dps / 10) * 10)

                meta['div_per_share'] = dps
                meta['div_yield']     = round(dps / max(1, stock['price']) * 100, 2)

                if dps > 0:
                    decided.append((name, dps, meta['div_yield'], sector))

            if not silent and decided:
                top = sorted(decided, key=lambda x: x[1], reverse=True)[:3]
                names_str = ', '.join(f"{n}({d:,}원/{y:.1f}%)" for n, d, y, _ in top)
                self.s.daily_news.append(
                    f"💰 [{cur.year}년 배당 공시] {len(decided)}개 기업 배당 결정. "
                    f"주요: {names_str} (4월 지급 예정)"
                )
            # 3월 처리 완료 플래그
            setattr(self.s, _div_done_key, True)

        # ── 4월: 배당락 + 지급 ──────────────────────────────────
        elif month == 4:
            paid_total = 0
            paid_count = 0
            for stock in self.s.stocks:
                meta = stock['meta']
                dps  = meta.get('div_per_share', 0)
                if dps <= 0:
                    continue

                # 배당락: 주가에서 배당금만큼 하락
                old_price     = stock['price']
                new_price     = max(10, old_price - dps)
                stock['price'] = new_price
                stock['market_cap'] = new_price * stock['shares']

                # 배당금 누적 (UI에서 my_portfolio 참조해서 지급)
                meta['div_ready'] = dps  # UI가 읽어서 지급 처리
                paid_count += 1

            if not silent and paid_count > 0:
                self.s.daily_news.append(
                    f"💸 [배당락일] {paid_count}개 종목 배당락 처리 완료. "
                    f"보유 종목 배당금은 계좌로 자동 입금됩니다."
                )
            # 4월 처리 완료 플래그 (이중 지급 방지)
            setattr(self.s, _div_done_key, True)