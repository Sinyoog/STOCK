"""
ui/group_view.py
그룹사 현황 전용 창/위젯 (비모달, 실시간 갱신 지원).
데이터 연산 금지 — GameService 통해 읽기만 합니다.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QTableWidget,
    QTableWidgetItem, QHeaderView, QPushButton
)
from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QColor, QKeySequence, QShortcut


def _fmt_cap(mc: int) -> str:
    """시총 단위 변환 — 양/자/해/경/조/억/원 전체 지원"""
    mc   = int(mc)
    _양  = 10 ** 48
    _자  = 10 ** 44
    _해  = 10 ** 40
    _경  = 10 ** 16
    _조  = 10 ** 12
    _억  = 10 ** 8
    if   mc >= _양:         return f"{mc/_양:.2f}양"
    elif mc >= _자:         return f"{mc/_자:.2f}자"
    elif mc >= _해:         return f"{mc/_해:.2f}해"
    elif mc >= _경:         return f"{mc/_경:.2f}경"
    elif mc >= 100 * _조:   return f"{mc//_조:,}조"
    elif mc >= 10  * _조:   return f"{mc/_조:.0f}조"
    elif mc >= _조:         return f"{mc/_조:.1f}조"
    elif mc >= _억:         return f"{mc/_억:.1f}억"
    else:                   return f"{mc:,}원"

from .styles import HTS_STYLE


class GroupInfoDialog(QDialog):
    def __init__(self, game_service, parent=None):
        super().__init__(parent)
        self.gs = game_service
        self._is_fullscreen = False
        self.setWindowTitle("🏢 글로벌 그룹사 경영 현황 (실시간)")
        self.resize(1500, 900)
        self.setStyleSheet(HTS_STYLE)

        layout = QVBoxLayout()

        # 상단 헤더 바
        header_bar = QHBoxLayout()
        upper_label = QLabel("📊 [그룹사 경영 순위 요약]")
        upper_label.setStyleSheet("color: #00FF00; font-weight: bold; font-size: 15px;")

        self.btn_fullscreen = QPushButton("⛶ 전체화면")
        self.btn_fullscreen.setFixedWidth(110)
        self.btn_fullscreen.setStyleSheet(
            "QPushButton { background-color: #1a1a1a; color: #00FF00; border: 1px solid #00FF00;"
            " padding: 4px 10px; font-weight: bold; border-radius: 3px; }"
            "QPushButton:hover { background-color: #003300; }"
        )
        self.btn_fullscreen.clicked.connect(self._toggle_fullscreen)

        # F11 단축키
        sc = QShortcut(QKeySequence("F11"), self)
        sc.activated.connect(self._toggle_fullscreen)

        header_bar.addWidget(upper_label)
        header_bar.addStretch()
        header_bar.addWidget(self.btn_fullscreen)
        layout.addLayout(header_bar)

        self.upper_table = QTableWidget(0, 4)
        self.upper_table.setFixedHeight(280)
        self.upper_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.upper_table.setStyleSheet(
            "QTableWidget { background-color: #000; color: #e0e0e0; gridline-color: #222; } "
            "QHeaderView::section { background-color: #222; color: #00FF00; }"
        )
        layout.addWidget(self.upper_table)

        lower_label = QLabel("📋 [그룹사별 상세 종목 현황]")
        lower_label.setStyleSheet("color: #00FF00; font-weight: bold; font-size: 15px; margin-top: 10px;")
        layout.addWidget(lower_label)

        self.lower_table = QTableWidget(0, 15)
        self.lower_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.lower_table.setHorizontalHeaderLabels([
            "전체순위", "규모", "상태", "그룹사", "산업", "회사명", "산업 상세",
            "주가", "주식수", "시가총액", "자사주", "대주주", "외국인", "기관", "개인"
        ])
        self.lower_table.setStyleSheet(
            "QTableWidget { background-color: #000; color: #e0e0e0; gridline-color: #222; } "
            "QHeaderView::section { background-color: #222; color: #00FF00; }"
        )
        layout.addWidget(self.lower_table)

        self.setLayout(layout)
        self.update_all_info()

    def update_all_info(self):
        state = self.gs.s

        # 그룹 데이터 수집
        group_list = []
        max_member_count = 0
        for gid, ginfo in state.groups.items():
            members = [s for s in state.stocks if s['meta'].get('group_id') == gid]
            if members:
                members.sort(key=lambda x: x['market_cap'], reverse=True)
                total_cap = sum(s['market_cap'] for s in members)
                max_member_count = max(max_member_count, len(members))
                group_list.append({
                    'name':        ginfo['name'],
                    'count':       len(members),
                    'total_cap':   total_cap,
                    'member_names': [s['meta']['c_name'] for s in members],
                })
        group_list.sort(key=lambda x: x['total_cap'], reverse=True)

        # 상단 테이블
        base_headers = ["그룹 순위", "그룹명", "계열사", "그룹 시가총액 합계"]
        dyn_headers  = [f"그룹 내 {i+1}위" for i in range(max_member_count)]
        self.upper_table.setColumnCount(len(base_headers) + len(dyn_headers))
        self.upper_table.setHorizontalHeaderLabels(base_headers + dyn_headers)
        self.upper_table.setRowCount(len(group_list))

        for i, g in enumerate(group_list):
            padding = ["-"] * (max_member_count - len(g['member_names']))
            cap_str = f"{g['total_cap']:,}원 ({_fmt_cap(int(g['total_cap']))})"
            row = [i + 1, g['name'], f"{g['count']}개", cap_str] + g['member_names'] + padding
            for j, val in enumerate(row):
                it = QTableWidgetItem(str(val))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if j == 3: it.setForeground(QColor("#FFD700"))
                if j == 4: it.setForeground(QColor("#00FF00"))
                self.upper_table.setItem(i, j, it)

        # 하단 테이블
        gs = sorted(
            [s for s in state.stocks if s['meta'].get('group')],
            key=lambda x: x['market_cap'], reverse=True
        )
        # 전체 시총 기준 순위 계산
        all_sorted_rank = sorted(state.stocks, key=lambda x: x['market_cap'], reverse=True)
        rank_map = {s['meta']['c_name']: i+1 for i, s in enumerate(all_sorted_rank)}

        self.lower_table.setRowCount(len(gs))
        for i, st in enumerate(gs):
            m        = st['meta']
            size_tag = f"[{m.get('tier', '소형주')[0]}]"
            rate     = st.get('rate', 0.0)
            real_rank = rank_map.get(m['c_name'], i+1)
            row = [
                real_rank, size_tag, f"[ {m['char']} ]", m.get('group', '-'),
                m['ind'], m['c_name'], f"{m['ind']}({m['sub']})",
                f"{int(st['price']):,}원 ({rate:+.2f}%)",
                f"{st['shares']:,}주",
                f"{st['market_cap']:,}원 ({_fmt_cap(int(st['market_cap']))})",
                f"{m.get('treasury_share', 0)*100:.1f}%",
                f"{m.get('owner_share',    0)*100:.1f}%",
                f"{m.get('foreign_share',  0)*100:.1f}%",
                f"{m.get('inst_share',     0)*100:.1f}%",
                f"{m.get('retail_share',   0)*100:.1f}%",
            ]
            for j, val in enumerate(row):
                it = QTableWidgetItem(str(val))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if j == 9:  it.setForeground(QColor("#FFD700"))
                elif j == 1 and size_tag == "[대]": it.setForeground(QColor("#FFD700"))
                elif "+" in str(val): it.setForeground(QColor("#FF4444"))
                elif "-"  in str(val) and j not in [0, 3]: it.setForeground(QColor("#4444FF"))
                self.lower_table.setItem(i, j, it)

        for t in [self.upper_table, self.lower_table]:
            t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
            t.resizeColumnsToContents()
            for col in range(t.columnCount()):
                t.setColumnWidth(col, t.columnWidth(col) + 25)

        # 산업 상세 컬럼 최소 너비 보장 (sub가 여러 개일 때 잘리지 않게)
        self.lower_table.setColumnWidth(6, max(self.lower_table.columnWidth(6), 280))
    def _toggle_fullscreen(self):
        """전체화면 ↔ 일반 창 토글 (F11 또는 버튼)"""
        if self._is_fullscreen:
            self.showNormal()
            self.btn_fullscreen.setText("⛶ 전체화면")
            self._is_fullscreen = False
        else:
            self.showMaximized()
            self.btn_fullscreen.setText("❐ 창 모드")
            self._is_fullscreen = True