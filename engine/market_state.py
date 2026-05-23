"""
engine/market_state.py
게임의 모든 가변 상태(State)를 한 곳에서 관리하는 데이터 클래스.
로직은 없고 상태만 담습니다.
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

        # ── GDP (버핏 지수용) ─────────────────────
        # 2000년 한국 명목 GDP ≈ 600조원 기준
        self.gdp: float = 600_000_000_000_000.0       # 단위: 원
        self.gdp_growth_rate: float = 0.05            # 연간 성장률
        self.buffett_index: float = 0.0               # 시총/GDP (%)

        # ── 지수 ──────────────────────────────────
        self.gri: float = 1000.0
        self.peak_gri: float = 1000.0
        self.initial_market_total_cap: float = 0.0
        self.gri_base_at_rebase: float = 1000.0
        self.prev_gri: float = 1000.0
        self.bubble_index: float = 0.0
        self.avg_earnings_growth: float = 0.05

        # ── 기술 레벨 ─────────────────────────────
        self.max_tech_reached: int = 1

        # ── 이벤트 큐 ─────────────────────────────
        self.pending_events: dict = {
            "earnings":  {},
            "macro":     {},
            "delist":    {},
            "splits":    {},
            "warning":   {},
            "tier_exam": {},
            "tech_jump": None,
            "v_rebound": None,
            "recovery":  None,
            "crash":     None,   # ★ 신규: 버블 붕괴 예약
        }
        self._branch_news_sent: bool = False
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

        # ── 경기 사이클 ───────────────────────────
        self.leading_index: float = 0.0
        self.leading_index_history: list = []
        self.cycle_stage: str = "확장"
        self.cycle_day: int = 0
        self.sentiment: float = 50.0

        # ── GRI 보조 ──────────────────────────────
        self._prev_total_market_cap: float = 0.0
        self._gri_history_20: list = []
        self._prev_stock_caps: dict = {}
        self._tech_upgrade_year: int = 1999

        # ── 분기점 ────────────────────────────────
        self._branch_activation_date = None
        self._depression_warning_sent_lv: int = 0
        self._prev_macro_snapshot: dict = {}

        # ── ★ 신규: 외국인 수급 지수 ─────────────
        self.foreign_flow_index: float = 0.0   # -100 ~ +100 (양수=순매수)

        # ── ★ 신규: 산업별 경쟁도 ────────────────
        # {ind: 기업 수} 형태로 매일 갱신
        self.industry_competition: dict = {}

        # ── ★ 신규: 그룹별 산업 재진입 쿨다운 ───
        # {그룹ID: {산업명: 재진입_가능_날짜(datetime)}}
        # 계열사 상장폐지 후 5년간 같은 산업 재진입 불가
        self.group_industry_cooldown: dict = {}