import sqlite3, json

conn = sqlite3.connect('stock_data.db')
cur = conn.cursor()

with open('save_game.json', 'r', encoding='utf-8') as f:
    save = json.load(f)

delisted = save.get('engine', {}).get('delisted_stocks', [])

# 상폐 종목 중 가격이 1원 미만인 데이터가 있는 종목 확인
print("가격 이상한 상폐 종목 (최솟값 1원 미만):")
weird = []
for s in delisted:
    name = s['meta']['c_name']
    cur.execute("SELECT MIN(price), MAX(price), COUNT(*) FROM stock_history WHERE company_name=?", (name,))
    row = cur.fetchone()
    if row and row[0] is not None and row[0] < 1:
        weird.append((name, row[0], row[1], row[2]))

print(f"총 {len(weird)}개")
for name, mn, mx, cnt in weird[:10]:
    print(f"  {name}: 최소={mn}, 최대={mx}, 데이터수={cnt}")

# 정상 종목 샘플
print("\n정상 상폐 종목 샘플 (최솟값 100원 이상):")
normal = []
for s in delisted[:5]:
    name = s['meta']['c_name']
    cur.execute("SELECT MIN(price), MAX(price), COUNT(*) FROM stock_history WHERE company_name=?", (name,))
    row = cur.fetchone()
    if row:
        print(f"  {name}: 최소={row[0]}, 최대={row[1]}, 데이터수={row[2]}")

conn.close()