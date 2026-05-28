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
            # ★ 8순위: 외부 충격 이벤트 체크 (매년 1월 1일)
            self._check_external_shock(silent)
            # ★ 9순위: 전쟁/분쟁 이벤트 체크
            self._check_war_event(silent)
            # ★ 10순위: 팬데믹 이벤트 체크
            self._check_pandemic_event(silent)
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

        # ★ 종목 수가 한 번이라도 400개를 달성한 후에만 외부 충격 발동
        # 시장이 충분히 형성되기 전 충격은 비현실적이고 게임 밸런스를 해침
        # 한 번 400개 달성 후 종목이 줄어도 플래그는 유지됨
        if len(self.s.stocks) >= self.s.MAX_STOCKS:
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

        # 발생 확률 체크
        roll = random.random()
        if roll < 0.03:
            war_type = '대규모전쟁'
            duration = random.randint(504, 1260)  # 2~5년
        elif roll < 0.11:  # 0.03 + 0.08
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

        if not silent:
            commodity_str = ', '.join(f"{k} x{v:.1f}" for k, v in shocks.items())
            self.s.daily_news.append(
                f"⚔️ [{war_type} 발생] {cur.year}년 {region} {war_type} 발발! "
                f"원자재 충격: {commodity_str} | GRI -{gri_impact*100:.0f}%"
            )
            self.s.daily_news.append(
                f"  └ 예상 지속: 약 {duration//252}년 {(duration%252)//21}개월 "
                f"| 재건 섹터 주목"
            )

    def _on_war_end(self, silent: bool):
        """종전 처리 — 재건 섹터 강세 시작"""
        war = getattr(self.s, 'war_event', {})
        region   = war.get('region', '')
        war_type = war.get('type', '')

        recon_days = random.randint(252, 756)  # 1~3년 재건

        # 재건 상태로 전환
        self.s.war_event['phase']       = '종전'
        self.s.war_event['recon_timer'] = recon_days

        # 재건 시나리오로 전환
        self.s.current_scenario = f'🏗️ {region} 전후 재건 (재건 섹터 강세)'
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
        # 팬데믹: 연 1.5% → 100년간 약 1.5회 (현실: 스페인독감/코로나 = 100년에 2번)
        if cur.month != 1 or cur.day > 7:
            return

        if random.random() > 0.015:
            return

        # 강도 결정
        intensity = random.uniform(0.20, 0.35)
        duration  = random.randint(365, 730)  # 1~2년 (현실 반영)

        # GRI 즉시 충격
        self.s.gri = max(100.0, self.s.gri * (1.0 - intensity))

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
            'ind_it':         wi('IT'),
            'ind_health':     wi('건강관리'),
            'ind_energy':     wi('에너지'),
            'ind_finance':    wi('금융'),
            'ind_industry':   wi('산업재'),
            'ind_material':   wi('소재'),
            'ind_realestate': wi('부동산'),
            'ind_rebuild':    wi('재건'),
            'ind_util':       wi('유틸리티'),
            'ind_consumer':   wi('자유소비재'),
            'ind_staple':     wi('필수소비재'),
            'ind_comm':       wi('커뮤니케이션'),
        }