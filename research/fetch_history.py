"""Download hourly Binance klines (public market data) for research. Run on GitHub Actions."""
import csv, gzip, sys, time
import httpx

BASE = "https://data-api.binance.vision/api/v3/klines"
START = {"BTCUSDT": "2019-01-01", "ETHUSDT": "2019-01-01", "SOLUSDT": "2020-08-11"}

def ms(d): return int(time.mktime(time.strptime(d, "%Y-%m-%d"))) * 1000

with httpx.Client(timeout=30) as c:
    for sym, start in START.items():
        cur, rows = ms(start), []
        while True:
            for attempt in range(5):
                try:
                    r = c.get(BASE, params={"symbol": sym, "interval": "1h", "startTime": cur, "limit": 1000}); r.raise_for_status(); break
                except Exception as e:
                    print("retry", e); time.sleep(3)
            b = r.json()
            if not b: break
            rows += [(k[0], k[1], k[2], k[3], k[4], k[5], k[7]) for k in b]
            if len(b) < 1000: break
            cur = b[-1][0] + 3_600_000
            time.sleep(0.1)
        with gzip.open(f"{sym}_1h.csv.gz", "wt", newline="") as f:
            w = csv.writer(f); w.writerow(["open_time", "open", "high", "low", "close", "volume", "quote_volume"]); w.writerows(rows)
        print(sym, len(rows)); sys.stdout.flush()
