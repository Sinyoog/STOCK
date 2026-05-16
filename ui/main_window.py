"""
ui/main_window.py
StockHTS: 메인 HTS 프레임.
엔진 데이터를 직접 건드리지 않고 game_service만 호출합니다.
"""
import math
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
    SystemMenuDialog, CustomConfirmDialog
)
from .group_view import GroupInfoDialog
from .news_view  import NewsWindow
from engine.constants import SECTOR_MAP


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

        self._init_ui()
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

        self.btn_system = QPushButton("✕")
        self.btn_system.setFixedSize(35, 35)
        self.btn_system.setStyleSheet(f"""
            QPushButton {{ background: #222; color: white; font-size: 18px; border-radius: 5px;
                font-weight: bold; border: 1px solid #444; }}
            QPushButton:hover {{ background: {COLOR['accent_red']}; border: 1px solid {COLOR['accent_red']}; }}
        """)
        self.btn_system.clicked.connect(self.open_system_menu)
        top_right.addWidget(self.asset_label)
        top_right.addWidget(self.btn_system)

        row1.addWidget(self.date_label)
        row1.addWidget(self.index_label, stretch=1)
        row1.addLayout(top_right)
        dash_lay.addLayout(row1)

        self.macro_label     = QLabel()
        self.macro_label.setStyleSheet("font-size: 14px; color: #00BFFF;")
        self.inflation_label = QLabel()
        self.inflation_label.setStyleSheet("font-size: 14px; color: #FFA500; font-weight: bold;")
        dash_lay.addWidget(self.macro_label)
        dash_lay.addWidget(self.inflation_label)
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

        self.stock_table = QTableWidget(0, 3)
        self.stock_table.setHorizontalHeaderLabels(["종목명", "현재가", "등락율"])
        self.stock_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.stock_table.cellClicked.connect(self.on_stock_clicked)
        left_panel.addWidget(self.stock_table)
        content_lay.addLayout(left_panel, 2)

        # 중앙 차트
        chart_lay = QVBoxLayout()
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
        self.holding_table.setFixedHeight(50)
        self.holding_table.setHorizontalHeaderLabels(["평균단가", "보유수량", "수익률", "총 금액"])
        self.holding_table.setStyleSheet("QTableWidget { background-color: #111; border: 1px solid #333; gridline-color: #222; } QHeaderView::section { background-color: #222; color: #aaa; font-size: 11px; }")
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
        rep_lay.addWidget(btn_earn)
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

        # 금요일 자동 저장
        if s.virtual_weekday == 4:
            self.game_service.save_game(self.my_cash, self.my_portfolio)

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
                if cn == "GroupInfoDialog":   dialog.update_all_info()
                elif cn == "InfoTableDialog": dialog.refresh_data()
                elif cn == "EarningsDialog":  dialog.load_cur()
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

    def handle_sell(self):
        s = self._get_selected_stock()
        if not s: return
        name = s['meta']['c_name']
        if name not in self.my_portfolio:
            QMessageBox.warning(self, "보유량 부족", "팔 주식이 없습니다."); return
        dialog = TradeDialog("매도", name, s['price'], self.my_portfolio[name].get('shares', self.my_portfolio[name].get('quantity', 0)), self)
        dialog.show()
        self.active_dialogs.append(dialog)

    def process_buy(self, name: str, price: int, num: int):
        ok, new_cash, new_port, msg = self.game_service.buy_stock(name, price, num, self.my_cash, self.my_portfolio)
        if not ok:
            QMessageBox.warning(self, "매수 실패", msg); return
        self.my_cash      = new_cash
        self.my_portfolio = new_port
        self.sync_ui_with_engine()
        self.game_service.save_game(self.my_cash, self.my_portfolio)

    def process_sell(self, name: str, price: int, num: int):
        ok, new_cash, new_port, msg = self.game_service.sell_stock(name, price, num, self.my_cash, self.my_portfolio)
        if not ok:
            QMessageBox.warning(self, "매도 실패", msg); return
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
        existing = next((d for d in self.active_dialogs if isinstance(d, InfoTableDialog)), None)
        if existing:
            existing.raise_(); existing.activateWindow(); return
        headers = ["No", "생애 주기", "등급", "상태", "그룹", "섹터", "회사명", "산업분류", "마지막 주가", "발행주수", "자사주 %"]
        d = InfoTableDialog("💀 상장폐지 역사관", headers, self.game_service, self)
        d.setModal(False); d.setWindowFlags(Qt.WindowType.Window); d.show()
        self.active_dialogs.append(d)

    def open_earnings_window(self):
        if self.selected_stock_name:
            d = EarningsDialog(self.selected_stock_name, self.game_service, self)
            d.show(); self.active_dialogs.append(d)

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
        existing = next((d for d in self.active_dialogs if isinstance(d, StockFilterDialog)), None)
        if existing:
            existing.raise_(); existing.activateWindow()
        else:
            d = StockFilterDialog(self)
            self.active_dialogs.append(d)
            d.show()

    def open_system_menu(self):
        SystemMenuDialog(self).exec()

    # ─────────────────────────────────────────────
    # 저장/초기화
    # ─────────────────────────────────────────────
    def save_and_exit(self):
        self.game_service.save_game(self.my_cash, self.my_portfolio)
        from PyQt6.QtWidgets import QApplication
        QApplication.quit()

    def reset_game_logic(self):
        if hasattr(self, 'news_window') and self.news_window:
            try:
                self.news_window.hts = None
                if self.news_window in self.active_dialogs:
                    self.active_dialogs.remove(self.news_window)
                self.news_window.close()
                self.news_window.deleteLater()
            except Exception:
                pass
        self.news_window = None

        self.game_service.reset_game(self.my_cash, self.my_portfolio)
        self.my_cash      = 1_000_000
        self.my_portfolio = {}
        self.selected_stock_name = ""

        self.report_panel.clear()
        self.curve.setData([])
        for d in self.active_dialogs[:]:
            try: d.close()
            except Exception: pass
        self.active_dialogs.clear()
        self.sync_ui_with_engine()

    # ─────────────────────────────────────────────
    # UI 동기화
    # ─────────────────────────────────────────────
    def sync_ui_with_engine(self):
        s = self.game_service.s
        if s.virtual_weekday <= 4:
            s.is_market_open = True
        is_open = s.is_market_open
        self.btn_buy.setEnabled(is_open)
        self.btn_sell.setEnabled(is_open)
        market_status = " [영업 중]" if is_open else " [장마감 - 휴장]"

        total_buy = total_eval = 0
        for name, data in self.my_portfolio.items():
            stock = self.game_service.get_stock_by_name(name)
            cur_p = stock['price'] if stock else 0
            qty = data.get('shares', data.get('quantity', 0))
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
        self.index_label.setText(f"📊 GRI: {s.gri:,.2f} | WSI: {s.wsi:,.2f} | LV.{s.max_tech_reached}")
        self.macro_label.setText(f"🌍 금리: {m['interest_rate']:.2f}% | 유가: ${m['oil_price']:.2f} | 물가: {m['cpi']:.2f}% | 환율: ₩{m['exchange_rate']:,.1f}")
        self.inflation_label.setText(f"🛍️ 물가체감: 2000년 ₩1,000 → 현재 ₩{s.base_item_price:,.0f}")

        self.filter_stocks()
        self.refresh_chart()

        selected = self._get_selected_stock()
        if selected:
            self._update_report(selected)

        port_info = self.my_portfolio.get(self.selected_stock_name)
        if port_info and selected:
            shares    = port_info.get('shares', port_info.get('quantity', 0))
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
        query  = self.search_bar.text().strip().lower()
        snaps  = [
            {"name": s['meta']['c_name'], "price": int(s['price']),
             "rate": s.get('rate', 0.0), "meta": s['meta'],
             "shares": s['shares']}
            for s in self.game_service.s.stocks
        ]
        filtered = []
        f_dialog = next((d for d in self.active_dialogs if isinstance(d, StockFilterDialog)), None)

        for snap in snaps:
            if f_dialog and f_dialog.isVisible():
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
                        f_dialog.sec_theme.isChecked()     and s_name == "Theme",
                    ]): continue

            if query not in snap['name'].lower(): continue
            filtered.append(snap)

        self._update_table(filtered)
        if len(filtered) == 1:
            self.selected_stock_name = filtered[0]['name']

    def _update_table(self, snaps: list):
        self.stock_table.setRowCount(len(snaps))
        for i, st in enumerate(snaps):
            col = rate_color(st['rate'])
            n_it = QTableWidgetItem(st['name'])
            p_it = QTableWidgetItem(f"{int(st['price']):,}원")
            r_it = QTableWidgetItem(f"{st['rate']:+.2f}%")
            if st['name'] == self.selected_stock_name:
                n_it.setForeground(QColor("#00FF00"))
                n_it.setFont(QFont("Malgun Gothic", 10, QFont.Weight.Bold))
            p_it.setForeground(QColor(col)); r_it.setForeground(QColor(col))
            self.stock_table.setItem(i, 0, n_it)
            self.stock_table.setItem(i, 1, p_it)
            self.stock_table.setItem(i, 2, r_it)

    def on_stock_clicked(self, r, c):
        it = self.stock_table.item(r, 0)
        if it:
            self.selected_stock_name = it.text()
            self.sync_ui_with_engine()

    def scroll_to_selected(self):
        for i in range(self.stock_table.rowCount()):
            it = self.stock_table.item(i, 0)
            if it and it.text() == self.selected_stock_name:
                self.stock_table.scrollToItem(it, QTableWidget.ScrollHint.PositionAtCenter)
                break

    def change_tf(self, tf: str):
        self.current_tf = tf
        for n, btn in self.tabs.items(): btn.setChecked(n == tf)
        self.refresh_chart()

    # ─────────────────────────────────────────────
    # 차트
    # ─────────────────────────────────────────────
    def refresh_chart(self):
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

        self.chart_widget.setAxisItems({'bottom': DateAxisItem(dates=dates, orientation='bottom')})

        cur_p = float(s['price'])
        if self.current_tf == "1일":
            rat = float(s.get('rate', 0))
            base_p = cur_p / (1 + rat / 100) if rat != -100 and (1 + rat / 100) != 0 else cur_p
            disp   = [base_p, cur_p]
        else:
            base_p = prices[0]
            count  = len(prices)
            step   = 1 if count < 180 else (7 if count < 1095 else (30 if count < 3650 else (180 if count < 14600 else 365)))
            disp   = [prices[i] for i in range(0, count, step)]
            if (count - 1) % step != 0: disp.append(prices[-1])

        smoothed = self._moving_avg(disp)
        diff     = (smoothed[-1] if smoothed else cur_p) - (smoothed[0] if smoothed else base_p)
        period_r = (diff / base_p * 100) if base_p != 0 else 0

        c_hex = "#FF4444" if diff > 0 else ("#4444FF" if diff < 0 else "#e0e0e0")
        sign  = "▲" if diff > 0 else ("▼" if diff < 0 else "─")

        self.curve.setPen(pg.mkPen(color=c_hex, width=2))
        self.baseline.setPos(base_p)
        self.curve.setData(smoothed)

        if smoothed:
            y_min, y_max = min(smoothed), max(smoothed)
            y_range = max(1.0, y_min * 0.01) if y_min == y_max else 0
            self.chart_widget.setYRange(y_min - y_range, y_max + y_range)

        self._update_chart_markers(smoothed)
        self.change_summary_label.setText(
            f"<span style='color:#ffffff;'>{self.current_tf} 기준: </span>"
            f"<span style='color:#aaaaaa;'>{int(base_p):,}원</span>"
            f"<span style='color:#ffffff;'> → </span>"
            f"<span style='color:{c_hex}; font-weight:bold;'>{int(smoothed[-1] if smoothed else cur_p):,}원 </span>"
            f"<span style='color:{c_hex};'>({sign}{int(abs(diff)):,}원, {period_r:+.2f}%)</span>"
        )

    def _update_chart_markers(self, smoothed: list):
        for attr in ['max_scatter', 'min_scatter', 'max_text', 'min_text']:
            if hasattr(self, attr):
                try: self.chart_widget.removeItem(getattr(self, attr))
                except Exception: pass

        if self.current_tf == "1일" or not smoothed: return

        max_val = max(smoothed); min_val = min(smoothed)
        max_idx = smoothed.index(max_val); min_idx = smoothed.index(min_val)
        n = len(smoothed)

        self.max_scatter = pg.ScatterPlotItem(size=10, brush=pg.mkBrush('#FF4444'), symbol='o')
        self.max_scatter.addPoints([{'pos': (max_idx, max_val)}])
        self.chart_widget.addItem(self.max_scatter)

        self.min_scatter = pg.ScatterPlotItem(size=10, brush=pg.mkBrush('#4444FF'), symbol='o')
        self.min_scatter.addPoints([{'pos': (min_idx, min_val)}])
        self.chart_widget.addItem(self.min_scatter)

        max_anchor = (1.1, 1.1) if max_idx > n * 0.75 else (0, 1)
        self.max_text = pg.TextItem(html=f"<span style='color: #FF4444; font-weight: bold; background-color: #000;'>최고: {int(max_val):,}</span>", anchor=max_anchor)
        self.max_text.setPos(max_idx, max_val); self.chart_widget.addItem(self.max_text)

        min_anchor = (1.1, -0.1) if min_idx > n * 0.75 else ((-0.1, -0.1) if min_idx < n * 0.25 else (0, 0))
        self.min_text = pg.TextItem(html=f"<span style='color: #4444FF; font-weight: bold; background-color: #000;'>최저: {int(min_val):,}</span>", anchor=min_anchor)
        self.min_text.setPos(min_idx, min_val); self.chart_widget.addItem(self.min_text)

    # ─────────────────────────────────────────────
    # 리포트
    # ─────────────────────────────────────────────
    def _update_report(self, stock: dict):
        m     = stock['meta']
        all_snaps = sorted(self.game_service.s.stocks, key=lambda x: x.get('market_cap', 0), reverse=True)
        rank  = next((i + 1 for i, s in enumerate(all_snaps) if s['meta'].get('c_name') == m.get('c_name')), 0)

        warning_badge = ""
        if "WARNING" in str(m.get('char', '')):
            if m.get('risk_score', 0) >= 100:
                warning_badge = "<span style='background-color:#FF0000;color:white;padding:2px 6px;border-radius:3px;font-size:13px;margin-left:5px;'>🚨 상장폐지위험</span>"
            else:
                warning_badge = "<span style='background-color:#FFA500;color:black;padding:2px 6px;border-radius:3px;font-size:13px;font-weight:bold;margin-left:5px;'>⚠️ 투자경고</span>"

        vals  = [m.get(k, 0) * 100 for k in ['treasury_share','owner_share','foreign_share','inst_share','retail_share']]
        total = sum(vals)
        ts, os, fs, ins, rs = [(v / total * 100 if total > 0 else v) for v in vals]
        size  = {"대형주": "[대기업]", "중형주": "[중견기업]", "소형주": "[중소기업]"}.get(m.get('tier','소형주'), "[중소기업]")

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
            상태: <b style='color:#00FF00;'>{m['char']}</b></p>
            <p style='font-size:13px;'><b>[발행 정보]</b><br/>
            주식수: {stock['shares']:,} 주<br/>시총: {stock['market_cap']:,} 원</p>
            <hr style='border: 0.5px solid #333;'/>
            <p style='font-size:13px;'><b>[지배구조]</b><br/>
            자사주: {ts:.1f}% | 대주주: {os:.1f}%<br/>
            외국인: {fs:.1f}% | 기관 : {ins:.1f}%<br/>
            개인 : {rs:.1f}%</p>
            <p style='color:#FF4444;font-size:14px;'><b>리스크: {m['risk_score']:.2f} / 150</b></p>
        </div>""")

    # ─────────────────────────────────────────────
    # 헬퍼
    # ─────────────────────────────────────────────
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
        self.gs = game_service
        self.setWindowTitle(title)
        self.resize(1300, 700)
        self.setStyleSheet(HTS_STYLE)

        layout = QVBoxLayout()
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #000; color: #e0e0e0; gridline-color: #222; } "
            "QHeaderView::section { background-color: #222; color: #00FF00; }"
        )
        layout.addWidget(self.table)
        self.setLayout(layout)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.refresh_data()

    def refresh_data(self):
        delisted = self.gs.s.delisted_stocks
        self.table.setRowCount(len(delisted))
        for i, st in enumerate(delisted):
            m   = st['meta']
            row = [
                i + 1,
                f"{m.get('listed_date')}~{m.get('delisted_date')}",
                "[소]", "[DELISTED]",
                m.get('group', '-'), m['ind'], m['c_name'],
                f"{m['ind']}({m['sub']})",
                f"{int(st['price']):,}원",
                f"{st['shares']:,}주",
                f"{m.get('treasury_share', 0)*100:.1f}%",
            ]
            for j, val in enumerate(row):
                it = QTableWidgetItem(str(val))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if "[DELISTED]" in str(val): it.setForeground(QColor("#FF4444"))
                self.table.setItem(i, j, it)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.resizeColumnsToContents()

    def _on_cell_clicked(self, row, col):
        it = self.table.item(row, 6)
        if it:
            name = it.text()
            stock_obj = next((s for s in self.gs.s.delisted_stocks if s['meta']['c_name'] == name), None)
            if stock_obj:
                d = DelistedDetailDialog(stock_obj, self.gs, self)
                d.show()