"""
engine/market_state.py
게임의 모든 가변 상태(State)를 한 곳에서 관리하는 데이터 클래스.
로직은 없고 상태만 담습니다.
다른 모듈들은 이 객체를 참조하여 읽고 씁니다.
"""
from datetime import datetime


class MarketState:
    def __init__(self):
        # ── 시간 ──────────────────────────────────
        self.start_date: datetime = datetime(1999, 12, 31)
        self.current_date: datetime = datetime(1999, 12, 31)
        self.is_market_open: bool = False
        self.virtual_weekday: int = 6   # 0=월 … 6=일

        # ── 종목 및 그룹 ──────────────────────────
        self.stocks: list = []
        self.groups: dict = {}
        self.delisted_stocks: list = []
        self.pending_listings: list = []
        self.name_registry: dict = {}

        # ── 시나리오 ──────────────────────────────
        self.current_scenario: str = "정상 성장"
        self.world_line: str = "Normal"
        self.scenario_timer: int = 0
        self.is_recovering: bool = False
        self.reserved_scenario: str = ""

        # ── 거시경제 ──────────────────────────────
        self.macro: dict = {
            "oil_price":     30.0,
            "exchange_rate": 1100.0,
            "interest_rate": 4.0,
            "cpi":           2.0,
            "fear_index":    10.0,
        }
        self.base_item_price: float = 1000.0
        self.cumulative_inflation: float = 1.0

        # ── 지수 ──────────────────────────────────
        self.wsi: float = 1500.0
        self.gri: float = 1000.0
        self.initial_market_total_cap: float = 0.0

        # ── 기술 레벨 ─────────────────────────────
        self.max_tech_reached: int = 1

        # ── 이벤트 큐 ─────────────────────────────
        self.pending_events: dict = {
            "earnings":  {},
            "macro":     {},
            "delist":    {},
            "splits":    {},
            "tech_jump": None,
            "v_rebound": None,
        }
        self.pre_reflection_events: list = []

        # ── 실적 히스토리 ─────────────────────────
        self.earnings_history: dict = {}

        # ── 플레이어 관련 ─────────────────────────
        self.has_paid_news_access: bool = False
        self.next_billing_date = None

        # ── 일일 로그 ─────────────────────────────
        self.daily_news: list = []
        self.history_records: list = []
        self.daily_history: dict = {}

        # ── 내부 카운터 ───────────────────────────
        self.MAX_STOCKS: int = 400
        self.max_group_count: int = 8
        self.group_limit_by_tech: dict = {1: 3, 2: 5, 3: 8, 4: 10}
        self.used_all_time: set = set()
        self.silent_mode: bool = False
        self.daily_delist_count: int = 0
        self.daily_splits: dict = {}
