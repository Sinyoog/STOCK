"""
engine/persistence.py
SaveManager: 게임 저장·불러오기(JSON), SQLite 주가 DB 관리.
UI 코드 금지.
"""
import json
import os
import copy
import sqlite3
from datetime import datetime


class SaveManager:
    def __init__(self, state):
        self.s    = state
        self.conn = sqlite3.connect("stock_data.db", check_same_thread=False)
        self._create_db()

    # ─────────────────────────────────────────────
    # DB 초기화
    # ─────────────────────────────────────────────
    def _create_db(self):
        try:
            cur = self.conn.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_history (
                    date         TEXT,
                    company_name TEXT,
                    price        INTEGER,
                    market_cap   INTEGER
                )
            """)
            self.conn.commit()
        except Exception as e:
            print(f"❌ DB 테이블 생성 실패: {e}")

    def insert_stock_records(self, records: list):
        """[(date_str, name, price, cap), …] 일괄 INSERT"""
        try:
            cur = self.conn.cursor()
            cur.executemany("INSERT INTO stock_history VALUES (?, ?, ?, ?)", records)
            self.conn.commit()
        except Exception as e:
            print(f"❌ DB 저장 오류: {e}")

    def get_chart_data(self, company_name: str, days: int = 30) -> list:
        try:
            cur = self.conn.cursor()
            if days > 900_000:
                cur.execute(
                    "SELECT price FROM stock_history WHERE company_name=? ORDER BY date ASC",
                    (company_name,)
                )
                return [r[0] for r in cur.fetchall()]
            else:
                cur.execute(
                    "SELECT price FROM stock_history WHERE company_name=? ORDER BY date DESC LIMIT ?",
                    (company_name, days)
                )
                rows = cur.fetchall()
                if not rows: return []
                return [r[0] for r in rows][::-1]
        except Exception as e:
            print(f"❌ DB 로드 실패: {e}")
            return []

    def update_adjusted_price(self, company_name: str, ratio: float):
        """분할/병합 시 과거 주가를 ratio 배 조정"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "UPDATE stock_history SET price=CAST(price*? AS INTEGER) WHERE company_name=?",
                (ratio, company_name)
            )
            self.conn.commit()
        except Exception as e:
            print(f"❌ DB 수정 주가 반영 실패: {e}")

    def clear_all_history(self):
        try:
            cur = self.conn.cursor()
            cur.execute("DELETE FROM stock_history")
            self.conn.commit()
        except Exception as e:
            print(f"❌ DB 초기화 중 오류: {e}")

    # ─────────────────────────────────────────────
    # 게임 저장
    # ─────────────────────────────────────────────
    def save_game(self, my_cash: float, my_portfolio: dict, filename: str = "save_game.json"):
        try:
            stocks_to_save   = copy.deepcopy(self.s.stocks)
            delisted_to_save = copy.deepcopy(self.s.delisted_stocks)

            for s_list in [stocks_to_save, delisted_to_save]:
                for st in s_list:
                    if 'meta' in st:
                        for key, value in st['meta'].items():
                            if isinstance(value, datetime):
                                st['meta'][key] = value.strftime("%Y-%m-%d %H:%M:%S")

            save_data = {
                "engine": {
                    "current_date":          self.s.current_date.strftime("%Y-%m-%d"),
                    "virtual_weekday":       self.s.virtual_weekday,
                    "daily_history":         {},
                    "stocks":                stocks_to_save,
                    "groups":                self.s.groups,
                    "macro":                 self.s.macro,
                    "base_item_price":       self.s.base_item_price,
                    "cumulative_inflation":  self.s.cumulative_inflation,
                    "has_paid_news_access":  self.s.has_paid_news_access,
                    "next_billing_date":     self.s.next_billing_date.strftime("%Y-%m-%d")
                                             if self.s.next_billing_date else None,
                    "gri":                   self.s.gri,
                    "wsi":                   self.s.wsi,
                    "max_tech_reached":      self.s.max_tech_reached,
                    "delisted_stocks":       delisted_to_save,
                    "current_scenario":      self.s.current_scenario,
                    "world_line":            self.s.world_line,
                    "earnings_history":      self.s.earnings_history,
                    "used_all_time":         list(self.s.used_all_time),
                },
                "player": {
                    "my_cash":       my_cash,
                    "my_portfolio":  my_portfolio,
                },
            }

            with open(filename, "w", encoding="utf-8") as f:
                json.dump(save_data, f, ensure_ascii=False, default=str)

            print(f"💾 [{self.s.current_date.strftime('%Y-%m-%d')}] 스냅샷이 안전하게 저장되었습니다.")
        except Exception as e:
            print(f"❌ 저장 중 오류: {e}")

    def load_game(self, filename: str = "save_game.json"):
        if not os.path.exists(filename):
            print("⚠️ 세이브 파일을 찾을 수 없습니다.")
            return None
        try:
            with open(filename, "r", encoding="utf-8") as f:
                data = json.load(f)

            eng = data["engine"]
            self.s.current_date          = datetime.strptime(eng["current_date"], "%Y-%m-%d")
            self.s.virtual_weekday       = eng["virtual_weekday"]
            self.s.daily_history         = {}
            self.s.stocks                = eng["stocks"]
            self.s.groups                = eng["groups"]
            self.s.macro                 = eng["macro"]
            self.s.base_item_price       = eng["base_item_price"]
            self.s.cumulative_inflation  = eng.get("cumulative_inflation", 1.0)
            self.s.gri                   = eng["gri"]
            self.s.wsi                   = eng["wsi"]
            self.s.max_tech_reached      = eng["max_tech_reached"]
            self.s.delisted_stocks       = eng["delisted_stocks"]
            self.s.current_scenario      = eng["current_scenario"]
            self.s.world_line            = eng["world_line"]
            self.s.earnings_history      = eng["earnings_history"]
            self.s.used_all_time         = set(eng.get("used_all_time", []))
            self.s.has_paid_news_access  = eng.get("has_paid_news_access", False)

            billing_str = eng.get("next_billing_date")
            self.s.next_billing_date = datetime.strptime(billing_str, "%Y-%m-%d") if billing_str else None

            print(f"✅ [{self.s.current_date.strftime('%Y-%m-%d')}] 게임을 성공적으로 불러왔습니다.")
            return data["player"]
        except Exception as e:
            print(f"❌ 불러오기 중 오류: {e}")
            return None

    def clear_save_file(self, filename: str = "save_game.json"):
        if os.path.exists(filename):
            os.remove(filename)
