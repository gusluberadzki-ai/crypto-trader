"""Download forward-looking market data history for research (run on GitHub Actions):
  - Crypto Fear & Greed index (alternative.me, daily since 2018)
  - Deribit BTC/ETH perpetual funding rates (crowding: how much longs pay shorts), hourly since 2019
  - Deribit DVOL implied volatility index (the options market's forecast of future volatility), daily since 2021
  - Hyperliquid SOL/BTC/ETH funding (hourly, since 2023)
"""
import csv, gzip, json, sys, time
import httpx

c = httpx.Client(timeout=30, headers={"User-Agent": "papertrader-research/1.0"})
DAY = 86_400_000
NOW = int(time.time() * 1000)


def get(url, **params):
    for a in range(5):
        try:
            r = c.get(url, params=params)
            if r.status_code == 429:
                time.sleep(3 + a * 3); continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print("retry", url, e, flush=True); time.sleep(2 + a * 2)
    return None


def save(name, header, rows):
    with gzip.open(name, "wt", newline="") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(rows)
    print(name, len(rows), flush=True)


# Fear & Greed
d = get("https://api.alternative.me/fng/", limit=0, format="json")
if d:
    save("fear_greed.csv.gz", ["timestamp", "value"], sorted((int(x["timestamp"]) * 1000, int(x["value"])) for x in d["data"]))

# Deribit funding (hourly), chunks of 30 days
for inst in ("BTC-PERPETUAL", "ETH-PERPETUAL"):
    rows, t = [], 1_546_300_800_000
    while t < NOW:
        e = min(t + 30 * DAY, NOW)
        j = get("https://www.deribit.com/api/v2/public/get_funding_rate_history", instrument_name=inst, start_timestamp=t, end_timestamp=e)
        if j and "result" in j:
            rows += [(x["timestamp"], x.get("interest_8h"), x.get("interest_1h")) for x in j["result"]]
        t = e
        time.sleep(0.15)
    save(f"funding_deribit_{inst.split('-')[0]}.csv.gz", ["timestamp", "interest_8h", "interest_1h"], sorted(set(rows)))

# Deribit DVOL (daily)
for cur in ("BTC", "ETH"):
    rows, t = [], 1_609_459_200_000
    while t < NOW:
        e = min(t + 900 * DAY, NOW)
        j = get("https://www.deribit.com/api/v2/public/get_volatility_index_data", currency=cur, start_timestamp=t, end_timestamp=e, resolution="1D")
        if j and "result" in j:
            rows += [tuple(x) for x in j["result"]["data"]]
        t = e
    save(f"dvol_{cur}.csv.gz", ["timestamp", "open", "high", "low", "close"], sorted(set(rows)))

# Hyperliquid funding (hourly, 500 per call)
for coin in ("SOL", "BTC", "ETH"):
    rows, t = [], 1_672_531_200_000
    while t < NOW:
        try:
            r = c.post("https://api.hyperliquid.xyz/info", json={"type": "fundingHistory", "coin": coin, "startTime": t})
            r.raise_for_status(); j = r.json()
        except Exception as e:
            print("hyperliquid", coin, e); break
        if not j:
            break
        rows += [(x["time"], x["fundingRate"]) for x in j]
        nt = j[-1]["time"] + 1
        if nt <= t or len(j) < 2:
            break
        t = nt
        time.sleep(0.2)
    save(f"funding_hl_{coin}.csv.gz", ["timestamp", "funding_1h"], sorted(set(rows)))
