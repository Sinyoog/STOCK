# events/IPO.py
from .base import CorporateEvent

class IPOAnnouncement(CorporateEvent):
    """자회사 신규 상장 관련 공시를 처리하는 클래스"""
    def __init__(self, dt, cat, meta, pub):
        super().__init__(dt, cat, meta, pub)

    def generate_news_text(self, is_subscribed: bool = False) -> str:
        # 상장예고 단계에서 구독하지 않은 유저에게 정보를 숨길지 여부 결정
        if "💎" in self.cat and not is_subscribed:
            return (
                f"■ 신규 상장 공시:\n"
                f"공시 주체: {self.pub}\n"
                f"------------------------------------------\n"
                f"해당 딜러 상장 일정은 프리미엄 라이선스 전용 정보입니다.\n"
                f"[ 프리미엄 전용 열람 가능 ]"
            )

        shares = self.meta.get('shares', 50_000_000)
        price = self.meta.get('price', 5_000)
        market_cap = shares * price

        return (
            f"■ 신규 상장 공시:\n"
            f"공시 주체: {self.pub}\n"
            f"------------------------------------------\n"
            f"내용: 자회사 [{self.meta.get('sub_name')}] 시장 신규 상장 확정\n"
            f"상장 예정 주식수: {shares:,.0f} 주\n"
            f"공모가 확정 금액: {price:,.0f} 원\n"
            f"예상 시가총액: {market_cap:,.0f} 원"
        )

    def apply_to_engine(self, engine):
        sub_name = self.meta.get('sub_name')
        price = self.meta.get('price', 5000)
        shares = self.meta.get('shares', 50_000_000)
        
        # 신형 도메인 백엔드 자산풀 엔진과 완벽 호환 연동
        if hasattr(engine, 'stock_prices'):
            engine.stock_prices[sub_name] = price
            engine.stock_shares[sub_name] = shares
            if hasattr(engine, 'cluster') and hasattr(engine.cluster, 'companies'):
                # 동적 가상 회사 객체 생성 후 주입하여 크래시 완전 방어
                from types import SimpleNamespace
                engine.cluster.companies[sub_name] = SimpleNamespace(
                    name=sub_name, group_id=self.meta.get('g_id'), ind="Growth",
                    sub_tech="IPO_Asset", tier="[소型주]", char_status="Normal",
                    hp=100.0, shield=10_000_000, split_count=0
                )