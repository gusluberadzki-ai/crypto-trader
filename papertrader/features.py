"""Technical indicators and ML features. Every value at bar t uses only data up to and including bar t."""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_COLS = [
    "rsi", "macd_hist", "bb_pctb", "bb_width", "atr_pct", "adx", "di_spread",
    "ret_1", "ret_3", "ret_6", "ret_12", "ret_24", "vol_24", "vol_ratio", "volume_z",
    "ema20_dist", "ema_20_50", "ema_50_200", "hour_sin", "hour_cos", "btc_ret_6", "btc_ret_24",
]

FEATURE_LABELS = {
    "rsi": "RSI(14)", "macd_hist": "MACD histogram", "bb_pctb": "position inside Bollinger Bands",
    "bb_width": "Bollinger Band width", "atr_pct": "volatility (ATR%)", "adx": "trend strength (ADX)",
    "di_spread": "directional pressure (+DI vs -DI)", "ret_1": "last-bar return", "ret_3": "3-bar return",
    "ret_6": "6-bar return", "ret_12": "12-bar return", "ret_24": "24-bar return",
    "vol_24": "realised volatility", "vol_ratio": "short vs long volatility", "volume_z": "volume vs normal",
    "ema20_dist": "distance from EMA20", "ema_20_50": "EMA20 vs EMA50", "ema_50_200": "EMA50 vs EMA200",
    "hour_sin": "time of day", "hour_cos": "time of day", "btc_ret_6": "BTC 6-bar move",
    "btc_ret_24": "BTC 24-bar move",
}

WARMUP_BARS = 210


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    au = wilder(d.clip(lower=0), n)
    ad = wilder(-d.clip(upper=0), n)
    out = 100 - 100 / (1 + au / ad.replace(0, np.nan))
    return out.fillna(100).where(ad.notna())


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def adx(df: pd.DataFrame, n: int = 14):
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = wilder(true_range(df), n)
    pdi = 100 * wilder(plus_dm, n) / tr
    mdi = 100 * wilder(minus_dm, n) / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return wilder(dx, n), pdi, mdi


def compute_features(df: pd.DataFrame, btc_close: pd.Series | None = None) -> pd.DataFrame:
    """df: open/high/low/close/volume[/quote_volume] indexed by open_time (ms), ascending."""
    c = df["close"].astype(float)
    f = pd.DataFrame(index=df.index)
    for col in ("open", "high", "low", "close"):
        f[col] = df[col].astype(float)
    f["ema20"], f["ema50"], f["ema200"] = ema(c, 20), ema(c, 50), ema(c, 200)
    f["rsi"] = rsi(c)
    macd = ema(c, 12) - ema(c, 26)
    f["macd_hist"] = (macd - macd.ewm(span=9, adjust=False).mean()) / c
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std()
    f["bb_mid"], f["bb_up"], f["bb_lo"] = mid, mid + 2 * sd, mid - 2 * sd
    f["bb_pctb"] = (c - f["bb_lo"]) / (4 * sd).replace(0, np.nan)
    f["bb_width"] = 4 * sd / mid
    f["atr"] = wilder(true_range(df), 14)
    f["atr_pct"] = f["atr"] / c
    f["adx"], pdi, mdi = adx(df)
    f["di_spread"] = (pdi - mdi) / 100
    for n in (1, 3, 6, 12, 24):
        f[f"ret_{n}"] = c.pct_change(n)
    lr = np.log(c).diff()
    f["vol_24"] = lr.rolling(24).std()
    f["vol_ratio"] = f["vol_24"] / lr.rolling(168, min_periods=48).std()
    vol = df["quote_volume"] if "quote_volume" in df and df["quote_volume"].sum() > 0 else df["volume"]
    lv = np.log1p(vol.astype(float))
    f["volume_z"] = (lv - lv.rolling(48).mean()) / lv.rolling(48).std().replace(0, np.nan)
    f["ema20_dist"] = c / f["ema20"] - 1
    f["ema_20_50"] = f["ema20"] / f["ema50"] - 1
    f["ema_50_200"] = f["ema50"] / f["ema200"] - 1
    f["vol_pctile"] = f["vol_24"].rolling(720, min_periods=100).rank(pct=True)
    hours = pd.to_datetime(df.index, unit="ms", utc=True).hour.to_numpy()
    f["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    f["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    b = c if btc_close is None else btc_close.reindex(df.index).ffill()
    f["btc_ret_6"] = b.pct_change(6, fill_method=None)
    f["btc_ret_24"] = b.pct_change(24, fill_method=None)
    return f.replace([np.inf, -np.inf], np.nan)


DAY_MS = 86_400_000


def daily_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Daily (UTC) candles built from hourly ones, with daily indicators."""
    idx = pd.to_datetime(df.index, unit="ms", utc=True)
    h = df.set_axis(idx)
    d = h.resample("1D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    out = pd.DataFrame(index=d.index)
    out["d_close"] = d["close"]
    out["d_sma100"] = d["close"].rolling(100).mean()
    out["d_sma200"] = d["close"].rolling(200).mean()
    out["d_mom30"] = d["close"].pct_change(30)
    out["d_high30"] = d["high"].rolling(30).max().shift(1)   # highest high of the 30 days BEFORE this one
    out["d_low20"] = d["low"].rolling(20).min().shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["close"].shift()).abs(), (d["low"] - d["close"].shift()).abs()], axis=1).max(axis=1)
    out["d_atr"] = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    out["day_ms"] = ((d.index - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(milliseconds=1)).astype("int64")
    return out.set_index("day_ms")


def attach_daily(f: pd.DataFrame, daily: pd.DataFrame, prefix: str = "") -> None:
    """For each hourly row, attach the latest COMPLETED day's values (a day completes when its 23:00 bar closes)."""
    key = ((f.index.to_numpy() + 3_600_000) // DAY_MS) * DAY_MS - DAY_MS
    vals = daily.reindex(key)
    for c in daily.columns:
        f[prefix + c] = vals[c].to_numpy()
    f[prefix + "d_day"] = key


def features_for_all(frames: dict[str, pd.DataFrame], btc_symbol: str = "BTCUSDT") -> dict[str, pd.DataFrame]:
    btc = frames[btc_symbol]["close"] if btc_symbol in frames else None
    out = {s: compute_features(df, None if s == btc_symbol else btc) for s, df in frames.items()}
    dailies = {s: daily_frame(df) for s, df in frames.items()}
    for s, f in out.items():
        attach_daily(f, dailies[s])
        if btc_symbol in dailies:
            b = dailies[btc_symbol]
            ok = (b["d_close"] > b["d_sma200"]).where(b["d_sma200"].notna())
            key = ((f.index.to_numpy() + 3_600_000) // DAY_MS) * DAY_MS - DAY_MS
            f["btc_trend_ok"] = ok.reindex(key).to_numpy()
    return out
