"""
ui/main_window.py
StockHTS: 메인 HTS 프레임.
엔진 데이터를 직접 건드리지 않고 game_service만 호출합니다.
"""
import math
import threading
from datetime import datetime, timedelta
import pyqtgraph as pg

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QTableWidget, QTableWidgetItem, QHeaderView,
    QPushButton, QFrame, QTextEdit, QLineEdit, QSizePolicy,
    QMessageBox, QDialog
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont

from .styles import HTS_STYLE, COLOR, rate_color, rate_arrow
from .dialogs import (
    TradeDialog, EarningsDialog, MyInvestmentDialog,
    StockFilterDialog, DelistedDetailDialog,
    SystemMenuDialog, CustomConfirmDialog,
    InvestorVolumeDialog
)
from .group_view import GroupInfoDialog
from .news_view  import NewsWindow
from engine.constants import SECTOR_MAP, TECH_PHASE, MAIN_INDUSTRIES, EXPORT_SANCTION_TYPES


def _get_tick(price: int) -> int:
    """현실 한국 주식 호가단위"""
    if price >= 500_000:   return 1_000
    elif price >= 200_000: return 500
    elif price >= 50_000:  return 100
    elif price >= 20_000:  return 50
    elif price >= 5_000:   return 10
    elif price >= 2_000:   return 5
    else:                  return 1


def _fmt_mc(v: float) -> str:
    """시가총액 한국 단위 포맷: 억 → 조 → 경(10^16) → 해(10^20) → 자(10^24)"""
    v = int(v)
    _자  = 10 ** 24
    _해  = 10 ** 20
    _경  = 10 ** 16
    _조  = 10 ** 12
    _억  = 10 ** 8
    if   v >= _자:         return f"{v / _자:.2f}자"
    elif v >= _해:         return f"{v / _해:.2f}해"
    elif v >= _경:         return f"{v / _경:.2f}경"
    elif v >= 100 * _조:   return f"{v // _조:,}조"
    elif v >= 10  * _조:   return f"{v / _조:.0f}조"
    elif v >= _조:         return f"{v / _조:.1f}조"
    elif v >= _억:         return f"{v / _억:.0f}억"
    else:                  return f"{v:,}원"


def _build_commodity_html(macro: dict) -> str:
    """원자재 현실 가격 HTML — 값이 있을 때만 표시"""
    lines = []
    if macro.get('grain_price') is not None:
        lines.append(f"🌾 밀($/부셸): ${macro['grain_price']:.0f}")
    if macro.get('metal_price') is not None:
        lines.append(f"⚙️ 구리($/톤): ${macro['metal_price']:,.0f}")
    if macro.get('semi_index') is not None:
        lines.append(f"💾 SOX 지수: {macro['semi_index']:,.0f}")
    if not lines:
        return ""
    return "<br/>" + "<br/>".join(lines)


def _build_event_html(s) -> str:
    """진행 중인 이벤트 HTML — 전쟁/팬데믹/QE/QT"""
    events = []

    # 전쟁/분쟁
    war = getattr(s, 'war_event', {})
    if war.get('phase') == '진행중':
        w_type  = war.get('type', '')
        region  = war.get('region', '')
        timer   = war.get('timer', 0)
        elapsed = s.scenario_timer - timer if s.scenario_timer > timer else 0
        icon = '💣' if w_type == '대규모전쟁' else '🔫'
        events.append(f"{icon} {region} {w_type} 진행중 (잔여 {timer//252}년 {(timer%252)//21}개월)")
    elif war.get('phase') == '종전':
        events.append(f"🏗️ {war.get('region','')} 전후 재건 중")

    # 팬데믹
    pandemic = getattr(s, 'pandemic_event', {})
    if pandemic.get('phase') == '진행중':
        timer = pandemic.get('timer', 0)
        events.append(f"🦠 팬데믹 진행중 (잔여 {timer//21}주)")

    # QE/QT
    if getattr(s, 'qe_active', False):
        events.append("🏛️ QE 진행중")
    if getattr(s, 'qt_active', False):
        events.append("🏛️ QT 진행중")

    # 수출 규제
    sanctions = getattr(s, 'export_sanctions', {})
    for sid, state in sanctions.items():
        s_def = EXPORT_SANCTION_TYPES.get(sid, {})
        phase = state.get('phase', '')
        timer = state.get('timer', 0)
        weeks = timer // 21
        if phase == '단기충격':
            events.append(f"{s_def.get('emoji','🚫')} {s_def.get('name','')} 단기충격 (잔여 {weeks}주)")
        elif phase == '중장기수혜':
            events.append(f"🔄 {s_def.get('name','')} 공급망재편 수혜 (잔여 {weeks}주)")

    if not events:
        return ""

    items = "".join(f"<br/>{e}" for e in events)
    return f"<p><b style='color:#FF6B6B;'>⚠️ 진행중 이벤트</b>{items}</p><hr style='border:0.5px solid #333;'/>"



