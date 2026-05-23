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
            # GRI 일별 저장
            self.db.insert_gri_record(
                date_str,
                self.s.gri,
                getattr(self.s, 'bubble_index', 0.0)
            )
            db_records = []
            for stock in self.s.stocks:
                # ★ 거시경제 섹터 민감도 적용 (유가/환율/금리 → 섹터별 주가)
                rate = stock.get('rate', 0.0)
                adjusted_rate = self.eco.apply_macro_sector_sensitivity(stock, rate)
                if adjusted_rate != rate:
                    # 섹터 조정분을 주가에 반영
                    adj_delta = (adjusted_rate - rate) / 100.0
                    stock['price'] = max(10, int(stock['price'] * (1 + adj_delta)))
                    stock['market_cap'] = stock['price'] * stock['shares']
                self.mkt.apply_stock_event(stock, silent)
                db_records.append((date_str, stock['meta']['c_name'],
                                   int(stock['price']), int(stock['market_cap'])))

            # 분할/병합 발생 시 과거 주가 DB 보정
            for name, ratio in self.s.daily_splits.items():
                self.db.update_adjusted_price(name, ratio)

            self.db.insert_stock_records(db_records)
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