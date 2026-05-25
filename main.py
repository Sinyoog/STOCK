"""
main.py
게임 실행 진입점.
모든 의존성을 여기서 조립(Wiring)합니다.
"""
import sys
from PyQt6.QtWidgets import QApplication

# ── 엔진 레이어 ─────────────────────────────
from engine.market_state import MarketState
from engine.company      import CompanyManager
from engine.economy      import MacroEngine
from engine.market       import StockMarket
from engine.persistence  import SaveManager

# ── 이벤트 레이어 ────────────────────────────
from events.earnings   import EarningsManager
from events.dispatcher import EventDispatcher

# ── 서비스 레이어 ────────────────────────────
from services.game_service import GameService
from services.news_service import NewsService

# ── UI 레이어 ────────────────────────────────
from ui.main_window import StockHTS


def create_app():
    """의존성 주입 컨테이너: 모든 객체를 순서대로 생성하고 연결합니다."""

    # 1. 공유 상태
    state = MarketState()

    # 2. 엔진 모듈 (순수 로직, UI 없음)
    persistence  = SaveManager(state)
    company_mgr  = CompanyManager(state)
    economy      = MacroEngine(state)
    market       = StockMarket(state, economy, company_mgr, db=persistence)
    earnings     = EarningsManager(state)

    # 3. 이벤트 디스패처 (next_day 순서 제어)
    dispatcher = EventDispatcher(
        state, economy, market, earnings, persistence, company_mgr
    )

    # 4. 서비스 레이어 (UI가 호출하는 유스케이스)
    game_service = GameService(
        state, dispatcher, market, persistence, company_mgr, economy
    )
    news_service = NewsService(state)

    return game_service, news_service


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    game_service, news_service = create_app()

    window = StockHTS(game_service, news_service)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()