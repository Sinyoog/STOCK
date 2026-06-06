"""
engine/market_state.py
게임의 모든 가변 상태(State)를 한 곳에서 관리하는 데이터 클래스.
로직은 없고 상태만 담습니다.

[변경 사항]
- _tech_upgrade_year → _tech_upgrade_years (LV별 진입 연도 딕셔너리)
- 호재 시나리오 상태 필드 추가 (boom_event)
- 대공황 자연 발생 트리거 카운터 추가
- 재건 버프용 임시 섹터 버프 필드 추가 (temp_sector_buff)
- 이벤트 쿨다운 추적 필드 추가
- LV4 전환 누적 확률 카운터 추가
- 분기점 역할 변경 (강도/임계값 조정용)
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
        self.scenario_timer: int = 0
        self.is_recovering: bool = False

        # ── 거시경제 ──────────────────────────────
        self.macro: dict = {
            "oil_price":     30.0,
            "exchange_rate": 1100.0,
            "interest_rate": 4.0,
            "cpi":           2.0,
            "fear_index":    10.0,
            "grain_price":   250.0,   # 곡물 밀 $/부셸
            "metal_price":   1800.0,  # 구리 $/톤
            "semi_index":    1000.0,  # 반도체 SOX 지수
        }

        # ── 전쟁/분쟁 상태 ────────────────────────
        # {"type": "지역분쟁"/"대규모전쟁", "region": str,
        #  "timer": int, "phase": "진행중"/"종전", "notified": bool}
        self.war_event: dict = {}

        self.base_item_price: float = 1000.0
        self.cumulative_inflation: float = 1.0

        # ── GDP (버핏 지수용) ─────────────────────
        self.gdp: float = 600_000_000_000_000.0  # 600조 (현실 한국 2000년 GDP 기준)
        self.gdp_growth_rate: float = 0.05
        self.buffett_index: float = 0.0

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

        # ── 기술 레벨별 진입 연도 ─────────────────
        # 기존 _tech_upgrade_year(단일값) 대체
        # economy.get_current_phase()에서 offset 계산에 사용
        self._tech_upgrade_years: dict = {
            1: 2000,   # 게임 시작 연도 고정
            2: None,   # LV2 진입 시 기록
            3: None,   # LV3 진입 시 기록
            4: None,   # LV4 진입 시 기록
        }

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
            "crash":     None,
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

        # ── 분기점 ────────────────────────────────
        # 역할 변경: 결말 확정 → 대공황 임계값/강도 조정
        self._branch_activation_date = None
        self._depression_warning_sent_lv: int = 0
        self._prev_macro_snapshot: dict = {}
        # 대공황 버블 임계값: 250 고정 (분기점 시스템 제거로 단일값)
        # 현실 버핏지수 기준: 200%+ = 역대급 과열 → 버블지수 250 수준
        self._depression_threshold: int = 250

        # ── 외국인 수급 지수 ──────────────────────
        self.foreign_flow_index: float = 0.0   # -100 ~ +100

        # ── 산업별 경쟁도 ─────────────────────────
        self.industry_competition: dict = {}

        # ── 그룹별 산업 재진입 쿨다운 ────────────
        self.group_industry_cooldown: dict = {}

        # ── 이름 생성기 상태 ──────────────────────
        self._name_generation: int = 1
        self._name_pool: list      = []

        # ── 수급 트렌드 연속성 ────────────────────
        self.investor_trends: dict = {}

        # ── 신용잔고 ──────────────────────────────
        self.margin_balance: dict = {}

        # ── 일별 투자자별 거래량 ──────────────────
        self.daily_volume: dict = {}

        # ── 기관 컨센서스 ─────────────────────────
        self.earnings_consensus: dict = {}

        # ── 대주주 행동 공시 큐 ───────────────────
        self.major_holder_action: dict = {}

        # ── 공매도 잔고 (short_interest) ──────────
        # 종목별 유통주식 대비 공매도 비율 (0.0 ~ 0.15)
        # market.py _calc_short_selling_adj에서 meta['short_interest']로 관리
        # (종목별 meta에 직접 저장, 여기서는 집계/조회용 캐시 불필요)

        # ══════════════════════════════════════════
        # ★ 신규 필드들
        # ══════════════════════════════════════════

        # ── 호재 시나리오 상태 ────────────────────
        # {"type": "수출호황"/"유동성장세"/"내수붐",
        #  "timer": int, "phase": "진행중"}
        self.boom_event: dict = {}
        self.export_sanctions: dict = {}   # ★ 수출 규제 이벤트 상태

        # ── 대공황 자연 발생 트리거 ───────────────
        # 복합 조건 누적 카운터 (30일 이상 충족 시 발동)
        self.depression_trigger_count: int = 0
        # 현재 대공황 진행 여부
        self.depression_active: bool = False
        # 대공황 회복 조건 누적 카운터
        self.recovery_trigger_count: int = 0

        # ── 임시 섹터 버프 ────────────────────────
        # 재건 이벤트 등에서 특정 산업에 임시 보너스 부여
        # {산업명: (계수: float, 만료일: datetime)}
        self.temp_sector_buff: dict = {}

        # ── 이벤트 쿨다운 추적 ───────────────────
        # 악재/호재 연속 발생 방지
        self._last_crisis_year: int = 0    # 마지막 악재 발생 연도
        self._last_boom_year: int = 0      # 마지막 호재 발생 연도
        self._market_fully_formed: bool = False  # 시장 형성 완료 플래그

        # ── LV4 전환 누적 확률 ────────────────────
        # LV3 진입 20년 후부터 매일 누적
        # random < lv4_chance_accum 이면 LV4 전환
        self.lv4_chance_accum: float = 0.0

        # ── 팬데믹 이벤트 상태 ────────────────────
        # {"phase": "진행중"/"종료", "timer": int, "intensity": float}
        self.pandemic_event: dict = {}

        # ── 시장 통계 캐시 (대공황 트리거 판단용) ─
        # dispatcher._collect_market_stats() 결과를 매일 갱신
        self._last_market_stats: dict = {}

        # ── ★ 신규: 페이즈 전환 추적 ─────────────────────────────
        # 페이즈 전환 감지용 — 마지막으로 처리된 페이즈 ID
        self._last_processed_phase: str = "1A"

        # ── ★ 테마 모멘텀 시스템 ──────────────────────────────────
        # 활성 테마 리스트
        # 각 테마: {
        #   'ind': str,          # 대상 산업
        #   'type': 'bull'/'bear', # 방향
        #   'peak': float,       # 최고 강도 (0~1)
        #   'duration': int,     # 전체 기간 (영업일)
        #   'elapsed': int,      # 경과 영업일
        #   'source': str,       # 발생 원인 ('phase'/'scenario'/'cycle')
        # }
        self.active_themes: list = []

        # 산업별 마지막 bull 테마 종료 연도 (쿨다운용)
        # {ind: year}
        self._theme_cooldown: dict = {}

        # ── A→B 페이즈 전환 보너스 ───────────────
        # 호재 시나리오 → 음수(가속), 악재 → 양수(지연)
        # get_current_phase()에서 elapsed에 더해 전환 시점 조정
        # 예: -2 → 기본 7년에서 5년으로 단축
        self._phase_offset_bonus: int = 0

        # ══════════════════════════════════════════
        # ★ 생산성 파급 시차 시스템
        # ══════════════════════════════════════════
        # 기술 도약 후 실제 실적 반영까지의 시차를 관리
        # {산업: {"boost": float, "expire_year": int, "lag_remaining_years": int}}
        # lag_remaining_years가 0이 되면 efficiency boost 실제 반영
        self._productivity_lag_queue: dict = {}

        # ══════════════════════════════════════════
        # ★ 시장 광기 지수 (Market Mania Index)
        # ══════════════════════════════════════════
        # 실제 EPS 대비 주가가 얼마나 앞서가는가 (PER 괴리 기반)
        # 1.0 = 정상 / 1.5 = 과열 / 2.0 = 버블 / 3.0+ = 붕괴 직전
        # 매 분기 실적 발표 시 업데이트
        self.market_mania_index: float = 1.0

        # 광기 지수 히스토리 (20개 유지)
        self._mania_history: list = []

        # ══════════════════════════════════════════
        # ★ 산업 패권 시스템
        # ══════════════════════════════════════════
        # 현재 패권 산업 (None = 분산형 시장)
        self.dominant_industry: str = None
        # 패권 강도: "약"/"중"/"강"/None
        self.dominance_level: str = None
        # 패권 지속 기간 (년)
        self.dominance_years: int = 0
        # 패권 후보 연속 충족 년수 {산업: 년수}
        self._dominance_counter: dict = {}
        # 버블 사이클 경험 횟수 (LV4 조건용)
        self._bubble_cycle_count: int = 0
        # 마지막 버블 붕괴 연도
        self._last_bubble_burst_year: int = 0

        # ══════════════════════════════════════════
        # ★ LV4 복합 조건 추적
        # ══════════════════════════════════════════
        # LV4_UNLOCK_CONDITIONS 조건 충족 연속 일수
        self._lv4_condition_days: int = 0
        # ★ [수정] 절대 경과일 카운터 (버그7: cycle_day 리셋과 독립)
        self._total_days_elapsed: int = 0
        # ★ [수정] 절대 경과일 기준 페이즈 전환 시점 (버그7)
        self._phase_transition_abs: int = -9999
        # 각 조건별 충족 여부 캐시
        self._lv4_condition_status: dict = {}

        # ══════════════════════════════════════════
        # ★ 시나리오 드리프트 패널티 (명시화)
        # ══════════════════════════════════════════
        self._scenario_drift_penalty: float = 0.0

        # ══════════════════════════════════════════
        # ★ 신규 시나리오 카운터 (초기화 누락 방지)
        # ══════════════════════════════════════════
        # 시나리오 발동 조건 누적 카운터
        self._inflation_shock_counter: int = 0    # 인플레이션 충격
        self._re_burst_counter: int = 0           # 부동산 버블 붕괴
        self._secular_stagnation_counter: int = 0 # 구조적 저성장
        self._easing_counter: int = 0             # 금융 완화 사이클
        self._fx_crisis_counter: int = 0          # 환율 위기
        self._tightening_counter: int = 0         # 긴축 쇼크
        self._stagflation_counter: int = 0        # 스태그플레이션

        # 이벤트 쿨다운 연도 (누락분 명시화)
        self._last_depression_year: int = 0       # 대공황 쿨다운
        self._last_pandemic_year: int = 0         # 팬데믹 쿨다운
        self._last_external_shock_year: int = 0   # 외부충격 쿨다운

        # 시나리오 로그용 note 버퍼
        self._last_scenario_trigger_note: str = ""

        # 페이즈 전환 부스트 기준일
        self._phase_transition_day: int = 0

        # GRI 히스토리 보조
        self._gri_history_60: list = []           # 60일 장기 정체 판단용