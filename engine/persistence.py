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
            # 기존 테이블 컬럼이 INTEGER면 REAL로 마이그레이션
            cur.execute("PRAGMA table_info(stock_history)")
            cols = {row[1]: row[2] for row in cur.fetchall()}
            if cols.get('price') == 'INTEGER' or cols.get('market_cap') == 'INTEGER':
                cur.execute("DROP TABLE IF EXISTS stock_history")
                self.conn.commit()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS stock_history (
                    date         TEXT,
                    company_name TEXT,
                    price        REAL,
                    market_cap   REAL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS gri_history (
                    date         TEXT PRIMARY KEY,
                    gri          REAL,
                    bubble_index REAL
                )
            """)
            self.conn.commit()
        except Exception as e:
            print(f"❌ DB 테이블 생성 실패: {e}")

    def insert_gri_record(self, date_str: str, gri: float, bubble_index: float = 0.0):
        """GRI 일별 데이터 저장"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "INSERT OR REPLACE INTO gri_history VALUES (?, ?, ?)",
                (date_str, round(gri, 2), round(bubble_index, 2))
            )
            self.conn.commit()
        except Exception as e:
            print(f"❌ GRI DB 저장 오류: {e}")

    def get_gri_history(self, days: int = 0) -> list:
        """GRI 히스토리 조회. days=0이면 전체"""
        try:
            cur = self.conn.cursor()
            if days == 0:
                cur.execute("SELECT date, gri FROM gri_history ORDER BY date ASC")
            else:
                cur.execute(
                    "SELECT date, gri FROM gri_history ORDER BY date DESC LIMIT ?",
                    (days,)
                )
                rows = cur.fetchall()
                return list(reversed(rows)) if rows else []
            return cur.fetchall()
        except Exception as e:
            print(f"❌ GRI DB 로드 실패: {e}")
            return []

    def insert_stock_records(self, records: list):
        """[(date_str, name, price, cap), …] 일괄 INSERT"""
        try:
            # SQLite REAL은 float64 — 값 float 변환으로 오버플로우 방지
            safe = [(d, n, float(p), float(c)) for d, n, p, c in records]
            cur = self.conn.cursor()
            cur.executemany("INSERT INTO stock_history VALUES (?, ?, ?, ?)", safe)
            self.conn.commit()
        except Exception as e:
            print(f"❌ DB 저장 오류: {e}")

    def get_first_price(self, company_name: str) -> float:
        """종목의 첫 번째 주가 기록 반환 (initial_price 복원용)"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT price FROM stock_history WHERE company_name=? ORDER BY date ASC LIMIT 1",
                (company_name,)
            )
            row = cur.fetchone()
            return float(row[0]) if row else 0.0
        except Exception:
            return 0.0

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

    def get_first_price(self, company_name: str) -> float:
        """종목의 첫 번째 주가 기록 반환 (initial_price 복원용)"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT price FROM stock_history WHERE company_name=? ORDER BY date ASC LIMIT 1",
                (company_name,)
            )
            row = cur.fetchone()
            return float(row[0]) if row else 0.0
        except Exception:
            return 0.0

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
            cur.execute("DELETE FROM gri_history")
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
                    "prev_gri":              getattr(self.s, 'prev_gri', self.s.gri),
                    "peak_gri":              getattr(self.s, 'peak_gri', self.s.gri),
                    "gri_base_at_rebase":    getattr(self.s, 'gri_base_at_rebase', 1000.0),
                    "bubble_index":          getattr(self.s, 'bubble_index', 0.0),
                    "avg_earnings_growth":   getattr(self.s, 'avg_earnings_growth', 0.05),
                    "max_tech_reached":      self.s.max_tech_reached,
                    "delisted_stocks":       delisted_to_save,
                    "current_scenario":      self.s.current_scenario,
                    "world_line":            self.s.world_line,
                    "reserved_scenario":     self.s.reserved_scenario,
                    "branch_activation_date": getattr(self.s, '_branch_activation_date', None) and
                                              self.s._branch_activation_date.strftime("%Y-%m-%d")
                                              if hasattr(getattr(self.s, '_branch_activation_date', None), 'strftime')
                                              else None,
                    "leading_index":         getattr(self.s, 'leading_index', 0.0),
                    "cycle_stage":           getattr(self.s, 'cycle_stage', '확장'),
                    "cycle_day":             getattr(self.s, 'cycle_day', 0),
                    "sentiment":             getattr(self.s, 'sentiment', 50.0),
                    "prev_total_market_cap": getattr(self.s, '_prev_total_market_cap', 0.0),
                    "gri_history_20":        getattr(self.s, '_gri_history_20', []),
                    "tech_upgrade_year":     getattr(self.s, '_tech_upgrade_year', 1999),
                    "tech_upgrade_year":     getattr(self.s, '_tech_upgrade_year', 1999),
                    "depression_warning_lv": getattr(self.s, '_depression_warning_sent_lv', 0),
                    "prev_macro_snapshot":   getattr(self.s, '_prev_macro_snapshot', {}),
                    "earnings_history":      self.s.earnings_history,
                    "pending_delist":        {
                        k: {"date": v["date"].strftime("%Y-%m-%d") if hasattr(v.get("date"), "strftime") else str(v.get("date", "")), "reason": v.get("reason", "")}
                        if isinstance(v, dict) else str(v)
                        for k, v in self.s.pending_events.get("delist", {}).items()
                    },
                    "gdp":                   getattr(self.s, 'gdp', 600_000_000_000_000.0),
                    "buffett_index":         getattr(self.s, 'buffett_index', 0.0),
                    "foreign_flow_index":    getattr(self.s, 'foreign_flow_index', 0.0),
                    "used_all_time":         list(self.s.used_all_time),
                    "pending_delist":        {
                        k: {"date": v["date"].strftime("%Y-%m-%d") if hasattr(v.get("date"), "strftime") else str(v.get("date", "")), "reason": v.get("reason", "")}
                        if isinstance(v, dict) else str(v)
                        for k, v in self.s.pending_events.get("delist", {}).items()
                    },
                    "gdp":                   getattr(self.s, 'gdp', 600_000_000_000_000.0),
                    "buffett_index":         getattr(self.s, 'buffett_index', 0.0),
                    "foreign_flow_index":    getattr(self.s, 'foreign_flow_index', 0.0),
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
            self.s.peak_gri              = eng.get("peak_gri", self.s.gri)
            self.s.prev_gri              = eng.get("prev_gri", self.s.gri)
            self.s.gri_base_at_rebase    = eng.get("gri_base_at_rebase", 1000.0)
            self.s.bubble_index          = eng.get("bubble_index", 0.0)
            self.s.avg_earnings_growth   = eng.get("avg_earnings_growth", 0.05)
            self.s.max_tech_reached      = eng["max_tech_reached"]
            self.s.delisted_stocks       = eng["delisted_stocks"]
            self.s.current_scenario      = eng["current_scenario"]
            self.s.world_line            = eng["world_line"]
            self.s.reserved_scenario     = eng.get("reserved_scenario", "")
            branch_date_str = eng.get("branch_activation_date")
            if branch_date_str:
                try:
                    self.s._branch_activation_date = datetime.strptime(branch_date_str, "%Y-%m-%d")
                except Exception:
                    self.s._branch_activation_date = None
            else:
                self.s._branch_activation_date = None
            self.s.leading_index               = eng.get("leading_index", 0.0)
            self.s.cycle_stage                 = eng.get("cycle_stage", "확장")
            self.s.cycle_day                   = eng.get("cycle_day", 0)
            self.s.sentiment                   = eng.get("sentiment", 50.0)
            self.s._prev_total_market_cap      = eng.get("prev_total_market_cap", 0.0)
            self.s._gri_history_20             = eng.get("gri_history_20", [])
            self.s._tech_upgrade_year          = eng.get("tech_upgrade_year", 1999)
            self.s._tech_upgrade_year          = eng.get("tech_upgrade_year", 1999)
            self.s._depression_warning_sent_lv = eng.get("depression_warning_lv", 0)
            self.s._prev_macro_snapshot        = eng.get("prev_macro_snapshot", {})
            self.s.earnings_history      = eng["earnings_history"]
            self.s.used_all_time         = set(eng.get("used_all_time", []))

            # 신규 필드 복원
            self.s.gdp                = eng.get("gdp", 600_000_000_000_000.0)
            self.s.buffett_index      = eng.get("buffett_index", 0.0)
            self.s.foreign_flow_index = eng.get("foreign_flow_index", 0.0)

            # pending_events delist 복원
            pending_delist = eng.get("pending_delist", {})
            self.s.pending_events["delist"] = {}
            for k, v in pending_delist.items():
                if isinstance(v, dict):
                    date_str = v.get("date", "")
                    try:
                        from datetime import datetime as _dt
                        self.s.pending_events["delist"][k] = {
                            "date":   _dt.strptime(date_str, "%Y-%m-%d"),
                            "reason": v.get("reason", "재무 파탄")
                        }
                    except Exception:
                        pass

            # ★ initial_price 복원 (DB에서 첫 주가 조회)
            for stock in self.s.stocks:
                meta = stock['meta']
                if not meta.get('initial_price') or meta.get('initial_price', 0) <= 10:
                    name = meta.get('c_name', '')
                    first_price = self.get_first_price(name)
                    if first_price > 10:
                        meta['initial_price'] = first_price

            # 신규 필드 복원
            self.s.gdp                = eng.get("gdp", 600_000_000_000_000.0)

            # ★ initial_price 복원 (DB에서 첫 주가 조회)
            # 기존 세이브 파일에 initial_price 없는 종목에 대해 DB에서 복원
            for stock in self.s.stocks:
                meta = stock['meta']
                if not meta.get('initial_price') or meta.get('initial_price', 0) <= 10:
                    name = meta.get('c_name', '')
                    first_price = self.get_first_price(name)
                    if first_price > 10:
                        meta['initial_price'] = first_price

            # delisted_stocks도 동일하게 복원
            for stock in self.s.delisted_stocks:
                meta = stock['meta']
                if not meta.get('initial_price') or meta.get('initial_price', 0) <= 10:
                    name = meta.get('c_name', '')
                    first_price = self.get_first_price(name)
                    if first_price > 10:
                        meta['initial_price'] = first_price
            self.s.buffett_index      = eng.get("buffett_index", 0.0)
            self.s.foreign_flow_index = eng.get("foreign_flow_index", 0.0)

            # pending_events delist 복원
            pending_delist = eng.get("pending_delist", {})
            self.s.pending_events["delist"] = {}
            for k, v in pending_delist.items():
                if isinstance(v, dict):
                    date_str = v.get("date", "")
                    try:
                        from datetime import datetime as _dt
                        self.s.pending_events["delist"][k] = {
                            "date":   _dt.strptime(date_str, "%Y-%m-%d"),
                            "reason": v.get("reason", "재무 파탄")
                        }
                    except Exception:
                        pass
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