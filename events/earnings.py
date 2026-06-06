"""
events/earnings.py
실적 예측(calculate_potential_earnings) 및 공시 확정(announce_earnings).
UI 코드 금지.
"""
import random
from datetime import timedelta


class EarningsManager:
    def __init__(self, state):
        self.s = state

    # ─────────────────────────────────────────────
    # 실적 수치 사전 확정 (PENDING 상태로 금고에 박제)
    # ─────────────────────────────────────────────
    def calculate_potential_earnings(self, stock: dict,
                                      _current_lv_cache: dict = None,
                                      _old_lv_cache: dict = None) -> dict:
        meta = stock['meta']
        tier = meta.get('tier', '소형주')

        # ════════════════════════════════════════════════════════
        # ★ 매출 계산 v2 — 거시경제 + 산업 + 대기업 연동 시스템
        # ════════════════════════════════════════════════════════

        # ── 0. 기본 변수 ──────────────────────────────────────
        cycle = getattr(self.s, 'cycle_stage', '확장')
        macro = self.s.macro
        ind   = meta.get('ind', '')
        name  = meta.get('c_name', '')
        group = meta.get('group', None)

        from engine.constants import SECTOR_MAP as _SM, EXPORT_DEPENDENCY
        _sector      = _SM.get(ind, 'Value')
        export_dep   = EXPORT_DEPENDENCY.get(ind, 0.3)
        domestic_dep = 1.0 - export_dep

        # ── 1. 전분기 매출 조회 ───────────────────────────────
        _hist = self.s.earnings_history.get(name, {})
        _prev_revenue = None
        for _yr in sorted(_hist.keys(), reverse=True):
            for _q in sorted(_hist[_yr].keys(), reverse=True):
                _r = _hist[_yr][_q].get('revenue')
                if _r and _r > 0:
                    _prev_revenue = _r
                    break
            if _prev_revenue:
                break

        # 전분기 없으면 assets 기반 초기화 (첫 실적)
        if not _prev_revenue or _prev_revenue <= 0:
            _init_rev = meta['assets'] * random.uniform(0.04, 0.10)
            _prev_revenue = _init_rev

        # ── 2. 기본 분기 성장률 (노이즈 포함) ─────────────────
        # 연간 성장률 ±20% 범위에서 분기별 확률적 변동
        # 기업별 모멘텀(momentum) 반영
        _momentum = meta.get('momentum', 0.0)
        _base_qoq = random.gauss(0.015 + _momentum * 0.05, 0.06)
        # 분기 변동 하드캡: ±30%
        _base_qoq = max(-0.30, min(0.30, _base_qoq))

        # ── 3. 거시경제 보정 계수 ─────────────────────────────
        _macro_adj = 1.0

        # 3-1. GDP 성장률 (전체 시장 파이)
        _gdp_growth = getattr(self.s, 'gdp_growth_rate', 0.05)
        # GDP 1% → 매출 0.3~0.8% (방어주는 절반)
        _gdp_sens = 0.4 if _sector == 'Defensive' else 0.7
        _macro_adj *= (1.0 + _gdp_growth * _gdp_sens * 0.25)  # 분기 기준

        # 3-2. 경기 사이클
        _cycle_adj = {
            '확장': 1.04,
            '정점': 1.00,
            '수축': 0.93,
            '저점': 0.86,
        }.get(cycle, 1.0)
        # 방어주는 사이클 영향 절반
        if _sector == 'Defensive':
            _cycle_adj = 1.0 + (_cycle_adj - 1.0) * 0.4
        _macro_adj *= _cycle_adj

        # 3-3. 금리 영향 (B2B/설비투자 비중 높은 산업 타격)
        _rate = macro.get('interest_rate', 4.0)
        _rate_sensitive = {'산업재': 0.8, '부동산': 1.0, '금융': -0.5,
                           'IT': 0.3, '소재': 0.4, '건강관리': 0.2}
        _rate_sens = _rate_sensitive.get(ind, 0.3)
        if _rate > 4.0:
            _rate_penalty = (_rate - 4.0) * _rate_sens * 0.02  # 금리 1%p 초과당
            _macro_adj *= max(0.85, 1.0 - _rate_penalty)
        elif _rate < 2.0 and ind == '금융':
            # 초저금리는 금융 이익 압박
            _macro_adj *= 0.92

        # 3-4. 환율 영향 (수출 기업 수혜/내수 기업 무관)
        _fx_prev = self.s._prev_macro_snapshot.get('exchange_rate', 1100)                    if hasattr(self.s, '_prev_macro_snapshot') else 1100
        _fx_now  = macro.get('exchange_rate', 1100)
        _fx_chg  = (_fx_now - _fx_prev) / max(1.0, _fx_prev)
        if abs(_fx_chg) >= 0.01:
            # 원화 약세(환율 상승) → 수출 매출 증가, 내수는 수입비용 상승
            _fx_adj  = 1.0 + _fx_chg * export_dep * 0.6
            _fx_adj -= _fx_chg * domestic_dep * 0.1  # 내수: 수입물가 상승 부담
            _macro_adj *= max(0.90, min(1.15, _fx_adj))

        # 3-5. 산업별 원자재/지수 연동
        _comm_adj = 1.0
        _oil  = macro.get('oil_price', 55)
        _sox  = macro.get('semi_index', 1000)
        _cu   = macro.get('metal_price', 2000)
        _sox_prev = self.s._prev_macro_snapshot.get('semi_index', _sox)                     if hasattr(self.s, '_prev_macro_snapshot') else _sox
        _cu_prev  = self.s._prev_macro_snapshot.get('metal_price', _cu)                     if hasattr(self.s, '_prev_macro_snapshot') else _cu
        _oil_prev = self.s._prev_macro_snapshot.get('oil_price', _oil)                     if hasattr(self.s, '_prev_macro_snapshot') else _oil

        _sox_chg = (_sox - _sox_prev) / max(1.0, _sox_prev)
        _cu_chg  = (_cu  - _cu_prev)  / max(1.0, _cu_prev)
        _oil_chg = (_oil - _oil_prev) / max(1.0, _oil_prev)

        if ind == 'IT':
            # SOX 20% 상승 → IT 매출 +5%
            _comm_adj += _sox_chg * 0.25
        elif ind == '에너지':
            # 유가 10% 변동 → 에너지 매출 ±6%
            _comm_adj += _oil_chg * 0.60
        elif ind == '소재':
            # 구리 10% 변동 → 소재 ±4%
            _comm_adj += _cu_chg * 0.40
        elif ind == '산업재':
            # 구리 + 유가 복합
            _comm_adj += _cu_chg * 0.25 + _oil_chg * 0.15
        elif ind == '금융':
            # 금리 상승 = 이자수입 증가 → 매출 증가
            if _rate > 3.0:
                _comm_adj += (_rate - 3.0) * 0.04
        elif ind == '건강관리':
            # 방어적 — 경기 무관, 인구 고령화 트렌드로 완만 성장
            _comm_adj += 0.005
        elif ind == '필수소비재':
            # CPI 상승 → 판가 인상 → 명목 매출 증가
            _cpi = macro.get('cpi', 2.0)
            _comm_adj += (_cpi - 2.0) * 0.015 if _cpi > 2.0 else 0

        _comm_adj = max(0.85, min(1.20, _comm_adj))
        _macro_adj *= _comm_adj

        # 3-6. 시나리오 직접 보정
        _scenario = self.s.current_scenario
        _scenario_rev_adj = 1.0
        if '대공황' in _scenario and '극복' not in _scenario:
            _scenario_rev_adj = 0.65  # 대공황: 전 산업 매출 -35%
        elif '스태그' in _scenario:
            _scenario_rev_adj = 0.88  # 스태그: -12%
        elif '팬데믹' in _scenario and '극복' not in _scenario:
            _pand_liq = getattr(self.s, '_pandemic_liquidity_active', False)
            if _pand_liq:
                # 유동성 장세: IT/헬스 수혜, 오프라인 타격
                if ind in ('IT', '건강관리', '커뮤니케이션'):
                    _scenario_rev_adj = 1.25
                elif ind in ('자유소비재', '부동산', '에너지'):
                    _scenario_rev_adj = 0.80
            else:
                # 팬데믹 초반: 전반적 매출 감소
                if ind in ('IT', '건강관리'):
                    _scenario_rev_adj = 1.05  # 비대면 수혜
                else:
                    _scenario_rev_adj = 0.82
        elif '전쟁' in _scenario or '분쟁' in _scenario:
            # 전쟁: 방산/에너지/소재 수혜, IT/소비재 타격
            if ind in ('에너지', '소재', '산업재'):
                _scenario_rev_adj = 1.15
            elif ind in ('자유소비재', '커뮤니케이션'):
                _scenario_rev_adj = 0.90
        elif '재건' in _scenario:
            # 전후 재건: 건설/산업재/소재 폭발 성장
            if ind in ('산업재', '소재', '부동산'):
                _scenario_rev_adj = 1.30
        elif '금융위기' in _scenario and '극복' not in _scenario:
            _scenario_rev_adj = 0.78
        elif '내수 소비 붐' in _scenario:
            _scenario_rev_adj = 1.0 + domestic_dep * 0.15
        elif 'FTA' in _scenario:
            _scenario_rev_adj = 1.0 + export_dep * 0.10
        _macro_adj *= _scenario_rev_adj

        # ── 4. 대기업 연동 (중소/중견만 적용) ─────────────────
        # 현실: 중소기업 매출의 30~50%가 대기업 납품
        # 같은 그룹사 대1 → 30% 연동, 동일 산업 대형주 평균 → 15% 연동
        _tier_raw = meta.get('tier_raw', '소')
        _large_cap_adj = 1.0

        if _tier_raw in ('소', '중'):
            # 같은 그룹사 대1 종목 실적 조회
            _group_id = meta.get('group_id', None)
            _anchor_growth = None

            if _group_id is not None:
                for _st in self.s.stocks:
                    _m = _st['meta']
                    if (_m.get('group_id') == _group_id and
                        _m.get('tier_raw') == '대1'):
                        # 같은 그룹사 대1의 전분기 매출 성장률
                        _anchor_name = _m.get('c_name', '')
                        _anchor_hist = self.s.earnings_history.get(_anchor_name, {})
                        _anchor_revs = []
                        for _ayr in sorted(_anchor_hist.keys(), reverse=True):
                            for _aq in sorted(_anchor_hist[_ayr].keys(), reverse=True):
                                _ar = _anchor_hist[_ayr][_aq].get('revenue', 0)
                                if _ar > 0:
                                    _anchor_revs.append(_ar)
                                if len(_anchor_revs) >= 2:
                                    break
                            if len(_anchor_revs) >= 2:
                                break
                        if len(_anchor_revs) >= 2:
                            _anchor_growth = (_anchor_revs[0] - _anchor_revs[1]) / max(1.0, _anchor_revs[1])
                        break

            if _anchor_growth is None:
                # 그룹사 없거나 대1 없으면 동일 산업 대형주 평균 성장률
                _ind_revs = []
                for _st in self.s.stocks:
                    _m = _st['meta']
                    if _m.get('ind') == ind and _m.get('tier_raw') == '대':
                        _an = _m.get('c_name', '')
                        _ah = self.s.earnings_history.get(_an, {})
                        _ar_list = []
                        for _ayr in sorted(_ah.keys(), reverse=True):
                            for _aq in sorted(_ah[_ayr].keys(), reverse=True):
                                _ar = _ah[_ayr][_aq].get('revenue', 0)
                                if _ar > 0:
                                    _ar_list.append(_ar)
                                if len(_ar_list) >= 2:
                                    break
                            if len(_ar_list) >= 2:
                                break
                        if len(_ar_list) >= 2:
                            _ind_revs.append((_ar_list[0] - _ar_list[1]) / max(1.0, _ar_list[1]))
                if _ind_revs:
                    _anchor_growth = sum(_ind_revs) / len(_ind_revs)

            if _anchor_growth is not None:
                # 연동 비율: 중소 30%, 중견 20%
                _link_ratio = 0.30 if _tier_raw == '소' else 0.20
                # anchor 성장률의 link_ratio만큼 매출에 반영
                # 단, 과도한 연동 방지: ±15% 캡
                _linked_adj = 1.0 + max(-0.15, min(0.15, _anchor_growth * _link_ratio))
                _large_cap_adj = _linked_adj

        # ── 5. 최종 매출 계산 ─────────────────────────────────
        # 전분기 × (1 + 기본성장률) × 거시경제 보정 × 대기업 연동
        revenue = _prev_revenue * (1.0 + _base_qoq) * _macro_adj * _large_cap_adj

        # 안전장치: 분기 변동 하드캡 ±40%
        _rev_max = _prev_revenue * 1.40
        _rev_min = _prev_revenue * 0.60
        revenue  = max(_rev_min, min(_rev_max, revenue))

        # ★ [수정] 매출 절대 하한 완화 (버그6: 대형주 revenue 인위 떠받침 방지)
        # 전분기 기준 하한 (0.60 클램핑) vs 초기 assets 0.005% 중 작은 값
        _assets_floor = meta.get('initial_assets', meta['assets']) * 0.005
        revenue = max(_assets_floor, revenue)

        # 비용 계산용 사이클 승수 (기존 유지)
        cycle_cost_mult = {
            '확장': 0.95,
            '정점': 1.00,
            '수축': 1.10,
            '저점': 1.15,
        }.get(cycle, 1.0)

        # efficiency는 주가와 무관하게 기업 자체 역량 반영
        effective_efficiency = meta.get('efficiency', 0.05)

        # ★ 페이즈별 매출 성장 승수 (핵심 — 이익 성장의 근원)
        # 현실: 삼성전자 2000년 매출 30조 → 2025년 300조 (10배)
        # 페이즈가 올라갈수록 시장 규모 자체가 커짐
        # Growth 섹터는 더 크게, Value/Defensive는 완만하게
        from engine.constants import SECTOR_MAP as _SM
        _sector = _SM.get(meta.get('ind', ''), 'Value')
        _cur_phase = getattr(self.s, '_last_processed_phase', '1A')

        # ★ [수정] phase_rev_mult 상한 하향 (버그1: 주가 폭발 방지)
        # Growth 4B: 28 → 14, 전 섹터 약 50% 축소
        # 현실 기준: 26년간 최상위 성장주 10배 수준
        _PHASE_REVENUE_MULT = {
            ("1A", "Growth"):    1.00,
            ("1B", "Growth"):    1.25,
            ("2A", "Growth"):    1.80,
            ("2B", "Growth"):    2.80,
            ("3A", "Growth"):    4.50,
            ("3B", "Growth"):    7.00,
            ("4A", "Growth"):   10.00,
            ("4B", "Growth"):   14.00,
            ("1A", "Cyclical"):  1.00,
            ("1B", "Cyclical"):  1.15,
            ("2A", "Cyclical"):  1.50,
            ("2B", "Cyclical"):  2.10,
            ("3A", "Cyclical"):  3.00,
            ("3B", "Cyclical"):  4.20,
            ("4A", "Cyclical"):  5.80,
            ("4B", "Cyclical"):  7.50,
            ("1A", "Value"):     1.00,
            ("1B", "Value"):     1.12,
            ("2A", "Value"):     1.35,
            ("2B", "Value"):     1.70,
            ("3A", "Value"):     2.20,
            ("3B", "Value"):     2.80,
            ("4A", "Value"):     3.60,
            ("4B", "Value"):     4.50,
            ("1A", "Defensive"): 1.00,
            ("1B", "Defensive"): 1.06,
            ("2A", "Defensive"): 1.14,
            ("2B", "Defensive"): 1.25,
            ("3A", "Defensive"): 1.38,
            ("3B", "Defensive"): 1.52,
            ("4A", "Defensive"): 1.70,
            ("4B", "Defensive"): 1.90,
        }
        phase_rev_mult = _PHASE_REVENUE_MULT.get((_cur_phase, _sector), 1.0)

        # ★ 버그 방지: 승수 상한 — 너무 빠른 성장 억제
        scenario = self.s.current_scenario
        if "대공황" in scenario and "극복" not in scenario:
            phase_rev_mult = max(1.0, phase_rev_mult * 0.5)
        elif cycle in ("수축", "저점"):
            phase_rev_mult = max(1.0, phase_rev_mult * 0.75)

        # ★ 수출/내수 의존도 기반 성장 차등
        from engine.constants import EXPORT_DEPENDENCY, EXPORT_SANCTION_TYPES
        ind = meta.get('ind', '')
        export_dep = EXPORT_DEPENDENCY.get(ind, 0.3)

        # 수출 호황 시나리오 — 수출 의존도 높을수록 더 큰 수혜
        boom = getattr(self.s, 'boom_event', {})
        if boom.get('phase') == '진행중' and boom.get('type') == '수출호황':
            export_bonus = 1.0 + (export_dep * 0.8)  # IT(0.75): +60%, 유틸(0.03): +2.4%
            phase_rev_mult *= export_bonus

        # 내수 붐 시나리오 — 내수 의존도 높을수록 더 큰 수혜
        elif boom.get('phase') == '진행중' and boom.get('type') == '내수붐':
            domestic_dep = 1.0 - export_dep
            domestic_bonus = 1.0 + (domestic_dep * 0.5)
            phase_rev_mult *= domestic_bonus

        # ★ 내수형 섹터 자연 성장 상한 (유틸/부동산/금융)
        # 아무리 페이즈가 올라가도 정부규제/인구한계로 제한
        _DOMESTIC_CAP = {
            "유틸리티":   2.0,   # 한전급 — 정부 요금 통제
            "부동산":     2.5,   # 국내 시장 한계
            "금융":       2.8,   # 예대마진 한계
            "필수소비재": 2.0,   # 인구 정체 (K-식품 수출 예외는 boom에서 처리)
        }
        if ind in _DOMESTIC_CAP:
            phase_rev_mult = min(phase_rev_mult, _DOMESTIC_CAP[ind])

        # ★ 수출 규제 이벤트 적용
        sanctions = getattr(self.s, 'export_sanctions', {})
        for sanction_id, sanction_state in sanctions.items():
            s_def = EXPORT_SANCTION_TYPES.get(sanction_id, {})
            if ind not in s_def.get('target_inds', []):
                continue
            phase = sanction_state.get('phase', '')
            if phase == '단기충격':
                # 단기 패널티: 매출 승수 축소
                penalty = s_def.get('short_penalty', -0.10)
                phase_rev_mult *= (1.0 + penalty)
                phase_rev_mult = max(0.5, phase_rev_mult)
            elif phase == '중장기수혜':
                # 공급망 재편 수혜: 매출 승수 소폭 증가
                benefit = s_def.get('long_benefit', 0.05)
                phase_rev_mult *= (1.0 + benefit)

        # ★ 시총 기반 성장 감쇠 — 기업이 커질수록 기본 성장 둔화
        # 호재(boom/수출/내수/수출규제)는 이미 위에서 phase_rev_mult에 반영됨
        # 여기서는 기업 자체 규모에 따른 자연 둔화만 적용
        # 단, 페이즈 전환 직후 1년간은 감쇠 완화 (LV전환 랠리 보호)
        _mc = stock.get('market_cap', 0)
        _조 = 1_000_000_000_000
        if   _mc < 1 * _조:    _mc_decay = 1.00   # 1조 미만: 풀 성장
        elif _mc < 5 * _조:    _mc_decay = 0.95
        elif _mc < 10 * _조:   _mc_decay = 0.88
        elif _mc < 30 * _조:   _mc_decay = 0.78
        elif _mc < 80 * _조:   _mc_decay = 0.65
        elif _mc < 200 * _조:  _mc_decay = 0.52
        else:                  _mc_decay = 0.42   # 200조+: 대형 우량주 수준

        # 페이즈 전환 부스트: 전환 후 252일간 감쇠 완화
        # ★ [수정] 절대 경과일 기준으로 전환 부스트 계산 (버그7: cycle_day 리셋 오용 방지)
        _trans_abs   = getattr(self.s, '_phase_transition_abs', -9999)
        _cur_abs     = getattr(self.s, '_total_days_elapsed', 0)
        _since_trans = _cur_abs - _trans_abs
        if 0 <= _since_trans <= 252:
            _boost = 1.0 - (_since_trans / 252.0)
            _mc_decay = _mc_decay + (1.0 - _mc_decay) * _boost

        # ★ [버그 수정] _mc_decay를 매출 수준이 아닌 성장률에 적용
        # 기존: phase_rev_mult = _base_rev_mult * _mc_decay → revenue *= 0.78 매 분기
        #       → 시총 20조 기업은 10년간 매출이 0.78^40 = 0.005%로 붕괴
        # 수정: 5번에서 이미 계산된 revenue(= prev × (1+qoq) × macro)에
        #       _mc_decay(성장 감쇠)와 _event_bonus(이벤트 배율)만 추가 적용
        # 주의: _macro_adj/_base_qoq는 5번에서 이미 반영됐으므로 재적용하지 않음
        _base_rev_mult = _PHASE_REVENUE_MULT.get((_cur_phase, _sector), 1.0)
        _event_bonus   = phase_rev_mult / max(0.01, _base_rev_mult)

        # revenue에 mc_decay(성장률 감쇠)를 반영
        # 대기업은 성장률이 낮지만 매출 수준 자체가 줄지 않음
        # 기존 qoq 성장분(revenue - _prev_revenue)에만 decay 적용
        _growth_amount = revenue - _prev_revenue
        revenue = _prev_revenue + _growth_amount * _mc_decay
        # 이벤트 배율 추가 (boom/수출 등)
        revenue = revenue * _event_bonus

        # ★ [수정] phase_rev_mult 적용 후 분기 변동 재캡 (버그1: 극단 노이즈×승수 폭발 방지)
        # 전분기 대비 분기 최대 변동 ±50%로 재제한
        _rev_abs_max = _prev_revenue * 1.50
        _rev_abs_min = _prev_revenue * 0.55
        revenue = max(_rev_abs_min, min(_rev_abs_max, revenue))

        # ★ 테크 레벨 성장 가중치 — 페이즈 기반으로 수정
        try:
            ind = meta.get('ind', '')
            # sub_list 우선, 없으면 sub 단일값으로 fallback
            sub_list = meta.get('sub_list') or [meta.get('sub', '')]
            current_subs = [s.strip() for s in sub_list if s]

            if _current_lv_cache is not None:
                current_phase_subs = _current_lv_cache.get(ind, set())
                old_phase_subs     = _old_lv_cache.get(ind, set())
            else:
                from engine.constants import INDUSTRY_LEVELS, TECH_PHASE
                ind_data   = INDUSTRY_LEVELS.get(ind, {})
                cur_phase  = getattr(self.s, '_last_processed_phase', '1A')
                # 현재 페이즈 전체 subs
                current_phase_subs = set()
                for t_list in ind_data.get(cur_phase, {}).values():
                    current_phase_subs.update(t_list)
                for t_list in ind_data.get('common', {}).values():
                    current_phase_subs.update(t_list)
                # 과거 페이즈 subs
                old_phase_subs = set()
                lv = self.s.max_tech_reached
                all_phases = [p["id"] for p in TECH_PHASE.get(lv, [])]
                for ph in all_phases:
                    if ph == cur_phase:
                        break
                    for t_list in ind_data.get(ph, {}).values():
                        old_phase_subs.update(t_list)

            if any(s in current_phase_subs for s in current_subs):
                # ★ 현재 페이즈 사업: 페이즈가 높을수록 더 큰 보너스
                _phase_tech_bonus = {
                    "1A": 1.10, "1B": 1.15,
                    "2A": 1.25, "2B": 1.35,
                    "3A": 1.45, "3B": 1.55,
                    "4A": 1.65, "4B": 1.80,
                }.get(_cur_phase, 1.30)
                tech_mult = _phase_tech_bonus
            elif any(s in old_phase_subs for s in current_subs):
                # ★ 구시대 사업: 페이즈 격차가 클수록 더 큰 패널티
                tech_mult = 0.85  # 0.9 → 0.85 강화
            else:
                tech_mult = 1.0
        except Exception:
            tech_mult = 1.0

        revenue *= tech_mult
        base_cost = {"대형주": 0.015, "중형주": 0.020, "소형주": 0.030}.get(tier, 0.030)
        base_cost *= cycle_cost_mult   # 경기 사이클 비용 반영
        tier_bonus = {"대형주": 0.02, "중형주": 0.01, "소형주": 0.00}.get(tier, 0)

        op_margin  = effective_efficiency - base_cost + tier_bonus
        op_income  = revenue * op_margin
        interest_rate  = self.s.macro.get('interest_rate', 4.0)
        # 피드백 루프 3: 금리↑ → 기업 이자비용↑ → 실적↓
        # 기존 0.01 → 0.05 (5배 강화, 금리 10%에서 실질 타격)
        # 이자비용 완화: 0.05 → 0.01 (전 종목 적자 방지)
        interest_cost  = meta['assets'] * (interest_rate / 100) * 0.01
        net_income = op_income - interest_cost

        # ★ [수정] net_income 상한 — ROE 200% 방지
        # 현실 최고 ROE: 삼성전자 20~25%, 엔비디아·애플 같은 최우량 성장주 50~80%
        # 분기 ROE 10% = 연 ROE 40% → 최상위 성장주(예: 카카오 전성기) 수준
        # 기존 분기 50% = 연 200%는 현실에 존재하지 않음
        _roe_cap = meta['assets'] * 0.10
        if net_income > _roe_cap:
            net_income = _roe_cap

        quarter_name = {2: "1분기", 5: "2분기", 8: "3분기", 11: "4분기"}.get(
            self.s.current_date.month, "분기"
        )
        announce_dt = self.s.current_date + timedelta(days=7)

        pending_text = (
            f"분기: {quarter_name}\n"
            f"공시 예정일: {announce_dt}\n"
            f"[실적 발표 예정 리포트]"
        )
        fixed_text = (
            f"분기: {quarter_name} (확정)\n"
            f"발표일: {announce_dt.strftime('%m월 %d일')}\n"
            f"매출액: {int(revenue):,} 원\n"
            f"영업이익: {int(op_income):,} 원\n"
            f"당기순이익: {int(net_income):,} 원"
        )

        # ★ 기관 컨센서스 저장 (선행매매 트리거용)
        # 실제 발표값에 ±20% 노이즈를 더해 기관의 "예측" 수치로 저장
        import random as _rnd
        consensus_noise = _rnd.uniform(-0.20, 0.20)
        consensus_ni    = net_income * (1.0 + consensus_noise)
        name = meta.get('c_name', '')
        if name:
            prev_consensus = self.s.earnings_consensus.get(name, {}).get('expected_ni', 0)
            self.s.earnings_consensus[name] = {
                "expected_ni":  consensus_ni,
                "direction":    1 if consensus_ni > 0 else -1,
                "confidence":   max(0.3, 1.0 - abs(consensus_noise)),  # 노이즈 작을수록 확신 높음
                "prev_ni":      prev_consensus,
            }

        return {
            "revenue":              revenue,
            "op_income":            op_income,
            "net_income":           net_income,
            "expected_net_income":  net_income,
            "is_surplus":           net_income > 0,
            "status":               "PENDING",
            "pending_text":         pending_text,
            "fixed_text":           fixed_text,
        }

    # ─────────────────────────────────────────────
    # 실적 공시 확정 (FIXED 상태로 전환 + 히스토리 기록)
    # ─────────────────────────────────────────────
    def announce_earnings(self, stock: dict, year_str: str = None) -> bool:
        meta = stock['meta']
        name = meta['c_name']

        if year_str is None:
            year_str = str(self.s.current_date.year)

        quarter_map = {
            3: "1분기", 4: "1분기",
            6: "2분기", 7: "2분기",
            9: "3분기", 10: "3분기",
            12: "4분기", 1: "4분기",
        }
        quarter = quarter_map.get(self.s.current_date.month, "수시")

        if not meta.get('expected_earnings'):
            meta['expected_earnings'] = self.calculate_potential_earnings(stock)

        earning_data = meta['expected_earnings']
        if earning_data.get('status') == 'FIXED':
            return True

        earning_data['status'] = 'FIXED'

        revenue    = earning_data['revenue']
        op_income  = earning_data['op_income']
        net_income = earning_data['net_income']

        # ★ efficiency 점진적 수렴 — 코스피 기준 + 페이즈 성장 반영
        # 코스피 평균 PER 10~13배 목표
        # 페이즈가 올라갈수록 기업들의 수익성이 자연스럽게 개선됨
        _tier      = meta.get('tier', '소형주')
        _eff_now   = meta.get('efficiency', 0.03)
        _cur_phase = getattr(self.s, '_last_processed_phase', '1A')

        # 페이즈별 efficiency 상한 — 높은 페이즈일수록 수익성 개선 허용
        _PHASE_EFF_CAP = {
            '1A': {'대형주': 0.14, '중형주': 0.10, '소형주': 0.07},
            '1B': {'대형주': 0.16, '중형주': 0.12, '소형주': 0.08},
            '2A': {'대형주': 0.18, '중형주': 0.14, '소형주': 0.10},
            '2B': {'대형주': 0.20, '중형주': 0.16, '소형주': 0.12},
            '3A': {'대형주': 0.23, '중형주': 0.18, '소형주': 0.14},
            '3B': {'대형주': 0.26, '중형주': 0.21, '소형주': 0.16},
            '4A': {'대형주': 0.30, '중형주': 0.24, '소형주': 0.18},
            '4B': {'대형주': 0.35, '중형주': 0.28, '소형주': 0.22},
        }
        _eff_cap = _PHASE_EFF_CAP.get(_cur_phase, {'대형주': 0.10, '중형주': 0.07, '소형주': 0.04})
        _cap_val = _eff_cap.get(_tier, 0.04)

        # 목표값: 현재 페이즈 상한의 70~90% 수준으로 수렴
        _eff_target = random.uniform(_cap_val * 0.70, _cap_val * 0.90)
        _eff_target = min(_eff_target, _cap_val)  # 상한 초과 방지

        # 시총 기반 수렴 속도 감쇠 — 대기업일수록 효율성 개선 더딤
        _mc_now = stock.get('market_cap', 0)
        _조_e   = 1_000_000_000_000
        if   _mc_now < 1  * _조_e:  _conv_speed = 0.10
        elif _mc_now < 10 * _조_e:  _conv_speed = 0.07
        elif _mc_now < 50 * _조_e:  _conv_speed = 0.04
        else:                        _conv_speed = 0.02

        # 10% → 속도로 수렴 (급격한 변화 방지)
        meta['efficiency'] = min(_cap_val, _eff_now + (_eff_target - _eff_now) * _conv_speed)

        # ── HP / Shield 반영 ──────────────────────────────────────
        assets      = max(1, meta['assets'])
        sensitivity = meta.get('risk_sensitivity', 1.0)
        hp          = meta.get('hp', 50.0)
        soft_cap    = meta.get('hp_soft_cap', 60.0)
        shield      = meta.get('shield', 0.0)   # 단위: 원(₩)
        tier        = meta.get('tier', '소형주')
        market_cap  = max(1, stock.get('market_cap', assets))

        # 체급별 쉴드 적립률 및 상한 비율
        _SHIELD_SPEC = {
            '대형주': {'accum': 0.00005, 'cap_ratio': 0.05},
            '중형주': {'accum': 0.00003, 'cap_ratio': 0.03},
            '소형주': {'accum': 0.00001, 'cap_ratio': 0.01},
        }
        spec       = _SHIELD_SPEC.get(tier, _SHIELD_SPEC['소형주'])
        shield_cap = market_cap * spec['cap_ratio']

        if net_income < 0:
            # 연속 적자 카운트
            loss_cnt = meta.get('continuous_loss_count', 0) + 1
            meta['continuous_loss_count'] = loss_cnt

            # ★ 최소 4분기(1년)치 실적이 쌓인 후에만 HP 차감
            # 첫 발표 때는 비교 기준이 없으므로 HP 차감 없음
            # 현실: 신규 상장 후 첫 적자로 재무위기 판정은 불합리
            _name = meta.get('c_name', '')
            _hist = self.s.earnings_history.get(_name, {})
            _total_q = sum(len(qd) for qd in _hist.values())
            if _total_q < 4:
                # 실적 기록 부족 — HP 차감 없이 assets/debt만 반영
                _init_a = meta.get('initial_assets', assets)
                meta['assets'] = max(
                    _init_a * 0.10,   # ★ 0.01 → 0.10 (earnings.py 본문과 통일)
                    meta['assets'] + net_income
                )
                debt = meta.get('debt', 0.0)
                new_debt_early = debt + abs(net_income) * 0.20
                # 부채비율 500% 캡 (본문과 동일)
                if meta['assets'] > 0 and new_debt_early / meta['assets'] > 5.0:
                    new_debt_early = meta['assets'] * 5.0
                meta['debt'] = new_debt_early
                if meta['assets'] > 0:
                    meta['debt_ratio'] = meta['debt'] / meta['assets']
                return True

            from engine.constants import SECTOR_MAP
            scenario = self.s.current_scenario
            sector   = SECTOR_MAP.get(meta.get('ind', ''), 'Value')
            if '대공황' in scenario and '극복' not in scenario:
                sw = 1.3 if sector == 'Growth' else 0.6 if sector == 'Defensive' else 1.0
            else:
                sw = 1.0

            loss_ratio = abs(net_income) / max(1.0, revenue)
            hp_dmg_raw = loss_ratio * 100 * sensitivity * sw

            # ★ 연속 적자 가속 (재무 기반 상폐 핵심)
            # ★ [수정] 연속 적자 HP 차감 가속 완화
            # 기존: 2년 연속 시 4배 가속 → 소형주 2년 연속 적자면 분기 최대 6.0
            # 코스피 현실: 연속 적자여도 회생 기회 있음 (수년간 적자 상태로 유지 가능)
            if   loss_cnt >= 8: hp_dmg_raw *= 2.5   # 기존 4.0 → 2.5
            elif loss_cnt >= 4: hp_dmg_raw *= 1.5   # 기존 2.0 → 1.5
            elif loss_cnt >= 2: hp_dmg_raw *= 1.2   # 기존 1.5 → 1.2

            # 부채비율 추가 차감 (극단값만, market.py와 중복 최소화)
            debt_ratio = meta.get('debt_ratio', 0.5)
            if   debt_ratio >= 10.0: hp_dmg_raw += 3.0  # 기존 5.0 → 3.0
            elif debt_ratio >= 5.0:  hp_dmg_raw += 1.5  # 기존 3.0 → 1.5
            elif debt_ratio >= 3.0:  hp_dmg_raw += 0.8  # 기존 2.0 → 0.8
            elif debt_ratio >= 2.0:  hp_dmg_raw += 0.3  # 기존 1.0 → 0.3
            # 1.5 이하는 제거 (정상 범위 부채비율은 적자여도 HP 추가 차감 없음)

            # HP 캡 (분기당 최대 차감량 제한)
            _BASE_CAP = {'대형주': 0.5, '중형주': 0.8, '소형주': 1.2}  # 기존 1.5 → 1.2
            base_cap  = _BASE_CAP.get(tier, 1.2)
            if   loss_cnt >= 8: cap_mult = 2.5   # 기존 4.0 → 2.5
            elif loss_cnt >= 4: cap_mult = 1.8   # 기존 2.5 → 1.8
            elif loss_cnt >= 2: cap_mult = 1.3   # 기존 1.5 → 1.3
            else:               cap_mult = 1.0
            hp_dmg = min(hp_dmg_raw, base_cap * cap_mult)

            # 방어막 상한 재조정 + 소진 처리
            shield_cap_now = market_cap * spec['cap_ratio']
            if shield > shield_cap_now:
                meta['shield'] = shield = round(shield_cap_now, 2)
            if shield < 100:
                meta['shield'] = shield = 0.0

            hp_ratio    = hp / max(1.0, soft_cap)
            shield_mode = hp_ratio < 0.30

            if shield_mode and shield > 0:
                shield_hp_val = shield / max(1.0, assets) * 100
                if shield_hp_val >= hp_dmg:
                    meta['shield'] = round(max(0.0, shield - hp_dmg / 100 * assets), 2)
                else:
                    remaining_dmg  = hp_dmg - shield_hp_val
                    meta['shield'] = 0.0
                    meta['hp']     = round(max(0.0, hp - remaining_dmg), 2)
            else:
                meta['hp'] = round(max(0.0, hp - hp_dmg), 2)

            # ★ [수정] 적자 시 assets 감소 — 하한선 초기값의 10%로 복구
            # 1%는 너무 낮아서 부채비율 1000%+ 폭발 유발
            # 코스피 현실: 자본잠식 진입해도 즉시 0이 되지 않음
            init_assets = meta.get('initial_assets', assets)
            meta['assets'] = max(
                init_assets * 0.10,   # 하한선: 초기값의 10% (기존 1% → 10%)
                meta['assets'] + net_income
            )

            # ★ 적자 시 부채 증가
            debt = meta.get('debt', 0.0)
            debt_increase = abs(net_income) * 0.20
            new_debt = debt + debt_increase

            # ★ [신규] 부채비율 상한 캡: 500% 초과 시 부채 탕감 처리
            # 현실: 부채비율 500%+ 되면 채권단 출자전환/워크아웃으로 부채 조정
            new_assets = meta['assets']
            if new_assets > 0 and new_debt / new_assets > 5.0:
                # 부채비율 500%로 상한 (현실적 채권단 개입 수준)
                new_debt = new_assets * 5.0
            meta['debt'] = new_debt
            # 부채비율 갱신
            if meta['assets'] > 0:
                meta['debt_ratio'] = meta['debt'] / meta['assets']

            # ★ 자본잠식 체크 (assets < debt)
            # [수정] 즉시 -5 → -1.0으로 완화 (코스피: 자본잠식 발생 시 관리종목 지정 후 1년 유예)
            # 즉시 퇴출이 아니라 서서히 악화되는 구조가 현실적
            if meta['assets'] < meta.get('debt', 0):
                meta['hp'] = max(0.0, meta.get('hp', 0) - 1.0)

            # ★ 적자 시 assets 감소 (하한선 보장)
            init_assets = meta.get('initial_assets', assets)
            meta['assets'] = max(
                init_assets * 0.1,
                meta['assets'] + net_income
            )

        else:
            # ── 흑자: 연속 적자 초기화 + HP 회복 + 부채 감소 ──
            meta['continuous_loss_count'] = 0

            # ★ 흑자 시 부채 상환 — 기업별 목표 부채비율로 수렴 (고정 하한선 제거)
            debt = meta.get('debt', 0.0)
            assets_now = meta.get('assets', 1.0)

            # 기업 생성 시 배정된 목표 부채비율 사용 (없으면 티어별 기본값)
            _DEFAULT_TARGET = {'대형주': 0.40, '중형주': 0.50, '소형주': 0.60}
            target_debt_ratio = meta.get('target_debt_ratio',
                                         _DEFAULT_TARGET.get(tier, 0.40))
            min_debt = assets_now * target_debt_ratio

            debt_repay = min(max(0.0, debt - min_debt), net_income * 0.3)
            meta['debt'] = max(min_debt, debt - debt_repay)
            if meta['assets'] > 0:
                meta['debt_ratio'] = meta['debt'] / meta['assets']

            # HP 회복량 (0.8배)
            heal   = (net_income / assets * 100) * 0.8
            new_hp = hp + heal

            if new_hp <= soft_cap:
                meta['hp'] = round(new_hp, 2)
            else:
                # soft_cap 초과분 → 전 체급 쉴드 적립 (상한선 적용)
                overflow      = new_hp - soft_cap
                meta['hp']    = soft_cap
                shield_gain   = overflow * (assets * spec['accum'])
                meta['shield'] = round(min(shield_cap, shield + shield_gain), 2)

                # 소형주는 추가로 주가 펌핑 (최대 +30%)
                if '소형' in tier:
                    pump = min(0.30, overflow * 0.02)
                    meta['_hp_overflow_pump'] = pump   # market.py에서 price에 반영
        # ────────────────────────────────────────────────────────

        # 히스토리 기록
        self.s.earnings_history.setdefault(name, {}).setdefault(year_str, {})[quarter] = {
            "date":       self.s.current_date.strftime("%m월 %d일"),
            "revenue":    revenue,
            "op_income":  op_income,
            "net_income": net_income,
            "is_surplus": net_income > 0,
            "surprise":   "공시 완료",
        }

        # assets는 적자/흑자 각 블록에서 처리됨 (위에서 처리)
        # ★ 흑자 시 assets 증가 — 재투자 비율 제한 + 상한 강화
        if net_income > 0:
            init_assets_v = meta.get('initial_assets', meta['assets'])
            # 티어별 재투자율 (나머지는 배당/자사주매입으로 유출)
            # 현실: 대기업은 이익의 30~50%만 재투자
            _reinvest = {
                '대형주': 0.30,  # 30% 재투자
                '중형주': 0.50,  # 50% 재투자
                '소형주': 0.70,  # 70% 재투자 (성장기)
            }.get(tier, 0.50)
            reinvested = net_income * _reinvest
            meta['assets'] = meta['assets'] + reinvested

        # ★ 신용등급 복합 판정 (HP + 부채비율 + 연속적자)
        hp_now     = meta.get('hp', 50.0)
        sc_now     = meta.get('hp_soft_cap', 60.0)
        hp_pct_now = hp_now / max(1.0, sc_now)
        dr_now     = meta.get('debt_ratio', 0.5)
        lc_now     = meta.get('continuous_loss_count', 0)

        if hp_pct_now >= 0.80 and dr_now < 1.0 and lc_now == 0:
            meta['credit_grade'] = 'AA'
        elif hp_pct_now >= 0.50 and dr_now < 2.0 and lc_now < 4:
            meta['credit_grade'] = 'BB'
        else:
            meta['credit_grade'] = 'CCC'

        # market.py 지배구조/momentum 반영용 플래그
        meta['_earnings_just_released'] = True

        # ★ 실적 서프라이즈/쇼크 — 컨센서스 대비로 계산
        consensus = self.s.earnings_consensus.get(name, {})
        consensus_ni = consensus.get('expected_ni', net_income)  # 컨센서스 없으면 실제값 사용

        if abs(consensus_ni) > 0:
            surprise_ratio = (net_income - consensus_ni) / abs(consensus_ni)
        elif net_income > 0:
            surprise_ratio = 0.15   # 흑자 전환
        else:
            surprise_ratio = -0.15  # 적자 전환

        # 서프라이즈 강도에 따라 shock 값 결정
        if surprise_ratio > 0.30:      # 컨센서스 30%+ 초과 → 강한 서프라이즈
            meta['_earnings_shock'] = min(0.20, surprise_ratio * 0.3)
        elif surprise_ratio > 0.10:    # 10~30% 초과 → 약한 서프라이즈
            meta['_earnings_shock'] = min(0.10, surprise_ratio * 0.2)
        elif surprise_ratio < -0.30:   # 컨센서스 30%+ 미달 → 강한 쇼크
            meta['_earnings_shock'] = max(-0.25, surprise_ratio * 0.3)
        elif surprise_ratio < -0.10:   # 10~30% 미달 → 약한 쇼크
            meta['_earnings_shock'] = max(-0.12, surprise_ratio * 0.2)
        else:                          # ±10% 이내 → 무반응
            meta['_earnings_shock'] = 0.0

        # 컨센서스 업데이트 (발표 후 실제값으로 갱신)
        self.s.earnings_consensus[name] = {
            "expected_ni": net_income,
            "direction":   1 if net_income > 0 else -1,
            "confidence":  0.9,
            "prev_ni":     consensus_ni,
        }

        return True

    # ─────────────────────────────────────────────
    # 실적 공시 스케줄 처리 (next_day에서 호출)
    # ─────────────────────────────────────────────
    def process_earnings_schedule(self, silent: bool):
        cur_month = self.s.current_date.month
        cur_day   = self.s.current_date.day
        cur_date  = self.s.current_date
        start_date = getattr(self.s, 'start_date', cur_date)

        # 1일: 다음 실적 발표일 예약 + 수치 확정
        if cur_month in [2, 5, 8, 11] and cur_day == 1:
            # ★ 페이즈 기반 캐시 (기존 숫자 LV 키 → 페이즈 ID 기반)
            from engine.constants import INDUSTRY_LEVELS as _IL, TECH_PHASE
            cur_phase = getattr(self.s, '_last_processed_phase', '1A')
            lv        = self.s.max_tech_reached

            # 현재 페이즈 subs 캐시
            _current_lv_cache = {}
            for ind, ind_data in _IL.items():
                subs = set()
                for t_list in ind_data.get(cur_phase, {}).values():
                    subs.update(t_list)
                for t_list in ind_data.get('common', {}).values():
                    subs.update(t_list)
                _current_lv_cache[ind] = subs

            # 구시대 페이즈 subs 캐시
            all_phases = [p["id"] for p in TECH_PHASE.get(lv, [])]
            _old_lv_cache = {}
            for ind, ind_data in _IL.items():
                old_subs = set()
                for ph in all_phases:
                    if ph == cur_phase:
                        break
                    for t_list in ind_data.get(ph, {}).values():
                        old_subs.update(t_list)
                _old_lv_cache[ind] = old_subs

            for stock in self.s.stocks:
                meta = stock['meta']
                meta['report_day']        = random.randint(7, 28)
                meta['earning_news_date'] = cur_date.strftime('%Y-%m-%d')
                meta['expected_earnings'] = self.calculate_potential_earnings(
                    stock, _current_lv_cache, _old_lv_cache
                )
                is_surplus = meta['expected_earnings'].get('is_surplus', True)
                meta['momentum'] += 0.02 if is_surplus else -0.02

        # 공시 발표일 처리
        report_target_months = [3, 4, 6, 7, 9, 10, 12]
        if cur_month not in report_target_months:
            return

        q_map = {3: "1분기", 4: "1분기", 6: "2분기", 7: "2분기",
                 9: "3분기", 10: "3분기", 12: "4분기"}
        target_q        = q_map.get(cur_month, "분기")
        target_year_str = str(self.s.current_date.year)

        for stock in self.s.stocks:
            meta    = stock['meta']
            history = self.s.earnings_history.setdefault(meta['c_name'], {}).setdefault(target_year_str, {})

            if target_q not in history:
                report_day  = meta.get('report_day', 15)
                is_deadline = cur_month == 12 and cur_day >= 24

                if (cur_day >= report_day or is_deadline) and self.s.is_market_open:
                    self.announce_earnings(stock, target_year_str)
                    if not silent:
                        self.s.daily_news.append(
                            f"📊 [실적공시] {meta['c_name']} {target_q} 발표"
                        )