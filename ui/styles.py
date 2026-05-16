"""
ui/styles.py
HTS 테마 스타일시트 상수 및 색상 팔레트.
이 파일 외의 곳에서 스타일 문자열을 하드코딩하지 마세요.
"""

# ─────────────────────────────────────────────
# 색상 팔레트
# ─────────────────────────────────────────────
COLOR = {
    "bg_main":       "#0d0d0d",
    "bg_panel":      "#111111",
    "bg_table":      "#000000",
    "bg_header":     "#1a1a1a",
    "bg_selected":   "#1a331a",
    "bg_detail":     "#0a0a0a",

    "text_default":  "#e0e0e0",
    "text_muted":    "#aaaaaa",
    "text_dim":      "#bbb",

    "accent_green":  "#00FF00",
    "accent_red":    "#FF4444",
    "accent_blue":   "#4488FF",
    "accent_yellow": "#FFCC00",
    "accent_cyan":   "#00FFFF",

    "border_main":   "#333333",
    "border_subtle": "#1a1a1a",
    "border_input":  "#444444",
    "grid_line":     "#1a1a1a",
}

# ─────────────────────────────────────────────
# 등락 색상 헬퍼
# ─────────────────────────────────────────────
def rate_color(rate: float) -> str:
    if rate > 0:  return COLOR["accent_red"]
    if rate < 0:  return COLOR["accent_blue"]
    return COLOR["text_default"]


def rate_arrow(rate: float) -> str:
    if rate > 0:  return "▲"
    if rate < 0:  return "▼"
    return " "


# ─────────────────────────────────────────────
# 공통 QSS
# ─────────────────────────────────────────────
HTS_STYLE = f"""
    QMainWindow, QDialog, QWidget {{
        background-color: {COLOR['bg_main']};
        color: {COLOR['text_default']};
        font-family: 'Malgun Gothic', 'NanumGothic', sans-serif;
    }}
    QLabel {{
        color: {COLOR['text_muted']};
    }}
    QTableWidget {{
        background-color: {COLOR['bg_table']};
        gridline-color: {COLOR['grid_line']};
        border: 1px solid {COLOR['border_main']};
        color: #ddd;
        selection-background-color: {COLOR['bg_selected']};
    }}
    QTableWidget::item:selected {{
        background-color: {COLOR['bg_selected']};
    }}
    QHeaderView::section {{
        background-color: {COLOR['bg_header']};
        color: {COLOR['accent_green']};
        padding: 8px;
        border: 1px solid #222;
        font-weight: bold;
    }}
    QComboBox {{
        background-color: {COLOR['bg_panel']};
        color: #eee;
        border: 1px solid {COLOR['border_input']};
        padding: 5px;
        border-radius: 2px;
    }}
    QComboBox QAbstractItemView {{
        background-color: {COLOR['bg_panel']};
        color: #eee;
        selection-background-color: {COLOR['bg_selected']};
    }}
    QLineEdit {{
        background-color: {COLOR['bg_table']};
        border: 1px solid {COLOR['accent_green']};
        color: {COLOR['accent_green']};
        padding: 5px;
        selection-background-color: #004400;
    }}
    QTextEdit {{
        background-color: #050505;
        border: 1px solid #222;
        color: {COLOR['text_dim']};
        font-size: 13px;
        line-height: 150%;
    }}
    QPushButton {{
        background-color: {COLOR['bg_header']};
        color: {COLOR['accent_green']};
        border: 1px solid {COLOR['accent_green']};
        padding: 6px 14px;
        border-radius: 2px;
    }}
    QPushButton:hover {{
        background-color: #1a2a1a;
    }}
    QPushButton:pressed {{
        background-color: #004400;
    }}
    QScrollBar:vertical {{
        background: {COLOR['bg_main']};
        width: 8px;
    }}
    QScrollBar::handle:vertical {{
        background: #333;
        border-radius: 4px;
    }}
    #DetailFrame {{
        background-color: {COLOR['bg_detail']};
        border-left: 2px solid {COLOR['accent_green']};
    }}
"""

# 뉴스 창 추가 스타일
NEWS_STYLE = HTS_STYLE + f"""
    #PremiumBadge {{
        color: {COLOR['accent_yellow']};
        font-weight: bold;
        font-size: 11px;
    }}
"""
