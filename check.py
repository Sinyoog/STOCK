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

# 연도별 상폐 수
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
# 상폐 사유 분포
reasons = {}
for st in delisted:
    # pending_events에서 reason 못 가져오므로 HP로 추정
    hp = st['meta'].get('hp', 0)
    if hp <= 0:
        r = "HP 소진"
    else:
        r = "연속적자(구버전)"
    reasons[r] = reasons.get(r, 0) + 1

print("=== 상폐 사유 추정 ===")
for r, cnt in sorted(reasons.items(), key=lambda x: x[1], reverse=True):
    print(f"  {r}: {cnt}개")

print()
# 현재 티어 분포
tiers = {}
for st in stocks:
    t = st['meta'].get('tier', '소형주')
    tiers[t] = tiers.get(t, 0) + 1
print("=== 현재 티어 분포 ===")
for t, cnt in tiers.items():
    print(f"  {t}: {cnt}개")

# 신규 상장 속도 (연도별)
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
    net = listed_cnt - delist_cnt
    print(f"  {y}년: 상장 {listed_cnt}개 / 상폐 {delist_cnt}개 / 순증 {net:+d}개")