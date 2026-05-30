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
            'split_cooldown_days', 'will_to_split', 'par_value',
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
            # ★ 시나리오 로그 테이블
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scenario_log (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    date          TEXT NOT NULL,
                    scenario      TEXT NOT NULL,
                    gri           REAL,
                    gri_high      REAL,
                    gri_low       REAL,
                    gri_end       REAL,
                    duration_days INTEGER,
                    bubble_index  REAL,
                    interest_rate REAL,
                    oil_price     REAL,
                    exchange_rate REAL,
                    cpi           REAL,
                    grain_price   REAL,
                    metal_price   REAL,
                    semi_index    REAL,
                    per_large     REAL,
                    per_mid       REAL,
                    per_small     REAL,
                    sector_growth REAL,
                    sector_value  REAL,
                    ind_it        REAL,
                    ind_health    REAL,
                    ind_energy    REAL,
                    ind_finance   REAL,
                    ind_industry  REAL,
                    ind_material  REAL,
                    ind_realestate REAL,
                    ind_rebuild   REAL,
                    ind_util      REAL,
                    ind_consumer  REAL,
                    ind_staple    REAL,
                    ind_comm      REAL,
                    war_event     TEXT,
                    note          TEXT
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS listing_snapshots (
                    c_name        TEXT PRIMARY KEY,
                    listed_date   TEXT,
                    tier          TEXT,
                    ind           TEXT,
                    sub           TEXT,
                    grp           TEXT,
                    price         REAL,
                    shares        INTEGER,
                    market_cap    REAL,
                    efficiency    REAL,
                    debt_ratio    REAL,
                    hp            REAL,
                    hp_soft_cap   REAL,
                    owner_share   REAL,
                    foreign_share REAL,
                    inst_share    REAL,
                    retail_share  REAL,
                    treasury_share REAL,
                    credit_grade  TEXT
                )
            """)
            self.conn.commit()

            # ★ scenario_log 컬럼 마이그레이션 (구버전 DB 호환)
            self._migrate_scenario_log()

        except Exception as e:
            print(f"❌ DB 테이블 생성 실패: {e}")


    def _migrate_scenario_log(self):
        """scenario_log 테이블에 누락된 컬럼 자동 추가 (구버전 DB 호환)"""
        new_cols = [
            ("gri_high",       "REAL",    "0"),
            ("gri_low",        "REAL",    "0"),
            ("gri_end",        "REAL",    "0"),
            ("duration_days",  "INTEGER", "0"),
            ("grain_price",    "REAL",    "0"),
            ("metal_price",    "REAL",    "0"),
            ("semi_index",     "REAL",    "0"),
            ("per_large",      "REAL",    "0"),
            ("per_mid",        "REAL",    "0"),
            ("per_small",      "REAL",    "0"),
            ("sector_growth",  "REAL",    "0"),
            ("sector_value",   "REAL",    "0"),
            ("ind_it",         "REAL",    "0"),
            ("ind_health",     "REAL",    "0"),
            ("ind_energy",     "REAL",    "0"),
            ("ind_finance",    "REAL",    "0"),
            ("ind_industry",   "REAL",    "0"),
            ("ind_material",   "REAL",    "0"),
            ("ind_realestate", "REAL",    "0"),
            ("ind_rebuild",    "REAL",    "0"),
            ("ind_util",       "REAL",    "0"),
            ("ind_consumer",   "REAL",    "0"),
            ("ind_staple",     "REAL",    "0"),
            ("ind_comm",       "REAL",    "0"),
        ]
        try:
            cur = self.conn.cursor()
            cur.execute("PRAGMA table_info(scenario_log)")
            existing = {row[1] for row in cur.fetchall()}
            for col_name, col_type, default in new_cols:
                if col_name not in existing:
                    cur.execute(
                        f"ALTER TABLE scenario_log ADD COLUMN {col_name} {col_type} DEFAULT {default}"
                    )
            self.conn.commit()
        except Exception as e:
            print(f"❌ scenario_log 마이그레이션 오류: {e}")

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
            'split_cooldown_days', 'will_to_split', 'par_value',
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

    def save_listing_snapshot(self, stock: dict):
        """종목 상장 시점 스냅샷 저장 — 상장 확정 즉시 1회 호출"""
        meta = stock.get('meta', {})
        try:
            self.conn.execute(
                """INSERT OR IGNORE INTO listing_snapshots
                   (c_name, listed_date, tier, ind, sub, grp,
                    price, shares, market_cap,
                    efficiency, debt_ratio, hp, hp_soft_cap,
                    owner_share, foreign_share, inst_share, retail_share,
                    treasury_share, credit_grade)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    meta.get('c_name', ''),
                    meta.get('listed_date', ''),
                    meta.get('tier', '소형주'),
                    meta.get('ind', ''),
                    meta.get('sub', ''),
                    meta.get('group', ''),
                    float(stock.get('price', 0)),
                    int(stock.get('shares', 0)),
                    float(stock.get('market_cap', 0)),
                    float(meta.get('efficiency', 0)),
                    float(meta.get('debt_ratio', 0)),
                    float(meta.get('hp', 0)),
                    float(meta.get('hp_soft_cap', 60)),
                    float(meta.get('owner_share', 0)),
                    float(meta.get('foreign_share', 0)),
                    float(meta.get('inst_share', 0)),
                    float(meta.get('retail_share', 0)),
                    float(meta.get('treasury_share', 0)),
                    meta.get('credit_grade', 'N/A'),
                )
            )
            self.conn.commit()
        except Exception as e:
            print(f"❌ listing_snapshot 저장 오류: {e}")

    def get_listing_snapshot(self, c_name: str) -> dict:
        """상장 시점 스냅샷 조회 — 없으면 빈 dict 반환"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                """SELECT c_name, listed_date, tier, ind, sub, grp,
                          price, shares, market_cap,
                          efficiency, debt_ratio, hp, hp_soft_cap,
                          owner_share, foreign_share, inst_share, retail_share,
                          treasury_share, credit_grade
                   FROM listing_snapshots WHERE c_name=?""",
                (c_name,)
            )
            row = cur.fetchone()
            if not row:
                return {}
            keys = ['c_name','listed_date','tier','ind','sub','grp',
                    'price','shares','market_cap',
                    'efficiency','debt_ratio','hp','hp_soft_cap',
                    'owner_share','foreign_share','inst_share','retail_share',
                    'treasury_share','credit_grade']
            return dict(zip(keys, row))
        except Exception as e:
            print(f"❌ listing_snapshot 조회 오류: {e}")
            return {}

    def get_chart_data_with_dates(self, company_name: str) -> list:
        """종목의 전체 (date, price) 튜플 리스트 반환 — 상폐 역사관 날짜 X축용"""
        try:
            cur = self.conn.cursor()
            cur.execute(
                "SELECT date, price FROM stock_history WHERE company_name=? ORDER BY date ASC",
                (company_name,)
            )
            return [(r[0], float(r[1])) for r in cur.fetchall()]
        except Exception as e:
            print(f"❌ get_chart_data_with_dates 실패: {e}")
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
        """DB 전체 초기화 — DB 파일 자체 삭제 후 재생성 (가장 빠르고 완전한 초기화)"""
        import os
        db_file = "stock_data.db"
        try:
            # 연결 먼저 닫기
            try:
                self.conn.close()
            except Exception:
                pass
            # DB 파일 + WAL 부산물 삭제
            for ext in ["", "-shm", "-wal"]:
                path = db_file + ext
                if os.path.exists(path):
                    os.remove(path)
            # 새 연결 + 재생성
            import sqlite3
            self.conn = sqlite3.connect(db_file, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self._create_db()
            # ★ 캐시 초기화 (초기화 후 새 게임 시작 시 중복 방지)
            self._scenario_log_cache = None
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
                    # ★ 신규 이벤트 상태
                    "war_event":             getattr(self.s, 'war_event', {}),
                    "pandemic_event":        getattr(self.s, 'pandemic_event', {}),
                    "market_fully_formed":   getattr(self.s, '_market_fully_formed', False),
                    "last_external_shock_year": getattr(self.s, '_last_external_shock_year', 0),
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

            # ★ 신규 이벤트 상태 복원
            self.s.war_event              = eng.get("war_event", {})
            self.s.pandemic_event         = eng.get("pandemic_event", {})
            self.s._market_fully_formed   = eng.get("market_fully_formed", False)
            self.s._last_external_shock_year = eng.get("last_external_shock_year", 0)

            # ★ macro 신규 원자재 필드 — 구버전 세이브 호환
            macro = self.s.macro
            if 'grain_price'  not in macro: macro['grain_price']  = 250.0
            if 'metal_price'  not in macro: macro['metal_price']  = 1800.0
            if 'semi_index'   not in macro: macro['semi_index']   = 1000.0

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


    # ─────────────────────────────────────────────
    # ★ 시나리오 로그
    # ─────────────────────────────────────────────
    def log_scenario_change(self, date_str: str, scenario: str,
                             gri: float, bubble: float,
                             macro: dict, war_event: dict = None,
                             note: str = "",
                             market_stats: dict = None):
        """시나리오 변경 시 DB에 기록 + 이전 시나리오 종료 업데이트"""
        try:
            war_str = ""
            if war_event and war_event.get('phase'):
                war_str = f"{war_event.get('region','')} {war_event.get('type','')} ({war_event.get('phase','')})"

            ms = market_stats or {}

            self.conn.execute("""
                INSERT INTO scenario_log
                (date, scenario, gri, gri_high, gri_low, gri_end, duration_days,
                 bubble_index, interest_rate, oil_price, exchange_rate, cpi,
                 grain_price, metal_price, semi_index,
                 per_large, per_mid, per_small,
                 sector_growth, sector_value,
                 ind_it, ind_health, ind_energy, ind_finance,
                 ind_industry, ind_material,
                 ind_realestate, ind_rebuild, ind_util,
                 ind_consumer, ind_staple, ind_comm,
                 war_event, note)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_str, scenario,
                round(gri, 2),
                round(gri, 2),   # gri_high 초기값 = 시작 GRI
                round(gri, 2),   # gri_low  초기값 = 시작 GRI
                round(gri, 2),   # gri_end  초기값
                0,               # duration_days 초기값
                round(bubble, 2),
                round(macro.get('interest_rate', 0), 2),
                round(macro.get('oil_price', 0), 2),
                round(macro.get('exchange_rate', 0), 2),
                round(macro.get('cpi', 0), 2),
                round(macro.get('grain_price', 250), 2),
                round(macro.get('metal_price', 1800), 2),
                round(macro.get('semi_index', 1000), 2),
                round(ms.get('per_large', 0), 1),
                round(ms.get('per_mid', 0), 1),
                round(ms.get('per_small', 0), 1),
                round(ms.get('sector_growth', 0), 1),
                round(ms.get('sector_value', 0), 1),
                round(ms.get('ind_it', 0), 1),
                round(ms.get('ind_health', 0), 1),
                round(ms.get('ind_energy', 0), 1),
                round(ms.get('ind_finance', 0), 1),
                round(ms.get('ind_industry', 0), 1),
                round(ms.get('ind_material', 0), 1),
                round(ms.get('ind_realestate', 0), 1),
                round(ms.get('ind_rebuild', 0), 1),
                round(ms.get('ind_util', 0), 1),
                round(ms.get('ind_consumer', 0), 1),
                round(ms.get('ind_staple', 0), 1),
                round(ms.get('ind_comm', 0), 1),
                war_str, note
            ))
            self.conn.commit()
            # ★ 캐시 리셋 (새 시나리오 ID로)
            cur2 = self.conn.cursor()
            cur2.execute("SELECT last_insert_rowid()")
            new_id = cur2.fetchone()[0]
            self._reset_scenario_log_cache(new_id, gri)
        except Exception as e:
            print(f"❌ 시나리오 로그 저장 오류: {e}")

    def update_scenario_log_daily(self, gri: float):
        """매일 최신 시나리오 행의 고점/저점/종료GRI/지속일수 업데이트
        ★ commit 없음 — flush_daily_db()에서 일괄 처리"""
        try:
            # ★ 메모리 캐시로 SELECT 최소화
            # _scenario_log_cache: (id, gri_high, gri_low) 유지
            cache = getattr(self, '_scenario_log_cache', None)
            if cache is None:
                # 최초 1회만 DB 조회
                cur = self.conn.cursor()
                cur.execute("SELECT id, gri_high, gri_low FROM scenario_log ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                if not row:
                    return
                cache = {'id': row[0], 'high': row[1], 'low': row[2], 'days': 0}
                self._scenario_log_cache = cache

            cache['high']  = max(cache['high'], gri)
            cache['low']   = min(cache['low'],  gri)
            cache['days'] += 1
            cache['end']   = gri

            # ★ commit 없이 execute만 (flush_daily_db에서 일괄 commit)
            self.conn.execute("""
                UPDATE scenario_log
                SET gri_high = ?, gri_low = ?, gri_end = ?,
                    duration_days = duration_days + 1
                WHERE id = ?
            """, (round(cache['high'], 2), round(cache['low'], 2),
                  round(gri, 2), cache['id']))
        except Exception as e:
            print(f"❌ 시나리오 로그 일별 업데이트 오류: {e}")

    def _reset_scenario_log_cache(self, new_id: int, gri: float):
        """시나리오 변경 시 캐시 리셋"""
        self._scenario_log_cache = {
            'id': new_id, 'high': gri, 'low': gri, 'end': gri, 'days': 0
        }

    def get_scenario_log(self) -> list:
        """시나리오 로그 전체 조회"""
        try:
            cur = self.conn.cursor()
            cur.execute("""
                SELECT date, scenario, gri, gri_high, gri_low, gri_end, duration_days,
                       bubble_index, interest_rate, oil_price, exchange_rate, cpi,
                       grain_price, metal_price, semi_index,
                       per_large, per_mid, per_small,
                       sector_growth, sector_value,
                       ind_it, ind_health, ind_energy, ind_finance,
                       ind_industry, ind_material,
                       ind_realestate, ind_rebuild, ind_util,
                       ind_consumer, ind_staple, ind_comm,
                       war_event, note
                FROM scenario_log ORDER BY id ASC
            """)
            rows = cur.fetchall()
            return [
                {
                    "date":          r[0],
                    "scenario":      r[1],
                    "gri":           r[2],
                    "gri_high":      r[3],
                    "gri_low":       r[4],
                    "gri_end":       r[5],
                    "duration_days": r[6],
                    "bubble":        r[7],
                    "interest_rate": r[8],
                    "oil_price":     r[9],
                    "exchange_rate": r[10],
                    "cpi":           r[11],
                    "grain_price":   r[12],
                    "metal_price":   r[13],
                    "semi_index":    r[14],
                    "per_large":     r[15],
                    "per_mid":       r[16],
                    "per_small":     r[17],
                    "sector_growth": r[18],
                    "sector_value":  r[19],
                    "ind_it":        r[20],
                    "ind_health":    r[21],
                    "ind_energy":    r[22],
                    "ind_finance":   r[23],
                    "ind_industry":  r[24],
                    "ind_material":   r[25],
                    "ind_realestate": r[26],
                    "ind_rebuild":    r[27],
                    "ind_util":       r[28],
                    "ind_consumer":   r[29],
                    "ind_staple":     r[30],
                    "ind_comm":       r[31],
                    "war_event":      r[32],
                    "note":           r[33],
                }
                for r in rows
            ]
        except Exception as e:
            print(f"❌ 시나리오 로그 조회 오류: {e}")
            return []

    def export_scenario_log_txt(self, filepath: str = "scenario_log.txt") -> str:
        """시나리오 로그를 TXT 파일로 내보내기"""
        rows = self.get_scenario_log()
        if not rows:
            return ""
        lines = []
        lines.append("=" * 130)
        lines.append("G.PY Economic System — 시나리오 변경 로그")
        lines.append("=" * 130)

        # 헤더
        lines.append(
            f"{'날짜':<12} {'시작GRI':>8} {'고점':>8} {'저점':>8} {'종료GRI':>8} {'일수':>5} "
            f"{'버블':>6} {'금리':>6} {'유가':>6} {'환율':>7} {'CPI':>5} "
            f"{'밀':>5} {'구리':>7} {'SOX':>6} "
            f"{'PER대':>6} {'PER중':>6} {'PER소':>6} "
            f"{'성장':>5} {'가치':>5} "
            f"{'IT':>5} {'건강':>5} {'에너지':>5} {'금융':>5} "
            f"{'산업재':>5} {'소재':>5} {'부동산':>5} {'재건':>5} "
            f"{'유틸':>5} {'자유소비':>6} {'필수소비':>6} {'커뮤':>5}  "
            f"{'시나리오':<30} {'전쟁/이벤트'}"
        )
        lines.append("-" * 180)

        for r in rows:
            war = r.get('war_event') or ""
            gri = r.get('gri', 0)
            gri_end = r.get('gri_end', gri)
            chg = f"({(gri_end/gri-1)*100:+.1f}%)" if gri > 0 else ""

            lines.append(
                f"{r['date']:<12} "
                f"{gri:>8,.0f} "
                f"{r.get('gri_high', gri):>8,.0f} "
                f"{r.get('gri_low',  gri):>8,.0f} "
                f"{gri_end:>8,.0f}{chg:>8} "
                f"{r.get('duration_days', 0):>5}일 "
                f"{r['bubble']:>6.1f} "
                f"{r['interest_rate']:>5.2f}% "
                f"${r['oil_price']:>5.1f} "
                f"₩{r['exchange_rate']:>6.0f} "
                f"{r['cpi']:>4.2f}% "
                f"${r.get('grain_price', 0):>4.0f} "
                f"${r.get('metal_price', 0):>6.0f} "
                f"{r.get('semi_index', 0):>6.0f} "
                f"{r.get('per_large', 0):>5.1f}x "
                f"{r.get('per_mid', 0):>5.1f}x "
                f"{r.get('per_small', 0):>5.1f}x "
                f"{r.get('sector_growth', 0):>+5.1f}% "
                f"{r.get('sector_value', 0):>+5.1f}% "
                f"{r.get('ind_it', 0):>+5.1f}% "
                f"{r.get('ind_health', 0):>+5.1f}% "
                f"{r.get('ind_energy', 0):>+5.1f}% "
                f"{r.get('ind_finance', 0):>+5.1f}% "
                f"{r.get('ind_industry', 0):>+5.1f}% "
                f"{r.get('ind_material', 0):>+5.1f}% "
                f"{r.get('ind_realestate', 0):>+5.1f}% "
                f"{r.get('ind_rebuild', 0):>+5.1f}% "
                f"{r.get('ind_util', 0):>+5.1f}% "
                f"{r.get('ind_consumer', 0):>+6.1f}% "
                f"{r.get('ind_staple', 0):>+6.1f}% "
                f"{r.get('ind_comm', 0):>+5.1f}%  "
                f"{r['scenario']:<30} "
                f"{war}"
            )

        lines.append("=" * 180)
        lines.append(f"총 {len(rows)}개 시나리오 기록")

        txt = "\n".join(lines)
        try:
            import os
            filepath = os.path.abspath(filepath)
            with open(filepath, 'w', encoding='utf-8-sig') as f:  # utf-8-sig: 메모장 호환
                f.write(txt)
            print(f"✅ 시나리오 로그 저장: {filepath}")
        except Exception as e:
            print(f"❌ TXT 내보내기 오류: {e}")
            return ""
        return filepath

    def clear_save_file(self, filename: str = "save_game.json"):
        if os.path.exists(filename):
            os.remove(filename)