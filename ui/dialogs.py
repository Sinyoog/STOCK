"""
ui/dialogs.py
각종 다이얼로그 창들 (데이터 연산 금지 - GameService 호출만 허용).
"""
import math
from datetime import datetime
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QPushButton, QLineEdit,
    QTextEdit, QButtonGroup, QApplication, QWidget
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont

from .styles import HTS_STYLE, COLOR


# ─────────────────────────────────────────────
# CustomConfirmDialog
# ─────────────────────────────────────────────
class CustomConfirmDialog(QDialog):
    def __init__(self, title: str, message: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedSize(450, 220)
        self.setStyleSheet(HTS_STYLE)

        layout = QVBoxLayout()
        layout.setContentsMargins(30, 30, 30, 30)

        header = QLabel("⚠️ SYSTEM ALERT")
        header.setObjectName("warning_header")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStyleSheet("color: #FF4444; font-size: 18px; font-weight: bold;")
        layout.addWidget(header)

        msg = QLabel(message)
        msg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        msg.setWordWrap(True)
        layout.addWidget(msg)

        layout.addSpacing(20)
        btn_lay = QHBoxLayout()
        self.btn_yes = QPushButton("CONFIRM (초기화)")
        self.btn_no  = QPushButton("CANCEL (취소)")
        btn_lay.addStretch()
        btn_lay.addWidget(self.btn_yes)
        btn_lay.addSpacing(10)
        btn_lay.addWidget(self.btn_no)
        btn_lay.addStretch()
        layout.addLayout(btn_lay)
        self.setLayout(layout)

        self.btn_yes.clicked.connect(self.accept)
        self.btn_no.clicked.connect(self.reject)


# ─────────────────────────────────────────────
# SystemMenuDialog
# ─────────────────────────────────────────────
class SystemMenuDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hts = parent
        self.setWindowTitle("시스템 관리")
        self.setFixedSize(320, 280)
        self.setStyleSheet("background-color: #1a1a1a; color: white; font-family: 'Malgun Gothic';")

        layout = QVBoxLayout()
        btn_style = """
            QPushButton { background: #333; color: white; padding: 12px;
                font-weight: bold; border-radius: 5px; border: 1px solid #444; }
            QPushButton:hover { background: #444; border: 1px solid #666; }
        """

        self.btn_cheat = QPushButton("🛠️ 디버그 콘솔 (치트)")
        self.btn_cheat.setStyleSheet(btn_style + "QPushButton { color: #00FF00; border: 1px solid #00FF00; }")
        self.btn_cheat.clicked.connect(self._open_cheat)

        self.btn_reset = QPushButton("데이터 초기화")
        self.btn_reset.setStyleSheet(btn_style + "QPushButton { color: #FF4444; }")
        self.btn_reset.clicked.connect(self._trigger_reset)

        self.btn_exit = QPushButton("게임 종료 (저장)")
        self.btn_exit.setStyleSheet(btn_style)
        self.btn_exit.clicked.connect(self.hts.save_and_exit)

        self.btn_close = QPushButton("돌아가기")
        self.btn_close.setStyleSheet(btn_style + "QPushButton { background: #222; }")
        self.btn_close.clicked.connect(self.close)

        layout.addWidget(QLabel("시스템 옵션을 선택하세요."))
        layout.addWidget(self.btn_cheat)
        layout.addWidget(self.btn_reset)
        layout.addWidget(self.btn_exit)
        layout.addSpacing(10)
        layout.addWidget(self.btn_close)
        self.setLayout(layout)

    def _open_cheat(self):
        self.hide()
        CheatConsoleDialog(self.hts).exec()
        self.close()

    def _trigger_reset(self):
        dlg = CustomConfirmDialog(
            "시스템 초기화 확인",
            "모든 자산과 투자 기록이 영구적으로 파기됩니다.\n정말 초기화하시겠습니까?",
            self.hts
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.hts.reset_game_logic()
            self.close()


# ─────────────────────────────────────────────
# CheatConsoleDialog
# ─────────────────────────────────────────────
class CheatConsoleDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hts = parent
        self.setWindowTitle("🛠️ SYSTEM DEBUG CONSOLE")
        self.setFixedSize(450, 180)
        self.setStyleSheet("""
            QDialog { background-color: #050505; border: 2px solid #00FF00; }
            QLabel  { color: #00FF00; font-family: 'Consolas'; font-size: 13px; font-weight: bold; }
            QLineEdit { background-color: #000; color: #00FF00; border: 1px solid #00FF00;
                font-family: 'Consolas'; font-size: 18px; padding: 8px; }
        """)

        layout = QVBoxLayout()
        self.label = QLabel(
            "COMMAND SHORTCUTS:\n"
            " [ 1:숫자 ] : 골드(예수금) 추가 (예: 1:1000000)\n"
            " [ 2:숫자 ] : 스피드 일수 지정 (예: 2:30)\n"
            "(기존 명령어인 GOLD, SPEED도 계속 사용 가능)"
        )
        layout.addWidget(self.label)

        self.input_line = QLineEdit()
        self.input_line.setPlaceholderText("단축 명령어를 입력하세요...")
        self.input_line.returnPressed.connect(self._execute_command)
        layout.addWidget(self.input_line)
        self.setLayout(layout)

    def _execute_command(self):
        cmd = self.input_line.text().strip().upper()
        if not cmd:
            return

        if ":" in cmd:
            try:
                prefix, value_str = cmd.split(":", 1)
                value = int(value_str)
                if prefix == "1":
                    self.hts.my_cash += value
                    self.hts.sync_ui_with_engine()
                    self.accept(); return
                elif prefix == "2":
                    self.hts.auto_speed_days = value
                    self.accept(); return
            except Exception:
                pass

        if cmd.startswith("GOLD:"):
            try:
                self.hts.my_cash += int(cmd.split(":")[1])
                self.hts.sync_ui_with_engine()
                self.accept()
            except Exception:
                pass
        elif cmd.startswith("SPEED:"):
            try:
                self.hts.auto_speed_days = int(cmd.split(":")[1])
                self.accept()
            except Exception:
                self.label.setText("❌ ERROR: 숫자를 입력하세요.")


# ─────────────────────────────────────────────
# TradeDialog
# ─────────────────────────────────────────────
class TradeDialog(QDialog):
    def __init__(self, mode: str, stock_name: str, price: int, limit: int, hts_parent):
        super().__init__(hts_parent)
        self.hts        = hts_parent
        self.mode       = mode
        self.stock_name = stock_name
        self.price      = price
        self.limit      = limit

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"💸 {mode} - {stock_name}")
        self.setFixedWidth(350)
        self.setStyleSheet(f"background-color: {COLOR['bg_main']}; color: {COLOR['text_default']}; font-family: 'Malgun Gothic';")

        layout     = QVBoxLayout()
        info_color = COLOR['accent_red'] if mode == "매수" else COLOR['accent_blue']

        title = QLabel(f"[{mode}] {stock_name}")
        title.setStyleSheet(f"font-size: 18px; font-weight: bold; color: {info_color};")
        layout.addWidget(title)

        self.price_label = QLabel(f"현재가: {int(price):,}원")
        layout.addWidget(self.price_label)
        self.limit_label = QLabel(f"가능 수량: {limit:,}주")
        layout.addWidget(self.limit_label)

        input_lay = QHBoxLayout()
        self.amount_input = QLineEdit("1")
        self.amount_input.setStyleSheet(
            f"background-color: #222; color: {COLOR['accent_green']}; font-size: 20px; "
            "font-weight: bold; border: 1px solid #444; height: 40px; padding-left: 10px;"
        )
        self.amount_input.textChanged.connect(self._update_total)

        btn_max = QPushButton("MAX")
        btn_max.setFixedSize(60, 40)
        btn_max.setStyleSheet(f"background-color: #333; color: {COLOR['accent_yellow']}; font-weight: bold; border: 1px solid #555;")
        btn_max.clicked.connect(lambda: self.amount_input.setText(str(self.limit)))

        input_lay.addWidget(self.amount_input)
        input_lay.addWidget(btn_max)
        layout.addLayout(input_lay)

        self.total_label = QLabel(f"총 금액: {int(price):,}원")
        self.total_label.setStyleSheet(f"font-size: 15px; color: {COLOR['accent_yellow']}; margin-top: 10px; font-weight: bold;")
        layout.addWidget(self.total_label)

        self.btn_confirm = QPushButton(f"{mode} 확정")
        self.btn_confirm.setFixedHeight(45)
        self.btn_confirm.setStyleSheet(f"background-color: {info_color}; color: white; font-weight: bold; font-size: 16px; border-radius: 5px;")
        self.btn_confirm.clicked.connect(self._execute_trade)
        layout.addWidget(self.btn_confirm)
        self.setLayout(layout)

    def _update_total(self, text: str):
        try:
            val = max(0, min(self.limit, int(text) if text else 0))
            if int(text or 0) > self.limit:
                self.amount_input.setText(str(self.limit))
            self.total_label.setText(f"총 금액: {int(self.price * val):,}원")
        except Exception:
            pass

    def _execute_trade(self):
        amount = int(self.amount_input.text() or 0)
        if amount <= 0:
            return
        if self.mode == "매수":
            self.hts.process_buy(self.stock_name, self.price, amount)
        else:
            self.hts.process_sell(self.stock_name, self.price, amount)
        self.update_info()

    def update_info(self):
        stock = self.hts.game_service.get_stock_by_name(self.stock_name)
        if stock:
            self.price = stock['price']
            self.price_label.setText(f"현재가: {int(self.price):,}원")

        if self.mode == "매수":
            self.limit = int(self.hts.my_cash // self.price)
        else:
            p = self.hts.my_portfolio.get(self.stock_name, {})
            self.limit = p.get('shares', p.get('quantity', 0))

        self.limit_label.setText(f"가능 수량: {self.limit:,}주")
        self._update_total(self.amount_input.text())


# ─────────────────────────────────────────────
# EarningsDialog
# ─────────────────────────────────────────────
class EarningsDialog(QDialog):
    def __init__(self, name: str, game_service, parent=None):
        super().__init__(parent)
        self.name    = name
        self.gs      = game_service
        self.setWindowTitle(f"🔎 [실적 상세] {name}")
        self.resize(700, 800)
        self.setStyleSheet(HTS_STYLE)

        layout  = QVBoxLayout()
        btn_lay = QHBoxLayout()
        self.year_in = QLineEdit()
        self.year_in.setPlaceholderText("연도")
        self.year_in.setFixedWidth(100)

        btn_style = "QPushButton { background: #333; color: white; padding: 5px; } QPushButton:hover { border: 1px solid #00FF00; }"
        for label, fn in [("조회", self.load_year), ("현재", self.load_cur), ("전체", self.load_all)]:
            b = QPushButton(label)
            b.clicked.connect(fn)
            b.setStyleSheet(btn_style)
            btn_lay.addWidget(b)

        btn_lay.insertWidget(0, self.year_in)
        btn_lay.insertStretch(1)
        layout.addLayout(btn_lay)

        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setStyleSheet("background-color: #000; border: 1px solid #333; padding: 20px;")
        layout.addWidget(self.report)
        self.setLayout(layout)
        self.load_cur()

    def _make_table_html(self, year: str, data: dict) -> str:
        all_h = self.gs.get_earnings_history(self.name)
        sorted_keys = [(y, q) for y in sorted(all_h.keys())
                       for q in ["1분기", "2분기", "3분기", "4분기"] if q in all_h.get(y, {})]

        html  = f"<h2 style='color:#00FF00;'>📅 {year}년도 실적 공시 리포트</h2>"
        html += "<table border='1' style='border-collapse: collapse; width: 100%; color: white; font-size: 14px;'>"
        html += "<tr style='background-color: #222; color: #00FF00;'><th>분기</th><th>공시일</th><th>매출액</th><th>영업이익</th><th>당기순이익</th></tr>"

        for q_n in ["1분기", "2분기", "3분기", "4분기"]:
            if q_n not in data:
                continue
            d = data[q_n]

            # 전 분기 값 가져오기
            p_rev = p_op = p_net = None
            try:
                idx = sorted_keys.index((year, q_n))
                if idx > 0:
                    p_y, p_q = sorted_keys[idx - 1]
                    p_rev = all_h[p_y][p_q]['revenue']
                    p_op  = all_h[p_y][p_q]['op_income']
                    p_net = all_h[p_y][p_q]['net_income']
            except Exception:
                pass

            def num_c(val):
                """숫자 색: 흑자=빨간, 적자=파란"""
                return "#FF4444" if val >= 0 else "#4444FF"

            def arrow_html(cur, prev):
                """화살표: 전 분기 대비, 없으면 빈 문자열"""
                if prev is None: return ""
                if cur > prev: return " <b style='color:#FF4444;'>▲</b>"
                if cur < prev: return " <b style='color:#4444FF;'>▼</b>"
                return " <b style='color:#888;'>─</b>"

            rev  = d['revenue']
            op   = d['op_income']
            net  = d['net_income']

            html += (
                f"<tr style='text-align: center;'>"
                f"<td>{q_n}</td>"
                f"<td>{d['date']}</td>"
                f"<td style='color:{num_c(rev)};'><b>{rev:,.0f}원</b>{arrow_html(rev, p_rev)}</td>"
                f"<td style='color:{num_c(op)};'><b>{op:,.0f}원</b>{arrow_html(op, p_op)}</td>"
                f"<td style='color:{num_c(net)};'><b>{net:,.0f}원</b>{arrow_html(net, p_net)}</td>"
                f"</tr>"
            )
        return html + "</table><br/>"

    def load_year(self):
        y = self.year_in.text().strip()
        h = self.gs.get_earnings_history(self.name)
        if y in h:
            _sb = self.report.verticalScrollBar()
            _pos = _sb.value()
            self.report.setHtml(self._make_table_html(y, h[y]))
            _sb.setValue(_pos)

    def load_cur(self):
        y = str(self.gs.s.current_date.year)
        self.year_in.setText(y)
        self.load_year()

    def load_all(self):
        h   = self.gs.get_earnings_history(self.name)
        html = "".join(self._make_table_html(y, h[y]) for y in sorted(h.keys()))
        _sb = self.report.verticalScrollBar()
        _pos = _sb.value()
        self.report.setHtml(html)
        _sb.setValue(_pos)


# ─────────────────────────────────────────────
# MyInvestmentDialog
# ─────────────────────────────────────────────
class MyInvestmentDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.hts = parent
        self.setWindowTitle("💰 내 투자 포트폴리오 (증권 계좌)")
        self.resize(1000, 600)
        self.setStyleSheet(HTS_STYLE)

        layout = QVBoxLayout()
        self.summary_label = QLabel()
        self.summary_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.summary_label.setStyleSheet("background-color: #1a1a1a; border: 1px solid #333; padding: 10px; border-radius: 5px; margin-bottom: 5px;")
        layout.addWidget(self.summary_label)

        self.label = QLabel("📊 실시간 보유 주식 상세 현황")
        self.label.setStyleSheet("font-size: 14px; font-weight: bold; color: #2ECC71;")
        layout.addWidget(self.label)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["종목명", "보유수량", "평균단가", "현재가", "매수금액", "평가금액", "수익률"])
        self.table.setStyleSheet("QTableWidget { background-color: #000; gridline-color: #222; } QHeaderView::section { background-color: #222; color: #2ECC71; }")
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.cellClicked.connect(self._go_to_stock)
        layout.addWidget(self.table)
        self.setLayout(layout)
        self.update_info()

    def update_info(self, total_eval_value=None, total_profit=None, total_rate=None):
        def _qty(d): return d.get('shares', d.get('quantity', 0))
        if total_eval_value is None:
            total_buy = total_eval = 0
            for name, data in self.hts.my_portfolio.items():
                stock = self.hts.game_service.get_stock_by_name(name)
                cur_p = stock['price'] if stock else 0
                qty = _qty(data)
                total_buy  += qty * data['avg_price']
                total_eval += qty * cur_p
            total_eval_value = total_eval
            total_profit     = total_eval - total_buy
            total_rate       = (total_profit / total_buy * 100) if total_buy > 0 else 0.0

        p_color = "#FF4444" if total_profit > 0 else ("#4444FF" if total_profit < 0 else "#e0e0e0")
        self.summary_label.setText(
            f"<div align='center'><table width='100%'><tr>"
            f"<td width='50%'><div style='background-color:#1a1a1a;padding:15px;border-radius:10px;border:1px solid #333;'>"
            f"<span style='color:#888;font-size:12px;'>현재 투자 금액</span><br/>"
            f"<span style='color:{p_color};font-size:24px;font-weight:bold;'>{int(total_eval_value):,}원</span>"
            f"<span style='color:{p_color};font-size:16px;'> ({total_rate:+.2f}%)</span></div></td>"
            f"<td width='50%'><div style='background-color:#1a1a1a;padding:15px;border-radius:10px;border:1px solid #333;margin-left:10px;'>"
            f"<span style='color:#888;font-size:12px;'>보유 현금(예수금)</span><br/>"
            f"<span style='color:#FFD700;font-size:24px;font-weight:bold;'>{int(self.hts.my_cash):,}원</span>"
            f"</div></td></tr></table></div>"
        )

        portfolio = self.hts.my_portfolio
        self.table.setRowCount(len(portfolio))
        for i, (name, data) in enumerate(portfolio.items()):
            stock   = self.hts.game_service.get_stock_by_name(name)
            cur_p   = float(stock['price']) if stock else 0
            shares  = _qty(data)
            avg_p   = data['avg_price']
            buy_t   = shares * avg_p
            eval_t  = shares * cur_p
            rate    = ((cur_p / avg_p) - 1) * 100 if avg_p > 0 else 0.0

            for j, text in enumerate([name, f"{shares:,}주", f"{int(avg_p):,}원",
                                        f"{int(cur_p):,}원", f"{int(buy_t):,}원",
                                        f"{int(eval_t):,}원", f"{rate:+.2f}%"]):
                it = QTableWidgetItem(text)
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if j == 6:
                    it.setForeground(QColor("#FF4444" if rate > 0 else ("#4444FF" if rate < 0 else "#e0e0e0")))
                self.table.setItem(i, j, it)

    def _go_to_stock(self, r, c):
        stock_name = self.table.item(r, 0).text()
        self.hts.selected_stock_name = stock_name
        self.hts.search_bar.clear()
        for i in range(self.hts.stock_table.rowCount()):
            item = self.hts.stock_table.item(i, 0)
            if item and item.text() == stock_name:
                self.hts.stock_table.setCurrentCell(i, 0)
                self.hts.on_stock_clicked(i, 0)
                break
        self.hts.scroll_to_selected()


# ─────────────────────────────────────────────
# StockFilterDialog
# ─────────────────────────────────────────────
class StockFilterDialog(QDialog):
    def __init__(self, hts_parent):
        super().__init__(hts_parent)
        self.hts = hts_parent
        self.setWindowTitle("⚙️ 종목 상세 검색 필터")
        self.resize(500, 450)
        self.setStyleSheet(HTS_STYLE)
        self.setModal(False)

        layout    = QVBoxLayout()
        btn_style = (
            "QPushButton { background: #222; color: #888; border: 1px solid #444; "
            "padding: 5px 8px; border-radius: 3px; }"
            "QPushButton:checked { color: #00FF00; font-weight: bold; border: 1px solid #00FF00; background: #001a00; }"
        )

        # 체급 필터
        tier_lay = QHBoxLayout()
        tier_lay.addWidget(QLabel("<b>체급:</b>"))
        self.tier_all   = self._toggle_btn("전체",  True)
        self.tier_large = self._toggle_btn("대형주", False)
        self.tier_mid   = self._toggle_btn("중형주", False)
        self.tier_small = self._toggle_btn("소형주", False)
        for b in [self.tier_all, self.tier_large, self.tier_mid, self.tier_small]:
            b.setStyleSheet(btn_style); b.clicked.connect(self._on_filter); tier_lay.addWidget(b)
        layout.addLayout(tier_lay)

        # 그룹 필터
        grp_lay = QHBoxLayout()
        grp_lay.addWidget(QLabel("<b>그룹사:</b>"))
        self.group_all = self._toggle_btn("전체",   True)
        self.group_yes = self._toggle_btn("계열사만", False)
        self.group_no  = self._toggle_btn("단독기업", False)
        for b in [self.group_all, self.group_yes, self.group_no]:
            b.setStyleSheet(btn_style); b.clicked.connect(self._on_filter); grp_lay.addWidget(b)
        layout.addLayout(grp_lay)

        # 가격대
        p_lay = QHBoxLayout()
        p_lay.addWidget(QLabel("<b>가격대:</b>"))
        self.price_min = self._input("최소(원)"); self.price_max = self._input("최대(원)")
        self.price_min.textChanged.connect(self._on_filter)
        self.price_max.textChanged.connect(self._on_filter)
        p_lay.addWidget(self.price_min); p_lay.addWidget(QLabel("~")); p_lay.addWidget(self.price_max)
        layout.addLayout(p_lay)

        # 발행주식수
        s_lay = QHBoxLayout()
        s_lay.addWidget(QLabel("<b>발행주식수:</b>"))
        self.shares_min = self._input("최소(주)"); self.shares_max = self._input("최대(주)")
        self.shares_min.textChanged.connect(self._on_filter)
        self.shares_max.textChanged.connect(self._on_filter)
        s_lay.addWidget(self.shares_min); s_lay.addWidget(QLabel("~")); s_lay.addWidget(self.shares_max)
        layout.addLayout(s_lay)

        # 산업군
        ind_lay = QHBoxLayout()
        self.ind_all = self._toggle_btn("전체", True)
        self.ind_all.setStyleSheet(btn_style)
        self.ind_all.clicked.connect(self._on_ind_all)
        ind_lay.addWidget(self.ind_all)
        self.ind_buttons = {}
        for ind in ["IT","에너지","건강관리","산업재","소재","자유소비재","커뮤니케이션","금융","필수소비재","유틸리티","부동산","재건"]:
            b = self._toggle_btn(ind, False)
            b.setStyleSheet(btn_style); b.clicked.connect(self._on_ind_btn)
            ind_lay.addWidget(b); self.ind_buttons[ind] = b
        layout.addLayout(ind_lay)

        # 섹터
        sec_lay = QHBoxLayout()
        sec_lay.addWidget(QLabel("<b>섹터:</b>"))
        self.sec_all       = self._toggle_btn("전체",     True)
        self.sec_grow      = self._toggle_btn("Growth",   False)
        self.sec_val       = self._toggle_btn("Value",    False)
        self.sec_defensive = self._toggle_btn("Defensive",False)
        self.sec_theme     = self._toggle_btn("Theme",    False)
        for b in [self.sec_all, self.sec_grow, self.sec_val, self.sec_defensive, self.sec_theme]:
            b.setStyleSheet(btn_style); b.clicked.connect(self._on_filter); sec_lay.addWidget(b)
        layout.addLayout(sec_lay)

        reset_btn = QPushButton("필터 초기화")
        reset_btn.setStyleSheet("background: #222; color: #FF4444; padding: 10px; border-radius: 4px; font-weight: bold;")
        reset_btn.clicked.connect(self.reset_filters)
        layout.addWidget(reset_btn)

        close_btn = QPushButton("적용 및 닫기")
        close_btn.setStyleSheet("background: #222; color: white; padding: 10px; border-radius: 4px; font-weight: bold;")
        close_btn.clicked.connect(self.close)
        layout.addWidget(close_btn)
        self.setLayout(layout)

    @staticmethod
    def _toggle_btn(text: str, checked: bool) -> QPushButton:
        b = QPushButton(text); b.setCheckable(True); b.setChecked(checked); return b

    @staticmethod
    def _input(placeholder: str) -> QLineEdit:
        e = QLineEdit(); e.setPlaceholderText(placeholder)
        e.setStyleSheet("background: #111; color: #00FF00; padding: 5px; border: 1px solid #333;")
        return e

    def _on_filter(self):
        sender = self.sender()
        if sender in [self.tier_large, self.tier_mid, self.tier_small] and sender.isChecked():
            self.tier_all.setChecked(False)
        elif sender == self.tier_all and self.tier_all.isChecked():
            self.tier_large.setChecked(False); self.tier_mid.setChecked(False); self.tier_small.setChecked(False)

        if sender == self.group_all and self.group_all.isChecked():
            self.group_yes.setChecked(False); self.group_no.setChecked(False)
        elif sender in [self.group_yes, self.group_no] and sender.isChecked():
            self.group_all.setChecked(False)
            (self.group_no if sender == self.group_yes else self.group_yes).setChecked(False)

        if sender in [self.sec_grow, self.sec_val, self.sec_defensive, self.sec_theme] and sender.isChecked():
            self.sec_all.setChecked(False)
        elif sender == self.sec_all and self.sec_all.isChecked():
            for b in [self.sec_grow, self.sec_val, self.sec_defensive, self.sec_theme]:
                b.setChecked(False)

        self.hts.filter_stocks()

    def _on_ind_all(self):
        if self.ind_all.isChecked():
            for b in self.ind_buttons.values(): b.setChecked(False)
        self.hts.filter_stocks()

    def _on_ind_btn(self):
        any_checked = any(b.isChecked() for b in self.ind_buttons.values())
        self.ind_all.setChecked(not any_checked)
        self.hts.filter_stocks()

    def reset_filters(self):
        self.tier_all.setChecked(True); self.tier_large.setChecked(False)
        self.tier_mid.setChecked(False); self.tier_small.setChecked(False)
        self.group_all.setChecked(True); self.group_yes.setChecked(False); self.group_no.setChecked(False)
        self.price_min.clear(); self.price_max.clear()
        self.shares_min.clear(); self.shares_max.clear()
        for b in self.ind_buttons.values(): b.setChecked(False)
        self.ind_all.setChecked(True)
        self.sec_all.setChecked(True)
        for b in [self.sec_grow, self.sec_val, self.sec_defensive, self.sec_theme]: b.setChecked(False)
        self.hts.filter_stocks()

    def keyPressEvent(self, event):
        # 엔터/리턴 키가 QDialog 기본 동작(accept)을 트리거하지 않도록 차단
        from PyQt6.QtCore import Qt
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            return  # 아무것도 안 함 — 텍스트 입력 중 엔터로 창 닫히거나 초기화 방지
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # active_dialogs에서 제거하지 않음 → 필터 조건 유지
        # 창만 숨기고 조건은 살아있게
        event.accept()


# ─────────────────────────────────────────────
# DelistedDetailDialog
# ─────────────────────────────────────────────
class DelistedDetailDialog(QDialog):
    def __init__(self, stock_obj: dict, game_service, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.gs         = game_service
        self.s          = stock_obj
        self.meta       = stock_obj['meta']
        self.stock_name = self.meta['c_name']
        self.current_tf = "전체"  # 상폐 역사관은 항상 전체 기준

        self.setWindowTitle(f"💀 [상장폐지 상세 기록] {self.stock_name}")
        self.resize(1400, 800)
        self.setStyleSheet(HTS_STYLE)
        self._init_ui()

    def _init_ui(self):
        from engine.constants import SECTOR_MAP
        main_layout = QVBoxLayout()

        header_lay = QHBoxLayout()
        title_vbox = QVBoxLayout()
        title_sub  = QHBoxLayout()

        self.title_label = QLabel(f"< {self.stock_name} >")
        self.title_label.setStyleSheet("font-size: 28px; font-weight: bold; color: #00FF00;")
        self.price_summary_label = QLabel("")
        self.price_summary_label.setStyleSheet("font-size: 18px; font-weight: bold; margin-left: 20px; margin-top: 5px;")
        self.price_summary_label.setTextFormat(Qt.TextFormat.RichText)
        title_sub.addWidget(self.title_label); title_sub.addWidget(self.price_summary_label); title_sub.addStretch()

        sector_name = SECTOR_MAP.get(self.meta['ind'], 'Value')
        sub_title = QLabel(f"상장일: {self.meta.get('listed_date','-')} | 폐지일: {self.meta.get('delisted_date','-')} | 섹터: {sector_name}")
        sub_title.setStyleSheet("color: #888; font-size: 14px;")
        title_vbox.addLayout(title_sub); title_vbox.addWidget(sub_title)
        header_lay.addLayout(title_vbox); header_lay.addStretch()

        btn_close = QPushButton("✕")
        btn_close.setFixedSize(40, 40)
        btn_close.setStyleSheet("QPushButton { background: #222; color: white; font-size: 20px; border-radius: 5px; border: 1px solid #444; } QPushButton:hover { background: #FF4444; }")
        btn_close.clicked.connect(self.close)
        header_lay.addWidget(btn_close, alignment=Qt.AlignmentFlag.AlignTop)
        main_layout.addLayout(header_lay)

        content_lay = QHBoxLayout()
        self.chart_widget = pg.PlotWidget()
        self.chart_widget.setBackground('#000000')
        self.chart_widget.setMouseEnabled(x=False, y=False)
        self.chart_widget.hideButtons()
        self.curve    = self.chart_widget.plot(pen=pg.mkPen(color='#5DADE2', width=2))
        self.baseline = pg.InfiniteLine(pos=0, angle=0,
                            pen=pg.mkPen('#555', width=1,
                            style=__import__('PyQt6.QtCore', fromlist=['Qt']).Qt.PenStyle.DashLine))
        self.chart_widget.addItem(self.baseline)
        content_lay.addWidget(self.chart_widget, 7)

        right_vbox = QVBoxLayout()
        self.report_panel = QTextEdit()
        self.report_panel.setReadOnly(True)
        self.report_panel.setStyleSheet("QTextEdit { background-color: #000; color: #e0e0e0; border: 1px solid #333; font-size: 13px; padding: 10px; }")
        self._update_report_html()
        right_vbox.addWidget(self.report_panel, 1)

        # ★ 하단 버튼 2개 — 각각 새 창으로 열림
        btn_style = ("QPushButton { background-color: #1a1a1a; color: #00FF00; "
                     "border: 1px solid #00FF00; border-radius: 3px; padding: 8px; "
                     "font-weight: bold; font-size: 13px; } "
                     "QPushButton:hover { background-color: #003300; }")
        btn_volume_style = ("QPushButton { background-color: #1a1a1a; color: #00FFFF; "
                            "border: 1px solid #00FFFF; border-radius: 3px; padding: 8px; "
                            "font-weight: bold; font-size: 13px; } "
                            "QPushButton:hover { background-color: #001a1a; }")

        btn_row = QHBoxLayout()
        btn_earnings = QPushButton("📊 실적 상세조회")
        btn_earnings.setFixedHeight(40)
        btn_earnings.setStyleSheet(btn_style)
        btn_earnings.clicked.connect(self._open_earnings_window)

        btn_volume = QPushButton("📈 호가창")
        btn_volume.setFixedHeight(40)
        btn_volume.setStyleSheet(btn_volume_style)
        btn_volume.clicked.connect(self._open_volume_window)

        btn_row.addWidget(btn_earnings)
        btn_row.addWidget(btn_volume)
        right_vbox.addLayout(btn_row)

        content_lay.addLayout(right_vbox, 3)
        main_layout.addLayout(content_lay)
        self.setLayout(main_layout)

        self._load_full_history()

    def _update_report_html(self):
        m     = self.meta
        vals  = [m.get(k, 0) * 100 for k in ['treasury_share','owner_share','foreign_share','inst_share','retail_share']]
        total = sum(vals)
        ts, os, fs, ins, rs = [(v / total * 100 if total > 0 else v) for v in vals]
        size  = {"대형주": "[대기업]", "중형주": "[중견기업]", "소형주": "[중소기업]"}.get(m.get('tier','소형주'), "[중소기업]")

        # 시총 단위 변환
        mc = self.s['market_cap']
        _조 = 1_000_000_000_000
        _경 = 10_000_000_000_000_000
        if   mc >= _경:           mc_str = f"{mc/_경:.2f}경"
        elif mc >= 100 * _조:     mc_str = f"{mc//_조:,.0f}조"
        elif mc >= 10  * _조:     mc_str = f"{mc/_조:.0f}조"
        elif mc >= _조:           mc_str = f"{mc/_조:.1f}조"
        elif mc >= 100_000_000:   mc_str = f"{mc/100_000_000:.0f}억"
        else:                     mc_str = f"{mc:,.0f}원"

        # HP / Shield
        hp       = m.get('hp', 0.0)
        soft_cap = m.get('hp_soft_cap', 60.0)
        shield   = m.get('shield', 0.0)
        hp_ratio = hp / max(1.0, soft_cap)
        filled   = round(hp_ratio * 10)
        hp_bar   = '■' * filled + '□' * (10 - filled)
        if hp_ratio >= 0.60:   hp_color = '#00FF00'
        elif hp_ratio >= 0.30: hp_color = '#FFA500'
        else:                  hp_color = '#FF4444'

        if shield >= 1_000_000_000_000:  shield_str = f"{shield/1_000_000_000_000:.2f}조"
        elif shield >= 100_000_000:       shield_str = f"{shield/100_000_000:.1f}억"
        else:                             shield_str = f"{shield:,.0f}원"

        shield_active = hp_ratio < 0.30 and shield > 0
        shield_label  = "발동중" if shield_active else "대기중"
        shield_color  = '#00FFFF' if shield_active else '#888888'

        # ── 밸류에이션 계산 ──────────────────────────────────────
        name = m.get('c_name', '')
        hist = self.gs.s.earnings_history.get(name, {})
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

        assets_v = max(1.0, m.get('assets', 1.0))

        # PER
        if annual_ni > 0:
            per_val  = mc / annual_ni
            if per_val > 9999: per_str, per_icon = "N/A", "⚪"
            else: per_str = f"{per_val:.1f}배"; per_icon = "🟢" if per_val < 15 else ("🟡" if per_val < 30 else ("🟠" if per_val < 50 else "🔴"))
        elif annual_ni < 0: per_str, per_icon = "적자", "🔴"
        else: per_str, per_icon = "N/A", "⚪"

        # PBR
        pbr_val = mc / assets_v
        if pbr_val > 9999: pbr_str, pbr_icon = "N/A (데이터 오류)", "⚪"
        else:
            pbr_str  = f"{pbr_val:.2f}배"
            pbr_icon = "🟢" if pbr_val < 1 else ("🟡" if pbr_val < 3 else ("🟠" if pbr_val < 5 else "🔴"))

        # ROE
        roe_val  = (annual_ni / assets_v * 100) if assets_v > 0 else 0.0
        roe_str  = f"{roe_val:.1f}%"
        roe_icon = "🟢" if roe_val >= 15 else ("🟡" if roe_val >= 8 else ("🟠" if roe_val >= 0 else "🔴"))

        # 영업이익률
        if annual_rev > 0:
            op_margin = annual_op / annual_rev * 100
            op_str    = f"{op_margin:.1f}%"
            op_icon   = "🟢" if op_margin >= 15 else ("🟡" if op_margin >= 5 else ("🟠" if op_margin >= 0 else "🔴"))
        else: op_str, op_icon = "N/A", "⚪"

        # 부채비율
        debt_ratio = m.get('debt_ratio', None)
        if debt_ratio is not None:
            dr_pct  = debt_ratio * 100
            dr_str  = f"{dr_pct:.1f}%"
            dr_icon = "🟢" if dr_pct < 50 else ("🟡" if dr_pct < 100 else ("🟠" if dr_pct < 200 else "🔴"))
        else: dr_str, dr_icon = "N/A", "⚪"

        # 신용등급
        credit       = m.get('credit_grade', 'N/A')
        credit_color = {'AA': '#00FF00', 'BB': '#FFA500', 'CCC': '#FF4444'}.get(credit, '#888888')

        # 52주 신고가/신저가
        high_52w = m.get('price_52w_high', self.s['price'])
        low_52w  = m.get('price_52w_low',  self.s['price'])

        self.report_panel.setHtml(f"""
        <div style='font-family: Malgun Gothic;'>
            <p><b style='color:#FFD700;font-size:14px;'>[기업 정보]</b><br/>
            그룹: {m.get('group','단독기업')}<br/>규모: <b style='color:#FFD700;'>{size}</b><br/>
            산업: {m['ind']} ({m['sub']})<br/>상태: <b style='color:#FF4444;'>{m['char']}</b></p>
            <p><b style='color:#FFD700;font-size:14px;'>[발행 정보]</b><br/>
            주식수: {self.s['shares']:,} 주<br/>
            <span style='color:#888888;'>방어막 {shield_str} [{shield_label}]</span><br/>
            시총: {mc:,} 원 <span style='color:#FFD700;font-weight:bold;'>({mc_str})</span></p>
            <p><b style='color:#FFD700;font-size:14px;'>[지배구조]</b><br/>
            자사주: {ts:.1f}% | 대주주: {os:.1f}%<br/>
            외국인: {fs:.1f}% | 기관 : {ins:.1f}%<br/>개인 : {rs:.1f}%</p>
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
            52주 신고가: {int(high_52w):,}원<br/>
            52주 신저가: {int(low_52w):,}원</p>
            <hr style='border: 0.5px solid #333;'/>
            <p style='color: #888; font-size: 11px;'>* 위 수치는 상장폐지 확정 시점의 데이터입니다.</p>
        </div>""")

    def _load_earnings_table(self):
        history = self.gs.get_earnings_history(self.stock_name)
        records = [(f"{y} {q}", d['revenue'], d['op_income'], d.get('net_income', 0))
                   for y in sorted(history.keys())
                   for q in ["1분기","2분기","3분기","4분기"] if q in history[y]
                   for d in [history[y][q]]]

        # 컬럼: 분기 | 매출액 | ▲▼ | 영업이익 | ▲▼ | 당기순이익 | ▲▼
        self.earnings_table.setColumnCount(7)
        self.earnings_table.setHorizontalHeaderLabels([
            "분기", "매출액 (원)", "↕", "영업이익 (원)", "↕", "당기순이익 (원)", "↕"
        ])
        hdr = self.earnings_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed); self.earnings_table.setColumnWidth(2, 28)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed); self.earnings_table.setColumnWidth(4, 28)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed); self.earnings_table.setColumnWidth(6, 28)

        self.earnings_table.setRowCount(len(records))

        for i, (qtr, rev, op, net) in enumerate(records):
            prev_rev = records[i-1][1] if i > 0 else None
            prev_op  = records[i-1][2] if i > 0 else None
            prev_net = records[i-1][3] if i > 0 else None

            def num_color(val):
                return "#FF4444" if val >= 0 else "#4444FF"

            def arrow_sym(cur, prev):
                if prev is None: return ""
                return "▲" if cur > prev else ("▼" if cur < prev else "─")

            def arrow_col(cur, prev):
                if prev is None: return "#888888"
                return "#FF4444" if cur > prev else ("#4444FF" if cur < prev else "#888888")

            # 분기
            it0 = QTableWidgetItem(qtr)
            it0.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it0.setForeground(QColor("#aaaaaa"))
            self.earnings_table.setItem(i, 0, it0)

            for col_idx, (val, prev_val) in enumerate(
                [(rev, prev_rev), (op, prev_op), (net, prev_net)]
            ):
                base_col = 1 + col_idx * 2

                # 숫자 셀 — 색상: 흑자/적자
                it_num = QTableWidgetItem(f"{val:,.0f}")
                it_num.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                it_num.setForeground(QColor(num_color(val)))
                self.earnings_table.setItem(i, base_col, it_num)

                # 화살표 셀 — 색상: 전 분기 대비
                it_arr = QTableWidgetItem(arrow_sym(val, prev_val))
                it_arr.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                it_arr.setForeground(QColor(arrow_col(val, prev_val)))
                self.earnings_table.setItem(i, base_col + 1, it_arr)

    def _load_full_history(self):
        try:
            raw = self.gs.get_delisted_stock_history(self.stock_name)
            if not raw: return
            count = len(raw)
            if count == 1:
                disp = [float(raw[0]), float(raw[0])]
            else:
                step = 1 if count < 180 else (7 if count < 1095 else (30 if count < 3650 else (180 if count < 14600 else 365)))
                disp = [float(raw[i]) for i in range(0, count, step)]
                if (count - 1) % step != 0: disp.append(float(raw[-1]))

            smoothed = disp[:]

            if smoothed:
                # ── 라인 그래프 그리기 ────────────────────────
                diff_color = "#FF4444" if float(raw[-1]) >= float(raw[0]) else "#4444FF"
                self.curve.setPen(pg.mkPen(color=diff_color, width=2))
                self.curve.setData(smoothed)

                # 기준선 (상장 첫날 가격)
                self.baseline.setPos(float(raw[0]))

                # raw 전체 기준 실제 최고/최저
                raw_floats = [float(x) for x in raw]
                real_max   = max(raw_floats)
                real_min   = min(raw_floats)

                # Y축 범위는 raw 전체 기준
                y_pad = max(1.0, (real_max - real_min) * 0.05) if real_max != real_min else real_min * 0.01
                self.chart_widget.setYRange(real_min - y_pad, real_max + y_pad)

                # 마커 x좌표는 smoothed에서 가장 가까운 위치로 근사
                n       = len(smoothed)
                max_idx = min(range(n), key=lambda i: abs(smoothed[i] - real_max))
                min_idx = min(range(n), key=lambda i: abs(smoothed[i] - real_min))
                max_val = real_max
                min_val = real_min

                for attr in ['max_scatter','min_scatter','max_text','min_text']:
                    if hasattr(self, attr):
                        try: self.chart_widget.removeItem(getattr(self, attr))
                        except: pass

                self.max_scatter = pg.ScatterPlotItem(size=10, brush=pg.mkBrush('#FF4444'), symbol='o')
                self.max_scatter.addPoints([{'pos': (max_idx, max_val)}])
                self.chart_widget.addItem(self.max_scatter)

                self.min_scatter = pg.ScatterPlotItem(size=10, brush=pg.mkBrush('#4444FF'), symbol='o')
                self.min_scatter.addPoints([{'pos': (min_idx, min_val)}])
                self.chart_widget.addItem(self.min_scatter)

                max_anchor = (1.1, 1.1) if max_idx > n * 0.75 else (0, 1)
                self.max_text = pg.TextItem(
                    html=f"<span style='color:#FF4444;font-weight:bold;background-color:#000;'>최고: {int(max_val):,}</span>",
                    anchor=max_anchor)
                self.max_text.setPos(max_idx, max_val)
                self.chart_widget.addItem(self.max_text)

                min_anchor = (1.1, -0.1) if min_idx > n * 0.75 else ((-0.1,-0.1) if min_idx < n * 0.25 else (0,0))
                self.min_text = pg.TextItem(
                    html=f"<span style='color:#4444FF;font-weight:bold;background-color:#000;'>최저: {int(min_val):,}</span>",
                    anchor=min_anchor)
                self.min_text.setPos(min_idx, min_val)
                self.chart_widget.addItem(self.min_text)

                # 전체 기준 수익률 표시
                start_p = float(raw[0]); end_p = float(raw[-1])
                diff    = end_p - start_p
                rate    = (diff / start_p * 100) if start_p != 0 else 0
                c_hex   = "#FF4444" if diff > 0 else ("#4444FF" if diff < 0 else "#e0e0e0")
                sign    = "▲" if diff > 0 else ("▼" if diff < 0 else "─")
                self.price_summary_label.setText(
                    f"<span style='color:#aaa;'>전체 기준: </span>"
                    f"<span style='color:#fff;'>{int(start_p):,}원</span>"
                    f"<span style='color:#fff;'> → </span>"
                    f"<span style='color:{c_hex};font-weight:bold;'>{int(end_p):,}원 "
                    f"({sign}{int(abs(diff)):,}원, {rate:+.2f}%)</span>"
                )
        except Exception as e:
            print(f"❌ 상장폐지 차트 로드 실패: {e}")

    def _open_earnings_window(self):
        """실적 상세조회 — 새 창"""
        history = self.gs.get_earnings_history(self.stock_name)
        records = [(f"{y} {q}", d['revenue'], d['op_income'], d.get('net_income', 0))
                   for y in sorted(history.keys())
                   for q in ["1분기","2분기","3분기","4분기"] if q in history[y]
                   for d in [history[y][q]]]

        dlg = QDialog(self, Qt.WindowType.Window)
        dlg.setWindowTitle(f"📊 실적 상세조회 — {self.stock_name}")
        dlg.resize(820, 550)
        dlg.setStyleSheet(HTS_STYLE)

        layout = QVBoxLayout()
        title = QLabel(f"[ {self.stock_name} ] 전체 실적 기록")
        title.setStyleSheet("color: #00FF00; font-weight: bold; font-size: 14px; padding: 5px;")
        layout.addWidget(title)

        table = QTableWidget(0, 7)
        table.setHorizontalHeaderLabels([
            "분기", "매출액 (원)", "↕", "영업이익 (원)", "↕", "당기순이익 (원)", "↕"
        ])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setStyleSheet(
            "QTableWidget { background-color: #000; color: #e0e0e0; gridline-color: #222; border: 1px solid #333; }"
            "QHeaderView::section { background-color: #222; color: #00FF00; }"
        )
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed); table.setColumnWidth(2, 28)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed); table.setColumnWidth(4, 28)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed); table.setColumnWidth(6, 28)
        table.setRowCount(len(records))

        def num_color(val):
            return "#FF4444" if val >= 0 else "#4444FF"

        def arrow_sym(cur, prev):
            if prev is None: return ""
            return "▲" if cur > prev else ("▼" if cur < prev else "─")

        def arrow_col(cur, prev):
            if prev is None: return "#888888"
            return "#FF4444" if cur > prev else ("#4444FF" if cur < prev else "#888888")

        for i, (qtr, rev, op, net) in enumerate(records):
            prev_rev = records[i-1][1] if i > 0 else None
            prev_op  = records[i-1][2] if i > 0 else None
            prev_net = records[i-1][3] if i > 0 else None

            it0 = QTableWidgetItem(qtr)
            it0.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            it0.setForeground(QColor("#aaaaaa"))
            table.setItem(i, 0, it0)

            for col_idx, (val, prev_val) in enumerate(
                [(rev, prev_rev), (op, prev_op), (net, prev_net)]
            ):
                base_col = 1 + col_idx * 2
                it_num = QTableWidgetItem(f"{val:,.0f}")
                it_num.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                it_num.setForeground(QColor(num_color(val)))
                table.setItem(i, base_col, it_num)

                it_arr = QTableWidgetItem(arrow_sym(val, prev_val))
                it_arr.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                it_arr.setForeground(QColor(arrow_col(val, prev_val)))
                table.setItem(i, base_col + 1, it_arr)

        layout.addWidget(table)
        dlg.setLayout(layout)
        dlg.show()

    def _open_volume_window(self):
        """호가창 — 새 창 (상폐일 기준 1년, state에서 직접 읽기)"""
        try:
            records = self.gs.get_investor_volume(self.stock_name, days=252)
        except Exception:
            records = []

        dlg = QDialog(self, Qt.WindowType.Window)
        dlg.setWindowTitle(f"📈 호가창 (수급 기록) — {self.stock_name}")
        dlg.resize(700, 500)
        dlg.setStyleSheet(HTS_STYLE)

        layout = QVBoxLayout()
        title = QLabel(f"[ {self.stock_name} ] 투자자별 순매수/순매도 (상폐일 기준 최근 1년)")
        title.setStyleSheet("color: #00FFFF; font-weight: bold; font-size: 14px; padding: 5px;")
        layout.addWidget(title)

        table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["일자", "외국인", "기관", "개인"])
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setStyleSheet(
            "QTableWidget { background-color: #000; color: #e0e0e0; gridline-color: #222; border: 1px solid #333; }"
            "QHeaderView::section { background-color: #222; color: #00FFFF; }"
        )
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setRowCount(len(records))

        for i, rec in enumerate(records):
            date_it = QTableWidgetItem(rec['date'])
            date_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            date_it.setForeground(QColor("#aaaaaa"))
            table.setItem(i, 0, date_it)
            for j, key in enumerate(["foreign", "inst", "retail"], start=1):
                val  = rec[key]
                sign = "+" if val >= 0 else ""
                it   = QTableWidgetItem(f"{sign}{val:,}")
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                it.setForeground(QColor("#FF4444") if val > 0 else (QColor("#4444FF") if val < 0 else QColor("#888888")))
                table.setItem(i, j, it)

        layout.addWidget(table)
        dlg.setLayout(layout)
        dlg.show()

    @staticmethod
    def _moving_avg(data: list, window: int = 3) -> list:
        return [sum(data[max(0, i - window + 1):i + 1]) / len(data[max(0, i - window + 1):i + 1]) for i in range(len(data))]

