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
        # WAL 모드: commit 속도 대폭 개선 + 읽기/쓰기 동시성
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_db()

    def _slim_earnings_history(self) -> dict:
        """earnings_history 최근 8분기(2년)만 유지 — JSON 경량화"""
        slim = {}
        for name, yearly in self.s.earnings_history.items():
            recent_years = sorted(yearly.keys())[-2:]
            slim[name] = {y: yearly[y] for y in recent_years}
        return slim

    def _slim_delisted_stocks(self) -> list:
        """delisted_stocks 무거운 필드 제거 + datetime 직렬화 — JSON 경량화"""
        _EXCLUDE = {
            'pending_split', '_earnings_shock', '_earnings_just_released',
            'momentum', 'cap_exceed_days', 'cap_below_days',
            'split_cooldown_days', 'will_to_split',
        }
        result = []
        for st in self.s.delisted_stocks:
            slim_meta = {}
            for k, v in st['meta'].items():
                if k in _EXCLUDE:
                    continue
                slim_meta[k] = v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v
            result.append({
                'price':      st.get('price', 0),
                'shares':     st.get('shares', 0),
                'market_cap': st.get('market_cap', 0),
                'rate':       st.get('rate', 0.0),
                'meta':       slim_meta,
            })
        return result

    def close(self):
        """게임 종료 시 명시적 DB 닫기 — 종료 딜레이 방지"""
        try:
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS investor_volume (
                    date         TEXT,
                    company_name TEXT,
                    foreign_vol  INTEGER,
                    inst_vol     INTEGER,
                    retail_vol   INTEGER,
                    PRIMARY KEY (date, company_name)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_investor_volume_name_date
                ON investor_volume (company_name, date DESC)
            """)
            # ★ stock_history 인덱스 — company_name 풀스캔 방지
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_stock_history_name
                ON stock_history (company_name)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_stock_history_name_date
                ON stock_history (company_name, date DESC)
            """)
            # ★ delisted_stocks 테이블 — JSON 대신 SQLite 관리
            cur.execute("""
                CREATE TABLE IF NOT EXISTS delisted_stocks (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    c_name        TEXT NOT NULL,
                    listed_date   TEXT,
                    delisted_date TEXT,
                    tier          TEXT,
                    ind           TEXT,
                    sub           TEXT,
                    grp           TEXT,
                    price         REAL,
                    shares        INTEGER,
                    market_cap    REAL,
                    rate          REAL,
                    meta_json     TEXT
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_delisted_name
                ON delisted_stocks (c_name)
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_delisted_date
                ON delisted_stocks (delisted_date DESC)
            """)
            self.conn.commit()
        except Exception as e:
            print(f"❌ DB 테이블 생성 실패: {e}")

    def insert_investor_volume(self, date_str: str, name: str,
                               foreign_vol: int, inst_vol: int, retail_vol: int):
        """단건 거래량 저장 (commit 없음 — flush_daily_db 에서 일괄 커밋)"""
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO investor_volume VALUES (?, ?, ?, ?, ?)",
                (date_str, name, int(foreign_vol), int(inst_vol), int(retail_vol))
            )
        except Exception as e:
            print(f"❌ 거래량 DB 저장 오류: {e}")

    def insert_investor_volume_batch(self, records: list):
        """[(date_str, name, f, i, r), ...] 일괄 INSERT (commit 없음 — flush_daily_db 에서 일괄 커밋)"""
        if not records:
            return
        try:
            safe = [(d, n, int(f), int(i), int(r)) for d, n, f, i, r in records]
            self.conn.executemany(
                "INSERT OR REPLACE INTO investor_volume VALUES (?, ?, ?, ?, ?)", safe
            )
        except Exception as e:
            print(f"❌ 거래량 DB 배치 저장 오류: {e}")

    def get_investor_volume(self, name: str, days: int = 1260,
                            since_date: str = None) -> list:
        """
        일별 투자자별 거래량 조회.
        days: 최대 조회 일수 (기본 1260 = 5년치 거래일)
        since_date: 이 날짜 이후만 조회 (상폐 역사관용 — 상폐일 기준 5년 전)
        """
        try:
            cur = self.conn.cursor()
            if since_date:
                cur.execute(
                    """SELECT date, foreign_vol, inst_vol, retail_vol
                       FROM investor_volume
                       WHERE company_name=? AND date >= ?
                       ORDER BY date DESC LIMIT ?""",
                    (name, since_date, days)
                )
            else:
                cur.execute(
                    """SELECT date, foreign_vol, inst_vol, retail_vol
                       FROM investor_volume
                       WHERE company_name=?
                       ORDER BY date DESC LIMIT ?""",
                    (name, days)
                )
            rows = cur.fetchall()
            return [
                {"date": r[0], "foreign": r[1], "inst": r[2], "retail": r[3]}
                for r in reversed(rows)
            ]
        except Exception as e:
            print(f"❌ 거래량 DB 조회 오류: {e}")
            return []

    def insert_gri_record(self, date_str: str, gri: float, bubble_index: float = 0.0):
        """GRI 일별 데이터 저장 (commit 없음 — flush_daily_db 에서 일괄 커밋)"""
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO gri_history VALUES (?, ?, ?)",
                (date_str, round(gri, 2), round(bubble_index, 2))
            )
        except Exception as e:
            print(f"❌ GRI DB 저장 오류: {e}")

    def save_delisted_stock(self, stock: dict):
        """상폐 종목 1개를 SQLite에 저장 — 상폐 발생 시 즉시 호출"""
        import json as _json
        meta = stock.get('meta', {})
        _EXCLUDE = {
            'pending_split', '_earnings_shock', '_earnings_just_released',
            'momentum', 'cap_exceed_days', 'cap_below_days',
            'split_cooldown_days', 'will_to_split',
        }
        from datetime import datetime as _dt
        slim_meta = {}
        for k, v in meta.items():
            if k in _EXCLUDE: continue
            slim_meta[k] = v.strftime("%Y-%m-%d") if isinstance(v, _dt) else v
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO delisted_stocks
                   (c_name, listed_date, delisted_date, tier, ind, sub, grp,
                    price, shares, market_cap, rate, meta_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    meta.get('c_name', ''),
                    meta.get('listed_date', ''),
                    meta.get('delisted_date', ''),
                    meta.get('tier', '소형주'),
                    meta.get('ind', ''),
                    meta.get('sub', ''),
                    meta.get('group', ''),
                    float(stock.get('price', 0)),
                    int(stock.get('shares', 0)),
                    float(stock.get('market_cap', 0)),
                    float(stock.get('rate', 0.0)),
                    _json.dumps(slim_meta, ensure_ascii=False),
                )
            )
            self.conn.commit()
        except Exception as e:
            print(f"❌ delisted_stocks DB 저장 오류: {e}")

    def get_delisted_stocks(self) -> list:
        """SQLite에서 상폐 종목 전체 조회 — 역사관 표시용"""
        import json as _json
        try:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT c_name, listed_date, delisted_date, tier, ind, sub,
                       grp, price, shares, market_cap, rate, meta_json
                FROM delisted_stocks ORDER BY id ASC
            """)
            result = []
            for row in cur.fetchall():
                meta = _json.loads(row[11]) if row[11] else {}
                meta.update({
                    'c_name': row[0], 'listed_date': row[1],
                    'delisted_date': row[2], 'tier': row[3],
                    'ind': row[4], 'sub': row[5], 'group': row[6],
                })
                result.append({
                    'price': row[7], 'shares': row[8],
                    'market_cap': row[9], 'rate': row[10],
                    'meta': meta,
                })
            return result
        except Exception as e:
            print(f"❌ delisted_stocks DB 조회 오류: {e}")
            return []

    def migrate_delisted_to_db(self, delisted_list: list):
        """기존 JSON의 delisted_stocks를 SQLite로 일괄 이관 (최초 1회)"""
        if not delisted_list:
            return
        try:
            cur = self.conn.cursor()
            cur.execute("SELECT COUNT(*) FROM delisted_stocks")
            if cur.fetchone()[0] > 0:
                return  # 이미 이관됨
            for stock in delisted_list:
                self.save_delisted_stock(stock)
            print(f"✅ delisted_stocks {len(delisted_list)}개 SQLite 이관 완료")
        except Exception as e:
            print(f"❌ delisted_stocks 이관 오류: {e}")

    def flush_daily_db(self):
        """하루치 INSERT를 모두 모은 뒤 한 번만 커밋 — next_day 끝에 1회 호출"""
        try:
            self.conn.commit()
        except Exception as e:
            print(f"❌ DB flush 오류: {e}")

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
        """DB 전체 초기화 — DELETE 대신 DROP+재생성으로 속도 개선"""
        try:
            cur = self.conn.cursor()
            # DROP TABLE이 DELETE보다 수십배 빠름 (수천만 행 삭제 시 렉 방지)
            cur.execute("DROP TABLE IF EXISTS stock_history")
            cur.execute("DROP TABLE IF EXISTS gri_history")
            cur.execute("DROP TABLE IF EXISTS investor_volume")
            cur.execute("DROP TABLE IF EXISTS delisted_stocks")
            self.conn.commit()
            # 테이블 재생성
            self._create_db()
        except Exception as e:
            print(f"❌ DB 초기화 중 오류: {e}")

    # ─────────────────────────────────────────────
    # 게임 저장
    # ─────────────────────────────────────────────
    def save_game(self, my_cash: float, my_portfolio: dict, filename: str = "save_game.json"):
        try:
            stocks_to_save   = copy.deepcopy(self.s.stocks)
            # delisted_stocks: 슬림화 + datetime 직렬화
            delisted_to_save = self._slim_delisted_stocks()

            for st in stocks_to_save:
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
                    "delisted_stocks":       [],  # SQLite delisted_stocks 테이블로 이관
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
                    "earnings_history":      self._slim_earnings_history(),  # 최근 8분기만
                    "pending_delist":        {
                        k: {"date": v["date"].strftime("%Y-%m-%d") if hasattr(v.get("date"), "strftime") else str(v.get("date", "")), "reason": v.get("reason", "")}
                        if isinstance(v, dict) else str(v)
                        for k, v in self.s.pending_events.get("delist", {}).items()
                    },
                    "gdp":                   getattr(self.s, 'gdp', 600_000_000_000_000.0),
                    "buffett_index":         getattr(self.s, 'buffett_index', 0.0),
                    "foreign_flow_index":    getattr(self.s, 'foreign_flow_index', 0.0),
                    "used_all_time":         list(self.s.used_all_time),
                    "name_generation":       getattr(self.s, '_name_generation', 1),
                    "name_pool":             getattr(self.s, '_name_pool', []),
                    "investor_trends":       getattr(self.s, 'investor_trends', {}),
                    "margin_balance":        getattr(self.s, 'margin_balance', {}),
                    "earnings_consensus":    getattr(self.s, 'earnings_consensus', {}),
                    "major_holder_action":   getattr(self.s, 'major_holder_action', {}),
                    # daily_volume: SQLite investor_volume으로 관리 — JSON 제외
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
            # delisted_stocks: JSON → SQLite 이관 (최초 1회)
            json_delisted = eng.get("delisted_stocks", [])
            self.migrate_delisted_to_db(json_delisted)
            self.s.delisted_stocks = self.get_delisted_stocks()
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

            # ★ 이름 생성기 상태 복원 (세대/풀 유지)
            self.s._name_generation = eng.get("name_generation", 1)
            self.s._name_pool       = eng.get("name_pool", [])

            # ★ 신규 state 복원
            self.s.investor_trends      = eng.get("investor_trends", {})
            self.s.margin_balance       = eng.get("margin_balance", {})
            self.s.earnings_consensus   = eng.get("earnings_consensus", {})
            self.s.major_holder_action  = eng.get("major_holder_action", {})
            self.s.daily_volume         = eng.get("daily_volume", {})

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