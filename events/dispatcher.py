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
        lv = self.eco.get_tech_level()

        # 4. 실적 스케줄 처리 (장 열림 여부와 무관하게 수치 확정은 매일 실행)
        self.ear.process_earnings_schedule(silent)

        # 5. 장이 열린 날에만 경제 연산
        if self.s.is_market_open:
            self.mkt.handle_group_expansion(silent)
            self.eco.update_macro_logic()
            self.mkt.apply_price_change()
            self.mkt.update_company_technology()

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
            self.cm.reassign_tiers_by_cap(self.s.stocks, self.s.daily_news, silent=True)

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
