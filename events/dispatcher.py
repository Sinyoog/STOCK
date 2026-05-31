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
            # ★ 10.7순위: 환율 위기 체크
            self._check_exchange_crisis(silent)
            # ★ 10.9순위: 박스권 횡보 감지
            self._check_boxrange(silent)
            # ★ 11순위: 대공황 자연 발생 트리거 체크 (신규)
            self._check_depression(silent)
            # ★ 12순위: 페이즈 전환 체크 (신규)
            self._check_phase_transition(silent)
            self.mkt.apply_price_change()
            self.mkt.update_company_technology()
            # 경고 진입/해제 7일 선반영 시스템
            self.mkt.check_warning_system()
            # ★ 산업 패권 시스템 (월 1일 체크)
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

        # ★ 티어 심사: 장 열림 여부와 무관하게 매일 체크
        # (3/6/9/12월 1일이 주말이면 다음 첫 거래일에 실행)
        self._check_tier_exam(silent)

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
        is_exam_day    = is_exam_month and cur_day <= 3 and weekday <= 4 and \
                         not getattr(self.s, f'_tier_exam_done_{cur_month}_{cur_date.year}', False)
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
            # 이번 분기 심사 완료 플래그 (월이 바뀌면 자동 소멸)
            setattr(self.s, f'_tier_exam_done_{cur_month}_{cur_date.year}', True)

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

        base_prob   = min(0.15, years_since_shock * 0.02)
        bubble      = getattr(self.s, 'bubble_index', 0.0)
        # 버블 100 이상부터 확률 가중 (최대 1.5배)
        bubble_mult = 1.0 + min(0.5, max(0.0, (bubble - 100) / 200))
        final_prob  = min(0.20, base_prob * bubble_mult)

        if _rnd.random() > final_prob:
            return

        shock_type = _rnd.choices(
            ["글로벌 금융위기", "글로벌 침체 동조", "일시적 패닉"],
            weights=[0.15, 0.35, 0.50]
        )[0]

        self.s._last_external_shock_year = cur.year
        self.s._last_crisis_year         = cur.year

        if shock_type == "글로벌 금융위기":
            intensity     = _rnd.uniform(0.30, 0.50)
            duration_days = _rnd.randint(504, 1260)
            scenario_name = "📉 글로벌 금융위기 (외부 충격)"
            edu_text      = "(글로벌 경기 동조화 → 동반 하락)"
        elif shock_type == "글로벌 침체 동조":
            intensity     = _rnd.uniform(0.15, 0.30)
            duration_days = _rnd.randint(252, 504)
            scenario_name = "📉 글로벌 침체 동조 (외부 충격)"
            edu_text      = "(해외 경기침체 → 수출 감소 → 실적 악화)"
        else:
            intensity     = _rnd.uniform(0.08, 0.18)
            duration_days = _rnd.randint(21, 63)
            scenario_name = "📉 일시적 시장 패닉 (외부 충격)"
            edu_text      = "(단기 패닉 → 빠른 회복 가능)"

        # ── GRI 충격 분산 적용 ────────────────────────
        # 즉시 충격: 40%, 최대 -15% 상한
        # 나머지 60%: _scenario_drift_penalty로 기간 분산
        immediate    = min(0.15, intensity * 0.40)
        deferred     = intensity * 0.60
        daily_penalty = -(deferred / max(1, duration_days))

        self.s.gri = max(100.0, self.s.gri * (1.0 - immediate))
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
        _REGION_COOLDOWN = {
            '중동':     3,   # 3년 (만성 분쟁 지역)
            '아프리카': 4,   # 4년 (자원 분쟁 반복)
            '동유럽':   8,   # 8년 (강대국 충돌 희귀)
            '동남아':   10,  # 10년 (공급망 충격 최희귀)
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

        roll = random.random()
        if roll < 0.015:
            war_type = '대규모전쟁'
            duration = random.randint(504, 1260)
        elif roll < 0.050:
            war_type = '지역분쟁'
            duration = random.randint(126, 504)
        else:
            return

        region = random.choices(
            list(region_weight.keys()),
            weights=list(region_weight.values())
        )[0]

        # ── 원자재 충격 즉시 적용 ─────────────────
        # 중동: 유가 단계적 상승 (즉시 1.4배 + 전쟁 기간 중 추가 압력)
        COMMODITY_SHOCK = {
            '중동':    {'oil_price':   1.4},   # 즉시 충격 (1.8→1.4, 나머지는 점진)
            '동유럽':  {'grain_price': 1.6, 'metal_price': 1.3},
            '동남아':  {'semi_index':  0.65, 'metal_price': 1.4},
            '아프리카': {'metal_price': 1.5},
        }
        shocks = COMMODITY_SHOCK.get(region, {})
        for key, mult in shocks.items():
            if key in self.s.macro:
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
        self.s.gri = max(100.0, self.s.gri * (1.0 - immediate))
        self.s._scenario_drift_penalty = -(deferred / max(1, duration))

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
        if last_boom and cur.year - last_boom < 2:
            return
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

        # anchor 계산
        lv  = self.s.max_tech_reached
        ye  = max(0, cur.year - 2000)
        lv_base = {
            1: 1000 * (1.075 ** ye),
            2: 1000 * (1.075**15) * (1.090**max(0, ye-15)),
            3: 1000 * (1.075**15) * (1.090**20) * (1.105**max(0, ye-35)),
            4: 1000 * (1.075**15) * (1.090**20) * (1.105**25) * (1.080**max(0, ye-60)),
        }.get(lv, 1000.0)
        anchor = self.s.gri / max(1.0, lv_base)

        roll = random.random()

        # ① 수출 호황 — SOX 강세 + 고환율 + 확장기
        sox_strong  = sox >= 1200.0
        high_fx     = exchange >= 1200.0
        expanding   = (cycle == '확장')
        export_prob = 0.030 if (sox_strong and high_fx and expanding) else 0.005

        if roll < export_prob:
            duration = random.randint(252, 756)
            self.s.boom_event = {'type': '수출호황', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "📈 수출 호황 (반도체/수출 슈퍼사이클)"
            self._trigger_scenario_themes('수출호황')
            self.s.scenario_timer = duration
            if not silent:
                cond = []
                if sox_strong: cond.append(f"SOX {sox:.0f}")
                if high_fx:    cond.append(f"환율 {exchange:.0f}원")
                self.s.daily_news.append(
                    f"📈 [수출 호황] {cur.year}년 반도체·수출 슈퍼사이클 진입! "
                    f"({' / '.join(cond)}) IT/산업재/소재 강세 예상"
                )
            return

        # ② 유동성 장세 — 금리 인하 사이클 + 외국인 이탈 없음
        rate_cutting = (rate < prev_rate) and (rate <= 3.5)
        ff_ok        = ff >= -10.0
        if rate_cutting and ff_ok and roll < (export_prob + 0.040):
            duration = random.randint(252, 504)
            self.s.boom_event = {'type': '유동성장세', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "💰 유동성 장세 (저금리 + 외국인 유입)"
            self._trigger_scenario_themes('유동성장세')
            self.s.scenario_timer = duration
            if not silent:
                self.s.daily_news.append(
                    f"💰 [유동성 장세] {cur.year}년 금리 인하 사이클({rate:.1f}%) + "
                    f"외국인 자금 유입 — 전 섹터 상승 모멘텀"
                )
            return

        # ③ 내수 소비 붐 — 저금리 + 확장기 + CPI 안정
        domestic_prob = 0.025 if (rate <= 3.0 and cpi <= 3.0 and expanding) else 0.004
        if roll < (export_prob + 0.040 + domestic_prob):
            duration = random.randint(126, 378)
            self.s.boom_event = {'type': '내수붐', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🛒 내수 소비 붐"
            self._trigger_scenario_themes('내수붐')
            self.s.scenario_timer = duration
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
            duration = random.randint(252, 504)
            self.s.boom_event = {'type': '혁신기술붐', 'phase': '진행중', 'timer': duration}
            phase_nm = {'2A': '모바일', '2B': '클라우드/플랫폼',
                        '3A': 'AI 상용화', '3B': '양자/바이오'}.get(cur_phase, cur_phase)
            self.s.current_scenario = f"🤖 혁신 기술 붐 ({phase_nm})"
            self._trigger_scenario_themes('혁신기술붐')
            self.s.scenario_timer = duration
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
            duration = random.randint(126, 378)
            self.s.boom_event = {'type': '외국인유입', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🌏 외국인 대규모 유입 (원화 강세 + 저평가)"
            self._trigger_scenario_themes('외국인유입')
            self.s.scenario_timer = duration
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
            duration = random.randint(126, 252)
            self.s.boom_event = {'type': '정부부양', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "🏛️ 정부 경기 부양 (재정 확대)"
            self._trigger_scenario_themes('정부부양')
            self.s.scenario_timer = duration
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
        # AI/모바일 전환 등 수요 폭발로 반도체 공급 부족
        sox_boom = sox >= 2000.0 and cur_phase in ('2A', '2B', '3A', '3B')
        if sox_boom and anchor < 1.5 and \
           roll < (export_prob + 0.040 + domestic_prob + 0.020 + 0.025 + 0.040 + 0.015):
            duration = random.randint(252, 630)
            self.s.boom_event = {'type': '반도체슈퍼사이클', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "💾 반도체 슈퍼사이클 (SOX 폭등)"
            self._trigger_scenario_themes('반도체슈퍼사이클')
            self.s.scenario_timer = duration
            if not silent:
                self.s.daily_news.append(
                    f"💾 [반도체 슈퍼사이클] {cur.year}년 SOX {sox:.0f} — "
                    f"AI/모바일 수요 폭발로 반도체 공급 부족! "
                    f"IT/소재 집중 수혜, 단 버블 경고"
                )
            return

        # ⑧ 원자재 슈퍼사이클 — 금속/곡물 동반 강세 + 에너지 상승
        # 신흥국 인프라 투자 붐 or 공급 부족으로 원자재 전반 강세
        oil_now  = macro.get('oil_price', 30.0)
        commodity_boom = (metal >= 3000.0 and oil_now >= 80.0 and grain >= 400.0)
        if commodity_boom and cycle == '확장' and \
           roll < (export_prob + 0.040 + domestic_prob + 0.020 + 0.025 + 0.040 + 0.015 + 0.015):
            duration = random.randint(378, 756)
            self.s.boom_event = {'type': '원자재슈퍼사이클', 'phase': '진행중', 'timer': duration}
            self.s.current_scenario = "⛏️ 원자재 슈퍼사이클 (에너지/금속 강세)"
            self._trigger_scenario_themes('원자재슈퍼사이클')
            self.s.scenario_timer = duration
            if not silent:
                self.s.daily_news.append(
                    f"⛏️ [원자재 슈퍼사이클] {cur.year}년 에너지·금속·곡물 동반 강세! "
                    f"유가 ${oil_now:.0f} / 구리 ${metal:,.0f} — "
                    f"에너지/소재/산업재 수혜, IT/소비재 비용 압박"
                )
            return

        cum_prob = export_prob + 0.040 + domestic_prob + 0.020 + 0.025 + 0.040 + 0.015 + 0.015

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

        # ★ 대공황 쿨다운: 직전 대공황 후 10년 이내 재발 없음
        last_depression = getattr(self.s, '_last_depression_year', 0)
        if last_depression and self.s.current_date.year - last_depression < 10:
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

        # 누적 카운터 관리 (임계 4)
        # 광기 지수가 높을수록 누적 임계값 낮아짐 (더 빨리 터짐)
        _trigger_threshold = 4
        if mmi >= MANIA_INDEX_THRESHOLDS["광기"]:
            _trigger_threshold = 3
        elif mmi >= MANIA_INDEX_THRESHOLDS["버블"]:
            _trigger_threshold = 3

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
            '전쟁': {
                'bull': [('에너지', 0.55, 252), ('소재', 0.40, 252)],
                'bear': [('IT', 0.25, 252)],
            },
            '재건': {
                'bull': [('산업재', 0.50, 378), ('소재', 0.45, 378)],
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
            self.s.gri = max(100.0, self.s.gri * (1.0 - immediate))
            self.s._scenario_drift_penalty = -(gri_drop * 0.70 / max(1, duration))
            self.s.current_scenario = "💻 기술 버블 붕괴 (성장주 디레이팅)"
            self.s.scenario_timer   = duration
            self.s._last_crisis_year = cur.year
            # ★ 버블 붕괴 시 광기 지수 리셋 (시장 정화)
            self.s.market_mania_index = 1.0
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
            self.s._scenario_drift_penalty = -(gri_drop / max(1, duration))
            self.s.current_scenario = "🔥 스태그플레이션 (물가↑ 성장↓)"
            self.s.scenario_timer   = duration
            self.s._stagflation_counter = 0
            self.s._last_crisis_year    = cur.year
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
            self.s._scenario_drift_penalty = -(gri_drop / max(1, duration))
            self.s.current_scenario = f"📊 긴축 쇼크 (금리 {rate:.1f}% / CPI {cpi:.1f}%)"
            self.s.scenario_timer   = duration
            self.s._tightening_counter = 0
            self._trigger_scenario_themes('긴축쇼크')
            if not silent:
                self.s.daily_news.append(
                    f"📊 [긴축 쇼크] {cur.year}년 금리 인상({rate:.1f}%) + "
                    f"물가({cpi:.1f}%) 압박 → 성장주 밸류에이션 하락 "
                    f"(Value/Defensive 상대적 방어)"
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
        self.s._scenario_drift_penalty = -(gri_drop / max(1, duration))
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
        lv_base = {
            1: 1000 * (1.075 ** ye),
            2: 1000 * (1.075**15) * (1.090**max(0, ye-15)),
            3: 1000 * (1.075**15) * (1.090**20) * (1.105**max(0, ye-35)),
            4: 1000 * (1.075**15) * (1.090**20) * (1.105**25) * (1.080**max(0, ye-60)),
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

        # 패권 산업이 있으면 광기 지수 가속
        if self.s.dominant_industry and self.s.dominance_level:
            from engine.constants import INDUSTRY_DOMINANCE
            mania_boost = INDUSTRY_DOMINANCE["mania_boost_per_level"].get(
                self.s.dominance_level, 0.0
            )
            self.s.market_mania_index = min(5.0, new_mmi + mania_boost)

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