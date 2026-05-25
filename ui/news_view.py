"""
ui/news_view.py
뉴스/공시 뷰어 창.
비즈니스 로직은 services/news_service.py 에서, UI만 이 파일에서 담당합니다.
"""
from datetime import datetime, timedelta
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QPushButton, QFrame,
    QTextEdit, QLineEdit, QComboBox, QWidget
)
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor, QFont

from .styles import HTS_STYLE, NEWS_STYLE, COLOR
from engine.constants import SECTOR_MAP


class NewsWindow(QDialog):
    def __init__(self, hts_parent, game_service, news_service, parent=None):
        super().__init__(parent)
        self.hts          = hts_parent
        self.gs           = game_service   # GameService
        self.ns           = news_service   # NewsService
        self.all_events   = []

        self.setModal(False)
        self.setWindowTitle("실시간 프리미엄 공시 분석 시스템 V12.5")
        self.resize(1400, 850)
        self.setStyleSheet(NEWS_STYLE)

        self._init_ui()
        self.refresh_data()

    # ─────────────────────────────────────────────
    # UI 구성
    # ─────────────────────────────────────────────
    def _init_ui(self):
        main_v = QVBoxLayout()
        main_v.setContentsMargins(10, 10, 10, 10)

        # 상단 바
        top_bar = QFrame()
        top_bar.setStyleSheet("background: #111; border-bottom: 1px solid #333;")
        top_lay = QHBoxLayout(top_bar)

        self.status_label = QLabel()
        self.status_label.setStyleSheet("font-size: 14px; font-weight: bold; border: none;")

        btn_style = """
            QPushButton { background-color: #222; color: #eee; border: 1px solid #444;
                padding: 7px 20px; font-weight: bold; border-radius: 3px; }
            QPushButton:hover { background-color: #333; border: 1px solid #00FF00; color: #00FF00; }
            QPushButton#premium_btn { background-color: #004400; border: 1px solid #00FF00; color: #00FF00; }
            QPushButton#cancel_btn  { background-color: #331111; border: 1px solid #FF4444; color: #FF4444; }
        """
        self.btn_buy = QPushButton()
        self.btn_buy.setObjectName("premium_btn")
        self.btn_buy.clicked.connect(self._buy_subscription)
        self.btn_buy.setStyleSheet(btn_style)

        self.btn_cancel = QPushButton("구독 해제")
        self.btn_cancel.setObjectName("cancel_btn")
        self.btn_cancel.clicked.connect(self._cancel_subscription)
        self.btn_cancel.setStyleSheet(btn_style)

        top_lay.addWidget(self.status_label)
        top_lay.addStretch()
        top_lay.addWidget(self.btn_buy)
        top_lay.addWidget(self.btn_cancel)
        main_v.addWidget(top_bar)

        # 중앙: 좌(7) + 우(3)
        content = QHBoxLayout()

        left = QWidget()
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)

        search_lay = QHBoxLayout()
        self.search_combo = QComboBox()
        self.search_combo.addItems(["전체 필터", "일자별", "구분별", "종목별", "내용 검색"])
        self.search_combo.currentIndexChanged.connect(self.filter_table)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("검색할 키워드를 입력하십시오...")
        self.search_input.textChanged.connect(self.filter_table)
        search_lay.addWidget(self.search_combo)
        search_lay.addWidget(self.search_input)
        left_lay.addLayout(search_lay)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["[ 일자 ]", "[ 구분 ]", "[ 대상/종목 ]", "[ 공시 데이터 요약 ]"])
        # 컬럼 너비: 일자/구분/종목은 고정, 공시요약은 나머지 공간
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # 일자
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)  # 구분
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)  # 종목
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)           # 공시요약 (나머지)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._update_detail_view)
        left_lay.addWidget(self.table)
        content.addWidget(left, 7)

        # 우측 상세 뷰
        self.detail_frame = QFrame()
        self.detail_frame.setObjectName("DetailFrame")
        detail_lay = QVBoxLayout(self.detail_frame)

        self.det_header = QLabel("● 문서 정보 대기 중")
        self.det_header.setStyleSheet("font-size: 11px; color: #555;")
        self.det_title = QLabel("공시를 선택하십시오")
        self.det_title.setStyleSheet("font-size: 17px; font-weight: bold; color: #00FF00; margin-top: 5px;")
        self.det_title.setWordWrap(True)

        meta_box = QFrame()
        meta_box.setStyleSheet("background: #111; border: 1px solid #222; padding: 10px;")
        meta_lay = QVBoxLayout(meta_box)
        self.det_meta_label = QLabel("-")
        self.det_meta_label.setStyleSheet("color: #999; font-size: 13px; border: none; line-height: 160%;")
        meta_lay.addWidget(self.det_meta_label)

        self.det_content = QTextEdit()
        self.det_content.setReadOnly(True)

        detail_lay.addWidget(self.det_header)
        detail_lay.addWidget(self.det_title)
        detail_lay.addWidget(QLabel("◈ 상세 분석 정보"))
        detail_lay.addWidget(meta_box)
        detail_lay.addWidget(QLabel("◈ 본문 내용"))
        detail_lay.addWidget(self.det_content)
        content.addWidget(self.detail_frame, 3)

        main_v.addLayout(content)
        self.setLayout(main_v)

    # ─────────────────────────────────────────────
    # 데이터 갱신
    # ─────────────────────────────────────────────
    def refresh_data(self):
        self.all_events = self.ns.get_all_cumulative_events()
        self.all_events.sort(key=lambda x: (x.get('date', ''), x.get('category', '')), reverse=True)
        if len(self.all_events) > 1000:
            self.all_events = self.all_events[:1000]

        is_sub     = self._is_subscribed()
        will_renew = self.gs.s.has_paid_news_access  # 이건 유지 (갱신 의사 표시용)

        if is_sub:
            expiry     = self.gs.s.next_billing_date
            expire_str = expiry.strftime('%Y-%m-%d') if hasattr(expiry, 'strftime') else str(expiry)
            if will_renew:
                self.status_label.setText(f"● 프리미엄 모드 활성 (차기 결제일: {expire_str})")
                self.status_label.setStyleSheet("color: #00FF00; font-weight: bold; border: none;")
                self.btn_buy.hide(); self.btn_cancel.show()
            else:
                self.status_label.setText(f"○ 라이선스 만료 예정: {expire_str}")
                self.status_label.setStyleSheet("color: #FFA500; font-weight: bold; border: none;")
                self.btn_buy.show(); self.btn_buy.setText("다시 구독"); self.btn_cancel.hide()
        else:
            self.status_label.setText("○ 일반 모드 (프리미엄 미가입)")
            self.status_label.setStyleSheet("color: #777; border: none;")
            self.btn_buy.show(); self.btn_buy.setText("프리미엄 구독 (1,000,000원)"); self.btn_cancel.hide()

        self.filter_table()

    def filter_table(self):
        idx     = self.search_combo.currentIndex()
        text    = self.search_input.text().lower().strip()
        is_sub  = self._is_subscribed()

        self.table.setRowCount(0)
        filtered = []

        for ev in self.all_events:
            pub      = ev.get('public_text', ev.get('public', '')).lower()
            target   = ev.get('target', '').lower()
            category = ev.get('category', ev.get('cat', '')).lower()
            date_str = ev.get('date', '').lower()

            # 검색어 없으면 전체 표시
            if not text:
                match = True
            elif idx == 0: match = any(text in s for s in [date_str, category, target, pub])
            elif idx == 1: match = text in date_str
            elif idx == 2: match = text in category
            elif idx == 3: match = text in target
            elif idx == 4: match = text in pub
            else:          match = True

            if not match:
                continue
            filtered.append(ev)

        self.table.setRowCount(len(filtered))
        for i, ev in enumerate(filtered):
            cat     = str(ev.get('category', ev.get('cat', '')))
            summary = ev.get('public_text', ev.get('public', ''))
            summary = summary[:55] + "..." if len(summary) > 55 else summary

            is_premium_row = "💎" in cat

            for j, val in enumerate([ev.get('date', '-'), cat, ev.get('target', '-'), summary]):
                it = QTableWidgetItem(str(val))
                it.setTextAlignment(
                    Qt.AlignmentFlag.AlignCenter if j < 3
                    else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
                )
                if is_premium_row:
                    if is_sub:
                        # 프리미엄 구독 중 → 형광 초록 텍스트 + 어두운 초록 배경
                        it.setForeground(QColor("#00FF00"))
                        it.setFont(QFont("Malgun Gothic", 9, QFont.Weight.Bold))
                        it.setBackground(QColor("#001a00"))
                    else:
                        it.setForeground(QColor("#888888"))
                elif "💀" in cat:
                    it.setForeground(QColor("#FF4444"))
                else:
                    it.setForeground(QColor("#FFFFFF"))
                it.setData(Qt.ItemDataRole.UserRole, ev)
                self.table.setItem(i, j, it)

    # ─────────────────────────────────────────────
    # 상세 뷰 업데이트
    # ─────────────────────────────────────────────
    def _update_detail_view(self):
        selected = self.table.selectedItems()
        if not selected:
            return
        ev      = self.table.item(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        is_sub  = self._is_subscribed()
        cat     = ev.get('category', ev.get('cat', ''))

        self.det_header.setText(f"● 문서 식별번호: {abs(id(ev)) % 1_000_000} | 분류: {cat}")
        self.det_title.setText(f"< {ev.get('target')} >")

        raw_content = ev.get('public_text', ev.get('public', ''))
        if "💎" in cat and "실적예고" in cat and not is_sub:
            display_content = (
                "■ 공시 분석 내용:\n"
                "------------------------------------------\n"
                "해당 공시는 프리미엄 전용 분석 데이터입니다.\n"
                "구독 시 상세 매출액 및 예상 영업이익 확인이 가능합니다.\n"
                "------------------------------------------\n"
                "[ 프리미엄 전용 열람 가능 ]"
            )
        else:
            display_content = raw_content

        meta        = ev.get('meta_ref', {})
        sector      = SECTOR_MAP.get(meta.get('ind', ''), 'Growth')
        report_text = (
            f"상장일: {meta.get('listed_date', '-')} | 섹터: {sector}\n"
            f"------------------------------------------\n"
            f"[기업 정보]\n"
            f"그룹: {meta.get('group', '독립')} | 규모: [{meta.get('tier', '기타')}]\n"
            f"산업: {meta.get('ind', '-')} ({meta.get('sub', '-')})\n\n"
            f"{display_content}"
        )
        self.det_content.setText(report_text)

    # ─────────────────────────────────────────────
    # 구독 처리 (GameService 위임)
    # ─────────────────────────────────────────────
    def _buy_subscription(self):
        if not self._is_subscribed():
            success, new_cash, msg = self.gs.subscribe_news(self.hts.my_cash)
            if not success:
                self._show_toast(msg); return
            self.hts.my_cash = new_cash
            self.hts.sync_ui_with_engine()
            self._show_toast("프리미엄 구독이 시작되었습니다")
        else:
            self.gs.s.has_paid_news_access = True
        self.refresh_data()

    def _cancel_subscription(self):
        self.gs.cancel_news_subscription()
        self.refresh_data()

    def _is_subscribed(self) -> bool:
        b_date = self.gs.s.next_billing_date
        if not b_date:
            return False
        if isinstance(b_date, str):
            try:   b_date = datetime.strptime(b_date, '%Y-%m-%d').date()
            except: return False
        elif hasattr(b_date, 'date'):
            b_date = b_date.date()
        # 만료일이 지나지 않았으면 has_paid_news_access 관계없이 구독 중으로 판단
        # (구독 해제는 갱신 안 함을 의미, 만료 전까지는 프리미엄 유지)
        return self.gs.s.current_date.date() <= b_date

    # ─────────────────────────────────────────────
    # 토스트 알림
    # ─────────────────────────────────────────────
    def _show_toast(self, message: str):
        self.toast_label = QLabel(message, self)
        self.toast_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.toast_label.setStyleSheet("""
            background-color: rgba(20, 0, 0, 230);
            color: #FF4444;
            border: 2px solid #FF4444;
            font-size: 20px;
            font-weight: bold;
            padding: 20px 40px;
            border-radius: 5px;
        """)
        self.toast_label.adjustSize()
        x = (self.width()  - self.toast_label.width())  // 2
        y = (self.height() - self.toast_label.height()) // 2
        self.toast_label.move(x, y)
        self.toast_label.show()
        QTimer.singleShot(1000, self.toast_label.deleteLater)