class DateAxisItem(pg.AxisItem):
    def __init__(self, dates, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dates = dates

    def tickStrings(self, values, scale, spacing):
        return [
            self.dates[int(v)].strftime('%y-%m-%d')
            if 0 <= int(v) < len(self.dates) else ""
            for v in values
        ]


class StockHTS(QMainWindow):
    def __init__(self, game_service, news_service):
        super().__init__()
        self.game_service = game_service
        self.news_service = news_service

        # 플레이어 상태
        self.my_cash      = 1_000_000
        self.my_portfolio = {}

        # UI 상태
        self.selected_stock_name = ""
        self.current_tf          = "1일"
        self.TF_LIMITS           = {"1일": 1, "1주": 7, "1달": 30, "3달": 90,
                                     "1년": 365, "5년": 1260, "전체": 999_999}
        self.active_dialogs      = []
        self.auto_speed_days     = 1
        self.current_loop        = 0
        self.is_auto_running     = False
        self.recent_stocks       = []
        self.current_macro_key   = "GRI"   # ★ 거시경제 탭 현재 선택 키
        self._macro_chart_cache  = {}      # ★ 차트 데이터 캐시

        # 저장 데이터 불러오기
        saved = game_service.load_game()
        if saved:
            self.my_cash      = saved["my_cash"]
            self.my_portfolio = saved["my_portfolio"]
            s = game_service.s
            if s.virtual_weekday <= 4:
                s.is_market_open = True
        else:
            game_service.initialize_market()

        # 뉴스창 (싱글턴)
        self.news_window = NewsWindow(self, game_service, news_service, self)
        self.news_window.hide()

        # 필터 다이얼로그 (싱글턴 — 닫혀도 조건 유지)
        self.filter_dialog = StockFilterDialog(self)
        self.active_dialogs.append(self.filter_dialog)
        self.filter_dialog.hide()

        self._init_ui()
        self.showMaximized()
        self.sync_ui_with_engine()

    # ─────────────────────────────────────────────
    # UI 초기화
    # ─────────────────────────────────────────────
    def _init_ui(self):
        self.setWindowTitle("G.PY Economic System Sync v10.0")
        self.resize(1800, 950)
        self.setStyleSheet(HTS_STYLE)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)

        # ── 대시보드 ─────────────────────────────
        self.dashboard = QFrame()
        self.dashboard.setStyleSheet(f"background-color: #000; border: 1px solid {COLOR['border_main']}; border-radius: 5px;")
        dash_lay = QVBoxLayout(self.dashboard)

        row1 = QHBoxLayout()
        self.date_label  = QLabel()
        self.date_label.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {COLOR['accent_green']};")
        self.date_label.setMinimumWidth(300)
        self.index_label = QLabel()
        self.index_label.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {COLOR['accent_green']}; margin-left: 20px;")
        self.index_label.setMinimumWidth(550)

        top_right = QHBoxLayout()
        self.asset_label = QLabel()
        self.asset_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.asset_label.setStyleSheet("color: white; font-family: 'Malgun Gothic'; border: none;")

        # ★ 시나리오 로그 버튼
        self.btn_scenario_log = QPushButton("📊 시나리오 로그")
        self.btn_scenario_log.setFixedSize(140, 35)
        self.btn_scenario_log.setStyleSheet("""
            QPushButton { background: #1a1a2e; color: #00BFFF; border: 1px solid #00BFFF;
                padding: 4px 12px; font-weight: bold; border-radius: 3px; font-size: 12px; }
            QPushButton:hover { background: #003355; }
        """)
        self.btn_scenario_log.clicked.connect(self._open_scenario_log)

        self.btn_system = QPushButton("✕")
        self.btn_system.setFixedSize(35, 35)
        self.btn_system.setStyleSheet(f"""
            QPushButton {{ background: #222; color: white; font-size: 18px; border-radius: 5px;
                font-weight: bold; border: 1px solid #444; }}
            QPushButton:hover {{ background: {COLOR['accent_red']}; border: 1px solid {COLOR['accent_red']}; }}
        """)
        self.btn_system.clicked.connect(self.open_system_menu)
        top_right.addWidget(self.asset_label)
        top_right.addWidget(self.btn_scenario_log)
        top_right.addWidget(self.btn_system)

        row1.addWidget(self.date_label)
        row1.addWidget(self.index_label)
        row1.addStretch(1)
        row1.addLayout(top_right)
        dash_lay.addLayout(row1)

        # 라인1: 좌측(금리/유가/물가/환율) + 우측(PER+섹터)
        macro_row = QHBoxLayout()
        self.macro_label = QLabel()
        self.macro_label.setStyleSheet("font-size: 13px; color: #00BFFF;")
        self.market_stats_label = QLabel()
        self.market_stats_label.setStyleSheet("font-size: 13px;")
        self.market_stats_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        macro_row.addWidget(self.macro_label)
        macro_row.addStretch()
        macro_row.addWidget(self.market_stats_label)
        dash_lay.addLayout(macro_row)

        # 라인2: 좌측(물가체감) + 중간(시장폭) + 우측(산업 12개)
        industry_row = QHBoxLayout()
        self.inflation_label = QLabel()
        self.inflation_label.setStyleSheet("font-size: 13px; color: #FFA500; font-weight: bold;")
        # ★ [신규] 시장폭 레이블 (상승/하락/상한가/하한가 수)
        self.breadth_label = QLabel()
        self.breadth_label.setStyleSheet("font-size: 12px;")
        self.breadth_label.setTextFormat(Qt.TextFormat.RichText)
        self.industry_label = QLabel()
        self.industry_label.setStyleSheet("font-size: 12px;")
        self.industry_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        industry_row.addWidget(self.inflation_label)
        industry_row.addSpacing(12)
        industry_row.addWidget(self.breadth_label)
        industry_row.addStretch()
        industry_row.addWidget(self.industry_label)
        dash_lay.addLayout(industry_row)

        # ── 최근 검색 + 상위 종목 행 ─────────────────
        bottom_row = QHBoxLayout()

        # 좌측: 최근 검색
        recent_lbl = QLabel("최근:")
        recent_lbl.setStyleSheet("color: #555; font-size: 12px; min-width: 35px;")
        bottom_row.addWidget(recent_lbl)
        self.recent_btns = []
        recent_btn_style = "QPushButton { background: #1a1a1a; color: #00BFFF; border: 1px solid #333; padding: 2px 8px; border-radius: 3px; font-size: 12px; } QPushButton:hover { border: 1px solid #00BFFF; }"
        for i in range(5):
            btn = QPushButton("")
            btn.setVisible(False)
            btn.setStyleSheet(recent_btn_style)
            btn.clicked.connect(lambda _, idx=i: self._on_recent_clicked(idx))
            bottom_row.addWidget(btn)
            self.recent_btns.append(btn)

        bottom_row.addStretch()

        # 우측: 오늘/전체 토글 + 상위 종목 버튼
        self._top_mode = "오늘"  # "오늘" or "전체"

        _toggle_base = """
            QPushButton {{
                background: {bg}; color: {fg};
                border: 1px solid {bd}; padding: 2px 10px;
                border-radius: 3px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ border: 1px solid #00FF00; }}
        """
        self.btn_top_today = QPushButton("오늘")
        self.btn_top_today.setFixedSize(48, 22)
        self.btn_top_total = QPushButton("전체")
        self.btn_top_total.setFixedSize(48, 22)

        def _refresh_toggle_style():
            if self._top_mode == "오늘":
                self.btn_top_today.setStyleSheet(_toggle_base.format(bg="#003300", fg="#00FF00", bd="#00FF00"))
                self.btn_top_total.setStyleSheet(_toggle_base.format(bg="#1a1a1a", fg="#555", bd="#333"))
            else:
                self.btn_top_today.setStyleSheet(_toggle_base.format(bg="#1a1a1a", fg="#555", bd="#333"))
                self.btn_top_total.setStyleSheet(_toggle_base.format(bg="#003300", fg="#00FF00", bd="#00FF00"))

        def _on_top_today():
            self._top_mode = "오늘"
            _refresh_toggle_style()
            self._refresh_top_stocks()

        def _on_top_total():
            self._top_mode = "전체"
            _refresh_toggle_style()
            self._refresh_top_stocks()

        self.btn_top_today.clicked.connect(_on_top_today)
        self.btn_top_total.clicked.connect(_on_top_total)
        self._refresh_toggle_style = _refresh_toggle_style
        _refresh_toggle_style()

        bottom_row.addWidget(self.btn_top_today)
        bottom_row.addWidget(self.btn_top_total)

        # 상위 종목 버튼 5개
        self.top_stock_btns = []
        top_btn_style = """
            QPushButton {{
                background: #0d1a0d; color: {fg};
                border: 1px solid #1a3a1a; padding: 2px 8px;
                border-radius: 3px; font-size: 12px;
            }}
            QPushButton:hover {{ border: 1px solid #00FF00; color: #00FF00; }}
        """
        for i in range(5):
            btn = QPushButton("")
            btn.setVisible(False)
            btn.setFixedHeight(22)
            btn.setStyleSheet(top_btn_style.format(fg="#39FF14"))
            btn.clicked.connect(lambda _, idx=i: self._on_top_stock_clicked(idx))
            bottom_row.addWidget(btn)
            self.top_stock_btns.append(btn)

        dash_lay.addLayout(bottom_row)
        main_layout.addWidget(self.dashboard)

        # ── 중앙 콘텐츠 ──────────────────────────
        content_lay = QHBoxLayout()

        # 좌측 테이블
        left_panel = QVBoxLayout()
        search_lay = QHBoxLayout()
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("🔍 회사명 검색...")
        self.search_bar.setStyleSheet("background-color: #222; color: white; border: 1px solid #444; height: 30px; padding-left: 10px; font-weight: bold;")
        self.search_bar.textChanged.connect(self.filter_stocks)

        btn_filter = QPushButton("⚙️ 상세 필터")
        btn_filter.setFixedSize(80, 30)
        btn_filter.setStyleSheet("QPushButton { background: #333; color: #00FF00; font-weight: bold; border: 1px solid #444; border-radius: 3px; } QPushButton:hover { border: 1px solid #00FF00; }")
        btn_filter.clicked.connect(self.open_stock_filter_window)
        search_lay.addWidget(self.search_bar)
        search_lay.addWidget(btn_filter)
        left_panel.addLayout(search_lay)

        self.stock_table = QTableWidget(0, 4)
        self.stock_table.setHorizontalHeaderLabels(["순위", "종목명", "현재가", "등락율"])
        hh = self.stock_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)      # 순위 고정
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)     # 종목명
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)     # 현재가
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)     # 등락율
        self.stock_table.setColumnWidth(0, 48)
        self.stock_table.verticalHeader().setVisible(False)
        self.stock_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stock_table.cellClicked.connect(self.on_stock_clicked)

        # 총 종목 수 레이블 (헤더 순위 컬럼 위)
        self.total_stocks_label = QLabel("0")
        self.total_stocks_label.setVisible(False)  # 헤더에 숫자로 표시
        left_panel.addWidget(self.stock_table)
        content_lay.addLayout(left_panel, 2)

        # 중앙 차트
        chart_lay = QVBoxLayout()

        # ── ★ 거시경제 지표 탭 버튼 ────────────────────────────
        macro_tab_lay = QHBoxLayout()
        macro_tab_lay.setSpacing(4)
        macro_tab_lay.setContentsMargins(0, 2, 0, 2)
        _MACRO_TABS = [
            ("GRI",           "GRI"),
            ("buffett_index", "버핏"),
            ("interest_rate", "금리"),
            ("oil_price",     "유가"),
            ("exchange_rate", "환율"),
            ("cpi",           "CPI"),
            ("metal_price",   "구리"),
            ("grain_price",   "밀"),
            ("semi_index",    "SOX"),
        ]
        self._macro_tab_btns = {}
        _mt_style = (
            "QPushButton {"
            "  background: #1a1a1a; color: #888;"
            "  border: 1px solid #333; padding: 3px 12px;"
            "  border-radius: 3px; font-size: 12px;"
            "}"
            "QPushButton:checked {"
            "  background: #001a00; color: #00FF00;"
            "  font-weight: bold; border: 1px solid #00FF00;"
            "}"
        )
        for _key, _lbl in _MACRO_TABS:
            _btn = QPushButton(_lbl)
            _btn.setCheckable(True)
            _btn.setFixedHeight(24)
            _btn.setStyleSheet(_mt_style)
            _btn.setChecked(_key == "GRI")
            _btn.clicked.connect(lambda _ch, k=_key: self._set_macro_tab(k))
            macro_tab_lay.addWidget(_btn)
            self._macro_tab_btns[_key] = _btn
        macro_tab_lay.addStretch()
        chart_lay.addLayout(macro_tab_lay)
        # ────────────────────────────────────────────────────────

        self.chart_widget = pg.PlotWidget()
        self.chart_widget.setBackground('#000000')
        self.chart_widget.setMouseEnabled(x=False, y=False)
        self.chart_widget.setMenuEnabled(False)
        self.chart_widget.hideButtons()
        self.curve    = self.chart_widget.plot(pen=pg.mkPen(color='#00FF00', width=2))
        self.baseline = pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('#555', width=1, style=Qt.PenStyle.DashLine))
        self.chart_widget.addItem(self.baseline)
        chart_lay.addWidget(self.chart_widget)

        tab_lay = QHBoxLayout()
        self.tabs = {}
        t_style = "QPushButton { background: #222; color: #888; border: 1px solid #444; padding: 5px; border-radius: 3px; } QPushButton:checked { background: #000; color: #00FF00; font-weight: bold; border: 2px solid #00FF00; }"
        for tf in self.TF_LIMITS:
            btn = QPushButton(tf)
            btn.setCheckable(True)
            btn.setFixedWidth(55)
            btn.setStyleSheet(t_style)
            if tf == "1일": btn.setChecked(True)
            btn.clicked.connect(lambda ch, t=tf: self.change_tf(t))
            tab_lay.addWidget(btn)
            self.tabs[tf] = btn

        self.change_summary_label = QLabel()
        self.change_summary_label.setStyleSheet("font-size: 14px; font-weight: bold; color: white;")
        tab_lay.addStretch()
        tab_lay.addWidget(self.change_summary_label)
        chart_lay.addLayout(tab_lay)
        content_lay.addLayout(chart_lay, 5)

        # 우측 리포트
        rep_lay = QVBoxLayout()
        self.report_panel = QTextEdit()
        self.report_panel.setReadOnly(True)
        self.report_panel.setStyleSheet("background-color: #000; color: #e0e0e0; border: 1px solid #333; font-size: 13px;")
        rep_lay.addWidget(self.report_panel, 1)

        self.holding_table = QTableWidget(1, 4)
        self.holding_table.setFixedHeight(75)
        self.holding_table.setHorizontalHeaderLabels(["평균단가", "보유수량", "수익률", "총 금액"])
        self.holding_table.setStyleSheet("""
            QTableWidget { background-color: #111; border: 1px solid #333; gridline-color: #222; }
            QHeaderView::section { background-color: #222; color: #aaa; font-size: 11px; padding: 3px; }
            QTableWidget::item { padding: 4px; font-size: 13px; font-weight: bold; }
        """)
        self.holding_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.holding_table.verticalHeader().setVisible(False)
        for j in range(4):
            it = QTableWidgetItem("-")
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.holding_table.setItem(0, j, it)
        rep_lay.addWidget(self.holding_table)

        trade_lay = QHBoxLayout()
        self.btn_buy = QPushButton("🔴 주식 매수")
        self.btn_buy.setFixedHeight(40)
        self.btn_buy.setStyleSheet("QPushButton { background-color: #C0392B; color: white; font-weight: bold; border-radius: 3px; } QPushButton:hover { background-color: #E74C3C; }")
        self.btn_buy.clicked.connect(self.handle_buy)

        self.btn_sell = QPushButton("🔵 주식 매도")
        self.btn_sell.setFixedHeight(40)
        self.btn_sell.setStyleSheet("QPushButton { background-color: #2980B9; color: white; font-weight: bold; border-radius: 3px; } QPushButton:hover { background-color: #3498DB; }")
        self.btn_sell.clicked.connect(self.handle_sell)
        trade_lay.addWidget(self.btn_buy)
        trade_lay.addWidget(self.btn_sell)
        rep_lay.addLayout(trade_lay)

        btn_earn = QPushButton("📊 실적 상세조회")
        btn_earn.setFixedHeight(35)
        btn_earn.setStyleSheet(f"QPushButton {{ background-color: #1a1a1a; color: {COLOR['accent_green']}; border: 1px solid {COLOR['accent_green']}; border-radius: 5px; }}")
        btn_earn.clicked.connect(self.open_earnings_window)

        btn_volume = QPushButton("📈 호가창")
        btn_volume.setFixedHeight(35)
        btn_volume.setStyleSheet(f"QPushButton {{ background-color: #1a1a1a; color: #00FFFF; border: 1px solid #00FFFF; border-radius: 5px; }}")
        btn_volume.clicked.connect(self.open_investor_volume_window)

        earn_row = QHBoxLayout()
        earn_row.addWidget(btn_earn)
        earn_row.addWidget(btn_volume)
        rep_lay.addLayout(earn_row)
        content_lay.addLayout(rep_lay, 3)
        main_layout.addLayout(content_lay)

        # ── 하단 버튼 ─────────────────────────────
        btm_lay = QHBoxLayout()
        b_style = "QPushButton { background-color: #222; color: white; font-weight: bold; border: 1px solid #444; border-radius: 5px; padding: 12px; } QPushButton:hover { border: 1px solid #00FF00; }"

        for label, fn in [
            ("🏢 그룹사 경영 현황 (전체)", self.open_group_window),
            ("💀 상장폐지 역사관 (전체)",  self.open_delisted_window),
            ("💰 내 투자 (포트폴리오)",     self.open_investment_window),
        ]:
            b = QPushButton(label); b.setStyleSheet(b_style); b.clicked.connect(fn); btm_lay.addWidget(b, 1)

        btn_news = QPushButton("📰 뉴스 보기")
        btn_news.setStyleSheet("QPushButton { background-color: #E67E22; color: white; font-weight: bold; border-radius: 5px; padding: 12px; }")
        btn_news.clicked.connect(self.open_news_window)
        btm_lay.addWidget(btn_news, 1)

        self.btn_nxt = QPushButton("▶ NEXT DAY")
        self.btn_nxt.setFixedHeight(50)
        self.btn_nxt.setStyleSheet("QPushButton { background-color: #E67E22; color: white; font-weight: bold; border-radius: 5px; min-width: 300px; font-size: 16px; }")
        self.btn_nxt.clicked.connect(self.handle_next_day)
        btm_lay.addWidget(self.btn_nxt, 2)

        main_layout.addLayout(btm_lay)

        cont = QWidget()
        cont.setLayout(main_layout)
        self.setCentralWidget(cont)

    # ─────────────────────────────────────────────
    # 게임 진행
    # ─────────────────────────────────────────────
    def handle_next_day(self):
        if self.is_auto_running: return
        self.current_loop    = 0
        self.is_auto_running = True
        self.btn_nxt.setEnabled(False)

        if self.auto_speed_days <= 1:
            self._run_next_day_logic()
            self.btn_nxt.setEnabled(True)
            self.is_auto_running = False
            self.auto_speed_days = 1
        else:
            self.auto_timer = QTimer()
            self.auto_timer.timeout.connect(self._auto_step)
            self.auto_timer.start(50)

    def _auto_step(self):
        if self.current_loop < self.auto_speed_days:
            self._run_next_day_logic()
            self.current_loop += 1
        else:
            self.auto_timer.stop()
            self.btn_nxt.setEnabled(True)
            self.is_auto_running = False

    def _run_next_day_logic(self):
        s = self.game_service.s
        if s.is_market_open:
            for st in s.stocks:
                st['start_price'] = float(st['price'])

        self.game_service.next_day(silent=True)
        self.my_portfolio = self.game_service.adjust_portfolio_for_splits(self.my_portfolio)

        # ★ 상장폐지된 종목 포트폴리오에서 제거
        delisted_names = {ds['meta']['c_name'] for ds in s.delisted_stocks}
        removed = [n for n in list(self.my_portfolio.keys()) if n in delisted_names]
        for name in removed:
            del self.my_portfolio[name]
            s.daily_news.append(f"💀 [포트폴리오] {name} 상장폐지로 보유 주식이 소모되었습니다.")

        # 구독 자동 연장 체크
        curr_now  = s.current_date.date()
        bill_date = s.next_billing_date
        if bill_date:
            if isinstance(bill_date, str):
                bill_date = datetime.strptime(bill_date, '%Y-%m-%d').date()
            elif hasattr(bill_date, 'date'):
                bill_date = bill_date.date()
            if curr_now >= bill_date and s.has_paid_news_access:
                if self.my_cash >= 1_000_000:
                    self.my_cash -= 1_000_000
                    s.next_billing_date = s.current_date + timedelta(days=30)
                else:
                    s.has_paid_news_access = False

        # 금요일 자동 저장 (비동기 — UI 멈춤 방지)
        if s.virtual_weekday == 4:
            threading.Thread(
                target=self.game_service.save_game,
                args=(self.my_cash, self.my_portfolio),
                daemon=True
            ).start()

        self.sync_ui_with_engine()

        # 뉴스창 갱신
        if hasattr(self, 'news_window') and self.news_window:
            try:
                if self.news_window.isVisible():
                    self.news_window.refresh_data()
                    self.news_window.raise_()
            except RuntimeError:
                self.news_window = None

        # 기타 팝업 갱신
        for dialog in self.active_dialogs[:]:
            try:
                if not dialog or not dialog.isVisible(): continue
                cn = dialog.__class__.__name__
                if cn == "GroupInfoDialog":        dialog.update_all_info()
                elif cn == "InfoTableDialog":      dialog.refresh_data()
                elif cn == "EarningsDialog":       dialog.refresh()
                elif cn == "InvestorVolumeDialog": dialog.refresh_data()
                elif cn in ("MyInvestmentDialog", "TradeDialog"): dialog.update_info()
            except Exception as e:
                if dialog in self.active_dialogs: self.active_dialogs.remove(dialog)

    # ─────────────────────────────────────────────
    # 매매
    # ─────────────────────────────────────────────
    def handle_buy(self):
        s = self._get_selected_stock()
        if not s: return
        max_shares = int(self.my_cash // s['price'])
        dialog = TradeDialog("매수", s['meta']['c_name'], s['price'], max_shares, self)
        dialog.show()
        self.active_dialogs.append(dialog)

    @staticmethod
    def _qty(data: dict) -> int:
        """구 키(quantity) / 신 키(shares) 모두 대응"""
        return data.get('shares', data.get('quantity', 0))

    def handle_sell(self):
        s = self._get_selected_stock()
        if not s: return
        name = s['meta']['c_name']
        if name not in self.my_portfolio:
            QMessageBox.warning(self, "보유량 부족", "팔 주식이 없습니다."); return
        dialog = TradeDialog("매도", name, s['price'], self._qty(self.my_portfolio[name]), self)
        dialog.show()
        self.active_dialogs.append(dialog)

    def show_toast(self, message: str, color: str = "#FF4444"):
        """메인 창 중앙에 1.5초 토스트 메시지 표시"""
        from PyQt6.QtCore import QTimer
        toast = QLabel(message, self)
        toast.setAlignment(Qt.AlignmentFlag.AlignCenter)
        toast.setStyleSheet(f"""
            background-color: rgba(20,0,0,230);
            color: {color};
            border: 2px solid {color};
            font-size: 18px; font-weight: bold;
            padding: 18px 36px; border-radius: 5px;
        """)
        toast.adjustSize()
        toast.move((self.width() - toast.width()) // 2,
                   (self.height() - toast.height()) // 2)
        toast.show(); toast.raise_()
        QTimer.singleShot(1500, toast.deleteLater)

    def process_buy(self, name: str, price: int, num: int):
        ok, new_cash, new_port, msg = self.game_service.buy_stock(name, price, num, self.my_cash, self.my_portfolio)
        if not ok:
            self.show_toast(msg); return
        self.my_cash      = new_cash
        self.my_portfolio = new_port
        self.sync_ui_with_engine()
        self.game_service.save_game(self.my_cash, self.my_portfolio)

    def process_sell(self, name: str, price: int, num: int):
        ok, new_cash, new_port, msg = self.game_service.sell_stock(name, price, num, self.my_cash, self.my_portfolio)
        if not ok:
            self.show_toast(msg); return
        self.my_cash      = new_cash
        self.my_portfolio = new_port
        self.sync_ui_with_engine()
        self.game_service.save_game(self.my_cash, self.my_portfolio)

    # ─────────────────────────────────────────────
    # 창 열기
    # ─────────────────────────────────────────────
    def open_group_window(self):
        d = GroupInfoDialog(self.game_service, self)
        d.show(); self.active_dialogs.append(d)

    def open_delisted_window(self):
        headers = ["No", "생애 주기", "등급", "상태", "그룹", "섹터", "회사명", "산업분류", "마지막 주가", "발행주수", "자사주 %"]
        d = InfoTableDialog("💀 상장폐지 역사관", headers, self.game_service, self)
        d.setModal(False)
        d.show()
        self.active_dialogs.append(d)

    def open_earnings_window(self):
        if self.selected_stock_name:
            d = EarningsDialog(self.selected_stock_name, self.game_service, self)
            d.show(); self.active_dialogs.append(d)

    def open_investor_volume_window(self):
        """호가창 — 실시간 갱신 지원"""
        if not self.selected_stock_name:
            self.show_toast("종목을 선택하세요.", "#FFA500")
            return
        dlg = InvestorVolumeDialog(self.selected_stock_name, self.game_service, self)
        dlg.show()
        self.active_dialogs.append(dlg)
    def open_investment_window(self):
        d = MyInvestmentDialog(self)
        d.show(); self.active_dialogs.append(d)

    def open_news_window(self):
        if self.news_window:
            self.news_window.show()
            self.news_window.raise_()
            self.news_window.activateWindow()
            self.news_window.refresh_data()
        else:
            self.news_window = NewsWindow(self, self.game_service, self.news_service, None)
            self.news_window.show()

    def open_stock_filter_window(self):
        self.filter_dialog.show()
        self.filter_dialog.raise_()
        self.filter_dialog.activateWindow()

    def open_system_menu(self):
        SystemMenuDialog(self).exec()

    # ─────────────────────────────────────────────
    # 저장/초기화
    # ─────────────────────────────────────────────
    def save_and_exit(self):
        self.game_service.save_game(self.my_cash, self.my_portfolio)
        # DB 명시적 종료 — 종료 딜레이 방지
        if hasattr(self.game_service, 'persistence'):
            self.game_service.persistence.close()
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()

    def _post_reset_ui(self):
        """reset 완료 후 메인 스레드에서 UI 갱신"""
        # 시장 통계 캐시 초기화 — 새 게임 데이터로 갱신되도록
        self._market_stats   = None
        self._prev_stats_txt = None
        self._prev_ind_txt   = None
        # ★ 거시경제 탭 초기화
        self.current_macro_key  = "GRI"
        self._macro_chart_cache = {}
        if hasattr(self, '_macro_tab_btns'):
            for k, btn in self._macro_tab_btns.items():
                btn.setChecked(k == "GRI")
        # ★ 상위 종목 캐시 초기화 — 이전 게임 수치 잔류 방지
        self._first_price_cache = {}
        self._top_stock_names   = []
        for btn in self.top_stock_btns:
            btn.setVisible(False)
        self.market_stats_label.setText("")
        self.industry_label.setText("")
        if hasattr(self, 'sector_count_label'): self.sector_count_label.setText("")
        if hasattr(self, 'ind_count_label'):    self.ind_count_label.setText("")

        # ★ NEXT DAY 버튼 복구
        self.btn_nxt.setEnabled(True)
        self.btn_nxt.setText("▶ NEXT DAY")

        # ★ 뉴스창 재생성 (None 상태 해소)
        self.news_window = NewsWindow(self, self.game_service, self.news_service, self)
        self.news_window.hide()

        self.sync_ui_with_engine()
        self.report_panel.clear()
        # 차트 명시적 초기화 — 빈 데이터로 0~1 축 뜨는 문제 방지
        self.curve.setData([1000.0, 1000.0])
        self.curve.setPen(pg.mkPen(color='#00FF00', width=2))
        self.chart_widget.setYRange(999.0, 1001.0)
        self.baseline.setPos(1000.0)
        self.chart_widget.getAxis('bottom').setTicks([[(0, "전일"), (1, "현재")]])
        self.refresh_chart()

    def closeEvent(self, event):
        """메인 창 닫힐 때 타이머 정리 + DB 명시적 종료"""
        if hasattr(self, 'auto_timer') and self.auto_timer is not None:
            try:
                self.auto_timer.stop()
            except Exception:
                pass
        if hasattr(self.game_service, 'persistence'):
            try:
                self.game_service.persistence.close()
            except Exception:
                pass
        event.accept()

    def reset_game_logic(self):
        # ── 1. UI 정리 (메인 스레드에서 먼저) ──────────────
        for d in self.active_dialogs[:]:
            try: d.close()
            except Exception: pass
        self.active_dialogs.clear()

        if hasattr(self, 'news_window') and self.news_window:
            try:
                self.news_window.hts = None
                self.news_window.close()
            except Exception:
                pass
        self.news_window = None

        self.my_cash             = 1_000_000
        self.my_portfolio        = {}
        self.selected_stock_name = ""
        self.recent_stocks       = []
        self._refresh_recent_btns()
        self.report_panel.clear()
        self.curve.setData([])

        # ── 2. NEXT DAY 버튼 비활성화 (리셋 중 조작 방지) ──
        self.btn_nxt.setEnabled(False)
        self.btn_nxt.setText("⏳ 초기화 중...")

        # ── 3. DB 리셋은 백그라운드, UI 갱신은 완료 후 메인 스레드 ──
        def _do_reset():
            self.game_service.reset_game(self.my_cash, self.my_portfolio)
            QTimer.singleShot(0, self._post_reset_ui)

        threading.Thread(target=_do_reset, daemon=True).start()

    # ─────────────────────────────────────────────
    # UI 동기화
    # ─────────────────────────────────────────────


    # ─────────────────────────────────────────────
    # 시장 통계 계산 — stocks 단일 순회 O(n), 결과 캐시
    # 장 열린 날 next_day 이후 1회만 호출
    # ─────────────────────────────────────────────
    def _calc_market_stats(self):
        s             = self.game_service.s
        earnings_hist = s.earnings_history

        # 누산기: [합계, 카운트]
        tier_per = {
            "대형주": [0.0, 0],
            "중형주": [0.0, 0],
            "소형주": [0.0, 0],
        }
        sector_rate = {
            "Growth":    [0.0, 0],
            "Value":     [0.0, 0],
            "Defensive": [0.0, 0],
            "Cyclical":  [0.0, 0],
        }
        # 산업별 수익률 — 시가총액 가중 평균
        # {ind: [가중합, 시총합]}
        industry_rate = {ind: [0.0, 0.0] for ind in MAIN_INDUSTRIES}
        # 섹터별도 시총 가중 평균으로
        sector_rate = {
            "Growth":    [0.0, 0.0],
            "Value":     [0.0, 0.0],
            "Defensive": [0.0, 0.0],
            "Cyclical":  [0.0, 0.0],
        }
        deficit_count = 0

        for stock in s.stocks:
            meta    = stock["meta"]
            tier    = meta["tier"]
            ind     = meta.get("ind", "")
            mc      = stock["market_cap"]

            # 누적 수익률: (현재가 / 상장 초기가 - 1) * 100
            price   = stock.get("price", 0)
            initial = meta.get("initial_price", 0)
            cumul   = ((price / initial) - 1) * 100 if initial > 0 else 0.0

            # ★ 시가총액 가중 누산
            sector = SECTOR_MAP.get(ind, "Value")
            sr = sector_rate.get(sector)
            if sr:
                sr[0] += cumul * mc   # 가중합
                sr[1] += mc           # 시총합

            ir = industry_rate.get(ind)
            if ir:
                ir[0] += cumul * mc
                ir[1] += mc

            # PER: 실적 없으면 즉시 스킵 (조기 탈출)
            c_name = meta.get("c_name", "")
            hist   = earnings_hist.get(c_name)
            if not hist:
                continue

            # 최근 4분기 net_income — 리스트 생성 없이 최소 연산
            ni_vals = []
            for yd in hist.values():
                for qd in yd.values():
                    ni = qd.get("net_income", 0)
                    if ni != 0:
                        ni_vals.append(ni)
            if not ni_vals:
                continue

            recent    = ni_vals[-4:]
            annual_ni = sum(recent) * (4.0 / len(recent))  # 연환산

            if annual_ni <= 0:
                deficit_count += 1
                continue   # 적자는 PER 집계 제외 (평균 왜곡 방지)

            per_val = mc / annual_ni
            if per_val > 9999:   # 비정상 수치 제외
                continue

            tp = tier_per.get(tier)
            if tp:
                tp[0] += per_val
                tp[1] += 1

        def _avg(d, k):
            s, c = d[k]
            return s / c if c > 0 else 0.0

        total = max(1, len(s.stocks))
        self._market_stats = {
            "per_large":   _avg(tier_per,    "대형주"),
            "per_mid":     _avg(tier_per,    "중형주"),
            "per_small":   _avg(tier_per,    "소형주"),
            "rate_growth": _avg(sector_rate, "Growth"),
            "rate_value":  _avg(sector_rate, "Value"),
            "rate_def":    _avg(sector_rate, "Defensive"),
            "rate_cyclical": _avg(sector_rate, "Cyclical"),
            "deficit_pct": deficit_count / total * 100,
            # 산업별 평균 수익률 — 약어 키로 저장
            "ind": {k: _avg(industry_rate, k) for k in industry_rate},
        }


    # ─────────────────────────────────────────────
    # ★ 시나리오 로그 뷰어
    # ─────────────────────────────────────────────
    def _open_scenario_log(self):
        """시나리오 로그 다이얼로그 열기"""
        import os
        from PyQt6.QtWidgets import (
            QDialog, QVBoxLayout, QHBoxLayout, QTableWidget,
            QTableWidgetItem, QHeaderView, QPushButton, QLabel, QFileDialog
        )
        from PyQt6.QtGui import QColor

        db = self.game_service.db   # GameService.db = persistence
        rows = db.get_scenario_log() if hasattr(db, 'get_scenario_log') else []

        dlg = QDialog(self)
        dlg.setWindowTitle("📊 시나리오 변경 로그")
        dlg.setStyleSheet(self.styleSheet())
        # ★ 최대화/최소화 버튼 + 일반 창 플래그
        dlg.setWindowFlags(
            Qt.WindowType.Window |
            Qt.WindowType.WindowMaximizeButtonHint |
            Qt.WindowType.WindowMinimizeButtonHint |
            Qt.WindowType.WindowCloseButtonHint
        )

        layout = QVBoxLayout(dlg)

        # 상단 안내
        header = QLabel(f"총 {len(rows)}개 시나리오 변경 기록  |  시나리오 변경 시점의 경제 지표 스냅샷")
        header.setStyleSheet("color: #00FF00; font-weight: bold; font-size: 13px; padding: 5px;")
        layout.addWidget(header)

        # 테이블
        cols = [
            "날짜", "시작GRI", "고점", "저점", "종료GRI", "일수",
            "버블", "금리", "유가", "환율", "CPI",
            "밀($/bu)", "구리($/t)", "SOX",
            "PER대", "PER중", "PER소",
            "성장섹", "가치섹",
            "IT", "건강", "에너지", "금융",
            "산업재", "소재", "부동산",
            "유틸", "자유소비", "필수소비", "커뮤",
            "시나리오", "전쟁/이벤트"
        ]
        tbl = QTableWidget(len(rows), len(cols))
        tbl.setHorizontalHeaderLabels(cols)
        tbl.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        tbl.setStyleSheet("""
            QTableWidget { background:#000; color:#ddd; gridline-color:#222; }
            QHeaderView::section { background:#1a1a1a; color:#00FF00; padding:6px; border:1px solid #333; }
        """)

        for i, r in enumerate(rows):
            gri     = r.get('gri', 0)
            gri_end = r.get('gri_end', gri)
            chg     = f"({(gri_end/gri-1)*100:+.1f}%)" if gri > 0 else ""
            vals = [
                r['date'],
                f"{gri:,.0f}",
                f"{r.get('gri_high', gri):,.0f}",
                f"{r.get('gri_low',  gri):,.0f}",
                f"{gri_end:,.0f}{chg}",
                f"{r.get('duration_days', 0)}일",
                f"{r['bubble']:.1f}",
                f"{r['interest_rate']:.2f}%",
                f"${r['oil_price']:.1f}",
                f"₩{r['exchange_rate']:,.0f}",
                f"{r['cpi']:.2f}%",
                f"${r.get('grain_price', 0):.0f}",
                f"${r.get('metal_price', 0):,.0f}",
                f"{r.get('semi_index', 0):,.0f}",
                f"{r.get('per_large', 0):.1f}x",
                f"{r.get('per_mid', 0):.1f}x",
                f"{r.get('per_small', 0):.1f}x",
                f"{r.get('sector_growth', 0):+.2f}%",
                f"{r.get('sector_value', 0):+.2f}%",
                f"{r.get('ind_it', 0):+.2f}%",
                f"{r.get('ind_health', 0):+.2f}%",
                f"{r.get('ind_energy', 0):+.2f}%",
                f"{r.get('ind_finance', 0):+.2f}%",
                f"{r.get('ind_industry', 0):+.2f}%",
                f"{r.get('ind_material', 0):+.2f}%",
                f"{r.get('ind_realestate', 0):+.2f}%",
                f"{r.get('ind_util', 0):+.2f}%",
                f"{r.get('ind_consumer', 0):+.2f}%",
                f"{r.get('ind_staple', 0):+.2f}%",
                f"{r.get('ind_comm', 0):+.2f}%",
                r['scenario'],
                r.get('war_event') or "",
            ]
            sc = r['scenario']
            for j, v in enumerate(vals):
                it = QTableWidgetItem(str(v))
                it.setTextAlignment(0x0004 | 0x0080)
                if '대공황' in sc:               it.setForeground(QColor("#FF4444"))
                elif '전쟁' in sc or '분쟁' in sc: it.setForeground(QColor("#FF8800"))
                elif '팬데믹' in sc:              it.setForeground(QColor("#FF44FF"))
                elif '극복' in sc or '재건' in sc: it.setForeground(QColor("#00FF00"))
                elif '침체' in sc:               it.setForeground(QColor("#FFCC00"))
                else:                            it.setForeground(QColor("#AAAAAA"))
                tbl.setItem(i, j, it)

        hdr = tbl.horizontalHeader()
        for c in range(len(cols)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(32, QHeaderView.ResizeMode.Stretch)  # 시나리오 열
        layout.addWidget(tbl)

        # 하단 버튼
        btn_row = QHBoxLayout()
        btn_dl = QPushButton("💾 TXT로 다운로드")
        btn_dl.setStyleSheet("""
            QPushButton { background:#003300; color:#00FF00; border:1px solid #00FF00;
                padding:8px 20px; font-weight:bold; border-radius:3px; }
            QPushButton:hover { background:#005500; }
        """)

        def _download():
            import os
            # 기본 저장 경로를 게임 폴더로
            default_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "..", "scenario_log.txt"
            )
            path, _ = QFileDialog.getSaveFileName(
                dlg, "시나리오 로그 저장",
                os.path.abspath(default_path),
                "Text Files (*.txt)"
            )
            if path:
                saved = db.export_scenario_log_txt(path)
                from PyQt6.QtWidgets import QMessageBox
                if not saved:
                    QMessageBox.warning(dlg, "저장 실패", "저장할 데이터가 없거나 오류가 발생했습니다.")

        btn_dl.clicked.connect(_download)
        btn_close = QPushButton("닫기")
        btn_close.setStyleSheet("""
            QPushButton { background:#1a1a1a; color:#aaa; border:1px solid #444;
                padding:8px 20px; border-radius:3px; }
            QPushButton:hover { background:#333; }
        """)
        btn_close.clicked.connect(dlg.close)
        btn_row.addWidget(btn_dl)
        btn_row.addStretch()
        btn_row.addWidget(btn_close)
        layout.addLayout(btn_row)

        dlg.show()
        dlg.showMaximized()
        dlg.exec()

    def sync_ui_with_engine(self):
        s = self.game_service.s
        is_open = s.is_market_open
        self.btn_buy.setEnabled(is_open)
        self.btn_sell.setEnabled(is_open)
        market_status = " [영업 중]" if is_open else " [장마감 - 휴장]"

        total_buy = total_eval = 0
        for name, data in self.my_portfolio.items():
            stock = self.game_service.get_stock_by_name(name)
            cur_p = stock['price'] if stock else 0
            qty = self._qty(data)
            total_buy  += qty * data['avg_price']
            total_eval += qty * cur_p

        total_profit = total_eval - total_buy
        total_rate   = (total_profit / total_buy * 100) if total_buy > 0 else 0.0
        p_color      = "#FF4444" if total_profit > 0 else ("#4444FF" if total_profit < 0 else "#e0e0e0")

        self.asset_label.setText(
            f"<div align='right'><table><tr>"
            f"<td><div style='background-color:#111;border:1px solid #333;padding:15px;border-radius:8px;min-width:200px;'>"
            f"<span style='color:#aaa;font-size:12px;'>보유 현금(예수금)</span><br/>"
            f"<span style='color:#FFD700;font-size:26px;font-weight:bold;'>{int(self.my_cash):,}원</span>"
            f"</div></td>"
            f"<td><div style='background-color:#111;border:1px solid #333;padding:15px;border-radius:8px;margin-left:10px;min-width:240px;'>"
            f"<span style='color:#aaa;font-size:12px;'>내 투자 금액</span><br/>"
            f"<span style='color:{p_color};font-size:26px;font-weight:bold;'>{int(total_eval):,}원</span><br/>"
            f"<span style='color:{p_color};font-size:14px;font-weight:bold;'>{int(total_profit):+,}원 ({total_rate:+.2f}%)</span>"
            f"</div></td></tr></table></div>"
        )
        self.asset_label.setMinimumWidth(550)

        m     = s.macro
        wdays = ['월','화','수','목','금','토','일']
        w     = wdays[s.virtual_weekday]
        self.date_label.setText(f"📅 {s.current_date.strftime('%Y-%m-%d')} ({w}){market_status}")
        bubble = getattr(s, 'bubble_index', 0.0)
        if   bubble >= 300: b_str = f"🔴 버블 {bubble:.0f}"
        elif bubble >= 200: b_str = f"🟠 버블 {bubble:.0f}"
        elif bubble >= 150: b_str = f"🟡 버블 {bubble:.0f}"
        elif bubble >= 50:  b_str = f"🟢 버블 {bubble:.0f}"
        else:               b_str = f"버블 {bubble:.0f}"
        # 페이즈 표시 (1A/1B/2A 등 + 페이즈 이름)
        try:
            phase = self.game_service.eco.get_current_phase()
            lv = s.max_tech_reached
            phase_name = next(
                (p["name"] for p in TECH_PHASE.get(lv, []) if p["id"] == phase),
                ""
            )
            phase_str = f"{phase} {phase_name}" if phase_name else phase
        except Exception:
            phase_str = str(s.max_tech_reached)
        total_mc = sum(st.get('market_cap', 0) for st in s.stocks)
        buffett  = getattr(s, 'buffett_index', 0.0)
        # ★ [수정] 버핏지수 정상범위 상향 (한국 코스피 기준: 70~100%가 정상)
        b_icon   = "🟢" if buffett < 80 else ("🟡" if buffett < 120 else ("🟠" if buffett < 160 else "🔴"))
        self.index_label.setText(
            f"📊 GRI: {s.gri:,.0f} | {b_str} | {b_icon} 버핏 {buffett:.1f}% | LV.{s.max_tech_reached} [{phase_str}] | 🏦 시총: {_fmt_mc(total_mc)}"
        )
        # ★ [수정] 거시경제 상태바 — 방향성 화살표 + 공매도 평균 추가
        macro_snap = self.game_service.get_macro_snapshot()
        rate_str = f"금리: {macro_snap['interest']:.2f}%{macro_snap['interest_dir']}"
        fx_str   = f"환율: ₩{macro_snap['exchange']:,.0f}{macro_snap['exchange_dir']}"
        oil_str  = f"유가: ${macro_snap['oil']:.1f}"
        cpi_str  = f"CPI: {macro_snap['cpi']:.2f}%"
        sox_str  = f"SOX: {macro_snap['semi']:,.0f}{macro_snap['semi_dir']}"
        si_str   = f"공매도: {macro_snap['avg_short_interest']:.1f}%"
        self.macro_label.setText(
            f"🌍 {rate_str} | {oil_str} | {cpi_str} | {fx_str} | {sox_str} | {si_str}"
        )
        self.inflation_label.setText(f"🛍️ 물가체감: 2000년 ₩1,000 → 현재 ₩{s.base_item_price:,.0f}")

        # ★ [신규] 시장 폭(breadth) 표시 — 상승/하락/상한가/하한가 수
        if s.is_market_open:
            mkt_sum = self.game_service.get_market_summary()
            _up_c   = f"<span style='color:#FF4444;'>▲{mkt_sum['up']}</span>"
            _dn_c   = f"<span style='color:#4488FF;'>▼{mkt_sum['down']}</span>"
            _fl_c   = f"<span style='color:#888888;'>━{mkt_sum['flat']}</span>"
            _lu_c   = (f"<span style='color:#FF0000;font-weight:bold;'>상한:{mkt_sum['limit_up']}</span> "
                       if mkt_sum['limit_up'] > 0 else "")
            _ld_c   = (f"<span style='color:#0000FF;font-weight:bold;'>하한:{mkt_sum['limit_down']}</span> "
                       if mkt_sum['limit_down'] > 0 else "")
            _vr_c   = f"<span style='color:#AAAAAA;'>거래량배율:{mkt_sum['avg_vol_ratio']:.1f}x</span>"
            _breadth_txt = f"{_up_c} {_dn_c} {_fl_c} &nbsp; {_lu_c}{_ld_c}&nbsp; {_vr_c}"
            if hasattr(self, 'breadth_label'):
                self.breadth_label.setText(_breadth_txt)

        # ── 시장 통계: 장 열린 날만 갱신, 캐시 활용 ──────────────
        if s.is_market_open:
            self._calc_market_stats()
        ms = getattr(self, "_market_stats", None)
        if ms:
            def _pc(v):
                if v <= 0:  return "#888888"
                if v <= 25: return "#00FF00"
                if v <= 50: return "#FFD700"
                return "#FF6600"
            def _rc(v):
                # 숫자 색상: 양수=빨강, 음수=파랑, 0=회색
                return "#FF4444" if v > 0 else ("#4488FF" if v < 0 else "#888888")

            # 형광 강조: 양수 중 상위 N개만 (음수 큰 값은 제외)
            def _highlight_top(values: dict, top_n: int = 2) -> set:
                """양수 값 중 상위 top_n 키 반환 (양수가 없으면 전체 abs 기준)"""
                pos = {k: v for k, v in values.items() if v > 0}
                if pos:
                    sorted_keys = sorted(pos, key=lambda k: pos[k], reverse=True)
                else:
                    sorted_keys = sorted(values, key=lambda k: abs(values[k]), reverse=True)
                return set(sorted_keys[:top_n])

            pl, pm, ps = ms["per_large"], ms["per_mid"], ms["per_small"]
            sec_vals = {
                "성장": ms["rate_growth"], "가치": ms["rate_value"],
                "방어": ms["rate_def"],    "테마": ms["rate_cyclical"],
            }
            sec_hot = _highlight_top(sec_vals, 2)
            sep = "<span style='color:#444;'> | </span>"

            # 라인1 우측: PER + 섹터 (누적 수익률)
            def _sec_span(label, val):
                lbl_color = "#39FF14" if label in sec_hot else "#666666"  # 형광연두 or 회색
                return (
                    f"<span style='color:{lbl_color};'>{label} </span>"
                    f"<span style='color:{_rc(val)};'>{val:+.1f}%</span>"
                )
            stats_txt = (
                f"<span style='color:#666;'>PER </span>"
                f"<span style='color:{_pc(pl)};'>대 {pl:.0f}배</span>{sep}"
                f"<span style='color:{_pc(pm)};'>중 {pm:.0f}배</span>{sep}"
                f"<span style='color:{_pc(ps)};'>소 {ps:.0f}배</span>"
                f"&nbsp;&nbsp;&nbsp;"
                f"<span style='color:#666;'>섹터 </span>"
                + sep.join(_sec_span(k, v) for k, v in sec_vals.items())
            )
            if getattr(self, "_prev_stats_txt", None) != stats_txt:
                self.market_stats_label.setText(stats_txt)
                self._prev_stats_txt = stats_txt

            # 라인2 우측: 산업 12개 (누적 수익률)
            IND_SHORT = {
                "IT": "IT", "에너지": "에너지", "건강관리": "건강",
                "산업재": "산업재", "소재": "소재", "자유소비재": "자유소비",
                "커뮤니케이션": "커뮤", "금융": "금융", "필수소비재": "필수소비",
                "유틸리티": "유틸", "부동산": "부동산",
            }
            ind_map  = ms.get("ind", {})
            ind_vals = {short: ind_map.get(full, 0.0) for full, short in IND_SHORT.items()}
            ind_hot  = _highlight_top(ind_vals, 3)  # 산업은 12개라 상위 3개 강조

            parts = []
            for short, v in ind_vals.items():
                lbl_color = "#39FF14" if short in ind_hot else "#666666"
                parts.append(
                    f"<span style='color:{lbl_color};'>{short} </span>"
                    f"<span style='color:{_rc(v)};'>{v:+.1f}%</span>"
                )
            ind_txt = f"<span style='color:#666;'>산업 </span>" + sep.join(parts)
            if getattr(self, "_prev_ind_txt", None) != ind_txt:
                self.industry_label.setText(ind_txt)
                self._prev_ind_txt = ind_txt

        self.filter_stocks()
        self.refresh_chart()
        self._refresh_top_stocks()

        selected = self._get_selected_stock()
        if self.selected_stock_name == "GRI":
            self._update_gri_report()
        elif selected:
            self._update_report(selected)

        port_info = self.my_portfolio.get(self.selected_stock_name)
        if port_info and selected:
            shares    = self._qty(port_info)
            avg_p     = port_info['avg_price']
            cur_p     = selected['price']
            eval_p    = shares * cur_p
            r         = ((cur_p / avg_p) - 1) * 100 if avg_p > 0 else 0.0
            r_col     = "#FF4444" if r > 0 else ("#4444FF" if r < 0 else "#e0e0e0")
            self.holding_table.item(0, 0).setText(f"{int(avg_p):,}원")
            self.holding_table.item(0, 1).setText(f"{shares:,}주")
            self.holding_table.item(0, 2).setText(f"{r:+.2f}%")
            self.holding_table.item(0, 2).setForeground(QColor(r_col))
            self.holding_table.item(0, 3).setText(f"{int(eval_p):,}원")
        else:
            for j in range(4): self.holding_table.item(0, j).setText("-")

        for dialog in self.active_dialogs[:]:
            try:
                # StockFilterDialog는 닫혀있어도 active_dialogs에 유지 (필터 조건 보존)
                if isinstance(dialog, StockFilterDialog):
                    continue
                if dialog and dialog.isVisible():
                    if isinstance(dialog, MyInvestmentDialog):
                        dialog.update_info(total_eval, total_profit, total_rate)
                    elif hasattr(dialog, 'update_info'):
                        dialog.update_info()
                else:
                    if dialog in self.active_dialogs: self.active_dialogs.remove(dialog)
            except (RuntimeError, Exception):
                if dialog in self.active_dialogs: self.active_dialogs.remove(dialog)

    # ─────────────────────────────────────────────
    # 종목 필터 / 테이블
    # ─────────────────────────────────────────────
    def filter_stocks(self):
        query      = self.search_bar.text().strip().lower()
        # rank_map은 항상 전체 stocks 기준 (필터/검색 무관)
        all_sorted = sorted(self.game_service.s.stocks,
                            key=lambda x: x.get('market_cap', 0), reverse=True)
        rank_map   = {s['meta']['c_name']: i+1 for i, s in enumerate(all_sorted)}
        # snaps도 전체 기준으로 생성 (필터는 이후에 적용)
        snaps = [
            {"name": s['meta']['c_name'], "price": int(s['price']),
             "rate": s.get('rate', 0.0), "meta": s['meta'],
             "shares": s['shares'],
             "rank": rank_map[s['meta']['c_name']],  # 전체 기준 순위 고정
             "char": s['meta'].get('char', 'Normal')}
            for s in all_sorted
        ]
        filtered = []
        f_dialog = getattr(self, 'filter_dialog', None)

        for snap in snaps:
            if f_dialog:  # 닫혀있어도 필터 조건 유지
                m   = snap['meta']
                tier = m.get('tier', '소형주')

                if not f_dialog.tier_all.isChecked():
                    if not any([
                        f_dialog.tier_large.isChecked() and "대" in tier,
                        f_dialog.tier_mid.isChecked()   and "중" in tier,
                        f_dialog.tier_small.isChecked() and "소" in tier,
                    ]): continue

                if not f_dialog.group_all.isChecked():
                    has_group = m.get('group_id') is not None
                    if f_dialog.group_yes.isChecked() and not has_group: continue
                    if f_dialog.group_no.isChecked()  and has_group:     continue

                p_min = f_dialog.price_min.text()
                p_max = f_dialog.price_max.text()
                if p_min.isdigit() and snap['price'] < int(p_min): continue
                if p_max.isdigit() and snap['price'] > int(p_max): continue

                s_min = f_dialog.shares_min.text()
                s_max = f_dialog.shares_max.text()
                if s_min.isdigit() and snap['shares'] < int(s_min): continue
                if s_max.isdigit() and snap['shares'] > int(s_max): continue

                ind_selected = [ind for ind, btn in f_dialog.ind_buttons.items() if btn.isChecked()]
                if not f_dialog.ind_all.isChecked() and ind_selected:
                    if not any(sel.lower() in m.get('ind', '').lower() for sel in ind_selected): continue

                if not f_dialog.sec_all.isChecked():
                    s_name = SECTOR_MAP.get(m.get('ind'), "Value")
                    if not any([
                        f_dialog.sec_grow.isChecked()      and s_name == "Growth",
                        f_dialog.sec_val.isChecked()       and s_name == "Value",
                        f_dialog.sec_defensive.isChecked() and s_name == "Defensive",
                        f_dialog.sec_cyclical.isChecked()  and s_name == "Cyclical",
                    ]): continue

            if query not in snap['name'].lower(): continue
            filtered.append(snap)

        # rank 순으로 정렬 (필터 후에도 시총 순위 유지)
        filtered.sort(key=lambda x: x.get('rank', 9999))
        self._last_snaps = filtered   # ★ macro 탭 전환 시 재사용
        self._update_table(filtered)
        total    = len(self.game_service.s.stocks)
        filtered_count = len(filtered)
        self.total_stocks_label.setText(str(filtered_count))
        # 헤더: 필터 적용 시 "필터수/전체수", 아닐 때 전체 수만
        header_txt = str(filtered_count)
        item = QTableWidgetItem(header_txt)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stock_table.setHorizontalHeaderItem(0, item)
        if len(filtered) == 1:
            self.selected_stock_name = filtered[0]['name']

    def _update_table(self, snaps: list):
        self.stock_table.setRowCount(len(snaps) + 1)  # GRI 고정 행 +1

        # ── ★ 지표 고정 행 (0번 행) — 현재 macro_tab에 따라 동적 표시 ──
        s   = self.game_service.s
        key = getattr(self, 'current_macro_key', 'GRI')

        _MACRO_ROW_META = {
            "GRI": (
                "GRI", "GRI 지수",
                lambda: s.gri,
                lambda: getattr(s, 'prev_gri', s.gri),
                lambda v: f"{v:,.0f}",
            ),
            "buffett_index": (
                "버핏", "버핏 지수",
                lambda: getattr(s, 'buffett_index', 0.0),
                lambda: getattr(s, '_ui_prev_macro', {}).get(
                            'buffett_index', getattr(s, 'buffett_index', 0.0)),
                lambda v: f"{v:.1f}%",
            ),
            "interest_rate": (
                "금리", "기준 금리",
                lambda: s.macro.get("interest_rate", 0.0),
                lambda: getattr(s, "_ui_prev_macro", s._prev_macro_snapshot).get("interest_rate",
                            s.macro.get("interest_rate", 0.0)),
                lambda v: f"{v:.2f}%",
            ),
            "oil_price": (
                "유가", "WTI 유가",
                lambda: s.macro.get("oil_price", 0.0),
                lambda: getattr(s, "_ui_prev_macro", s._prev_macro_snapshot).get("oil_price",
                            s.macro.get("oil_price", 0.0)),
                lambda v: f"${v:.2f}",
            ),
            "exchange_rate": (
                "환율", "원/달러",
                lambda: s.macro.get("exchange_rate", 0.0),
                lambda: getattr(s, "_ui_prev_macro", s._prev_macro_snapshot).get("exchange_rate",
                            s.macro.get("exchange_rate", 0.0)),
                lambda v: f"₩{v:,.0f}",
            ),
            "cpi": (
                "CPI", "소비자물가",
                lambda: s.macro.get("cpi", 0.0),
                lambda: getattr(s, "_ui_prev_macro", s._prev_macro_snapshot).get("cpi",
                            s.macro.get("cpi", 0.0)),
                lambda v: f"{v:.2f}%",
            ),
            "metal_price": (
                "구리", "구리($/톤)",
                lambda: s.macro.get("metal_price", 0.0),
                lambda: getattr(s, "_ui_prev_macro", s._prev_macro_snapshot).get("metal_price",
                            s.macro.get("metal_price", 0.0)),
                lambda v: f"${v:,.0f}",
            ),
            "grain_price": (
                "밀", "밀($/부셸)",
                lambda: s.macro.get("grain_price", 0.0),
                lambda: getattr(s, "_ui_prev_macro", s._prev_macro_snapshot).get("grain_price",
                            s.macro.get("grain_price", 0.0)),
                lambda v: f"${v:.0f}",
            ),
            "semi_index": (
                "SOX", "SOX 반도체",
                lambda: s.macro.get("semi_index", 0.0),
                lambda: getattr(s, "_ui_prev_macro", s._prev_macro_snapshot).get("semi_index",
                            s.macro.get("semi_index", 0.0)),
                lambda v: f"{v:,.0f}",
            ),
        }
        _meta   = _MACRO_ROW_META.get(key, _MACRO_ROW_META["GRI"])
        _ind_lbl, _name_lbl, _cur_fn, _prev_fn, _fmt_fn = _meta
        _cur_v  = _cur_fn()
        _prev_v = _prev_fn()
        # 금리/CPI는 절대값 차이, 환율은 원 차이, 나머지는 비율
        _ABS_DIFF = {"interest_rate", "cpi", "buffett_index"}
        if key in _ABS_DIFF:
            _chg    = _cur_v - _prev_v        # %p 단위
            _chg_str = f"{_chg:+.2f}%p"
        elif key == "exchange_rate":
            _chg    = _cur_v - _prev_v
            _chg_str = f"{_chg:+.0f}원"
        else:
            _chg    = ((_cur_v / max(1e-9, _prev_v)) - 1.0) * 100 if _prev_v != 0 else 0.0
            _chg_str = f"{_chg:+.2f}%"
        _r0col  = rate_color(_chg)

        r0_ind = QTableWidgetItem(_ind_lbl)
        r0_ind.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        r0_ind.setForeground(QColor("#888888"))
        r0_ind.setFont(QFont("Malgun Gothic", 8, QFont.Weight.Bold))

        r0_name = QTableWidgetItem(_name_lbl)
        r0_name.setForeground(QColor("#AAAAAA"))
        r0_name.setFont(QFont("Malgun Gothic", 9))

        r0_price = QTableWidgetItem(_fmt_fn(_cur_v))
        r0_rate  = QTableWidgetItem(_chg_str)
        r0_price.setForeground(QColor(_r0col))
        r0_rate.setForeground(QColor(_r0col))
        r0_price.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        r0_rate.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self.stock_table.setItem(0, 0, r0_ind)
        self.stock_table.setItem(0, 1, r0_name)
        self.stock_table.setItem(0, 2, r0_price)
        self.stock_table.setItem(0, 3, r0_rate)

        for i, st in enumerate(snaps):
            i += 1  # GRI 행 때문에 +1
            col   = rate_color(st['rate'])
            rank  = st.get('rank', i+1)
            name  = st['name']
            char  = st.get('char', 'Normal')
            is_selected = (name == self.selected_stock_name)

            # 경고/위험 상태별 종목명 앞에 아이콘 추가
            if char == 'BANKRUPT':
                display_name = f"☠️ {name}"
            elif char == 'DANGER':
                display_name = f"🚨 {name}"
            elif char == 'WARNING':
                display_name = f"⚠️ {name}"
            elif 'EXIT' in str(char):
                display_name = f"💀 {name}"
            else:
                display_name = name

            rank_it = QTableWidgetItem(str(rank))
            rank_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            rank_it.setForeground(QColor("#555555"))
            n_it = QTableWidgetItem(display_name)
            p_it = QTableWidgetItem(f"{int(st['price']):,}원")
            r_it = QTableWidgetItem(f"{st['rate']:+.2f}%")

            # 색상 우선순위: 선택(연두) > BANKRUPT(빨강) > DANGER(주황빨강) > WARNING(노랑) > 기본
            if is_selected:
                n_it.setForeground(QColor("#00FF00"))
                n_it.setFont(QFont("Malgun Gothic", 10, QFont.Weight.Bold))
                rank_it.setForeground(QColor("#00FF00"))
            elif char == 'BANKRUPT':
                n_it.setForeground(QColor("#FF4444"))
                n_it.setFont(QFont("Malgun Gothic", 9, QFont.Weight.Bold))
            elif char == 'DANGER':
                n_it.setForeground(QColor("#FF6600"))
                n_it.setFont(QFont("Malgun Gothic", 9, QFont.Weight.Bold))
            elif char == 'WARNING':
                n_it.setForeground(QColor("#FFD700"))
                n_it.setFont(QFont("Malgun Gothic", 9, QFont.Weight.Bold))
            elif 'EXIT' in str(char):
                n_it.setForeground(QColor("#FFA500"))

            p_it.setForeground(QColor(col))
            r_it.setForeground(QColor(col))
            self.stock_table.setItem(i, 0, rank_it)
            self.stock_table.setItem(i, 1, n_it)
            self.stock_table.setItem(i, 2, p_it)
            self.stock_table.setItem(i, 3, r_it)

    def on_stock_clicked(self, r, c):
        # 0번 행 = GRI 고정 행 → 메인 차트에 GRI 표시
        if r == 0:
            self.selected_stock_name = "GRI"
            self.sync_ui_with_engine()
            return

        it = self.stock_table.item(r, 1)  # 컬럼 1 = 종목명
        if it:
            raw = it.text()
            for prefix in ["☠️ ", "🚨 ", "⚠️ ", "💀 "]:
                if raw.startswith(prefix):
                    raw = raw[len(prefix):]
                    break
            self.selected_stock_name = raw
            self._add_recent_stock(raw)
            self.sync_ui_with_engine()

    def _update_gri_report(self):
        """GRI 선택 시 우측 리포트 패널에 GRI 정보 표시"""
        s = self.game_service.s
        gri  = s.gri
        prev = getattr(s, 'prev_gri', gri)
        rate = ((gri / max(1.0, prev)) - 1.0) * 100
        col  = "#FF4444" if rate >= 0 else "#4444FF"
        sign = "▲" if rate > 0 else ("▼" if rate < 0 else "─")
        bubble = getattr(s, 'bubble_index', 0.0)

        # 스크롤 위치 저장
        _sb = self.report_panel.verticalScrollBar()
        _prev_scroll = _sb.value()

        if   bubble >= 300: b_color = "#FF0000"; b_label = "🔴 위험"
        elif bubble >= 200: b_color = "#FF6600"; b_label = "🟠 경고"
        elif bubble >= 150: b_color = "#FFD700"; b_label = "🟡 주의"
        elif bubble >= 50:  b_color = "#00FF00"; b_label = "🟢 정상"
        else:               b_color = "#888888"; b_label = "⚪ 안정"

        lv_names = {1: "1단계 (PC/인터넷)", 2: "2단계 (모바일/클라우드)",
                    3: "3단계 (AI/양자)", 4: "4단계 (기술 특이점)"}
        lv_name = lv_names.get(s.max_tech_reached, f"Lv.{s.max_tech_reached}")

        # ── 전체 시가총액 계산 ────────────────────
        stocks    = s.stocks
        total_mc  = sum(st.get('market_cap', 0) for st in stocks)
        stock_cnt = len(stocks)
        large_cnt = sum(1 for st in stocks if st['meta'].get('tier') == '대형주')
        mid_cnt   = sum(1 for st in stocks if st['meta'].get('tier') == '중형주')
        small_cnt = stock_cnt - large_cnt - mid_cnt

        # ★ [수정] 상위 3종목 집중도 추가
        sorted_by_mc = sorted(stocks, key=lambda st: st.get('market_cap', 0), reverse=True)
        top1_mc    = sorted_by_mc[0].get('market_cap', 0) if sorted_by_mc else 0
        top3_mc    = sum(st.get('market_cap', 0) for st in sorted_by_mc[:3])
        top1_name  = sorted_by_mc[0]['meta'].get('c_name', '?') if sorted_by_mc else '?'
        top1_ratio = (top1_mc / total_mc * 100) if total_mc > 0 else 0
        top3_ratio = (top3_mc / total_mc * 100) if total_mc > 0 else 0
        buffett    = getattr(s, 'buffett_index', 0.0)

        # ★ [신규] 버핏지수 해석 레이블
        if buffett < 60:    b_eval = "저평가"
        elif buffett < 100: b_eval = "정상"
        elif buffett < 130: b_eval = "주의"
        elif buffett < 170: b_eval = "경고"
        else:               b_eval = "위험"

        # ★ 버핏지수 색상 사전 계산 (f-string 중첩 방지)
        if b_eval == "저평가":  b_eval_color = "#00AAFF"
        elif b_eval == "정상":  b_eval_color = "#888888"
        elif b_eval == "주의":  b_eval_color = "#FFD700"
        elif b_eval == "경고":  b_eval_color = "#FF6600"
        else:                   b_eval_color = "#FF0000"
        _SM = SECTOR_MAP
        sec_cnt  = {"Growth": 0, "Value": 0, "Defensive": 0, "Cyclical": 0}
        ind_cnt  = {ind: 0 for ind in MAIN_INDUSTRIES}
        for st in stocks:
            ind = st['meta'].get('ind', '')
            sec = _SM.get(ind, 'Value')
            if sec in sec_cnt:  sec_cnt[sec]  += 1
            if ind in ind_cnt:  ind_cnt[ind]  += 1

        SEC_KR = {"Growth": "성장주", "Value": "가치주", "Defensive": "방어주", "Cyclical": "테마주"}
        sec_rows = "".join(
            f"<span style='color:#888;'>{SEC_KR[k]}</span> "
            f"<b style='color:#aaa;'>{sec_cnt[k]}개</b>&nbsp;&nbsp;"
            for k in ["Growth", "Value", "Defensive", "Cyclical"]
        )
        IND_SHORT = {
            "IT": "IT", "에너지": "에너지", "건강관리": "건강",
            "산업재": "산업재", "소재": "소재", "자유소비재": "자유소비",
            "커뮤니케이션": "커뮤", "금융": "금융", "필수소비재": "필수소비",
            "유틸리티": "유틸", "부동산": "부동산",
        }
        ind_rows = "&nbsp; ".join(
            f"<span style='color:#666;'>{short}</span>"
            f"<b style='color:#888;'> {ind_cnt.get(full,0)}</b>"
            for full, short in IND_SHORT.items()
        )

        self.report_panel.setHtml(f"""
        <div style='font-family: Malgun Gothic; padding: 10px;'>
            <p style='font-size:28px; font-weight:bold; color:#00FF00;'>GRI 지수</p>
            <p style='font-size:36px; font-weight:bold; color:{col};'>
                {gri:,.0f}
                <span style='font-size:18px;'> {sign}{abs(rate):.2f}%</span>
            </p>
            <hr style='border:0.5px solid #333;'/>
            <p><b style='color:#FFD700;'>🏦 전체 시가총액</b><br/>
            <span style='font-size:22px; font-weight:bold; color:#00FFFF;'>{_fmt_mc(total_mc)}</span><br/>
            <span style='color:#aaa; font-size:12px;'>
            상장 종목 {stock_cnt}개 &nbsp;|&nbsp;
            대형 {large_cnt} · 중형 {mid_cnt} · 소형 {small_cnt}<br/>
            1위 집중도: {top1_ratio:.1f}% ({top1_name} / {_fmt_mc(top1_mc)})
            &nbsp;|&nbsp; 상위3: {top3_ratio:.1f}%<br/>
            버핏지수: {buffett:.1f}% <span style='color:{b_eval_color};'>[{b_eval}]</span>
            </span></p>
            <hr style='border:0.5px solid #222;'/>
            <p><b style='color:#FFD700;'>기술 레벨</b><br/>
            {lv_name}</p>
            <p><b style='color:#FFD700;'>버블 지수</b><br/>
            <span style='color:{b_color}; font-size:20px; font-weight:bold;'>{bubble:.1f}</span>
            <span style='color:{b_color};'> {b_label}</span></p>
            <p><b style='color:#FFD700;'>현재 시나리오</b><br/>
            {s.current_scenario}</p>
            <hr style='border:0.5px solid #222;'/>
            <p><b style='color:#FFD700;'>섹터별 종목 수</b><br/>
            <span style='font-size:12px;'>{sec_rows}</span></p>
            <p><b style='color:#FFD700;'>산업별 종목 수</b><br/>
            <span style='font-size:12px;'>{ind_rows}</span></p>
            <hr style='border:0.5px solid #222;'/>
            <p><b style='color:#FFD700;'>거시경제</b><br/>
            <span style='font-size:12px;'>
            금리: {s.macro['interest_rate']:.2f}% &nbsp;|&nbsp;
            유가: ${s.macro['oil_price']:.1f}<br/>
            환율: ₩{s.macro['exchange_rate']:,.0f} &nbsp;|&nbsp;
            CPI: {s.macro['cpi']:.2f}%<br/>
            🌾 밀: ${s.macro.get('grain_price', 250):.0f}/bu &nbsp;|&nbsp;
            ⚙️ 구리: ${s.macro.get('metal_price', 1800):,.0f}/t<br/>
            💾 SOX: {s.macro.get('semi_index', 1000):,.0f}<br/>
            <br/>
            버핏지수: <b style='color:{b_eval_color};'>{buffett:.1f}% [{b_eval}]</b><br/>
            GDP 성장률: <span style='color:{"#00FF88" if getattr(s,"gdp_growth_rate",0)>=0 else "#FF4444"};'>
            {getattr(s,"gdp_growth_rate",0)*100:+.1f}%</span> (연환산)<br/>
            경기 사이클: <b>{getattr(s,"cycle_stage","확장")}</b> &nbsp;|&nbsp;
            투자심리: {getattr(s,"sentiment",50.0):.0f}<br/>
            외국인 수급: <span style='color:{"#FF4444" if getattr(s,"foreign_flow_index",0)>=0 else "#4444FF"};'>
            {getattr(s,"foreign_flow_index",0):+.1f}</span><br/>
            광기지수(MMI): <span style='color:{"#FF4444" if getattr(s,"market_mania_index",1)>=1.5 else "#888888"};'>
            {getattr(s,"market_mania_index",1.0):.2f}</span>
            </span>
            </p>
            {_build_event_html(s)}
            <hr style='border:0.5px solid #333;'/>
            <p style='color:#555; font-size:11px;'>* GRI 차트를 보려면 좌측 기간 버튼을 클릭하세요.</p>
        </div>
        """)
        # 매수/매도 버튼 비활성화
        self.btn_buy.setEnabled(False)
        self.btn_sell.setEnabled(False)
        # 스크롤 위치 복원
        _sb.setValue(_prev_scroll)

    @staticmethod
    def _get_chart_step(count: int, tf: str = "전체") -> int:
        """
        데이터 기간에 따른 step 결정.
        항상 최소 10포인트 이상 표시되도록 fallback 보장.

        전체 버튼 — 실제 데이터 기간 기반:
          1년 이하   : 1일
          1~3년      : 1주  (7일)
          3~10년     : 1달  (30일)
          10~30년    : 1분기(90일)
          30~50년    : 반기 (180일)
          50년+      : 1년  (365일)

        1년/5년 버튼도 데이터가 부족하면 자동으로 더 작은 step으로 fallback.
        """
        MIN_POINTS = 10

        if tf in ("1주", "1달", "3달"):
            step = 1
        elif tf == "1년":
            step = 7
        elif tf == "5년":
            step = 30
        else:
            years = count / 252
            if   years <= 1:  step = 1
            elif years <= 3:  step = 7
            elif years <= 10: step = 30
            elif years <= 30: step = 90
            elif years <= 50: step = 180
            else:             step = 365

        # 데이터 부족 시 fallback: MIN_POINTS 이상이 되도록 step 축소
        while step > 1 and count // step < MIN_POINTS:
            if   step >= 365: step = 180
            elif step >= 180: step = 90
            elif step >= 90:  step = 30
            elif step >= 30:  step = 7
            else:             step = 1

        return max(1, step)

    def _refresh_gri_chart(self):
        """메인 차트에 GRI 지수 표시"""
        lim = self.TF_LIMITS.get(self.current_tf, 1)
        s   = self.game_service.s

        # ── 1일 버튼: 전일GRI → 현재GRI 단순 2포인트 표시 ──────
        if lim == 1:
            self.chart_widget.getAxis('bottom').setTicks(None)  # ticks 충돌 방지
            gri_now  = s.gri
            gri_prev = getattr(s, 'prev_gri', gri_now)
            disp = [gri_prev, gri_now]

            # 기존 마커 제거
            for attr in ['_gri_max_scatter','_gri_min_scatter','_gri_max_text','_gri_min_text']:
                if hasattr(self, attr):
                    try: self.chart_widget.removeItem(getattr(self, attr))
                    except: pass

            col = "#FF4444" if gri_now >= gri_prev else "#4444FF"
            self.curve.setPen(pg.mkPen(color=col, width=2))
            self.curve.setData(disp)

            pad = max(1.0, abs(gri_now - gri_prev) * 0.5) if gri_now != gri_prev else gri_now * 0.001
            self.chart_widget.setYRange(min(disp) - pad, max(disp) + pad)
            self.baseline.setPos(float(gri_prev))
            self.chart_widget.getAxis('bottom').setTicks([[(0, "전일"), (1, "현재")]])

            rate  = ((gri_now / max(1.0, gri_prev)) - 1.0) * 100
            sign  = "▲" if rate > 0 else ("▼" if rate < 0 else "─")
            c_hex = "#FF4444" if rate > 0 else ("#4444FF" if rate < 0 else "#e0e0e0")
            self.change_summary_label.setText(
                f"<span style='color:#aaa;'>1일 기준: </span>"
                f"<span style='color:#fff;'>{gri_prev:,.0f}</span>"
                f" → <span style='color:{c_hex};font-weight:bold;'>{gri_now:,.0f} "
                f"({sign}{abs(rate):.2f}%)</span>"
            )
            return

        # ── 1일 외: DB에서 히스토리 조회 후 표시 ────────────────
        rows = self.game_service.db.get_gri_history(0 if lim > 900_000 else lim)
        if not rows:
            return

        dates  = [r[0] for r in rows]
        prices = [float(r[1]) for r in rows]
        if len(prices) == 1:
            prices = [prices[0], prices[0]]
            dates  = [dates[0], dates[0]]

        count = len(prices)
        step  = self._get_chart_step(count, self.current_tf)

        disp       = prices[::step]
        disp_dates = dates[::step]
        if prices[-1] not in disp:
            disp.append(prices[-1])
            disp_dates.append(dates[-1])
        if len(disp) < 2:
            disp       = [prices[0], prices[-1]]
            disp_dates = [dates[0], dates[-1]]

        # X축 날짜 레이블
        x_ticks  = []
        n_ticks  = min(6, len(disp_dates))
        t_indices = [int(i * (len(disp_dates)-1) / max(1, n_ticks-1)) for i in range(n_ticks)]
        seen     = set()
        for idx in t_indices:
            if idx < len(disp_dates):
                d = disp_dates[idx]
                label = d[2:7] if len(d) >= 7 else d
                if label not in seen:
                    x_ticks.append((idx, label))
                    seen.add(label)
        self.chart_widget.getAxis('bottom').setTicks([x_ticks])

        col = "#FF4444" if disp[-1] >= disp[0] else "#4444FF"
        self.curve.setPen(pg.mkPen(color=col, width=2))
        self.curve.setData(disp)

        mn, mx = min(prices), max(prices)
        pad = max(1.0, (mx - mn) * 0.05) if mx != mn else mx * 0.01
        self.chart_widget.setYRange(mn - pad, mx + pad)
        self.baseline.setPos(float(disp[0]))

        # _gri_* 전용 마커 제거 (이전 방식 잔재 정리)
        for attr in ['_gri_max_scatter','_gri_min_scatter','_gri_max_text','_gri_min_text']:
            if hasattr(self, attr):
                try: self.chart_widget.removeItem(getattr(self, attr)); delattr(self, attr)
                except: pass

        self._update_chart_markers(disp, prices)

        rate  = ((disp[-1] / max(1.0, disp[0])) - 1.0) * 100
        sign  = "▲" if rate > 0 else ("▼" if rate < 0 else "─")
        c_hex = "#FF4444" if rate > 0 else ("#4444FF" if rate < 0 else "#e0e0e0")
        self.change_summary_label.setText(
            f"<span style='color:#aaa;'>{self.current_tf} 기준: </span>"
            f"<span style='color:#fff;'>{disp[0]:,.0f}</span>"
            f" → <span style='color:{c_hex};font-weight:bold;'>{disp[-1]:,.0f} "
            f"({sign}{abs(rate):.2f}%)</span>"
        )

        for i in range(self.stock_table.rowCount()):
            it = self.stock_table.item(i, 1)
            if it:
                raw = it.text()
                for prefix in ["☠️ ", "🚨 ", "⚠️ ", "💀 "]:
                    if raw.startswith(prefix): raw = raw[len(prefix):]; break
                if raw == self.selected_stock_name:
                    self.stock_table.scrollToItem(it, QTableWidget.ScrollHint.PositionAtCenter)
                    break

    def change_tf(self, tf: str):
        self.current_tf = tf
        for n, btn in self.tabs.items(): btn.setChecked(n == tf)
        self.refresh_chart()

    # ─────────────────────────────────────────────
    # ★ 거시경제 탭 전환
    # ─────────────────────────────────────────────
    def _set_macro_tab(self, key: str):
        """거시경제 탭 버튼 클릭 처리 — 0번 행 선택 상태일 때만 차트 갱신"""
        self.current_macro_key = key
        self._macro_chart_cache = {}
        for k, btn in self._macro_tab_btns.items():
            btn.setChecked(k == key)
        if key == "GRI":
            self.selected_stock_name = "GRI"
        else:
            # 종목 선택 중이면 0번 행으로 강제 전환 후 차트 표시
            self.selected_stock_name = "GRI"
        self.refresh_chart()
        if hasattr(self, '_last_snaps'):
            self._update_table(self._last_snaps)

    # ─────────────────────────────────────────────
    # ★ 거시경제 지표 차트
    # ─────────────────────────────────────────────
    def _refresh_macro_chart(self, key: str):
        """macro_history DB에서 지표를 읽어 차트 표시 (GRI 차트와 동일 구조)"""
        _FMT = {
            "buffett_index": lambda v: f"{v:.1f}%",
            "interest_rate": lambda v: f"{v:.2f}%",
            "oil_price":     lambda v: f"${v:.2f}",
            "exchange_rate": lambda v: f"₩{v:,.0f}",
            "cpi":           lambda v: f"{v:.2f}%",
            "metal_price":   lambda v: f"${v:,.0f}",
            "grain_price":   lambda v: f"${v:.2f}",
            "semi_index":    lambda v: f"{v:,.1f}",
        }
        # 등락 표시 방식: 절대값 차이(%p) vs 비율(%)
        # 금리/CPI/버핏 → 절대값 차이 (0.07%p), 나머지 → 비율 (3.18%)
        _ABS_DIFF_KEYS = {"interest_rate", "cpi", "buffett_index"}

        def _change_str(cur, prev, k):
            diff = cur - prev
            sign = "▲" if diff > 0 else ("▼" if diff < 0 else "─")
            c    = "#FF4444" if diff > 0 else ("#4444FF" if diff < 0 else "#e0e0e0")
            if k in _ABS_DIFF_KEYS:
                txt = f"{sign}{abs(diff):.2f}%p"
            elif k == "exchange_rate":
                txt = f"{sign}{abs(diff):,.0f}원"
            else:
                pct = ((cur / max(1e-9, prev)) - 1.0) * 100 if prev != 0 else 0.0
                txt = f"{sign}{abs(pct):.2f}%"
            return c, txt

        fmt = _FMT.get(key, lambda v: f"{v:.2f}")
        lim = self.TF_LIMITS.get(self.current_tf, 1)
        s   = self.game_service.s

        # ── 1일: 전일값 → 현재값 2포인트 ────────────────────────
        if lim == 1:
            self.chart_widget.getAxis('bottom').setTicks(None)
            if key == "buffett_index":
                cur_v  = getattr(s, 'buffett_index', 0.0)
                prev_v = getattr(s, '_ui_prev_macro', {}).get('buffett_index', cur_v)
            else:
                cur_v  = s.macro.get(key, 0.0)
                prev_v = getattr(s, "_ui_prev_macro", s._prev_macro_snapshot).get(key, cur_v)
            disp   = [prev_v, cur_v]

            for attr in ['_gri_max_scatter','_gri_min_scatter','_gri_max_text','_gri_min_text',
                         'max_scatter','min_scatter','max_text','min_text']:
                if hasattr(self, attr):
                    try: self.chart_widget.removeItem(getattr(self, attr))
                    except: pass

            col = "#FF4444" if cur_v >= prev_v else "#4444FF"
            self.curve.setPen(pg.mkPen(color=col, width=2))
            self.curve.setData(disp)
            pad = max(1e-6, abs(cur_v - prev_v) * 0.5) if cur_v != prev_v \
                  else max(abs(cur_v) * 0.001, 0.01)
            self.chart_widget.setYRange(min(disp) - pad, max(disp) + pad)
            self.baseline.setPos(float(prev_v))
            self.chart_widget.getAxis('bottom').setTicks([[(0, "전일"), (1, "현재")]])

            rate  = ((cur_v / max(1e-9, prev_v)) - 1.0) * 100 if prev_v != 0 else 0.0
            sign  = "▲" if rate > 0 else ("▼" if rate < 0 else "─")
            c_hex = "#FF4444" if rate > 0 else ("#4444FF" if rate < 0 else "#e0e0e0")
            c_hex, chg_txt = _change_str(cur_v, prev_v, key)
            self.change_summary_label.setText(
                f"<span style='color:#aaa;'>1일 기준: </span>"
                f"<span style='color:#fff;'>{fmt(prev_v)}</span>"
                f" → <span style='color:{c_hex};font-weight:bold;'>{fmt(cur_v)} "
                f"({chg_txt})</span>"
            )
            return

        # ── 1일 외: DB 조회 (메모리 캐시 활용) ──────────────────
        cache_id = (key, lim)
        cache    = getattr(self, '_macro_chart_cache', {})
        if cache.get('id') == cache_id:
            rows = cache['rows']
        else:
            rows = self.game_service.db.get_macro_history(
                key, 0 if lim > 900_000 else lim
            )
            self._macro_chart_cache = {'id': cache_id, 'rows': rows}

        if not rows:
            return

        dates  = [r[0] for r in rows]
        values = [float(r[1]) for r in rows]
        if len(values) == 1:
            values = [values[0], values[0]]
            dates  = [dates[0], dates[0]]

        count = len(values)
        step  = self._get_chart_step(count, self.current_tf)

        disp       = values[::step]
        disp_dates = dates[::step]
        if values[-1] not in disp:
            disp.append(values[-1])
            disp_dates.append(dates[-1])
        if len(disp) < 2:
            disp       = [values[0], values[-1]]
            disp_dates = [dates[0], dates[-1]]

        # X축 날짜 레이블
        x_ticks = []
        n_ticks = min(6, len(disp_dates))
        t_idx   = [int(i * (len(disp_dates)-1) / max(1, n_ticks-1)) for i in range(n_ticks)]
        seen    = set()
        for idx in t_idx:
            if idx < len(disp_dates):
                d = disp_dates[idx]
                lbl = d[2:7] if len(d) >= 7 else d
                if lbl not in seen:
                    x_ticks.append((idx, lbl))
                    seen.add(lbl)
        self.chart_widget.getAxis('bottom').setTicks([x_ticks])

        col = "#FF4444" if disp[-1] >= disp[0] else "#4444FF"
        self.curve.setPen(pg.mkPen(color=col, width=2))
        self.curve.setData(disp)

        mn, mx = min(values), max(values)
        pad = max(1e-6, (mx - mn) * 0.05) if mx != mn else max(abs(mx) * 0.01, 0.01)
        self.chart_widget.setYRange(mn - pad, mx + pad)
        self.baseline.setPos(float(disp[0]))

        # 최고/최저 마커 — fmt 전달로 소수점 표시
        self._update_chart_markers(disp, values, fmt=fmt)

        c_hex, chg_txt = _change_str(disp[-1], disp[0], key)
        self.change_summary_label.setText(
            f"<span style='color:#aaa;'>{self.current_tf} 기준: </span>"
            f"<span style='color:#fff;'>{fmt(disp[0])}</span>"
            f" → <span style='color:{c_hex};font-weight:bold;'>{fmt(disp[-1])} "
            f"({chg_txt})</span>"
        )

    # ─────────────────────────────────────────────
    # 차트
    # ─────────────────────────────────────────────
    def refresh_chart(self):
        self.chart_widget.getAxis('bottom').setTicks(None)
        # ★ macro 차트 캐시 무효화 — 1일 뷰는 state 직접 읽으므로 제외
        if self.current_tf != "1일":
            self._macro_chart_cache = {}
        # GRI용 + 주식용 마커 모두 제거
        for attr in ['_gri_max_scatter', '_gri_min_scatter', '_gri_max_text', '_gri_min_text',
                     'max_scatter', 'min_scatter', 'max_text', 'min_text']:
            if hasattr(self, attr):
                try: self.chart_widget.removeItem(getattr(self, attr)); delattr(self, attr)
                except: pass

        # ★ 0번 행(지표 행) 선택 시에만 macro 탭 작동
        # 일반 종목 선택 시 macro_key 상태와 무관하게 종목 차트 표시
        if self.selected_stock_name == "GRI":
            key = getattr(self, 'current_macro_key', 'GRI')
            if key == "GRI":
                self._refresh_gri_chart()
            else:
                self._refresh_macro_chart(key)
            return

        s = self._get_selected_stock()
        if not s: return
        name = s['meta']['c_name']
        lim  = self.TF_LIMITS.get(self.current_tf, 1)

        cur_db = self.game_service.db.conn.cursor()
        if lim > 900_000:
            cur_db.execute("SELECT date, price FROM stock_history WHERE company_name=? ORDER BY date ASC", (name,))
        else:
            cur_db.execute("SELECT date, price FROM stock_history WHERE company_name=? ORDER BY date DESC LIMIT ?", (name, lim))
        rows = cur_db.fetchall()
        if not rows: return
        if lim <= 900_000: rows = rows[::-1]

        dates  = [datetime.strptime(r[0], '%Y-%m-%d') for r in rows]
        prices = [float(r[1]) for r in rows]
        cur_p  = float(s['price'])

        if self.current_tf == "1일":
            rat    = float(s.get('rate', 0))
            base_p = cur_p / (1 + rat / 100) if rat != -100 and (1 + rat / 100) != 0 else cur_p
            disp       = [base_p, cur_p]
            disp_dates = dates[-1:] + dates[-1:] if len(dates) >= 1 else [datetime.now(), datetime.now()]
        else:
            base_p = prices[0]
            count  = len(prices)

            # 기간 기반 step — _get_chart_step이 데이터 부족 시 자동 fallback
            step = self._get_chart_step(count, self.current_tf)

            indices = list(range(0, count, step))
            if not indices or indices[-1] != count - 1:
                indices.append(count - 1)
            indices = sorted(set(indices))

            disp       = [prices[i] for i in indices]
            disp_dates = [dates[i]  for i in indices]
            disp[-1]   = cur_p

        # X축 날짜 ticks (setAxisItems 금지 — curve 분리 방지)
        if len(disp_dates) >= 2:
            n_ticks   = min(6, len(disp_dates))
            t_indices = [int(i * (len(disp_dates)-1) / max(1, n_ticks-1)) for i in range(n_ticks)]
            x_ticks   = []
            seen_lbl  = set()
            for idx in t_indices:
                if idx < len(disp_dates):
                    d = disp_dates[idx]
                    label = d.strftime('%y-%m-%d') if hasattr(d, 'strftime') else str(d)[2:10]
                    if label not in seen_lbl:
                        x_ticks.append((idx, label))
                        seen_lbl.add(label)
            self.chart_widget.getAxis('bottom').setTicks([x_ticks])

        smoothed = disp[:]
        diff     = smoothed[-1] - smoothed[0]
        period_r = (diff / base_p * 100) if base_p != 0 else 0
        c_hex = "#FF4444" if diff > 0 else ("#4444FF" if diff < 0 else "#e0e0e0")
        sign  = "▲" if diff > 0 else ("▼" if diff < 0 else "─")

        self.curve.setPen(pg.mkPen(color=c_hex, width=2))
        self.baseline.setPos(base_p)
        self.curve.setData(smoothed)

        raw_for_range = prices if self.current_tf != "1일" else smoothed
        if raw_for_range:
            y_min = min(raw_for_range)
            y_max = max(raw_for_range)
            y_pad = max(1.0, (y_max - y_min) * 0.05) if y_min != y_max else y_min * 0.01
            self.chart_widget.setYRange(y_min - y_pad, y_max + y_pad)

        self._update_chart_markers(smoothed, prices if self.current_tf != "1일" else None)
        self.change_summary_label.setText(
            f"<span style='color:#ffffff;'>{self.current_tf} 기준: </span>"
            f"<span style='color:#aaaaaa;'>{int(base_p):,}원</span>"
            f"<span style='color:#ffffff;'> → </span>"
            f"<span style='color:{c_hex}; font-weight:bold;'>{int(smoothed[-1]):,}원 </span>"
            f"<span style='color:{c_hex};'>({sign}{int(abs(diff)):,}원, {period_r:+.2f}%)</span>"
        )

    def _update_chart_markers(self, smoothed: list, raw_prices: list = None, fmt=None):
        for attr in ['max_scatter', 'min_scatter', 'max_text', 'min_text']:
            if hasattr(self, attr):
                try: self.chart_widget.removeItem(getattr(self, attr))
                except Exception: pass

        if self.current_tf == "1일" or not smoothed or len(smoothed) < 2: return

        # fmt 없으면 주가용 int 포맷 (기존 동작 유지)
        if fmt is None:
            fmt = lambda v: f"{int(v):,}"

        # raw_prices가 있으면 실제 최고/최저 사용, 없으면 smoothed 기준
        if raw_prices and len(raw_prices) > 0:
            real_max = max(raw_prices)
            real_min = min(raw_prices)
            max_idx = min(range(len(smoothed)), key=lambda i: abs(smoothed[i] - real_max))
            min_idx = min(range(len(smoothed)), key=lambda i: abs(smoothed[i] - real_min))
            max_val = real_max
            min_val = real_min
        else:
            max_val = max(smoothed); min_val = min(smoothed)
            max_idx = smoothed.index(max_val); min_idx = smoothed.index(min_val)
        n = len(smoothed)

        self.max_scatter = pg.ScatterPlotItem(size=10, brush=pg.mkBrush('#FF4444'), symbol='o')
        self.max_scatter.addPoints([{'pos': (max_idx, max_val)}])
        self.chart_widget.addItem(self.max_scatter)

        self.min_scatter = pg.ScatterPlotItem(size=10, brush=pg.mkBrush('#4444FF'), symbol='o')
        self.min_scatter.addPoints([{'pos': (min_idx, min_val)}])
        self.chart_widget.addItem(self.min_scatter)

        def get_pos_and_anchor(idx, is_max):
            y_anchor = 1.0 if is_max else 0.0
            if idx >= n // 2:
                return idx, (1.0, y_anchor)
            else:
                return idx, (0.0, y_anchor)

        max_x, max_anchor = get_pos_and_anchor(max_idx, True)
        min_x, min_anchor = get_pos_and_anchor(min_idx, False)

        self.max_text = pg.TextItem(
            html=f"<span style='color:#FF4444;font-weight:bold;background-color:#000;'>최고: {fmt(max_val)}</span>",
            anchor=max_anchor)
        self.max_text.setPos(max_x, max_val)
        self.chart_widget.addItem(self.max_text)

        self.min_text = pg.TextItem(
            html=f"<span style='color:#4444FF;font-weight:bold;background-color:#000;'>최저: {fmt(min_val)}</span>",
            anchor=min_anchor)
        self.min_text.setPos(min_x, min_val)
        self.chart_widget.addItem(self.min_text)
        
    def _update_report(self, stock: dict):
        m         = stock['meta']
        all_snaps = sorted(self.game_service.s.stocks, key=lambda x: x.get('market_cap', 0), reverse=True)
        rank      = next((i + 1 for i, s in enumerate(all_snaps) if s['meta'].get('c_name') == m.get('c_name')), 0)

        # ── HP / Shield ──────────────────────────────────────────
        hp       = m.get('hp', 0.0)
        soft_cap = m.get('hp_soft_cap', 60.0)
        shield   = m.get('shield', 0.0)
        hp_ratio = hp / max(1.0, soft_cap)
        filled   = round(hp_ratio * 10)
        hp_bar   = '■' * filled + '□' * (10 - filled)

        if hp_ratio >= 0.60:   hp_color = '#00FF00'
        elif hp_ratio >= 0.30: hp_color = '#FFA500'
        else:                  hp_color = '#FF4444'

        if   shield >= 1_000_000_000_000: shield_str = f"{shield/1_000_000_000_000:.2f}조"
        elif shield >= 100_000_000:       shield_str = f"{shield/100_000_000:.1f}억"
        else:                              shield_str = f"{shield:,.0f}원"

        shield_active = hp_ratio < 0.30 and shield > 0
        shield_label  = "🛡️ 발동중" if shield_active else "대기중"
        shield_color  = '#00FFFF' if shield_active else '#888888'

        # ── 경고 뱃지 ────────────────────────────────────────────
        warning_info  = self.game_service.s.pending_events.get("warning", {}).get(m['c_name'])
        warning_badge = ""
        char          = str(m.get('char', ''))

        if hp <= 0:
            warning_badge = "<span style='background-color:#FF0000;color:white;padding:2px 6px;border-radius:3px;font-size:13px;margin-left:5px;'>☠️ 상장폐지확정</span>"
        elif char == 'DANGER':
            warning_badge = "<span style='background-color:#FF4400;color:white;padding:2px 6px;border-radius:3px;font-size:13px;font-weight:bold;margin-left:5px;'>🚨 상장폐지위험</span>"
        elif warning_info and warning_info.get('type') in ('IN', 'DANGER'):
            try:
                w_date = warning_info['date']
                if isinstance(w_date, str):
                    from datetime import datetime as _dt
                    w_date = _dt.strptime(w_date, '%Y-%m-%d')
                days_left = max(0, (w_date.date() - self.game_service.s.current_date.date()).days)
            except Exception:
                days_left = 0
            warning_badge = f"<span style='background-color:#FF6600;color:white;padding:2px 6px;border-radius:3px;font-size:13px;margin-left:5px;'>⚠️ 투자경고 지정 예정 (D-{days_left})</span>"
        elif 'WARNING' in char:
            warning_badge = "<span style='background-color:#FFA500;color:black;padding:2px 6px;border-radius:3px;font-size:13px;font-weight:bold;margin-left:5px;'>⚠️ 투자경고</span>"

        vals  = [m.get(k, 0) * 100 for k in ['treasury_share','owner_share','foreign_share','inst_share','retail_share']]
        total = sum(vals)
        ts, os, fs, ins, rs = [(v / total * 100 if total > 0 else v) for v in vals]
        size  = {"대형주": "[대기업]", "중형주": "[중견기업]", "소형주": "[중소기업]"}.get(m.get('tier','소형주'), "[중소기업]")

        def fmt_cap(v):
            if   v >= 1e16:          return f"{v/1e16:.2f}경"
            elif v >= 100 * 1e12:    return f"{v//1e12:,.0f}조"   # 100조 이상 → 1,104조
            elif v >= 10  * 1e12:    return f"{v/1e12:.0f}조"     # 10조~100조
            elif v >= 1e12:          return f"{v/1e12:.1f}조"     # 1조~10조
            elif v >= 1e8:           return f"{v/1e8:.0f}억"
            else:                    return f"{v:,.0f}원"

        mc     = stock['market_cap']
        mc_str = fmt_cap(mc)

        # ── 밸류에이션 지표 계산 ─────────────────────────────────
        name = m.get('c_name', '')
        hist = self.game_service.s.earnings_history.get(name, {})
        all_ni = []; all_op = []; all_rev = []
        for yd in hist.values():
            for qd in yd.values():
                all_ni.append(qd.get('net_income', 0))
                all_op.append(qd.get('op_income', 0))
                all_rev.append(qd.get('revenue', 0))

        recent_ni  = all_ni[-4:]  if len(all_ni)  >= 4 else all_ni
        recent_op  = all_op[-4:]  if len(all_op)  >= 4 else all_op
        recent_rev = all_rev[-4:] if len(all_rev) >= 4 else all_rev
        annual_ni  = sum(recent_ni);  annual_op = sum(recent_op);  annual_rev = sum(recent_rev)
        if 0 < len(recent_ni) < 4:
            f = 4 / len(recent_ni)
            annual_ni *= f; annual_op *= f; annual_rev *= f

        assets = max(1.0, m.get('assets', 1.0))

        # PER
        if annual_ni > 0:
            per_val  = mc / annual_ni
            per_str  = f"{per_val:.1f}배"
            per_icon = "🟢" if per_val < 15 else ("🟡" if per_val < 30 else ("🟠" if per_val < 50 else "🔴"))
        elif annual_ni < 0:
            per_str = "적자"; per_icon = "🔴"
        else:
            per_str = "N/A"; per_icon = "⚪"

        # PBR
        pbr_val  = mc / assets
        # PBR 상한 표시 (비정상 수치 방지)
        if pbr_val > 9999:
            pbr_str = "N/A (데이터 오류)"; pbr_icon = "⚪"
        else:
            pbr_str  = f"{pbr_val:.2f}배"
            pbr_icon = "🟢" if pbr_val < 1 else ("🟡" if pbr_val < 3 else ("🟠" if pbr_val < 5 else "🔴"))

        # ROE
        roe_val  = (annual_ni / assets * 100) if assets > 0 else 0.0
        roe_str  = f"{roe_val:.1f}%"
        roe_icon = "🟢" if roe_val >= 15 else ("🟡" if roe_val >= 8 else ("🟠" if roe_val >= 0 else "🔴"))

        # 영업이익률
        if annual_rev > 0:
            op_margin = annual_op / annual_rev * 100
            op_str    = f"{op_margin:.1f}%"
            op_icon   = "🟢" if op_margin >= 15 else ("🟡" if op_margin >= 5 else ("🟠" if op_margin >= 0 else "🔴"))
        else:
            op_str = "N/A"; op_icon = "⚪"

        # 부채비율
        debt_ratio = m.get('debt_ratio', None)
        if debt_ratio is not None:
            dr_pct  = debt_ratio * 100
            dr_str  = f"{dr_pct:.1f}%"
            dr_icon = "🟢" if dr_pct < 50 else ("🟡" if dr_pct < 100 else ("🟠" if dr_pct < 200 else "🔴"))
        else:
            dr_str = "N/A"; dr_icon = "⚪"

        # 신용등급
        credit       = m.get('credit_grade', 'N/A')
        credit_color = {'AA': '#00FF00', 'BB': '#FFA500', 'CCC': '#FF4444'}.get(credit, '#888888')

        # 52주 신고가/신저가
        high_52w   = m.get('price_52w_high', stock['price'])
        low_52w    = m.get('price_52w_low',  stock['price'])
        cur_p      = stock['price']
        high_badge = " <b style='color:#FF4444;'>★신고가</b>" if cur_p >= high_52w * 0.999 else ""
        low_badge  = " <b style='color:#4444FF;'>★신저가</b>" if cur_p <= low_52w  * 1.001 else ""

        # 버핏 지수
        buffett      = getattr(self.game_service.s, 'buffett_index', 0.0)
        buffett_str  = f"{buffett:.1f}%"
        buffett_icon = "🟢 저평가" if buffett < 80 else ("🟡 적정" if buffett < 100 else ("🟠 고평가" if buffett < 130 else "🔴 버블"))

        # 스크롤 위치 저장 — setHtml은 Qt가 맨 위로 리셋하므로 복원 필요 O(1)
        _sb = self.report_panel.verticalScrollBar()
        _prev_scroll = _sb.value()
        self.report_panel.setHtml(f"""
        <div style='font-family: Malgun Gothic;'>
            <h2 style='color:#00FF00;margin-bottom:0px;'>
                <span style='color:#FFD700;font-size:18px;'>[{rank}위]</span> &lt; {m['c_name']} &gt; {warning_badge}
            </h2>
            <p style='color:#888;font-size:11px;margin-top:5px;'>상장일: {m['listed_date']} | 섹터: {SECTOR_MAP.get(m['ind'],'Value')}</p>
            <hr style='border: 0.5px solid #333;'/>
            <p style='font-size:13px;'><b>[기업 정보]</b><br/>
            그룹: {m.get('group','단독기업')}<br/>
            규모: <b style='color:#FFD700;'>{size}</b><br/>
            산업: {m['ind']} ({m['sub']})<br/>
            {f"<span style='color:#00BFFF;font-size:12px;'>📋 사업: {' / '.join(m.get('sub_list', [m['sub']]))}</span><br/>" if len(m.get('sub_list', [])) > 1 else ""}
            상태: <b style='color:#00FF00;'>{char}</b></p>
            <p style='font-size:13px;'><b>[발행 정보]</b><br/>
            주식수: {stock['shares']:,} 주<br/>
            <span style='color:{shield_color};'>방어막 {shield_str} [{shield_label}]</span><br/>
            시총: {mc:,} 원 <span style='color:#FFD700;font-weight:bold;'>({mc_str})</span><br/>
            <span style='color:#AAAAAA;'>호가단위: {_get_tick(int(stock["price"])):,}원 &nbsp;|&nbsp; 액면가: {m.get("par_value", 500):,}원 &nbsp;|&nbsp; 분할: {m.get("split_count", 0)}회</span></p>
            <hr style='border: 0.5px solid #333;'/>
            <p style='font-size:13px;'><b>[지배구조]</b><br/>
            자사주: {ts:.2f}% | 대주주: {os:.2f}%<br/>
            외국인: {fs:.2f}% | 기관 : {ins:.2f}%<br/>
            개인 : {rs:.2f}%</p>
            <hr style='border: 0.5px solid #333;'/>
            <p style='font-size:14px;'><b>[재무 체력]</b></p>
            <p style='font-size:20px;font-family:monospace;letter-spacing:2px;margin:4px 0;'>
            <span style='color:{hp_color};'>{hp_bar}</span></p>
            <p style='font-size:15px;font-weight:bold;margin:4px 0;'>
            <span style='color:{hp_color};'>HP {hp:.2f} / {soft_cap:.0f}</span>
            <span style='color:#888;font-size:13px;'> ({hp_ratio*100:.1f}%)</span></p>
            <hr style='border: 0.5px solid #333;'/>
            <p style='font-size:13px;'><b>[밸류에이션]</b><br/>
            PER&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: {per_icon} {per_str}<br/>
            PBR&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: {pbr_icon} {pbr_str}<br/>
            ROE&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;: {roe_icon} {roe_str}<br/>
            영업이익률: {op_icon} {op_str}<br/>
            부채비율&nbsp;&nbsp;: {dr_icon} {dr_str}<br/>
            신용등급&nbsp;&nbsp;: <b style='color:{credit_color};'>{credit}</b></p>
            <hr style='border: 0.5px solid #333;'/>
            <p style='font-size:13px;'><b>[시장 지표]</b><br/>
            52주 신고가: {int(high_52w):,}원{high_badge}<br/>
            52주 신저가: {int(low_52w):,}원{low_badge}<br/>
            버핏 지수&nbsp;&nbsp;: {buffett_str} {buffett_icon}</p>
        </div>""")
        _sb.setValue(_prev_scroll)  # 스크롤 위치 복원 O(1)

    # ─────────────────────────────────────────────
    # 헬퍼
    # ─────────────────────────────────────────────
    def _add_recent_stock(self, name: str):
        if name in self.recent_stocks:
            self.recent_stocks.remove(name)
        self.recent_stocks.insert(0, name)
        self.recent_stocks = self.recent_stocks[:5]
        self._refresh_recent_btns()

    def _refresh_top_stocks(self):
        """오늘/전체 모드에 따라 상위 5개 종목 버튼 갱신"""
        stocks = self.game_service.s.stocks
        if not stocks:
            for btn in self.top_stock_btns:
                btn.setVisible(False)
            return

        if self._top_mode == "오늘":
            ranked = sorted(stocks, key=lambda x: x.get('rate', 0.0), reverse=True)[:5]
            def label(s):
                r = s.get('rate', 0.0)
                col = "#FF4444" if r >= 0 else "#4488FF"
                return s['meta']['c_name'], f"{r:+.1f}%", col
        else:
            db = self.game_service.db

            def get_first_price(name):
                """차트 전체 기준과 완전히 동일한 방식으로 첫 가격 조회"""
                try:
                    cur = db.conn.cursor()
                    cur.execute(
                        "SELECT price FROM stock_history WHERE company_name=? ORDER BY date ASC LIMIT 1",
                        (name,)
                    )
                    row = cur.fetchone()
                    return float(row[0]) if row else 0.0
                except Exception:
                    return 0.0

            # ★ 캐시 없이 매번 정확히 계산 (전체 버튼 클릭 시만 실행)
            cumul_map = {}
            for s in stocks:
                name = s['meta']['c_name']
                p    = s.get('price', 0)
                first = get_first_price(name)
                # DB에 기록 없으면 initial_price 폴백
                if first <= 0:
                    first = s['meta'].get('initial_price', 0)
                cumul_map[name] = ((p / first) - 1) * 100 if first > 0 else 0.0

            ranked = sorted(stocks, key=lambda s: cumul_map[s['meta']['c_name']], reverse=True)[:5]

            def label(s):
                c = cumul_map[s['meta']['c_name']]
                col = "#FF4444" if c >= 0 else "#4488FF"
                return s['meta']['c_name'], f"{c:+.0f}%", col

            ranked = sorted(stocks, key=lambda s: cumul_map[s['meta']['c_name']], reverse=True)[:5]

            def label(s):
                c = cumul_map[s['meta']['c_name']]
                col = "#FF4444" if c >= 0 else "#4488FF"
                return s['meta']['c_name'], f"{c:+.0f}%", col

        self._top_stock_names = [s['meta']['c_name'] for s in ranked]

        _btn_style = """
            QPushButton {{
                background: #0d1a0d; color: {col};
                border: 1px solid #1a3a1a; padding: 2px 8px;
                border-radius: 3px; font-size: 12px; font-weight: bold;
            }}
            QPushButton:hover {{ border: 1px solid #555; }}
        """
        for i, btn in enumerate(self.top_stock_btns):
            if i < len(ranked):
                name, pct, col = label(ranked[i])
                btn.setText(f"{name} {pct}")
                # ★ hover 시 색상 변경 없음 (클릭해도 형광색 안 됨)
                btn.setStyleSheet(_btn_style.format(col=col))
                btn.setVisible(True)
            else:
                btn.setVisible(False)

    def _on_top_stock_clicked(self, idx: int):
        """상위 종목 버튼 클릭 → 종목 선택 + 차트 이동"""
        names = getattr(self, '_top_stock_names', [])
        if idx < len(names):
            name = names[idx]
            self.selected_stock_name = name
            self._add_recent_stock(name)
            # 테이블에서 해당 종목으로 스크롤
            for i in range(self.stock_table.rowCount()):
                it = self.stock_table.item(i, 1)
                if it:
                    raw = it.text()
                    for prefix in ["☠️ ", "🚨 ", "⚠️ ", "💀 "]:
                        if raw.startswith(prefix): raw = raw[len(prefix):]; break
                    if raw == name:
                        self.stock_table.setCurrentCell(i, 1)
                        self.stock_table.scrollToItem(it)
                        break
            self.sync_ui_with_engine()

    def _refresh_recent_btns(self):
        for i, btn in enumerate(self.recent_btns):
            if i < len(self.recent_stocks):
                btn.setText(self.recent_stocks[i])
                btn.setVisible(True)
            else:
                btn.setVisible(False)

    def _on_recent_clicked(self, idx: int):
        if idx < len(self.recent_stocks):
            name = self.recent_stocks[idx]
            self.selected_stock_name = name
            self.search_bar.clear()
            for i in range(self.stock_table.rowCount()):
                it = self.stock_table.item(i, 1)
                if it:
                    raw = it.text()
                    for prefix in ["☠️ ", "🚨 ", "⚠️ ", "💀 "]:
                        if raw.startswith(prefix): raw = raw[len(prefix):]; break
                    if raw == name:
                        self.stock_table.setCurrentCell(i, 1)
                        break
            self.sync_ui_with_engine()

    def _get_selected_stock(self) -> dict | None:
        return self.game_service.get_stock_by_name(self.selected_stock_name)

    @staticmethod
    def _moving_avg(data: list, window: int = 3) -> list:
        return [sum(data[max(0, i - window + 1):i + 1]) / len(data[max(0, i - window + 1):i + 1]) for i in range(len(data))]


# ─────────────────────────────────────────────
# InfoTableDialog (상장폐지 역사관)
# ─────────────────────────────────────────────
class InfoTableDialog(QDialog):
    def __init__(self, title: str, headers: list, game_service, parent=None):
        super().__init__(parent)
        self.gs        = game_service
        self.all_data  = []   # 전체 데이터 캐시
        self.setWindowTitle(title)
        self.resize(1300, 750)
        self.setStyleSheet(HTS_STYLE)

        layout = QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # ── 상단: 총 개수 + 검색창 + 필터 ───────────────────────
        top_bar = QHBoxLayout()

        # 총 개수 라벨
        self.count_label = QLabel("총 0개")
        self.count_label.setStyleSheet(
            "color: #FF4444; font-weight: bold; font-size: 13px; "
            "background: #1a0000; border: 1px solid #FF4444; "
            "padding: 4px 10px; border-radius: 3px;"
        )
        self.count_label.setFixedWidth(90)

        # 필터 콤보박스
        from PyQt6.QtWidgets import QComboBox
        self.filter_combo = QComboBox()
        self.filter_combo.addItems([
            "전체 검색", "회사명", "그룹명", "산업", "섹터", "생애주기", "등급"
        ])
        self.filter_combo.setStyleSheet(
            "QComboBox { background:#111; color:#eee; border:1px solid #444; "
            "padding:4px; border-radius:3px; min-width:100px; }"
            "QComboBox QAbstractItemView { background:#111; color:#eee; "
            "selection-background-color:#1a331a; }"
        )
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)

        # 검색 입력창
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 검색어를 입력하세요...")
        self.search_input.setStyleSheet(
            "QLineEdit { background:#000; color:#00FF00; border:1px solid #00FF00; "
            "padding:5px; border-radius:3px; font-size:13px; }"
        )
        self.search_input.textChanged.connect(self._apply_filter)

        # 초기화 버튼
        btn_clear = QPushButton("초기화")
        btn_clear.setFixedWidth(70)
        btn_clear.setStyleSheet(
            "QPushButton { background:#222; color:#aaa; border:1px solid #444; "
            "padding:5px; border-radius:3px; }"
            "QPushButton:hover { background:#333; color:#fff; }"
        )
        btn_clear.clicked.connect(lambda: self.search_input.clear())

        top_bar.addWidget(self.count_label)
        top_bar.addWidget(self.filter_combo)
        top_bar.addWidget(self.search_input)
        top_bar.addWidget(btn_clear)
        layout.addLayout(top_bar)

        # ── 테이블 ────────────────────────────────────────────────
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #000; color: #e0e0e0; gridline-color: #222; } "
            "QHeaderView::section { background-color: #222; color: #00FF00; }"
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table)
        self.setLayout(layout)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.refresh_data()

    def refresh_data(self):
        """전체 데이터 로드 및 캐시"""
        delisted    = self.gs.get_delisted_stocks()
        total_count = len(delisted)

        # 총 개수 업데이트
        self.count_label.setText(f"총 {total_count:,}개")

        # 데이터 캐시 (역순: 최근 상폐가 위로)
        self.all_data = []
        for i, st in enumerate(reversed(delisted)):
            m    = st['meta']
            tier = m.get('tier', '소형주')
            tier_map = {'대형주': '[대]', '중형주': '[중]', '소형주': '[소]'}
            tier_str = tier_map.get(tier, '[소]')
            self.all_data.append({
                'no':        total_count - i,
                'lifecycle': f"{m.get('listed_date', '-')} ~ {m.get('delisted_date', '-')}",
                'tier':      tier_str,
                'status':    '[DELISTED]',
                'group':     m.get('group', '독립'),
                'sector':    m.get('ind', '-'),
                'name':      m['c_name'],
                'sub':       f"{m['ind']}({m['sub']})",
                'price':     f"{int(st['price']):,}원",
                'shares':    f"{st['shares']:,}주",
                'treasury':  f"{m.get('treasury_share', 0)*100:.1f}%",
                '_stock':    st,
            })

        self._apply_filter()

    def _apply_filter(self):
        """검색 필터 적용"""
        keyword = self.search_input.text().lower().strip()
        idx     = self.filter_combo.currentIndex()

        filtered = []
        for row in self.all_data:
            if not keyword:
                filtered.append(row)
                continue

            if   idx == 0:  # 전체
                target = f"{row['name']} {row['group']} {row['sector']} {row['sub']} {row['lifecycle']}"
            elif idx == 1:  # 회사명
                target = row['name']
            elif idx == 2:  # 그룹명
                target = row['group']
            elif idx == 3:  # 산업
                target = row['sector']
            elif idx == 4:  # 섹터
                target = row['sub']
            elif idx == 5:  # 생애주기
                target = row['lifecycle']
            elif idx == 6:  # 등급
                target = row['tier']
            else:
                target = str(row)

            if keyword in target.lower():
                filtered.append(row)

        self.table.setRowCount(len(filtered))
        for i, row in enumerate(filtered):
            vals = [
                row['no'], row['lifecycle'], row['tier'], row['status'],
                row['group'], row['sector'], row['name'], row['sub'],
                row['price'], row['shares'], row['treasury'],
            ]
            for j, val in enumerate(vals):
                it = QTableWidgetItem(str(val))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                it.setData(Qt.ItemDataRole.UserRole, row['_stock'])
                if '[DELISTED]' in str(val):
                    it.setForeground(QColor("#FF4444"))
                elif '[대]' in str(val):
                    it.setForeground(QColor("#FFD700"))
                elif '[중]' in str(val):
                    it.setForeground(QColor("#00AAFF"))
                self.table.setItem(i, j, it)

        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.resizeColumnsToContents()
        for col in range(self.table.columnCount()):
            self.table.setColumnWidth(col, self.table.columnWidth(col) + 15)

    def closeEvent(self, event):
        parent = self.parent()
        if parent and hasattr(parent, 'active_dialogs') and self in parent.active_dialogs:
            parent.active_dialogs.remove(self)
        event.accept()

    def _on_cell_clicked(self, row, col):
        it = self.table.item(row, 0)
        if it:
            stock_obj = it.data(Qt.ItemDataRole.UserRole)
            if stock_obj:
                d = DelistedDetailDialog(stock_obj, self.gs, self)
                d.show()