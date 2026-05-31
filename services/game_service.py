"""
services/game_service.py
GameService: UI 레이어가 호출하는 유스케이스(Use-Case) 모음.
- UI는 엔진 내부를 직접 건드리지 않고 이 서비스만 호출합니다.
- 비즈니스 판단은 여기서, 화면 처리는 ui/ 레이어에서 합니다.
"""
from datetime import datetime, timedelta
from engine.constants import MAIN_INDUSTRIES, NAME_DB


class GameService:
    def __init__(self, state, dispatcher, market, persistence, company_mgr, economy):
        self.s   = state
        self.dp  = dispatcher
        self.mkt = market
        self.db  = persistence
        self.cm  = company_mgr
        self.eco = economy

    # ─────────────────────────────────────────────
    # 게임 진행
    # ─────────────────────────────────────────────
    def next_day(self, silent: bool = False) -> dict:
        return self.dp.next_day_process() if not silent else self._next_day_silent()

    def _next_day_silent(self) -> dict:
        self.dp.next_day(silent=True)
        self.dp.record_current_state()
        return self.dp.get_ui_packet()

    # ─────────────────────────────────────────────
    # 시장 초기화
    # ─────────────────────────────────────────────
    def initialize_market(self, silent: bool = False):
        import random
        print("🚀 [시스템] v19.0 가치 본위 엔진 가동 중...")

        # ★ CompanyManager의 used_all_time을 state와 동일 객체로 연결
        self.cm.used_all_time = self.s.used_all_time

        # ── 그룹사 생성 ──────────────────────────────────────────
        # 최상위 3개 그룹 ("대1" 티어)
        # ★ IT/커뮤/건강관리/산업재/소재 중 3개를 중복 없이 랜덤 배정
        # 각 산업의 1B/2A 핵심 사업을 sub_list에 포함 → 페이즈 전환 시 자연 수혜
        _ELITE_INDS = ["IT", "커뮤니케이션", "건강관리", "산업재", "소재"]
        _elite_pool = random.sample(_ELITE_INDS, 3)  # 중복 없이 3개 선택

        # 산업별 페이즈 선행 사업 (1B/2A 대형 핵심 sub)
        _ELITE_SUBS = {
            "IT":           ["PC용D램", "모바일AP", "팹리스파운드리"],
            "커뮤니케이션": ["이동통신사", "종합포털서비스", "모바일메신저"],
            "건강관리":     ["MRI부품제조", "mRNA백신", "바이오시밀러"],
            "산업재":       ["고속철도차량", "이차전지셀제조", "전기차부품"],
            "소재":         ["정밀화학", "양극재소재", "리튬정제"],
        }

        top3_groups = ["제니스", "서한", "가온"]
        for i, gn in enumerate(top3_groups):
            gid      = f"GROUP_{gn}"
            elite_ind = _elite_pool[i]
            self.s.groups[gid] = {"name": gn, "active": True}

            # 대1 종목: 엘리트 산업 + 선행 사업 sub_list
            stock_d1 = self.cm.create_stock_data(None, elite_ind, "대1", gid)
            # sub_list를 해당 산업의 핵심 사업으로 덮어씀
            elite_subs = _ELITE_SUBS.get(elite_ind, [])
            if elite_subs:
                stock_d1['meta']['sub_list'] = elite_subs[:]
                stock_d1['meta']['sub']      = elite_subs[0]
            self.s.stocks.append(stock_d1)

            # 계열사 1개: 나머지 산업 중 랜덤
            remaining_inds = [x for x in MAIN_INDUSTRIES if x != elite_ind]
            ind2 = random.choice(remaining_inds)
            self.s.stocks.append(self.cm.create_stock_data(None, ind2, "대", gid))

        # 일반 그룹사 2개 ("대" 티어: 시총 1조~20조)
        normal_groups = ["범양", "버거"]
        for gn in normal_groups:
            gid = f"GROUP_{gn}"
            self.s.groups[gid] = {"name": gn, "active": True}
            for ind in random.sample(MAIN_INDUSTRIES, 2):
                self.s.stocks.append(self.cm.create_stock_data(None, ind, "대", gid))

        # ── 독립 대기업 5개 (1조~20조) ───────────────────────────
        for _ in range(5):
            self.s.stocks.append(
                self.cm.create_stock_data(None, random.choice(MAIN_INDUSTRIES), "대")
            )

        # ── 독립 중견 25개 (1000억~2조) ──────────────────────────
        for _ in range(25):
            self.s.stocks.append(
                self.cm.create_stock_data(None, random.choice(MAIN_INDUSTRIES), "중")
            )

        # ── 독립 중소 10개 (100억~1500억) ────────────────────────
        for _ in range(10):
            self.s.stocks.append(
                self.cm.create_stock_data(None, random.choice(MAIN_INDUSTRIES), "소")
            )

        # reassign_tiers_by_cap은 초기화 시 호출하지 않음
        # (tier를 직접 지정해서 생성하므로 재배정 불필요)

        date_str   = self.s.current_date.strftime('%Y-%m-%d')
        db_records = []

        for stock in self.s.stocks:
            self.mkt.apply_stock_event(stock, silent=silent)
            meta = stock['meta']
            tier = meta.get('tier', '소형주')
            rate = stock.get('rate', 0.0) / 100.0

            m = {"대형주": 0.5, "중형주": 1.0, "소형주": 2.5}.get(tier, 1.0)
            if rate > 0:
                meta['foreign_share'] = max(0.001, min(0.99, meta['foreign_share'] * (1.0 + rate * 1.5 * m)))
                meta['inst_share']    = max(0.001, min(0.99, meta['inst_share']    * (1.0 + rate * 1.8 * m)))
                meta['retail_share']  = max(0.001, min(0.99, meta['retail_share']  * (1.0 - rate * 0.8 * m)))
            else:
                meta['foreign_share'] = max(0.001, min(0.99, meta['foreign_share'] * (1.0 + rate * 2.0 * m)))
                meta['inst_share']    = max(0.001, min(0.99, meta['inst_share']    * (1.0 + rate * 1.8 * m)))
                meta['retail_share']  = max(0.001, min(0.99, meta['retail_share']  * (1.0 - rate * 0.5 * m)))

            fixed = meta.get('treasury_share', 0.0) + meta.get('owner_share', 0.0)
            remaining = max(0.0, 1.0 - fixed)
            s_sum = meta['foreign_share'] + meta['inst_share'] + meta['retail_share']
            if s_sum > 0:
                meta['foreign_share'] = (meta['foreign_share'] / s_sum) * remaining
                meta['inst_share']    = (meta['inst_share']    / s_sum) * remaining
                meta['retail_share']  = (meta['retail_share']  / s_sum) * remaining

            db_records.append((date_str, meta['c_name'], int(stock['price']), int(stock['market_cap'])))

        self.db.insert_stock_records(db_records)
        print(f"📦 상장일({date_str}) 데이터 {len(db_records)}건이 DB에 저장되었습니다.")

        # ★ 초기 종목 전체 상장 시점 스냅샷 저장
        for stock in self.s.stocks:
            self.db.save_listing_snapshot(stock)

        self.s.initial_market_total_cap = sum(s['market_cap'] for s in self.s.stocks) or 1.0

    # ─────────────────────────────────────────────
    # 매매
    # ─────────────────────────────────────────────
    def buy_stock(self, name: str, price: int, quantity: int,
                  my_cash: float, my_portfolio: dict) -> tuple:
        """
        Returns (success: bool, new_cash, new_portfolio, message)
        """
        cost = price * quantity
        if cost > my_cash:
            return False, my_cash, my_portfolio, "잔고가 부족합니다."

        new_cash = my_cash - cost
        new_port = dict(my_portfolio)
        if name in new_port:
            held = new_port[name]
            total_qty  = held['shares'] + quantity
            avg_price  = (held['avg_price'] * held['shares'] + price * quantity) / total_qty
            new_port[name] = {'shares': total_qty, 'avg_price': avg_price}
        else:
            new_port[name] = {'shares': quantity, 'avg_price': price}

        return True, new_cash, new_port, f"{name} {quantity}주 매수 완료"

    def sell_stock(self, name: str, price: int, quantity: int,
                   my_cash: float, my_portfolio: dict) -> tuple:
        """
        Returns (success: bool, new_cash, new_portfolio, message)
        """
        if name not in my_portfolio:
            return False, my_cash, my_portfolio, "보유하지 않은 종목입니다."
        held = my_portfolio[name]
        if held['shares'] < quantity:
            return False, my_cash, my_portfolio, "보유 수량이 부족합니다."

        new_cash = my_cash + price * quantity
        new_port = dict(my_portfolio)
        remaining = held['shares'] - quantity
        if remaining == 0:
            del new_port[name]
        else:
            new_port[name] = {'shares': remaining, 'avg_price': held['avg_price']}

        profit = (price - held['avg_price']) * quantity
        return True, new_cash, new_port, f"{name} {quantity}주 매도 완료 (손익: {profit:+,.0f}원)"

    # ─────────────────────────────────────────────
    # 뉴스 구독
    # ─────────────────────────────────────────────
    def subscribe_news(self, my_cash: float) -> tuple:
        """
        Returns (success: bool, new_cash, message)
        """
        COST = 1_000_000
        if my_cash < COST:
            return False, my_cash, "잔액이 부족합니다."

        new_cash   = my_cash - COST
        expiry     = self.s.current_date + timedelta(days=30)
        self.s.next_billing_date    = expiry
        self.s.has_paid_news_access = True
        return True, new_cash, f"프리미엄 구독이 시작되었습니다. (만료: {expiry.strftime('%Y-%m-%d')})"

    def cancel_news_subscription(self):
        self.s.has_paid_news_access = False

    # ─────────────────────────────────────────────
    # 포트폴리오 분할 보정
    # ─────────────────────────────────────────────
    def adjust_portfolio_for_splits(self, my_portfolio: dict) -> dict:
        new_port = dict(my_portfolio)
        for name, ratio in self.s.daily_splits.items():
            if name in new_port:
                held = new_port[name]
                new_port[name] = {
                    'shares':  int(held['shares'] * ratio),
                    'avg_price': held['avg_price'] / ratio,
                }
        return new_port

    # ─────────────────────────────────────────────
    # 치트 / 시나리오 강제 설정
    # ─────────────────────────────────────────────
    def apply_cheat_scenario(self, cheat_key: str):
        SCENARIOS = {
            "대공황V":  "✨ 대공황V (고난과 부활)",
            "대공황":   "💀 대공황 (시스템 붕괴)",
            "T4":       "🚀 T4 발전 (기술 특이점)",
            "테크4":    "🚀 T4 발전 (기술 특이점)",
            "T3":       "🟢 T3 유지 (저성장 정체)",
            "테크3":    "🟢 T3 유지 (저성장 정체)",
            "정상":     "정상 성장",
        }
        target = "정상 성장"
        for key, val in SCENARIOS.items():
            if key.upper() in cheat_key.upper():
                target = val
                break

        if self.s.current_date.year < 2045:
            self.s.world_line          = "Normal"
            self.s.reserved_scenario   = target
            self.s._branch_news_sent   = False  # 분기점 뉴스 재발송 허용
            return f"🔮 [미래 예약] 2050~2060년 분기점 운명이 '{target}'로 고정되었습니다."
        else:
            # 대공황 극복 시나리오: 30일 후 전환 예약
            if "극복" in target or "대공황V" in target:
                from datetime import timedelta
                recovery_date = self.s.current_date + timedelta(days=30)
                self.s.pending_events["recovery"] = {
                    "date":     recovery_date,
                    "scenario": target,
                    "notified": False,
                }
                return f"🔮 [극복 예약] 30일 후({recovery_date.strftime('%Y-%m-%d')}) '{target}' 시나리오가 발동됩니다."
            else:
                self.s.world_line          = "Decided"
                self.s.current_scenario    = target
                return f"✅ 시나리오를 '{target}'로 변경했습니다."

    # ─────────────────────────────────────────────
    # 게임 저장 / 불러오기
    # ─────────────────────────────────────────────
    def save_game(self, my_cash: float, my_portfolio: dict):
        self.db.save_game(my_cash, my_portfolio)

    def load_game(self) -> dict | None:
        result = self.db.load_game()
        if result is not None:
            # 세이브 파일 로드 후 이름 생성기 상태 복원
            self.cm.sync_from_state()
        return result

    def reset_game(self, my_cash: float, my_portfolio: dict):
        """완전 초기화 후 새 게임 시작"""
        self.db.clear_save_file()
        self.db.clear_all_history()
        from engine.market_state import MarketState
        # state를 새 인스턴스로 교체하는 대신 필드를 리셋
        new = MarketState()
        self.s.__dict__.update(new.__dict__)
        # ★ CompanyManager도 완전 리셋 (이전 이름 상태 초기화)
        from engine.constants import NAME_DB, GROUP_BASE_NAMES
        import random
        self.cm.used_all_time      = self.s.used_all_time  # state와 동일 객체 참조
        self.cm.current_generation = 1
        self.cm.name_pool          = [n for n in NAME_DB if n not in GROUP_BASE_NAMES]
        random.shuffle(self.cm.name_pool)
        # ★ dispatcher의 시나리오 로그 추적 변수 초기화
        self.dp._logged_scenario = None
        self.dp._scenario_log_cache = None  # persistence 캐시도 함께
        self.db._scenario_log_cache = None
        self.initialize_market()

    # ─────────────────────────────────────────────
    # UI 조회 헬퍼
    # ─────────────────────────────────────────────
    def get_ui_packet(self) -> dict:
        return self.dp.get_ui_packet()

    def get_macro_snapshot(self) -> dict:
        m = self.s.macro
        return {
            "interest":    m.get("interest_rate", 0),
            "oil":         m.get("oil_price", 0),
            "exchange":    m.get("exchange_rate", 0),
            "cpi_display": f"물가체감: 2000년 ₩1,000 → 현재 ₩{self.s.base_item_price:,.0f}",
        }

    def get_investor_volume(self, name: str, days: int = 252) -> list:
        """호가창용 — SQLite investor_volume 테이블에서 조회"""
        if hasattr(self.db, 'get_investor_volume'):
            return self.db.get_investor_volume(name, days)
        # 폴백: state.daily_volume (구버전 호환)
        vol_list = self.s.daily_volume.get(name, [])
        return vol_list[-days:] if len(vol_list) > days else vol_list

    def get_chart_data(self, company_name: str, days: int = 30) -> list:
        return self.db.get_chart_data(company_name, days)

    def get_stock_by_name(self, name: str) -> dict | None:
        for s in self.s.stocks:
            if s['meta']['c_name'] == name:
                return s
        return None

    def get_delisted_stocks(self) -> list:
        """상폐 종목 전체 조회 — SQLite 우선, 폴백은 state"""
        if hasattr(self.db, 'get_delisted_stocks'):
            return self.db.get_delisted_stocks()
        return self.s.delisted_stocks

    def get_delisted_stock_history(self, name: str) -> list:
        return self.db.get_chart_data(name, 999_999)

    def get_delisted_stock_history_with_dates(self, name: str) -> list:
        """(date, price) 튜플 리스트 반환 — 상폐 역사관 날짜 X축용"""
        return self.db.get_chart_data_with_dates(name)

    def get_listing_snapshot(self, name: str) -> dict:
        """상장 시점 스냅샷 조회"""
        return self.db.get_listing_snapshot(name)

    def get_earnings_history(self, company_name: str) -> dict:
        return self.s.earnings_history.get(company_name, {})

    def get_pre_events(self) -> list:
        return self.s.pre_reflection_events

    def add_pre_event(self, date_str, d_day_val, category, target, public_txt, premium_txt):
        self.s.pre_reflection_events.append({
            "date":         date_str,
            "d_day":        f"D-{d_day_val}",
            "category":     category,
            "target":       target,
            "public_text":  public_txt,
            "premium_text": premium_txt,
        })

    def clear_pre_events(self):
        self.s.pre_reflection_events = []