# ─────────────────────────────────────────────
# InvestorVolumeDialog — 호가창 (실시간 갱신)
# ─────────────────────────────────────────────
class InvestorVolumeDialog(QDialog):
    def __init__(self, stock_name: str, game_service, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.stock_name   = stock_name
        self.gs           = game_service
        self.setWindowTitle(f"📈 호가창 (수급 현황) — {stock_name}")
        self.resize(720, 520)
        self.setStyleSheet(HTS_STYLE)
        self._init_ui()
        self.refresh_data()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(12, 12, 12, 12)

        # 헤더
        header = QHBoxLayout()
        self.title_label = QLabel(f"[ {self.stock_name} ] 투자자별 순매수/순매도 (최근 1년)")
        self.title_label.setStyleSheet("color: #00FFFF; font-weight: bold; font-size: 14px;")
        self.date_label  = QLabel("")
        self.date_label.setStyleSheet("color: #888; font-size: 12px;")
        header.addWidget(self.title_label)
        header.addStretch()
        header.addWidget(self.date_label)
        layout.addLayout(header)

        # 합산 요약 바
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet(
            "background-color: #111; border: 1px solid #333; "
            "padding: 6px; font-size: 12px; color: #ccc;"
        )
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.summary_label)

        # 테이블
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["일자", "외국인", "기관", "개인"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setStyleSheet(
            "QTableWidget { background-color: #000; color: #e0e0e0; gridline-color: #1a1a1a; }"
            "QHeaderView::section { background-color: #1a1a1a; color: #00FFFF; padding: 6px; }"
            "QTableWidget::item:selected { background-color: #001a1a; }"
        )
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        layout.addWidget(self.table)
        self.setLayout(layout)

    def refresh_data(self):
        """NEXT DAY 때마다 호출 — state에서 직접 읽기 (DB 없음)"""
        try:
            records = self.gs.get_investor_volume(self.stock_name, days=252)
        except Exception:
            records = []

        self.table.setRowCount(len(records))

        total_f = total_i = total_r = 0

        for i, rec in enumerate(reversed(records)):   # 최신 날짜가 위로
            date_it = QTableWidgetItem(rec['date'])
            date_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            date_it.setForeground(QColor("#888888"))
            self.table.setItem(i, 0, date_it)

            for j, key in enumerate(["foreign", "inst", "retail"], start=1):
                val  = rec[key]
                sign = "+" if val >= 0 else ""
                it   = QTableWidgetItem(f"{sign}{val:,}")
                it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                color = "#FF4444" if val > 0 else ("#4444FF" if val < 0 else "#888888")
                it.setForeground(QColor(color))
                self.table.setItem(i, j, it)

            total_f += rec['foreign']
            total_i += rec['inst']
            total_r += rec['retail']

        # 날짜 라벨 갱신
        if records:
            self.date_label.setText(f"기준일: {records[-1]['date']} | 최근 {len(records)}거래일 (최대 1년)")

        # 합산 요약
        def fmt(v):
            sign  = "+" if v >= 0 else ""
            color = "#FF4444" if v > 0 else ("#4444FF" if v < 0 else "#888888")
            return f"<span style='color:{color};font-weight:bold;'>{sign}{v:,}</span>"

        self.summary_label.setText(
            f"1년 합산 &nbsp;|&nbsp; "
            f"외국인: {fmt(total_f)} &nbsp; "
            f"기관: {fmt(total_i)} &nbsp; "
            f"개인: {fmt(total_r)}"
        )