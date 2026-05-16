"""
engine/company.py
- StockData 생성 로직 (create_stock_data)
- 이름 중복 방지 (get_unique_name)
- 티어 재배정 (reassign_tiers_by_cap)
- 포맷 유틸 (format_cap, pad_text, get_width)
UI 코드 절대 금지.
"""
import random
from .constants import NAME_DB, GROUP_BASE_NAMES, MAIN_INDUSTRIES, SECTOR_MAP, TIER_CONFIG, INDUSTRY_LEVELS


class CompanyManager:
    """종목 생성·이름·티어 관리. MarketState를 참조하지만 직접 수정하지 않음."""

    def __init__(self, market_state):
        self.state = market_state
        self.used_all_time: set = set()
        self.name_pool: list = [n for n in NAME_DB if n not in GROUP_BASE_NAMES]
        random.shuffle(self.name_pool)
        self.current_generation: int = 1

    # ─────────────────────────────────────────────
    # 이름 생성
    # ─────────────────────────────────────────────
    def get_unique_name(self, is_group_member: bool, group_name, info) -> str:
        if is_group_member:
            final_name = f"{group_name} {info}"
            if final_name in self.used_all_time:
                suffix = 2
                while f"{final_name} {suffix}" in self.used_all_time:
                    suffix += 1
                final_name = f"{final_name} {suffix}"
            self.used_all_time.add(final_name)
            return final_name

        base_core_name = info if info else (self.name_pool.pop(0) if self.name_pool else "미명")
        final_name = base_core_name if self.current_generation == 1 else f"{base_core_name} {self.current_generation}"

        if final_name in self.used_all_time:
            if self.name_pool:
                return self.get_unique_name(False, None, self.name_pool.pop(0))
            else:
                self.current_generation += 1
                self.name_pool = [n for n in NAME_DB if n not in GROUP_BASE_NAMES]
                random.shuffle(self.name_pool)
                return self.get_unique_name(False, None, self.name_pool.pop(0))

        self.used_all_time.add(final_name)
        return final_name

    # ─────────────────────────────────────────────
    # 종목 생성
    # ─────────────────────────────────────────────
    def create_stock_data(self, base_name, ind: str, tier: str = "소", group_id=None) -> dict:
        """지배구조 엔진 포함 종목 딕셔너리 생성"""
        current_date = self.state.current_date
        actual_lv = self.state.max_tech_reached
        sector = SECTOR_MAP.get(ind, "Value")

        # 1. 황제주 성향
        dice_split = random.random()
        will_to_split = {
            "대": dice_split > 0.25,
            "중": dice_split > 0.05,
        }.get(tier, True)

        # 2. 자사주 결정
        sector_ts_range = {
            "Growth":    (0.00, 0.05),
            "Value":     (0.08, 0.15),
            "Defensive": (0.05, 0.10),
        }
        lo, hi = sector_ts_range.get(sector, (0.01, 0.07))
        base_ts = random.uniform(lo, hi)
        if tier == "소":   base_ts -= 0.01
        elif tier == "대": base_ts += 0.02
        final_ts = max(0.0, base_ts)

        # 3. 지분 배분
        if tier == "대":
            owner_r   = random.uniform(0.35, 0.45)
            foreign_r = random.uniform(0.30, 0.40)
            inst_r    = random.uniform(0.10, 0.15)
        elif tier == "중":
            owner_r   = random.uniform(0.30, 0.45)
            foreign_r = random.uniform(0.05, 0.15)
            inst_r    = random.uniform(0.10, 0.20)
        else:
            owner_r   = random.uniform(0.25, 0.40)
            foreign_r = random.uniform(0.01, 0.05)
            inst_r    = random.uniform(0.05, 0.10)

        retail_r = max(0.0, 1.0 - (owner_r + foreign_r + inst_r))
        rem_p = 1.0 - final_ts
        owner_abs   = owner_r   * rem_p
        foreign_abs = foreign_r * rem_p
        inst_abs    = inst_r    * rem_p
        retail_abs  = retail_r  * rem_p

        # 4. 가격 및 주식수
        if tier == "대":
            p       = random.randint(40000, 80000)
            s_count = random.randint(100, 1000) * 1_000_000
        elif tier == "중":
            p       = random.randint(15000, 35000)
            s_count = random.randint(50, 200) * 1_000_000
        else:
            p       = random.randint(1000, 15000)
            s_count = random.randint(1, 50) * 1_000_000

        # 5. 이름
        is_group = (group_id is not None)
        gn_arg   = self.state.groups[group_id]['name'] if is_group else None
        info_arg = ind if is_group else base_name
        full_name = self.get_unique_name(is_group, gn_arg, info_arg)

        ind_levels = INDUSTRY_LEVELS.get(ind, {}).get(actual_lv, ["기본 산업"])

        return {
            "meta": {
                "c_name":               full_name,
                "will_to_split":        will_to_split,
                "listed_date_dt":       current_date,
                "listed_date":          current_date.strftime('%Y-%m-%d'),
                "group_id":             group_id,
                "group":                gn_arg,
                "tier":                 f"{tier}형주",
                "ind":                  ind,
                "sub":                  random.choice(ind_levels),
                "char":                 "Normal",
                "risk_score":           0.0,
                "delist_timer":         0,
                "split_count":          0,
                "merge_count":          0,
                "assets":               float(p * s_count),
                "efficiency":           random.uniform(0.02, 0.08),
                "momentum":             0.0,
                "continuous_loss_count": 0,
                "risk_sensitivity":     {"대": 0.1, "중": 0.5, "소": 1.2}.get(tier, 1.0),
                "treasury_share":       final_ts,
                "owner_share":          owner_abs,
                "foreign_share":        foreign_abs,
                "inst_share":           inst_abs,
                "retail_share":         retail_abs,
            },
            "price":      p,
            "rate":       0.0,
            "shares":     s_count,
            "market_cap": p * s_count,
        }

    # ─────────────────────────────────────────────
    # 티어 재배정
    # ─────────────────────────────────────────────
    def reassign_tiers_by_cap(self, stocks: list, daily_news: list, silent: bool = False):
        stocks.sort(key=lambda x: x['market_cap'], reverse=True)
        total = len(stocks)
        b_lim = max(1, int(total * 0.11))
        m_lim = max(1, int(total * 0.33))

        for i, s in enumerate(stocks):
            old_tier = s['meta']['tier']
            if i < b_lim:
                s['meta']['tier'] = "대형주"
            elif i < m_lim:
                s['meta']['tier'] = "중형주"
            else:
                s['meta']['tier'] = "소형주"

            if not silent and old_tier == "대형주" and s['meta']['tier'] == "중형주":
                daily_news.append(f"📉 [체급강등] {s['meta']['c_name']}이 시가총액 밀려나며 중견기업으로 강등되었습니다.")

    # ─────────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────────
    @staticmethod
    def format_cap(cap: int) -> str:
        if cap >= 1_000_000_000_000_000: return f"{cap / 1_000_000_000_000_000:.2f}경"
        if cap >= 1_000_000_000_000:     return f"{cap / 1_000_000_000_000:.2f}조"
        if cap >= 100_000_000:            return f"{cap / 100_000_000:.1f}억"
        return f"{cap:,.0f}"

    @staticmethod
    def get_width(text: str) -> int:
        return sum(2 if '가' <= c <= '힣' else 1 for c in text)

    @classmethod
    def pad_text(cls, text: str, target: int) -> str:
        return text + " " * max(0, target - cls.get_width(text))
