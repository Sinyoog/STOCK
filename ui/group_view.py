"""
ui/group_view.py
그룹사 현황 전용 창/위젯 (비모달, 실시간 갱신 지원).
데이터 연산 금지 — GameService 통해 읽기만 합니다.
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QHeaderView
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor

from .styles import HTS_STYLE


class GroupInfoDialog(QDialog):
    def __init__(self, game_service, parent=None):
        super().__init__(parent)
        self.gs = game_service
        self.setWindowTitle("🏢 글로벌 그룹사 경영 현황 (실시간)")
        self.resize(1500, 900)
        self.setStyleSheet(HTS_STYLE)

        layout = QVBoxLayout()

        upper_label = QLabel("📊 [그룹사 경영 순위 요약]")
        upper_label.setStyleSheet("color: #00FF00; font-weight: bold; font-size: 15px;")
        layout.addWidget(upper_label)

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
        self.lower_table.setHorizontalHeaderLabels([
            "No", "규모", "상태", "그룹사", "산업", "회사명", "산업 상세",
            "주가", "주식수", "시가총액", "자사주", "대주주", "외국인", "기관", "개인"
        ])
        self.lower_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
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
            row = [i + 1, g['name'], f"{g['count']}개", f"{g['total_cap']:,.0f}원"] + g['member_names'] + padding
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
        self.lower_table.setRowCount(len(gs))
        for i, st in enumerate(gs):
            m        = st['meta']
            size_tag = f"[{m.get('tier', '소형주')[0]}]"
            rate     = st.get('rate', 0.0)
            row = [
                i + 1, size_tag, f"[ {m['char']} ]", m.get('group', '-'),
                m['ind'], m['c_name'], f"{m['ind']}({m['sub']})",
                f"{int(st['price']):,}원 ({rate:+.2f}%)",
                f"{st['shares']:,}주", f"{st['market_cap']:,.0f}원",
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