import json
from datetime import datetime

with open('save_game.json', encoding='utf-8') as f:
    s = json.load(f)

delisted = s['engine'].get('delisted_stocks', [])
stocks = s['engine']['stocks']
current_date = s['engine'].get('current_date', '?')

print(f"현재 날짜: {current_date}")
print(f"현재 상장 종목: {len(stocks)}개")
print(f"상장폐지 종목: {len(delisted)}개")
print()

# ── 분할 관련 종목 진단 ──────────────────────────────
print("=== 분할 미발생 고가 종목 진단 ===")
print(f"{'종목명':<20} {'주가':>14} {'주식수':>12} {'will_to_split':>14} {'cooldown':>10} {'split_count':>12} {'tier':>8}")
print("-" * 100)

HIGH_PRICE = 5_000_000  # 대형주 분할 트리거 기준

for st in sorted(stocks, key=lambda x: x['price'], reverse=True):
    meta  = st['meta']
    price = st['price']
    tier  = meta.get('tier', '소형주')
    if '소형' in tier:
        continue
    if price < HIGH_PRICE:
        break  # 정렬돼 있으니 이 아래는 다 낮음

    name        = meta.get('c_name', '?')
    shares      = st.get('shares', 0)
    wts         = meta.get('will_to_split', '없음')
    cooldown    = meta.get('split_cooldown_days', 0)
    split_count = meta.get('split_count', 0)
    pending     = '예약중' if meta.get('pending_split') else '-'

    print(f"{name:<20} {price:>14,}원  {shares:>12,}주  {str(wts):>14}  {cooldown:>10}일  {split_count:>12}회  {tier:>8}  {pending}")

print()

# ── 연도별 상폐 수 ───────────────────────────────────
by_year = {}
for st in delisted:
    d = st['meta'].get('delisted_date', '')
    if d:
        y = d[:4]
        by_year[y] = by_year.get(y, 0) + 1

print("=== 연도별 상폐 수 ===")
for y in sorted(by_year.keys()):
    print(f"  {y}년: {by_year[y]}개")

print()
# ── 상폐 사유 분포 ───────────────────────────────────
reasons = {}
for st in delisted:
    hp = st['meta'].get('hp', 0)
    r  = "HP 소진" if hp <= 0 else "연속적자(구버전)"
    reasons[r] = reasons.get(r, 0) + 1

print("=== 상폐 사유 추정 ===")
for r, cnt in sorted(reasons.items(), key=lambda x: x[1], reverse=True):
    print(f"  {r}: {cnt}개")

print()
# ── 현재 티어 분포 ───────────────────────────────────
tiers = {}
for st in stocks:
    t = st['meta'].get('tier', '소형주')
    tiers[t] = tiers.get(t, 0) + 1
print("=== 현재 티어 분포 ===")
for t, cnt in tiers.items():
    print(f"  {t}: {cnt}개")

# ── 연도별 신규 상장 수 ──────────────────────────────
by_year_listed = {}
for st in stocks + delisted:
    d = st['meta'].get('listed_date', '')
    if d:
        y = d[:4]
        by_year_listed[y] = by_year_listed.get(y, 0) + 1

print()
print("=== 연도별 신규 상장 수 ===")
for y in sorted(by_year_listed.keys()):
    delist_cnt = by_year.get(y, 0)
    listed_cnt = by_year_listed[y]
    net        = listed_cnt - delist_cnt
    print(f"  {y}년: 상장 {listed_cnt}개 / 상폐 {delist_cnt}개 / 순증 {net:+d}개")