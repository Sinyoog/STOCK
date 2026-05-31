"""
engine/company.py
- StockData 생성 로직 (create_stock_data)
- 이름 중복 방지 (get_unique_name)
- 티어 재배정 (reassign_tiers_by_cap)
- 포맷 유틸 (format_cap, pad_text, get_width)
UI 코드 절대 금지.
"""
import random
from .constants import (NAME_DB, GROUP_BASE_NAMES, MAIN_INDUSTRIES, SECTOR_MAP,
                         TIER_CONFIG, INDUSTRY_LEVELS, TECH_PHASE)


class CompanyManager:
    def __init__(self, market_state):
        self.state = market_state
        self.used_all_time: set = set()
        self.name_pool: list = [n for n in NAME_DB if n not in GROUP_BASE_NAMES]
        random.shuffle(self.name_pool)
        self.current_generation: int = 1

    def sync_from_state(self):
        """세이브 파일 로드 후 이름 생성기 상태를 MarketState에서 복원"""
        gen  = getattr(self.state, '_name_generation', 1)
        pool = getattr(self.state, '_name_pool', [])
        if gen > 1 or pool:
            self.current_generation = gen
            self.name_pool = pool if pool else [
                n for n in NAME_DB if n not in GROUP_BASE_NAMES
            ]
        # used_all_time은 MarketState에서 직접 관리
        self.used_all_time = self.state.used_all_time

    def sync_to_state(self):
        """매 턴 이름 생성기 상태를 MarketState에 저장"""
        self.state._name_generation = self.current_generation
        self.state._name_pool       = list(self.name_pool)

    # ─────────────────────────────────────────────
    # 이름 생성
    # ─────────────────────────────────────────────
    def get_unique_name(self, is_group_member: bool, group_name, info) -> str:
        # used_all_time은 항상 state와 동기화
        self.used_all_time = self.state.used_all_time

        if is_group_member:
            final_name = f"{group_name} {info}"
            # ★ 시장 전체에 동일 이름이 있으면 산업명 뒤에 숫자 suffix
            # 단, 이미 같은 그룹+산업 조합이 존재하면 suffix가 아닌 새 산업으로 가야 함
            # → handle_group_expansion에서 산업 중복 체크로 1차 방어
            # → 여기서는 최후 방어선으로 suffix 처리
            existing_names = {s['meta']['c_name'] for s in self.state.stocks} \
                if hasattr(self.state, 'stocks') else set()
            existing_names |= self.used_all_time
            if final_name in existing_names:
                suffix = 2
                while f"{final_name} {suffix}" in existing_names:
                    suffix += 1
                final_name = f"{final_name} {suffix}"
            self.used_all_time.add(final_name)
            self.sync_to_state()
            return final_name

        # ── 독립 기업 이름 생성 (루프 구조) ──────────────────────
        # 현재 세대 풀을 진짜 다 소진해야 다음 세대로 넘어감
        while True:
            while self.name_pool:
                candidate = self.name_pool.pop(0)
                suffix = f" {self.current_generation}" if self.current_generation > 1 else ""
                final_name = f"{candidate}{suffix}"
                if final_name not in self.used_all_time:
                    self.used_all_time.add(final_name)
                    self.sync_to_state()
                    return final_name

            # 현재 세대 풀을 전부 소진했을 때만 다음 세대로 넘어감
            self.current_generation += 1
            self.name_pool = [n for n in NAME_DB if n not in GROUP_BASE_NAMES]
            random.shuffle(self.name_pool)
            self.sync_to_state()

    # ─────────────────────────────────────────────
    # 현재 페이즈 ID 계산 (economy.py 없이 독립 계산)
    # ─────────────────────────────────────────────
    def _get_current_phase(self) -> str:
        lv = self.state.max_tech_reached
        upgrade_years = getattr(self.state, '_tech_upgrade_years', {1: 2000})
        lv_start = upgrade_years.get(lv) or 2000
        elapsed  = self.state.current_date.year - lv_start
        for phase in TECH_PHASE.get(lv, []):
            if phase["offset_start"] <= elapsed < phase["offset_end"]:
                return phase["id"]
        phases = TECH_PHASE.get(lv, [])
        return phases[-1]["id"] if phases else str(lv)

    # ─────────────────────────────────────────────
    # 페이즈+티어 기반 sub 선택
    # ─────────────────────────────────────────────
    def _get_sub_pool(self, ind: str, tier_key: str) -> list:
        """
        현재 페이즈와 티어에 맞는 sub 항목 풀 반환.
        우선순위: 현재페이즈 해당티어 → common 해당티어 → 인접티어 → 전체
        """
        phase    = self._get_current_phase()
        ind_data = INDUSTRY_LEVELS.get(ind, {})

        # 1차: 현재 페이즈 + 해당 티어
        pool = ind_data.get(phase, {}).get(tier_key, [])
        if pool:
            return pool

        # 2차: common + 해당 티어 (항상 존재하는 항목)
        pool = ind_data.get("common", {}).get(tier_key, [])
        if pool:
            return pool

        # 3차: 현재 페이즈에서 인접 티어 fallback
        phase_data = ind_data.get(phase, {})
        fallback_order = {
            "대": ["중", "소"],
            "중": ["소", "대"],
            "소": ["중", "대"],
        }.get(tier_key, ["중", "소"])
        for fb_tier in fallback_order:
            pool = phase_data.get(fb_tier, [])
            if pool:
                return pool

        # 4차: common 인접 티어
        common_data = ind_data.get("common", {})
        for fb_tier in fallback_order:
            pool = common_data.get(fb_tier, [])
            if pool:
                return pool

        # 5차: 전 페이즈 통합
        all_subs = []
        for p_key, p_data in ind_data.items():
            if isinstance(p_data, dict):
                for t_key, t_list in p_data.items():
                    if isinstance(t_list, list):
                        all_subs.extend(t_list)
        return all_subs if all_subs else [f"{ind} 기본산업"]

    # ─────────────────────────────────────────────
    # 종목 생성
    # ─────────────────────────────────────────────
    def create_stock_data(self, base_name, ind: str, tier: str = "소", group_id=None) -> dict:
        current_date = self.state.current_date
        actual_lv    = self.state.max_tech_reached
        sector       = SECTOR_MAP.get(ind, "Value")

        # 1. 분할 의지 — 섹터별 현실 반영
        dice_split = random.random()
        # Growth: 90%, Value: 40%, Defensive: 20%, Theme: 70%
        split_prob = {
            "Growth":    0.10,   # 90% 분할 의지
            "Value":     0.60,   # 40%
            "Defensive": 0.80,   # 20%
            "Cyclical":  0.30,   # 70%
        }.get(sector, 0.40)
        will_to_split = dice_split > split_prob

        # 2. 자사주
        sector_ts_range = {
            "Growth":    (0.00, 0.05),
            "Value":     (0.08, 0.15),
            "Defensive": (0.05, 0.10),
        }
        lo, hi   = sector_ts_range.get(sector, (0.01, 0.07))
        base_ts  = random.uniform(lo, hi)
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

        retail_r    = max(0.0, 1.0 - (owner_r + foreign_r + inst_r))
        rem_p       = 1.0 - final_ts
        owner_abs   = owner_r   * rem_p
        foreign_abs = foreign_r * rem_p
        inst_abs    = inst_r    * rem_p
        retail_abs  = retail_r  * rem_p

        # 4. 가격 및 주식수 — 시총 목표 역산
        # 코스피 현실 기준 (2000년 초)
        # 최상위("대1"): 5조~30조 (삼성전자급 → 26년 후 800% = 40조~240조)
        # 일반 대기업("대"): 5000억~5조
        # 중견("중"): 1000억~1조
        # 중소("소"): 100억~3000억
        if tier == "대1":
            # 현실 2000년 기준: KT 37조, 삼성전자 32조, SKT 28조
            # 목표 평균 30조 → 26년 성장배수 7~8배 → 200~240조 도달
            case = random.randint(1, 3)
            if case == 1:   # KT형: 발행주 많고 주가 낮음
                p       = random.randint(30_000, 50_000)
                s_count = random.randint(300, 800) * 1_000_000
            elif case == 2: # 삼성전자형: 주가/주식수 균형 ← 상향
                p       = random.randint(70_000, 150_000)
                s_count = random.randint(300, 600) * 1_000_000
            else:           # SKT형: 고주가 소량 ← 상향
                p       = random.randint(300_000, 700_000)
                s_count = random.randint(20, 50) * 1_000_000
        elif tier == "대":
            # 현실 2000년 포항제철/현대전자급: 1조~8조
            p       = random.randint(10_000, 30_000)
            s_count = random.randint(100, 300) * 1_000_000
        elif tier == "중":
            # 중견: 500억~5000억
            p       = random.randint(3_000, 15_000)
            s_count = random.randint(20, 100) * 1_000_000
        else:
            # 중소: 50억~500억
            p       = random.randint(500, 5_000)
            s_count = random.randint(5, 30) * 1_000_000

        # 5. 이름
        is_group  = (group_id is not None)
        gn_arg    = self.state.groups[group_id]['name'] if is_group else None
        info_arg  = ind if is_group else base_name
        full_name = self.get_unique_name(is_group, gn_arg, info_arg)

        # ★ 페이즈+티어 기반 sub 풀 (기존 actual_lv 단일 리스트 대체)
        _tier_key_for_sub = "대" if tier in ("대", "대1") else tier
        ind_levels = self._get_sub_pool(ind, _tier_key_for_sub)

        # 6. HP / Shield
        initial_market_cap = float(p * s_count)
        _tier_key = "대" if tier == "대1" else tier
        hp_spec = {
            "대": {"soft_cap": 100.0, "hp": 75.0, "shield_ratio": 0.015},
            "중": {"soft_cap": 80.0,  "hp": 65.0, "shield_ratio": 0.003},
            "소": {"soft_cap": 60.0,  "hp": 50.0, "shield_ratio": 0.0},
        }.get(_tier_key, {"soft_cap": 60.0, "hp": 50.0, "shield_ratio": 0.0})

        init_shield = initial_market_cap * hp_spec["shield_ratio"]

        # ★ 7. 부채비율 초기화 (현실적 범위)
        # 부채비율 = 부채 / 자산 × 100
        # 대형주: 30~60%, 중형주: 50~120%, 소형주: 60~180%
        debt_ratio_range = {
            "대": (0.30, 0.60),
            "대1": (0.30, 0.60),
            "중": (0.50, 1.20),
            "소": (0.60, 1.80),
        }.get(tier, (0.50, 1.20))
        init_debt_ratio = random.uniform(*debt_ratio_range)
        init_assets     = float(p * s_count)
        init_debt       = init_assets * init_debt_ratio

        # ★ 8. 신용등급 초기화 (HP 기반)
        # AA: hp >= 80%, BB: hp >= 50%, CCC: hp < 50%
        hp_pct = hp_spec["hp"] / hp_spec["soft_cap"]
        if hp_pct >= 0.80:
            init_credit = "AA"
        elif hp_pct >= 0.50:
            init_credit = "BB"
        else:
            init_credit = "CCC"

        return {
            "meta": {
                "c_name":               full_name,
                "will_to_split":        will_to_split,
                "par_value":            {
                    "대1": 5000,
                    "대":  random.choice([1000, 500]),
                }.get(tier, 500),   # 중/소형주는 500원 고정
                "listed_date_dt":       current_date,
                "listed_date":          current_date.strftime('%Y-%m-%d'),
                "group_id":             group_id,
                "group":                gn_arg,
                "tier":                 "대형주" if tier == "대1" else f"{tier}형주",
                "ind":                  ind,
                "sub":                  random.choice(ind_levels) if ind_levels else f"{ind} 기본산업",
                # ★ 신규: 사업 목록 (복수 사업 지원)
                # 초기에는 단일 사업으로 시작, 페이즈 전환 시 추가/교체
                "sub_list":             [random.choice(ind_levels) if ind_levels else f"{ind} 기본산업"],
                # ★ 신규: 티어 원본 키 (대1/대/중/소) — TIER_BUSINESS_SPEC 참조용
                "tier_raw":             tier,
                "char":                 "Normal",
                "risk_score":           0.0,
                # ── HP / Shield ────────────────────
                "hp":                   hp_spec["hp"],
                "hp_soft_cap":          hp_spec["soft_cap"],
                "shield":               init_shield,
                # ── 티어 심사 카운터 ────────────────
                "cap_exceed_days":      0,
                "cap_below_days":       0,
                # ── 기타 ────────────────────────────
                "delist_timer":         0,
                "split_count":          0,
                "split_cooldown_days":  0,   # ★ 신규: 분할 쿨다운 카운터
                "merge_count":          0,
                "assets":               init_assets,
                "initial_assets":       init_assets,     # ★ 하한선 계산용 초기값
                "initial_price":        float(p),        # ★ 주가/HP 연결용 초기 주가
                "debt":                 init_debt,       # ★ 신규: 부채
                "debt_ratio":           init_debt_ratio, # ★ 신규: 부채비율
                "target_debt_ratio":    init_debt_ratio, # ★ 신규: 기업별 목표 부채비율 (하한선)
                "credit_grade":         init_credit,     # ★ 신규: 신용등급
                # ★ efficiency — 초기값은 흑자 가능한 범위로 설정
                # PER 정상화는 efficiency 수렴(announce_earnings)에서 페이즈별로 처리
                # 초기값이 너무 낮으면 전 종목 구조적 적자 → 대량 상폐
                "efficiency":           {
                    "대1": random.uniform(0.12, 0.20),
                    "대":  random.uniform(0.10, 0.18),
                    "중":  random.uniform(0.06, 0.12),
                    "소":  random.uniform(0.04, 0.08),
                }.get(tier, random.uniform(0.04, 0.08)),
                "momentum":             0.0,
                "continuous_loss_count": 0,
                "risk_sensitivity":     {"대": 0.1, "대1": 0.1, "중": 0.5, "소": 1.2}.get(tier, 1.0),
                "treasury_share":       final_ts,
                "owner_share":          owner_abs,
                "foreign_share":        foreign_abs,
                "inst_share":           inst_abs,
                "retail_share":         retail_abs,
                # ── ★ 신규: 52주 신고가/신저가 ──────
                "price_52w_high":       float(p),
                "price_52w_low":        float(p),
                "price_52w_days":       0,   # 갱신 주기 카운터
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
        b_lim = max(1, int(total * 0.15))
        m_lim = max(1, int(total * 0.60))

        _HP_SPEC = {
            "대형주": {"soft_cap": 100.0, "sensitivity": 0.1, "shield_ratio": 0.015},
            "중형주": {"soft_cap": 80.0,  "sensitivity": 0.5, "shield_ratio": 0.003},
            "소형주": {"soft_cap": 60.0,  "sensitivity": 1.2, "shield_ratio": 0.0},
        }

        for i, s in enumerate(stocks):
            meta     = s['meta']
            old_tier = meta['tier']

            if i < b_lim:
                meta['tier'] = "대형주"
            elif i < m_lim:
                meta['tier'] = "중형주"
            else:
                meta['tier'] = "소형주"

            if old_tier != meta['tier']:
                spec    = _HP_SPEC[meta['tier']]
                new_cap = spec["soft_cap"]
                old_cap = meta.get('hp_soft_cap', new_cap)
                old_hp  = meta.get('hp', old_cap)
                hp_ratio = old_hp / max(1.0, old_cap)
                meta['hp_soft_cap']      = new_cap
                meta['hp']               = round(min(new_cap, hp_ratio * new_cap), 2)
                meta['risk_sensitivity'] = spec["sensitivity"]

                # ★ tier_raw도 동기화 (TIER_BUSINESS_SPEC 참조용)
                tier_map = {"대형주": "대", "중형주": "중", "소형주": "소"}
                meta['tier_raw'] = tier_map.get(meta['tier'], "소")

                cur_shield     = meta.get('shield', 0.0)
                new_shield_max = s['market_cap'] * spec["shield_ratio"]
                if meta['tier'] == "소형주":
                    meta['shield'] = 0.0
                elif old_tier == "소형주":
                    meta['shield'] = new_shield_max
                else:
                    meta['shield'] = min(cur_shield, new_shield_max)

                # ★ 신용등급도 티어 변경 시 재산정
                hp_pct = meta['hp'] / max(1.0, meta['hp_soft_cap'])
                if hp_pct >= 0.80:
                    meta['credit_grade'] = "AA"
                elif hp_pct >= 0.50:
                    meta['credit_grade'] = "BB"
                else:
                    meta['credit_grade'] = "CCC"

                if not silent:
                    if old_tier == "대형주" and meta['tier'] == "중형주":
                        daily_news.append(
                            f"📉 [체급강등] {meta['c_name']}이 시가총액 밀려나며 중견기업으로 강등되었습니다."
                        )
                    elif old_tier in ("소형주", "중형주") and meta['tier'] == "대형주":
                        daily_news.append(
                            f"📈 [체급승격] {meta['c_name']}이 대기업 반열에 올랐습니다!"
                        )

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