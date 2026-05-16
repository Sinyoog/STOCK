# events/base.py
class CorporateEvent:
    """모든 기업 공시 및 뉴스 이벤트의 기반이 되는 추상 클래스"""
    def __init__(self, dt, cat, meta, pub):
        self.dt = dt          # 공시 발생 날짜 (datetime)
        self.cat = cat        # 공시 카테고리 (예: "📢 실적공시", "💎 상장예고")
        self.meta = meta      # 공시 세부 데이터 (딕셔너리)
        self.pub = pub        # 공시 주체 (회사명 또는 기관명)

    def generate_news_text(self, is_subscribed: bool = False) -> str:
        """HTS 뉴스 창에 보여줄 구체적인 본문 텍스트를 생성 (구독 여부 방어막 반영)"""
        raise NotImplementedError("하위 클래스에서 반드시 구현해야 합니다.")

    def apply_to_engine(self, engine):
        """이 공시가 발생했을 때 게임 엔진(주가, 자산 등)에 미치는 영향을 반영"""
        pass

    def to_ui_dict(self, is_subscribed: bool = False) -> dict:
        """기존 news.py의 딕셔너리 파이프라인 규격과 100% 호환되도록 가상 변환 어댑터"""
        return {
            "date": self.dt.strftime('%Y-%m-%d') if hasattr(self.dt, 'strftime') else str(self.dt),
            "cat": self.cat,
            "target": self.pub,
            "public": self.generate_news_text(is_subscribed),
            "meta_ref": self.meta,
            "shares": self.meta.get('shares', 50_000_000),
            "market_cap": self.meta.get('market_cap', 0),
            "start_price": self.meta.get('price', 5000)
        }