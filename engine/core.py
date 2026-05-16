# engine/core.py
import sqlite3
import random
import math
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta

# 💡 [치명적 에러 완파] 절대 경로 대신 상대 경로(.)를 사용하여 순환 참조 및 import 격리 차단
from .company import Company, ClusterManager
from .economy import EconomyEngine

# 🌟 [이벤트 클래스 통합] UI(news_view.py)가 읽어갈 수 있도록 백엔드 이벤트 객체 인프라 임포트
from events.earnings import EarningsAnnouncement
from events.IPO import IPOAnnouncement

class GameCore:
    """
    SQLite3 시가총액 히스토리 DB 인프라 관리, 플레이어 매매 비즈니스 연산 원장,
    그리고 '2단계 정보 선반영 타임라인 시스템'을 집행하는 컨트롤 타워 엔진입니다.
    """
    def __init__(self, db_path: str = "market_history.db"):
        self.db_path: str = db_path
        self.cluster: ClusterManager = ClusterManager()
        self.economy: EconomyEngine = EconomyEngine()
        
        self.stock_prices: Dict[str, int] = {}
        self.stock_shares: Dict[str, int] = {}
        
        self.player_cash: int = 1000000
        self.player_portfolio: Dict[str, Dict[str, Any]] = {} 
        self.tax_ratio: float = 0.0025
        
        self.current_day: int = 1
        # 💡 [날짜 오차 정상화] 1999년 백투더퓨처 제거 -> 2000-01-01 고정
        self.current_date: datetime = datetime(2000, 1, 1)
        
        self.corporate_events: Dict[str, Dict[str, Any]] = {}
        self.historical_events_log: List[Any] = []
        self.earnings_history: Dict[str, Dict[str, Any]] = {}

        self.init_database()

    def init_database(self) -> None:
        """시가총액 역사 및 플레이어 계좌 스냅샷 기록을 위한 SQLite3 전용 테이블 구성"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS market_history (
                    day INTEGER,
                    company_name TEXT,
                    tier TEXT,
                    price INTEGER,
                    shares INTEGER,
                    market_cap INTEGER,
                    hp REAL,
                    shield INTEGER,
                    status TEXT,
                    PRIMARY KEY (day, company_name)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS player_ledger (
                    day INTEGER PRIMARY KEY,
                    cash INTEGER,
                    total_asset INTEGER
                )
            """)
            conn.commit()

    def clear_save_file(self) -> None:
        """[시스템 파이프라인] 게임 리셋 시 SQLite3 마켓 역사관 및 플레이어 원장 테이블을 완전히 청소합니다."""
        import sqlite3
        print("🗑️ [백엔드] 기존 마켓 데이터베이스 테이블 초기화를 집행합니다.")
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # 기존 데이터 테이블만 깔끔하게 드롭하거나 비웁니다.
                cursor.execute("DROP TABLE IF EXISTS market_history")
                cursor.execute("DROP TABLE IF EXISTS player_ledger")
                conn.commit()
            # 테이블 구조를 다시 복원 생성합니다.
            self.init_database()
        except Exception as e:
            print(f"⚠️ [백엔드] 데이터베이스 초기화 중 예외 무시: {e}")

    def clear_all_history(self) -> None:
        """[로그 파괴] 메모리에 적재된 기존 타임라인 공시 로그들을 휘발시킵니다."""
        self.historical_events_log.clear()
        self.earnings_history.clear()

    def bootstrap_market(self, initial_count: int = 50) -> None:
        """정해진 수급 한도(예: 50개 제한) 내에서 테크 1단계 및 그룹사 연동 하에 개장"""
        # 💡 [상장 개수 통제]: 허용치 이하로 제한
        count = min(initial_count, self.economy.MAX_STOCKS)
        
        b_limit = max(1, int(count * self.economy.TIER_CONFIG["대형주"]["ratio"]))
        m_limit = max(1, int(count * self.economy.TIER_CONFIG["중형주"]["ratio"]))
        
        # 💡 하위 대기업 그룹군 뼈대 사전 구축 (group_id 주입 인프라 복원)
        group_pool = [f"{name}그룹" for name in self.cluster.group_base_names]

        for i in range(count):
            if i < b_limit:
                tier = "대형주"
                # 대형주 중 일부는 확률적으로 모기업(그룹사) 완장 부여
                g_id = random.choice(group_pool) if random.random() < 0.6 else None
            elif i < b_limit + m_limit:
                tier = "중형주"
                g_id = random.choice(group_pool) if random.random() < 0.3 else None
            else:
                tier = "소형주"
                g_id = random.choice(group_pool) if random.random() < 0.1 else None
                
            config = self.economy.TIER_CONFIG[tier]
            shares = random.randint(config["shares"][0], config["shares"][1]) * 1_000_000
            price = random.randint(config["price"][0], config["price"][1])
            
            ind = random.choice(self.economy.MAIN_INDUSTRIES)
            
            # 💡 [치명적 테크 규칙 복원] 시작 시 무조건 기술 1단계 풀에서 명칭 추출
            self.economy.tech_levels[ind] = 1 
            sub_tech = random.choice(self.economy.INDUSTRY_LEVELS[ind][1])
            
            # 이름 빌딩 시 그룹 멤버 여부 처리
            if g_id:
                unique_name = self.cluster.get_unique_name(True, g_id, f"제조/계열")
            else:
                unique_name = self.cluster.get_unique_name(False, None, None)
                
            comp = Company(name=unique_name, ind=ind, sub_tech=sub_tech, tier=tier, group_id=g_id)
            comp.sync_tier_spec(tier, init_mode=True)
            
            self.cluster.companies[unique_name] = comp
            self.stock_prices[unique_name] = price
            self.stock_shares[unique_name] = shares

    def query_premium_information(self) -> List[Dict[str, Any]]:
        """[유료 정보 레이어 제공 구역]: 플레이어가 정보 비용을 지불하고 접근하는 3일 뒤 미래 실적 예고 선반영 리스트업"""
        leaks = []
        for name, event in self.corporate_events.items():
            days_left = event["target_day"] - self.current_day
            if 0 < days_left <= 3:
                leaks.append({
                    "company_name": name,
                    "days_left": days_left,
                    "expected_impact": "호실적 어닝 서프라이즈 예고" if event["raw_dice"] > 3.0 else "실적 부진 및 재정 대미지 경고" if event["raw_dice"] < -3.0 else "평이한 실적 예상"
                })
        return leaks

    def process_one_day_pipeline(self) -> List[str]:
        """
        [요일 밀림 버그 완파] 
        - 월~금요일 정밀 개장 및 토~일요일 정밀 휴장 룰 집행
        - 날짜 전진 전 '현재 날짜'의 요일을 선제 판정하여 화~토 작동 에러 차단
        """
        # 💡 [정밀 교정 ①] 날짜가 넘어가기 전, 현재 날짜의 요일을 가장 먼저 체크합니다.
        # weekday(): 월=0, 화=1, 수=2, 목=3, 금=4, 토=5, 일=6
        current_weekday = self.current_date.weekday()

        if current_weekday in [5, 6]:  # 5(토), 6(일) 이면 마켓 비즈니스 로직 올스톱
            msg = f"📅 [{self.current_date.strftime('%Y-%m-%d')}] 토/일 주말 정기 휴장으로 인해 대한민국 거래소 운영이 중단됩니다."
            
            # 💡 주말이므로 주가 변동이나 공시 없이 '날짜와 턴'만 깔끔하게 하루 전진시키고 탈출합니다.
            self.current_day += 1
            self.current_date += timedelta(days=1)
            return [msg]

        # 💡 정확히 월(0) ~ 금(4)요일 일 때만 아래의 1~5단계 파이프라인이 집행됩니다.
        daily_announcements = []

        # ==========================================
        # 1단계: 거시경제 변동 및 테크 도약 시 기존 상장사 실시간 싱크
        # ==========================================
        old_tech_levels = self.economy.tech_levels.copy()
        macro_news = self.economy.step_macro_economy()
        daily_announcements.extend(macro_news)
        
        for main_ind, new_lv in self.economy.tech_levels.items():
            if new_lv > old_tech_levels[main_ind]:
                for comp in self.cluster.companies.values():
                    if comp.ind == main_ind:
                        comp.sub_tech = random.choice(self.economy.INDUSTRY_LEVELS[main_ind][new_lv])

        self.cluster.apply_group_synergy_and_shocks()
        inflation_mult = self.economy.get_inflation_multiplier()

        # ==========================================
        # 2단계: 타임라인 이벤트 스케줄러 & 2단계 선반영 엔진 집행
        # ==========================================
        for name, comp in list(self.cluster.companies.items()):
            if name not in self.corporate_events:
                if random.random() < 0.05:
                    self.corporate_events[name] = {
                        "target_day": self.current_day + 4,
                        "raw_dice": random.uniform(-10.0, 10.0) * inflation_mult,
                        "insider_leak": False
                    }

            if name in self.corporate_events:
                event = self.corporate_events[name]
                days_left = event["target_day"] - self.current_day
                
                if days_left > 0:
                    event["insider_leak"] = True
                    leak_impact = event["raw_dice"] * 0.02 / days_left 
                    self.stock_prices[name] = max(100, int(self.stock_prices[name] * (1.0 + leak_impact)))
                    
                    if days_left == 3:
                        meta_payload = {
                            "c_name": name, "ind": comp.ind, "sub": comp.sub_tech, "tier": comp.tier,
                            "group": comp.group_id, "rev": int(abs(event["raw_dice"]) * 10_000_000_000),
                            "op": int(event["raw_dice"] * 1_000_000_000), "ni": int(event["raw_dice"] * 800_000_000)
                        }
                        earn_event = EarningsAnnouncement(self.current_date, "💎 실적예고(P)", meta_payload, name, mode="예고")
                        self.historical_events_log.append(earn_event)
                
                elif days_left == 0:
                    final_dice = event["raw_dice"]
                    meta_payload = {
                        "c_name": name, "ind": comp.ind, "sub": comp.sub_tech, "tier": comp.tier,
                        "group": comp.group_id, "rev": int(abs(final_dice) * 10_000_000_000),
                        "op": int(final_dice * 1_000_000_000), "ni": int(final_dice * 800_000_000),
                        "prev_rev": 50_000_000_000, "prev_op": 5_000_000_000
                    }
                    
                    if final_dice < -3.0:
                        daily_announcements.append(f"📉 [실적공시] {name}가 당기순이익 적자 전환으로 인해 재정 타격을 입었습니다.")
                    elif final_dice > 4.0:
                        daily_announcements.append(f"🚀 [실적공시] {name}이 어닝 서프라이즈를 달성하며 대규모 자본 유보금(Shield)을 확충했습니다.")
                    
                    confirm_event = EarningsAnnouncement(self.current_date, "📢 실적공시", meta_payload, name, mode="확정")
                    confirm_event.apply_to_engine(self)
                    self.historical_events_log.append(confirm_event)
                    
                    q_str = f"{((self.current_day // 90) % 4) + 1}분기"
                    self.earnings_history.setdefault(name, {}).setdefault(str(self.current_date.year), {})[q_str] = meta_payload
                    self.corporate_events.pop(name)

            warning_news = comp.update_warning_status()
            if warning_news:
                daily_announcements.append(warning_news)

        # ==========================================
        # 3단계: 평시 주가 변동성 다이스 투사 및 5대 지분 이동
        # ==========================================
        for name, comp in list(self.cluster.companies.items()):
            sector = self.economy.SECTOR_MAP.get(comp.ind, "Value")
            up_w, down_w = self.economy.calculate_price_volatility_weights(comp.hp, comp.hp_soft_cap, sector)
            
            base_change = random.uniform(-0.14, 0.14)
            final_change = base_change * up_w if base_change >= 0 else base_change * down_w
            
            current_p = self.stock_prices[name]
            self.stock_prices[name] = max(100, (int(current_p * (1.0 + final_change)) // 100) * 100)
            comp.shift_share_structure(final_change)
            
            p, s, action_msg = comp.check_and_execute_corporate_action(self.stock_prices[name], self.stock_shares[name])
            if action_msg:
                self.stock_prices[name] = p
                self.stock_shares[name] = s
                daily_announcements.append(action_msg)

        # ==========================================
        # 4단계: 시가총액 본위 스펙 서열 재정렬
        # ==========================================
        tier_news = self.cluster.reassign_all_tiers(self.stock_prices, self.stock_shares)
        daily_announcements.extend(tier_news)

        # ==========================================
        # 5단계: 한계 기업 상장폐지 퇴출 및 우회 상장 순환
        # ==========================================
        for name, comp in list(self.cluster.companies.items()):
            if comp.hp <= 0 or (comp.is_warning and comp.warning_countdown == 0):
                daily_announcements.append(f"☠️ [상장폐지] {name}({comp.ind})가 재정 완전 파산으로 인해 영구 퇴출됩니다.")
                
                self.cluster.delisted_companies.append(comp)
                self.cluster.companies.pop(name)
                self.stock_prices.pop(name, None)
                self.stock_shares.pop(name, None)
                
                if name in self.player_portfolio:
                    daily_announcements.append(f"🚨 [자산손실] 보유 종목 {name}의 상장폐지로 인해 투자 원금이 소멸되었습니다.")
                    self.player_portfolio.pop(name)
                
                new_ind = random.choice(self.economy.MAIN_INDUSTRIES)
                new_lv = self.economy.tech_levels[new_ind]
                new_sub = random.choice(self.economy.INDUSTRY_LEVELS[new_ind][new_lv])
                new_name = self.cluster.get_unique_name(False, None, None)
                
                cfg = self.economy.TIER_CONFIG["소형주"]
                ipo_price = random.randint(cfg["price"][0], cfg["price"][1])
                ipo_shares = random.randint(cfg["shares"][0], cfg["shares"][1]) * 1_000_000
                
                ipo_meta = {
                    "sub_name": new_name, "shares": ipo_shares, "price": ipo_price,
                    "market_cap": ipo_price * ipo_shares, "g_id": None, "ind": new_ind,
                    "sub": new_sub, "tier": "소형주"
                }
                
                ipo_event = IPOAnnouncement(self.current_date, "🚀 신규상장", ipo_meta, new_name)
                ipo_event.apply_to_engine(self)
                
                self.stock_prices[new_name] = ipo_price
                self.stock_shares[new_name] = ipo_shares
                self.historical_events_log.append(ipo_event)
                
                daily_announcements.append(f"🌱 [우회상장] 스타트업 {new_name}({new_ind} - {new_sub})가 거래소에 긴급 특례 상장되었습니다.")

        # 💡 [정밀 교정 ②] 모든 평일 마켓 연산이 완료된 최종 시점에 스냅샷을 찍고 날짜를 전진시킵니다.
        self.save_daily_market_snapshot()
        self.current_day += 1
        self.current_date += timedelta(days=1)
        return daily_announcements

    def execute_user_order(self, order_type: str, company_name: str, amount: int) -> Dict[str, Any]:
        """[매매 백엔드 트랜잭션 원장]: 플레이어의 예수금 및 보유 수량을 세금/수수료 공식에 맞게 정밀 차감/가산 처리합니다."""
        if company_name not in self.cluster.companies:
            return {"success": False, "msg": "존재하지 않거나 상장 폐지된 종목입니다."}
            
        current_price = self.stock_prices[company_name]
        
        if order_type == "BUY":
            total_cost = int(current_price * amount * (1.0 + self.tax_ratio))
            if self.player_cash < total_cost:
                return {"success": False, "msg": f"예수금이 부족합니다. (필요 자산: {total_cost}원)"}
            
            self.player_cash -= total_cost
            pf = self.player_portfolio.setdefault(company_name, {"amount": 0, "avg_price": 0.0})
            
            total_amount = pf["amount"] + amount
            total_value = (pf["amount"] * pf["avg_price"]) + (current_price * amount)
            pf["avg_price"] = round(total_value / total_amount, 2)
            pf["amount"] = total_amount
            
            return {"success": True, "msg": f"🎉 {company_name} {amount}주 매수 완료. 체결가: {current_price}원"}
            
        elif order_type == "SELL":
            pf = self.player_portfolio.get(company_name, {"amount": 0, "avg_price": 0.0})
            if pf["amount"] < amount:
                return {"success": False, "msg": f"보유 수량이 부족합니다. (현재 보유: {pf['amount']}주)"}
                
            total_revenue = int(current_price * amount * (1.0 - self.tax_ratio))
            self.player_cash += total_revenue
            pf["amount"] -= amount
            
            if pf["amount"] == 0:
                self.player_portfolio.pop(company_name)
                
            return {"success": True, "msg": f"💵 {company_name} {amount}주 매도 체결 완료. 환수 예수금: {total_revenue}원"}

        return {"success": False, "msg": "잘못된 주문 타입 유형입니다."}

    def save_daily_market_snapshot(self) -> None:
        """현재 턴의 모든 종목 가격과 플레이어 총자산 변화 이력을 SQLite3 인프라에 일괄 적재합니다."""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            records = []
            for name, comp in self.cluster.companies.items():
                price = self.stock_prices[name]
                shares = self.stock_shares[name]
                records.append((
                    self.current_day, name, comp.tier, price, shares,
                    price * shares, round(comp.hp, 2), comp.shield, comp.char_status
                ))
            
            cursor.executemany("""
                INSERT OR REPLACE INTO market_history 
                (day, company_name, tier, price, shares, market_cap, hp, shield, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, records)
            
            holding_value = sum(self.stock_prices[name] * data["amount"] for name, data in self.player_portfolio.items() if name in self.stock_prices)
            total_asset = self.player_cash + holding_value
            cursor.execute("""
                INSERT OR REPLACE INTO player_ledger (day, cash, total_asset) VALUES (?, ?, ?)
            """, (self.current_day, self.player_cash, total_asset))
            conn.commit()