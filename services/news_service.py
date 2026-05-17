"""
services/news_service.py
NewsService: 뉴스 목록 생성, 누적 이벤트 조회, 공시 텍스트 빌드.
UI 코드 금지 — PyQt 임포트 없음.
"""
from datetime import datetime, timedelta
from engine.constants import SECTOR_MAP


class NewsService:
    def __init__(self, state):
        self.s = state

    # ─────────────────────────────────────────────
    # 현재 날짜 기준 뉴스 목록 생성
    # ─────────────────────────────────────────────
    def generate_news_list(self) -> list:
        """
        엔진 상태를 읽어 오늘의 공시/뉴스 항목 리스트를 반환합니다.
        각 항목: {"title", "content", "type", "stock_name"(optional)}
        """
        news_list     = []
        current_date  = self.s.current_date

        for stock in self.s.stocks:
            meta = stock['meta']
            name = meta['c_name']
            expected = meta.get('expected_earnings')

            if expected and self._is_earnings_day(meta, current_date):
                if self.s.has_paid_news_access:
                    net = expected.get('net_income', 0)
                    content = f"[{name}] 분기 실적 발표: 당기순이익 {int(net):,}원 달성"
                else:
                    content = f"[{name}] 분기 실적 발표: 실적 수치는 프리미엄 구독자에게만 공개됩니다."
                news_list.append({
                    "title":      "기업 공시",
                    "content":    content,
                    "type":       "EARNINGS",
                    "stock_name": name,
                })

        # 상장폐지 예고
        for name, info in self.s.pending_events.get("delist", {}).items():
            p_date = info.get('date') if isinstance(info, dict) else info
            days_left = (p_date.date() - current_date.date()).days if p_date else -1
            if 0 < days_left <= 7:
                news_list.append({
                    "title":      "상장폐지 예고",
                    "content":    f"[{name}] D-{days_left}: 상장폐지 예정입니다.",
                    "type":       "DELIST_WARNING",
                    "stock_name": name,
                })

        # 기술 도약 예고
        tech_jump = self.s.pending_events.get("tech_jump")
        if tech_jump:
            p_date = tech_jump.get('date')
            days_left = (p_date.date() - current_date.date()).days if p_date else -1
            if 0 < days_left <= 30:
                lv = tech_jump.get('target_lv', '?')
                news_list.append({
                    "title":   "기술 도약 예고",
                    "content": f"D-{days_left}: 문명이 Lv.{lv}로 도약할 예정입니다.",
                    "type":    "TECH_JUMP",
                })

        # 신규 상장 예고
        for pending in self.s.pending_listings:
            news_list.append({
                "title":      "신규 상장 예고",
                "content":    f"[{pending['meta']['c_name']}] {pending['meta']['listed_date']} 상장 예정",
                "type":       "IPO",
                "stock_name": pending['meta']['c_name'],
            })

        return news_list

    # ─────────────────────────────────────────────
    # 누적 이벤트 전체 조회 (뉴스 창 목록)
    # ─────────────────────────────────────────────
    def get_all_cumulative_events(self) -> list:
        cumulative  = []
        curr_dt_obj = self.s.current_date
        curr_date   = curr_dt_obj.date()
        is_sub      = self.s.has_paid_news_access

        # 1. 상장 예정 리스트
        for stock in self.s.pending_listings:
            if not is_sub:
                continue
            meta = stock.get('meta', {})
            try:
                l_date_str  = meta.get('listed_date', '2000-01-01')
                listed_dt   = datetime.strptime(l_date_str, '%Y-%m-%d').date()
                trigger_date = listed_dt - timedelta(days=7)
                if trigger_date.year >= 2000 and trigger_date <= curr_date:
                    sh      = stock.get('shares', 0)
                    mc      = stock.get('market_cap', 0)
                    pr      = mc // sh if sh > 0 else 0
                    content = self._make_stock_content(meta, "예고", sh, mc, pr)
                    ev      = self._create_ev(trigger_date, "💎 상장예고", meta, content, stock)
                    if ev:
                        cumulative.append(ev)
            except Exception:
                continue

        # 2. 상장된 종목 이벤트
        for stock in self.s.stocks:
            try:
                meta       = stock.get('meta', {})
                c_name     = meta.get('c_name', 'Unknown')
                l_date_str = meta.get('listed_date', '2000-01-01')
                listed_dt  = datetime.strptime(l_date_str, '%Y-%m-%d').date()
                sh         = stock.get('shares', 0)
                mc         = stock.get('market_cap', 0)
                pr         = mc // sh if sh > 0 else 0

                if listed_dt <= curr_date:
                    # 신규 상장 이벤트
                    cumulative.append(
                        self._create_ev(listed_dt, "🚀 신규상장", meta,
                                        self._make_stock_content(meta, "공시", sh, mc, pr), stock)
                    )
                    # 상장 예고 (프리미엄)
                    if is_sub:
                        t_date = listed_dt - timedelta(days=7)
                        if t_date.year >= 2000:
                            cumulative.append(
                                self._create_ev(t_date, "💎 상장예고", meta,
                                                self._make_stock_content(meta, "예고", sh, mc, pr), stock)
                            )

                # 실적 이벤트
                if 'report_day' in meta and curr_dt_obj.year >= 2000:
                    y = curr_dt_obj.year
                    for report_m in [3, 6, 9, 12]:
                        r_day      = min(meta.get('report_day', 15), 28)
                        report_dt  = datetime(y, report_m, r_day).date()
                        d_7_date   = report_dt - timedelta(days=7)
                        q_name     = {3: "1분기", 6: "2분기", 9: "3분기", 12: "4분기"}[report_m]

                        # 확정 공시
                        if report_dt <= curr_date:
                            history = self.s.earnings_history.get(c_name, {}).get(str(y), {})
                            h_data  = history.get(q_name)
                            cumulative.append(
                                self._create_ev(report_dt, "📢 실적공시", meta,
                                                self._make_earn_content(meta, y, q_name, h_data, "확정", report_dt),
                                                stock)
                            )

                        # 실적 예고 (D-7)
                        if d_7_date <= curr_date:
                            data = meta.get('expected_earnings', {})
                            cumulative.append(
                                self._create_ev(d_7_date, "💎 실적예고(P)", meta,
                                                self._make_earn_content(meta, y, q_name, data, "예고", report_dt),
                                                stock)
                            )

            except Exception:
                continue

        # 수동 등록 이벤트
        for ev in self.s.pre_reflection_events:
            cumulative.append(ev)

        cumulative.sort(
            key=lambda x: (x.get('date', ''), x.get('category', '')),
            reverse=True
        )
        return cumulative

    # ─────────────────────────────────────────────
    # 종목 공시 텍스트 생성
    # ─────────────────────────────────────────────
    def _make_stock_content(self, meta: dict, mode: str,
                             shares: int, m_cap: int, price: int) -> str:
        """상장 공시/예고 본문 생성.
        - 공시(상장 당일): 무료 포함 전체 공개
        - 예고(D-7):      프리미엄만 표시 (호출 전에 is_sub 체크 후 호출할 것)
        """
        name    = meta.get('c_name', '')
        sector  = SECTOR_MAP.get(meta.get('ind', ''), 'Growth')
        tier    = meta.get('tier', '소형주')
        size    = {"대형주": "[대기업]", "중형주": "[중견기업]", "소형주": "[중소기업]"}.get(tier, "[중소기업]")

        shares_txt = f"{shares:,} 주" if shares > 0 else "산정 중"

        # 시총 단위 변환
        def fmt_cap(v):
            if v >= 10_000_000_000_000_000: return f"{v/10_000_000_000_000_000:.2f}경"
            elif v >= 1_000_000_000_000:    return f"{v/1_000_000_000_000:.2f}조"
            elif v >= 100_000_000:          return f"{v/100_000_000:.0f}억"
            else:                           return f"{v:,.0f}원"

        mcap_txt = f"{m_cap:,} 원 ({fmt_cap(m_cap)})" if m_cap > 0 else "산정 중"

        ts  = meta.get('treasury_share', 0) * 100
        os_ = meta.get('owner_share',    0) * 100
        fs  = meta.get('foreign_share',  0) * 100
        ins = meta.get('inst_share',     0) * 100
        rs  = meta.get('retail_share',   0) * 100

        # HP / Shield
        hp       = meta.get('hp', 0.0)
        soft_cap = meta.get('hp_soft_cap', 60.0)
        shield   = meta.get('shield', 0.0)
        hp_ratio = hp / max(1.0, soft_cap)
        filled   = round(hp_ratio * 10)
        hp_bar   = '■' * filled + '□' * (10 - filled)

        if shield >= 1_000_000_000_000:   shield_str = f"{shield/1_000_000_000_000:.2f}조"
        elif shield >= 100_000_000:        shield_str = f"{shield/100_000_000:.1f}억"
        else:                              shield_str = f"{shield:,.0f}원"

        # 뉴스 상 rank 계산 (간이)
        all_stocks = self.s.stocks
        sorted_stocks = sorted(all_stocks, key=lambda x: x.get('market_cap', 0), reverse=True)
        rank = next((i+1 for i, s in enumerate(sorted_stocks) if s['meta'].get('c_name') == name), 0)
        rank_str = f"[{rank}위] " if rank > 0 else ""

        return (
            f"{rank_str}< {name} >\n"
            f"상장일: {meta.get('listed_date', '-')} | 섹터: {sector}\n"
            f"[기업 정보] 그룹: {meta.get('group', '독립')} 규모: {size} "
            f"산업: {meta.get('ind', '-')} ({meta.get('sub', '-')}) 상태: {meta.get('char', 'Normal')}\n"
            f"[발행 정보] 주식수: {shares_txt} 시총: {mcap_txt}\n"
            f"[지배구조] 자사주: {ts:.1f}% | 대주주: {os_:.1f}% "
            f"외국인: {fs:.1f}% | 기관 : {ins:.1f}% 개인 : {rs:.1f}%\n"
            f"[재무 체력]\n"
            f"{hp_bar}\n"
            f"HP {hp:.2f} / {soft_cap:.0f} ({hp_ratio*100:.1f}%)\n"
            f"방어막 {shield_str} [{'발동중' if hp_ratio < 0.30 and shield > 0 else '대기중'}]"
        )

    def _make_earn_content(self, meta: dict, y: int, q_name: str,
                            data, mode: str, r_date=None) -> str:
        c_name = meta.get('c_name', '')

        # 확정 히스토리 우선 참조
        real_data = self.s.earnings_history.get(c_name, {}).get(str(y), {}).get(q_name)
        target    = real_data if real_data else (data or {})

        if not target:
            return f"{q_name}\n데이터 분석 중입니다..."

        # 프리미엄 미구독 + 예고 → 예상 공시일만 표시
        if mode == "예고" and not self.s.has_paid_news_access:
            d_val = r_date.strftime('%Y년 %m월 %d일') if r_date else '예정'
            return (
                f"■ {q_name} 실적 예고\n"
                f"예상 공시일: {d_val}\n"
                f"------------------------------------------\n"
                f"상세 수치는 프리미엄 구독자에게만 공개됩니다."
            )

        rev = target.get('revenue', 0)
        op  = target.get('op_income', target.get('operating_income', 0))
        net = target.get('net_income', 0)

        def get_rev_diff():
            try:
                q_idx = ["1분기","2분기","3분기","4분기"].index(q_name)
                p_y   = y if q_idx > 0 else y - 1
                p_q   = ["1분기","2분기","3분기","4분기"][q_idx - 1]
                p_data = self.s.earnings_history.get(c_name, {}).get(str(p_y), {}).get(p_q)
                if p_data:
                    p_rev = p_data.get('revenue', 0)
                    if p_rev > 0:
                        return " (↑)" if rev > p_rev else (" (↓)" if rev < p_rev else " (-)")
            except Exception:
                pass
            return ""

        def get_status(val):
            return " (흑자)" if val > 0 else (" (적자)" if val < 0 else " (보합)")

        status_tag  = "(확정)" if mode == "확정" else "(예고)"
        date_label  = "공시일" if mode == "확정" else "예상공시일"
        d_val       = target.get('date') or (r_date.strftime('%m월 %d일') if r_date else '당일')

        return (
            f"■ 공시 분석 내용:\n"
            f"{q_name} {status_tag}\n"
            f"{date_label}: {d_val}\n"
            f"------------------------------------------\n"
            f"매출액 : {rev:,.0f}원{get_rev_diff()}\n"
            f"영업이익 : {op:,.0f}원{get_status(op)}\n"
            f"당기순이익 : {net:,.0f}원{get_status(net)}"
        )

    def _create_ev(self, dt, cat: str, meta: dict,
                    pub: str, stock=None) -> dict | None:
        try:
            # dt가 date 객체일 수도, 문자열일 수도 있음
            date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)

            shares     = meta.get('shares', 0)
            if shares <= 0 and stock:
                shares = stock.get('shares', 50_000_000)

            price      = stock.get('price', 0) if stock else 0
            if price <= 0:
                assets = meta.get('assets', 0)
                price  = int(assets / max(1, shares))

            market_cap = stock.get('market_cap', price * shares) if stock else price * shares

            return {
                "date":          date_str,
                "category":      cat,
                "cat":           cat,          # filter_table 호환용
                "target":        meta.get('c_name', ''),
                "public_text":   pub,
                "public":        pub,          # update_detail_view 호환용
                "meta_ref":      meta,
                "shares":        int(shares),
                "market_cap":    int(market_cap),
                "start_price":   int(price),
            }
        except Exception:
            return None

    # ─────────────────────────────────────────────
    # 헬퍼
    # ─────────────────────────────────────────────
    def _is_earnings_day(self, meta: dict, current_date) -> bool:
        report_day = meta.get('report_day', 15)
        cur_month  = current_date.month
        cur_day    = current_date.day
        return cur_month in [3, 6, 9, 12] and cur_day == report_day