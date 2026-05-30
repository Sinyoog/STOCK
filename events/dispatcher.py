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
            # ★ 11순위: 대공황 자연 발생 트리거 체크 (신규)
            self._check_depression(silent)
            # ★ 12순위: 페이즈 전환 체크 (신규)
            self._check_phase_transition(silent)
            self.mkt.apply_price_change()
            self.mkt.update_company_technology()
            # 경고 진입/해제 7일 선반영 시스템
            self.mkt.check_warning_system()
            # 6월/12월 티어 심사 D-7 예고 + D-Day 실행
            self._check_tier_exam(silent)

            # DB 저장 (모든 INSERT를 모은 뒤 flush_daily_db로 commit 1회)
            date_str   = self.s.current_date.strftime('%Y-%m-%d')
            # GRI 일별 저장 (commit 없음)
            self.db.insert_gri_record(
                date_str,
                self.s.gri,
                getattr(self.s, 'bubble_index', 0.0)
            )
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

            # 주가·거래량 일괄 INSERT (commit 없음)
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

        # ★ 매일 고점/저점/지속일수 업데이트
        if hasattr(self.db, 'update_scenario_log_daily'):
            self.db.update_scenario_log_daily(self.s.gri)

        # ★ 시나리오 변경 시 DB 로그 기록
        cur_scenario = self.s.current_scenario
        if cur_scenario != _prev_scenario and hasattr(self.db, 'log_scenario_change'):
            date_str = self.s.current_date.strftime('%Y-%m-%d')
            war      = getattr(self.s, 'war_event', {})
            note     = f"{_prev_scenario} → {cur_scenario}" if _prev_scenario else ""

            # market_stats 수집 (PER/섹터/산업)
            market_stats = self._collect_market_stats()
            # ★ 대공황 트리거 판단용 캐시 업데이트
            self.s._last_market_stats = market_stats

            self.db.log_scenario_change(
                date_str     = date_str,
                scenario     = cur_scenario,
                gri          = self.s.gri,
                bubble       = getattr(self.s, 'bubble_index', 0.0),
                macro        = self.s.macro,
                war_event    = war,
                note         = note,
                market_stats = market_stats,
            )
            self._logged_scenario = cur_scenario

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
    # ★ 역할 변경: 결말 확정 → 대공황 임계값/강도 조정
    # ─────────────────────────────────────────────
    def _check_branching_point_news(self, silent: bool):
        """
        테크3 도달 이후 분기점이 결정됐을 때 프리미엄 예고 뉴스 발송.
        ★ 변경: 시나리오 강제 설정 대신 대공황 트리거 임계값 조정.
        """
        if self.s.max_tech_reached < 3:
            return
        if self.s.world_line == "Decided":
            return
        if not self.s.reserved_scenario:
            return
        if getattr(self.s, '_branch_news_sent', False):
            return

        self.s._branch_news_sent = True
        scenario = self.s.reserved_scenario
        cy = self.s.current_date.year

        # ★ 분기점에 따라 대공황 트리거 임계값 조정
        if "낙관" in scenario or "T4" in scenario:
            self.s._depression_threshold = 250   # 버블 임계 상향 → 대공황 어려워짐
            branch_desc = "낙관적 분기 — 경제 안정성 강화"
        elif "비관" in scenario or "대공황" in scenario:
            self.s._depression_threshold = 150   # 버블 임계 하향 → 대공황 쉬워짐
            branch_desc = "비관적 분기 — 경제 불안정성 증가"
        else:
            self.s._depression_threshold = 200   # 기본값
            branch_desc = "중립적 분기"

        # 프리미엄 전용 예고
        if self.s.has_paid_news_access and not silent:
            self.s.daily_news.append(
                f"💎 [분기점 결정] {cy}년, {branch_desc}. "
                f"경제 임계값이 조정되었습니다. (프리미엄 전용 정보)"
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
        D-7: 프리미엄 예고
        D-0: 전체 종목 비율(대형15/중형45/소형40) 기준 재배정 확정
        """
        cur_date  = self.s.current_date
        cur_month = cur_date.month
        cur_day   = cur_date.day

        is_exam_day    = cur_month in [3, 6, 9, 12] and cur_day == 1
        is_preview_day = cur_month in [2, 5, 8, 11] and cur_day == 24

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

        # 티어별 최하위 시총 계산
        large_stocks = [s['market_cap'] for s in stocks if s['meta']['tier'] == '대형주']
        mid_stocks   = [s['market_cap'] for s in stocks if s['meta']['tier'] == '중형주']

        # 최하위 시총 (없으면 0으로 강등 없음)
        large_min = min(large_stocks) if large_stocks else 0
        mid_min   = min(mid_stocks)   if mid_stocks   else 0

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
    # ★ 외부 충격 이벤트 (8순위)
    # 매년 1월 1일 확률 체크 — 무조건 발생하지 않음, 시드마다 다름
    # 내부 버블과 무관하게 외부에서 오는 경제 충격
    # 현실 사례: 닷컴버블(2000), 금융위기(2008), 코로나(2020)
    # ─────────────────────────────────────────────
    def _check_external_shock(self, silent: bool):
        import random as _rnd
        cur = self.s.current_date

        # 매년 1월 1~7일 중 한 번만 체크
        if cur.month != 1 or cur.day > 7:
            return

        # 이미 대공황 진행 중이면 외부 충격 없음
        if "대공황" in self.s.current_scenario and "극복" not in self.s.current_scenario:
            return

        # 이미 올해 외부 충격이 발생했으면 스킵
        last_shock_year = getattr(self.s, '_last_external_shock_year', 0)
        if last_shock_year == cur.year:
            return

        # ★ 시장 형성 조건: 게임 시작 후 최소 3년 경과 + 종목 200개 이상
        # (기존 400개 달성 조건은 너무 빠름 → 초반 팬데믹/전쟁 방지)
        years_since_start = self.s.current_date.year - self.s.start_date.year
        if years_since_start >= 3 and len(self.s.stocks) >= 200:
            self.s._market_fully_formed = True
        if not getattr(self.s, '_market_fully_formed', False):
            return

        # ── 충격 유형별 확률 ──────────────────────
        # 현실 기준:
        #   금융위기:   100년에 2~3회 → 연 2~3%
        #   글로벌 침체: 100년에 4~5회 → 연 4~5%
        #   일시적 패닉: 100년에 6~8회 → 연 6~8%
        # 직전 충격 후 최소 3년 쿨다운 적용
        years_since_shock = cur.year - last_shock_year if last_shock_year > 0 else 10

        # 3년 미만이면 발생 안 함
        if years_since_shock < 3:
            return

        # 경과 연수에 따라 확률 증가 (최대 15%)
        base_prob = min(0.15, years_since_shock * 0.02)  # 최대 15%

        roll = _rnd.random()
        if roll > base_prob:
            return

        # 충격 유형 결정
        # 금융위기 < 침체 < 패닉 순으로 빈도
        shock_type = _rnd.choices(
            ["글로벌 금융위기", "글로벌 침체 동조", "일시적 패닉"],
            weights=[0.15, 0.35, 0.50]
        )[0]

        self.s._last_external_shock_year = cur.year

        if shock_type == "글로벌 금융위기":
            # GRI -30~50%, 회복 2~5년
            intensity      = _rnd.uniform(0.30, 0.50)
            duration_days  = _rnd.randint(504, 1260)   # 2~5년
            scenario_name  = "📉 글로벌 금융위기 (외부 충격)"
            edu_text       = "(글로벌 경기 동조화 → 내부 버블 없어도 동반 하락 가능)"

        elif shock_type == "글로벌 침체 동조":
            # GRI -15~30%, 회복 1~2년
            intensity      = _rnd.uniform(0.15, 0.30)
            duration_days  = _rnd.randint(252, 504)    # 1~2년
            scenario_name  = "📉 글로벌 침체 동조 (외부 충격)"
            edu_text       = "(해외 경기침체 동조화 → 수출 감소 → 기업 실적 악화)"

        else:   # 일시적 패닉
            # GRI -10~20%, 회복 1~3개월
            intensity      = _rnd.uniform(0.10, 0.20)
            duration_days  = _rnd.randint(21, 63)      # 1~3개월
            scenario_name  = "📉 일시적 시장 패닉 (외부 충격)"
            edu_text       = "(단기 패닉 → 빠른 회복 가능 (코로나형))"

        # GRI 즉시 충격 적용
        shock_gri = self.s.gri * (1.0 - intensity)
        self.s.gri = max(100.0, shock_gri)

        # 시나리오 전환 + 타이머
        prev_scenario = self.s.current_scenario
        self.s.current_scenario = scenario_name
        self.s.scenario_timer   = duration_days

        # 회복 이벤트 예약
        from datetime import timedelta
        recovery_date = cur + timedelta(days=duration_days)
        self.s.pending_events["recovery"] = {
            "date":     recovery_date,
            "scenario": f"✨ {shock_type} 극복 (회복 국면)",
            "notified": False,
        }

        # 뉴스
        if not silent:
            self.s.daily_news.append(
                f"🌏 [외부 충격] {cur.year}년 {shock_type} 발생! "
                f"GRI -{intensity*100:.0f}% 충격 예상 {edu_text}"
            )
            self.s.daily_news.append(
                f"  └ 예상 지속 기간: 약 {duration_days//252}년 {(duration_days%252)//21}개월 "
                f"| 회복 예정: {recovery_date.strftime('%Y-%m-%d')}"
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
            return  # 전쟁 중엔 새 전쟁 발생 안 함

        # 종전 후 재건 중이면 timer 차감
        if war.get('phase') == '종전':
            recon_timer = war.get('recon_timer', 0)
            if recon_timer <= 0:
                self.s.war_event = {}  # 완전 종료
                if not silent:
                    self.s.daily_news.append("✅ [재건 완료] 전후 재건이 마무리되었습니다.")
            else:
                self.s.war_event['recon_timer'] = recon_timer - 1
            return

        # ★ 새 전쟁 발생 체크 — 매년 랜덤 월에만
        if cur.month != random.randint(1, 7) or cur.day > 7:
            return

        # 대공황 중엔 전쟁 없음
        if '대공황' in self.s.current_scenario and '극복' not in self.s.current_scenario:
            return

        # 시장 형성 전엔 없음
        if not getattr(self.s, '_market_fully_formed', False):
            return

        # ★ 악재 쿨다운: 마지막 위기 후 2년 이내엔 전쟁 없음
        last_crisis = getattr(self.s, '_last_crisis_year', 0)
        if last_crisis and cur.year - last_crisis < 2:
            return

        # ★ 발생 확률 하향 (기존: 대규모 3% + 지역 8% = 11% → 대규모 1.5% + 지역 3.5% = 5%)
        roll = random.random()
        if roll < 0.015:
            war_type = '대규모전쟁'
            duration = random.randint(504, 1260)  # 2~5년
        elif roll < 0.05:  # 0.015 + 0.035
            war_type = '지역분쟁'
            duration = random.randint(126, 504)   # 6개월~2년
        else:
            return

        # 지역 결정
        region = random.choice(['중동', '동유럽', '동남아', '아프리카'])

        # 원자재 충격 즉시 적용
        COMMODITY_SHOCK = {
            '중동':    {'oil_price':   1.8},
            '동유럽':  {'grain_price': 1.6, 'metal_price': 1.3},
            '동남아':  {'semi_index':  0.65, 'metal_price': 1.4},
            '아프리카': {'metal_price': 1.5},
        }
        shocks = COMMODITY_SHOCK.get(region, {})
        for key, mult in shocks.items():
            if key in self.s.macro:
                self.s.macro[key] *= mult

        # GRI 즉시 충격
        gri_impact = {'지역분쟁': 0.10, '대규모전쟁': 0.25}[war_type]
        self.s.gri = max(100.0, self.s.gri * (1.0 - gri_impact))

        # 전쟁 상태 저장
        self.s.war_event = {
            'type':    war_type,
            'region':  region,
            'timer':   duration,
            'phase':   '진행중',
            'notified': False,
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

        # ★ 악재 쿨다운 기록
        self.s._last_crisis_year = cur.year

        if not silent:
            commodity_str = ', '.join(f"{k} x{v:.1f}" for k, v in shocks.items())
            self.s.daily_news.append(
                f"⚔️ [{war_type} 발생] {cur.year}년 {region} {war_type} 발발! "
                f"원자재 충격: {commodity_str} | GRI -{gri_impact*100:.0f}%"
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

        # ★ 재건 섹터 대신 기존 산업에 임시 버프 주입
        from datetime import timedelta
        expire_dt = self.s.current_date + timedelta(days=recon_days)
        self.s.temp_sector_buff = {
            "산업재":   (0.015, expire_dt),
            "소재":     (0.010, expire_dt),
            "유틸리티": (0.008, expire_dt),
        }

        # ★ 동남아 전쟁 종전 시 SOX 점진 회복
        if region == '동남아':
            self.s.macro['semi_index'] = self.s.macro.get('semi_index', 1000.0) * 1.15

        # 재건 시나리오로 전환
        self.s.current_scenario = f'🏗️ {region} 전후 재건 (산업재/소재 강세)'
        self.s.scenario_timer   = recon_days

        if not silent:
            self.s.daily_news.append(
                f"🕊️ [종전] {region} {war_type} 종료! "
                f"재건 국면 돌입 — 재건/산업재/소재 섹터 수혜 예상"
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
        if cur.day > 7:
            return

        # ★ 팬데믹 진행 중이면 매일 timer 차감 (월 체크 전에 먼저 처리)
        pandemic = getattr(self.s, 'pandemic_event', {})
        if pandemic.get('phase') == '진행중':
            timer = pandemic.get('timer', 0)
            if timer <= 0:
                self.s.pandemic_event = {'phase': '종료'}
                from datetime import timedelta
                rec_date = cur + timedelta(days=random.randint(126, 252))
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

        # ★ 악재 쿨다운: 전쟁/위기 후 2년 이내엔 팬데믹 없음
        last_crisis = getattr(self.s, '_last_crisis_year', 0)
        if last_crisis and cur.year - last_crisis < 2:
            return

        if random.random() > 0.04:
            return

        # ★ 팬데믹 발생 시 쿨다운 기록
        self.s._last_crisis_year = cur.year

        # 강도 결정
        intensity = random.uniform(0.20, 0.35)
        duration  = random.randint(365, 730)  # 1~2년 (현실 반영)

        # GRI 즉시 충격
        self.s.gri = max(100.0, self.s.gri * (1.0 - intensity))

        # ★ 팬데믹 금리/CPI 강제 인하 (코스피 현실 반영)
        # 실제 2020년: 한국 기준금리 0.5%까지 인하, 초기 물가 하락
        # 수요 위축 → CPI 하락 → 중앙은행 긴급 금리 인하
        _cur_rate = self.s.macro.get('interest_rate', 4.0)
        _cur_cpi  = self.s.macro.get('cpi', 2.0)
        self.s.macro['interest_rate'] = max(0.5, _cur_rate * 0.30)   # 금리 → 30% 수준으로 급락
        self.s.macro['cpi']           = max(0.5, min(1.5, _cur_cpi * 0.50))  # CPI → 0.5~1.5%로 하락

        # 팬데믹 상태 저장
        if not hasattr(self.s, 'pandemic_event'):
            self.s.pandemic_event = {}
        self.s.pandemic_event = {
            'phase': '진행중',
            'timer': duration,
            'intensity': intensity,
        }

        # 시나리오 전환
        self.s.current_scenario = '🦠 글로벌 팬데믹 (비대면 전환)'
        self.s.scenario_timer   = duration

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
                recent_yr = sorted(hist.keys())[-1]
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
    # ★ 호재 시나리오 (신규)
    # ─────────────────────────────────────────────
    def _check_boom_event(self, silent: bool):
        """
        호재 시나리오 체크 및 진행.
        현실 기준:
          수출 호황 (반도체/자동차 슈퍼사이클): 10년에 2~3회
          유동성 장세 (저금리 + 외국인 유입):   10년에 1~2회
          내수 소비 붐:                         10년에 2~3회
        """
        cur = self.s.current_date

        # 진행 중인 호재 tick
        boom = getattr(self.s, 'boom_event', {})
        if boom.get('phase') == '진행중':
            boom['timer'] = boom.get('timer', 0) - 1
            if boom['timer'] <= 0:
                self.s.boom_event       = {}
                self.s.current_scenario = "정상 성장"
                self.s._last_boom_year  = cur.year
                if not silent:
                    self.s.daily_news.append("📊 [호황 종료] 경기 호황이 마무리됩니다.")
            return

        # ── 발생 조건 ─────────────────────────────
        # 악재/재건 중엔 호재 없음
        scenario = self.s.current_scenario
        if any(x in scenario for x in ["전쟁", "분쟁", "대공황", "팬데믹", "외부충격"]):
            return

        # 시장 형성 전엔 없음
        if not getattr(self.s, '_market_fully_formed', False):
            return

        # 악재 쿨다운: 최근 위기 후 1년
        last_crisis = getattr(self.s, '_last_crisis_year', 0)
        if last_crisis and cur.year - last_crisis < 1:
            return

        # 호재 쿨다운: 최근 호재 후 2년
        last_boom = getattr(self.s, '_last_boom_year', 0)
        if last_boom and cur.year - last_boom < 2:
            return

        # ★ 매달 1일 체크 (기존: 1월만 → 매달로 확대)
        if cur.day != 1:
            return

        # ★ 월별 발생 확률 (연간으로 보면 현실적)
        # 수출호황: 월 1.2% → 연 약 14% (10년에 1.4회)
        # 유동성장세: 월 0.8% → 연 약 10% (10년에 1회, 금리 조건 포함)
        # 내수붐: 월 1.0% → 연 약 12% (10년에 1.2회)
        roll = random.random()

        if roll < 0.012:  # 수출 호황
            duration = random.randint(252, 756)
            self.s.boom_event = {
                'type':  '수출호황',
                'phase': '진행중',
                'timer': duration,
            }
            self.s.current_scenario = "📈 수출 호황 (반도체/수출 슈퍼사이클)"
            self.s.scenario_timer   = duration
            if not silent:
                self.s.daily_news.append(
                    f"📈 [수출 호황] {cur.year}년, 반도체·수출 슈퍼사이클 진입! "
                    f"IT/산업재/소재 섹터 강세 예상"
                )

        elif roll < 0.020:  # 유동성 장세 (금리 3.5% 이하 조건)
            if self.s.macro.get('interest_rate', 4.0) <= 3.5:
                duration = random.randint(252, 504)
                self.s.boom_event = {
                    'type':  '유동성장세',
                    'phase': '진행중',
                    'timer': duration,
                }
                self.s.current_scenario = "💰 유동성 장세 (저금리 + 외국인 유입)"
                self.s.scenario_timer   = duration
                if not silent:
                    self.s.daily_news.append(
                        f"💰 [유동성 장세] {cur.year}년, 저금리 환경에 외국인 자금 유입! "
                        f"전 섹터 상승 모멘텀"
                    )

        elif roll < 0.030:  # 내수 소비 붐
            duration = random.randint(126, 378)
            self.s.boom_event = {
                'type':  '내수붐',
                'phase': '진행중',
                'timer': duration,
            }
            self.s.current_scenario = "🛒 내수 소비 붐"
            self.s.scenario_timer   = duration
            if not silent:
                self.s.daily_news.append(
                    f"🛒 [내수 붐] {cur.year}년, 내수 소비 호황! "
                    f"필수소비재/자유소비재/커뮤니케이션 강세"
                )


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

        # 트리거 조건 점수 계산
        bi        = self.s.bubble_index
        threshold = getattr(self.s, '_depression_threshold', 200)
        rate      = self.s.macro.get('interest_rate', 4.0)
        cpi       = self.s.macro.get('cpi', 2.0)
        ff        = getattr(self.s, 'foreign_flow_index', 0.0)

        # 시장 통계에서 PER 참조
        stats   = getattr(self.s, '_last_market_stats', {})
        per_l   = stats.get('per_large', 0)

        trigger_score = 0
        if bi >= threshold:           trigger_score += 3   # 핵심 조건
        if bi >= threshold * 0.75:    trigger_score += 1
        if per_l >= 60:               trigger_score += 1
        if rate >= 6.0:               trigger_score += 1
        if cpi >= 5.0:                trigger_score += 1
        if ff <= -60:                 trigger_score += 1
        # GRI 고점 대비 -30% 이상
        peak = getattr(self.s, 'peak_gri', self.s.gri)
        if self.s.gri < peak * 0.70:  trigger_score += 2

        # 누적 카운터 관리
        if trigger_score >= 3:
            self.s.depression_trigger_count = getattr(self.s, 'depression_trigger_count', 0) + 1
        else:
            self.s.depression_trigger_count = max(
                0, getattr(self.s, 'depression_trigger_count', 0) - 1
            )

        # 30일 이상 조건 지속 시 대공황 발동
        if getattr(self.s, 'depression_trigger_count', 0) >= 30:
            self.s.depression_active         = True
            self.s.depression_trigger_count  = 0
            self.s.current_scenario          = "💀 대공황 (시스템 붕괴)"
            # ★ 자연 발생 대공황: 2~4년 (현실적)
            # 분기점 대공황(10년)과 구분
            self.s.scenario_timer            = 252 * random.randint(2, 4)
            self.s._last_crisis_year         = self.s.current_date.year

            # ★ 대공황 진입 시 버블 지수 강제 붕괴
            # 버블이 터져서 대공황이 오는 것 — 버블은 폭락해야 함
            # 직전 버블의 20~30% 수준으로 강제 하락
            _prev_bubble = getattr(self.s, 'bubble_index', 100.0)
            self.s.bubble_index = max(10.0, _prev_bubble * random.uniform(0.20, 0.30))

            if not silent:
                self.s.daily_news.append(
                    "💀 [대공황 발생] 복합 경제 위기가 임계점을 돌파했습니다! "
                    "버블 붕괴, 금리, 외국인 이탈이 동시에 폭발했습니다."
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
        if bi <= 60:          recovery_score += 1   # 거품 해소 (50→60 완화)
        if rate <= 3.5:       recovery_score += 1   # 금리 인하(부양) (3.0→3.5 완화)
        if ff >= 10:          recovery_score += 1   # 외국인 복귀 (20→10 완화)
        if gri_drop <= 0.65:  recovery_score += 1   # -35% 바닥 확인 (-60%→-35% 완화)

        self.s.recovery_trigger_count = getattr(self.s, 'recovery_trigger_count', 0)
        if recovery_score >= 2:  # 3개→2개 완화
            self.s.recovery_trigger_count += 1
        else:
            self.s.recovery_trigger_count = max(0, self.s.recovery_trigger_count - 1)

        # 30일 이상 회복 조건 유지 시 대공황V 전환
        if self.s.recovery_trigger_count >= 30:
            self.s.depression_active       = False
            self.s.recovery_trigger_count  = 0
            self.s.current_scenario        = "✨ 대공황V (고난과 부활)"
            if not silent:
                self.s.daily_news.append(
                    "🌅 [회복 신호] 경제 지표가 바닥을 확인했습니다. "
                    "대공황 극복 국면에 진입합니다!"
                )
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

        # ── 페이즈 전환 확정 ──────────────────────
        self.s._last_processed_phase = cur_phase
        cy = self.s.current_date.year

        if not silent:
            lv = self.s.max_tech_reached
            phase_name = next(
                (p["name"] for p in TECH_PHASE.get(lv, []) if p["id"] == cur_phase),
                cur_phase
            )
            self.s.daily_news.append(
                f"🔄 [시대 전환] {cy}년, 경제 패러다임이 [{last_phase}] → [{cur_phase} {phase_name}]로 전환됩니다!"
            )

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