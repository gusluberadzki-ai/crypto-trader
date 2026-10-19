"""Machine-learning model: gradient-boosted classifier predicting whether the next `horizon` bars
gain more than `label_threshold` (enough to cover fees + slippage).

Controlled retraining:
  * time-ordered split: the most recent `validation_bars` are held out, with a purge gap of
    `horizon` bars so no training label overlaps the validation window
  * a candidate is promoted only if, on that out-of-sample window, it
      - reaches min AUC,
      - has positive expectancy after costs on its own buy signals,
      - beats unconditional (random-entry) expectancy, and
      - beats the currently active model evaluated on the SAME window
  * every candidate is versioned on disk; the active one can be rolled back
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from .config import INTERVAL_MS
from .features import FEATURE_COLS, FEATURE_LABELS, WARMUP_BARS, features_for_all


@dataclass
class TrainedModel:
    model: HistGradientBoostingClassifier
    meta: dict = field(default_factory=dict)

    @property
    def version(self) -> int:
        return int(self.meta.get("version", 0))

    def proba(self, X: pd.DataFrame | np.ndarray) -> np.ndarray:
        X = X[FEATURE_COLS] if isinstance(X, pd.DataFrame) else X
        return self.model.predict_proba(np.asarray(X, dtype=float))[:, 1]


def round_trip_cost(cfg: dict) -> float:
    c = cfg["costs"]
    return 2 * (c["fee_rate"] + c["slippage_bps"] / 1e4)


def build_dataset(cfg: dict, frames: dict[str, pd.DataFrame], end_time: int | None = None,
                  feats: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
    """Rows of features + forward return + label. Only uses candles with open_time <= end_time."""
    h = cfg["ml"]["horizon"]
    if end_time is not None:
        frames = {s: df[df.index <= end_time] for s, df in frames.items()}
        if feats is not None:
            feats = {s: f[f.index <= end_time] for s, f in feats.items()}
    feats = feats or features_for_all(frames)
    parts = []
    for s, f in feats.items():
        close = f["close"]
        d = f[FEATURE_COLS].copy()
        d["fwd"] = close.shift(-h) / close - 1  # NaN for the last h rows -> dropped (label unknown yet)
        d["symbol"] = s
        d["time"] = f.index
        d = d.iloc[WARMUP_BARS:]
        parts.append(d.dropna(subset=["fwd"]))
    if not parts:
        return pd.DataFrame()
    data = pd.concat(parts, ignore_index=True)
    data["label"] = (data["fwd"] > cfg["ml"]["label_threshold"]).astype(int)
    return data.sort_values(["time", "symbol"]).reset_index(drop=True)


def split(cfg: dict, data: pd.DataFrame):
    times = np.sort(data["time"].unique())
    vb = cfg["ml"]["validation_bars"]
    if len(times) <= vb + 100:
        vb = max(50, len(times) // 5)
    val_start = times[-vb]
    purge = cfg["ml"]["horizon"] * INTERVAL_MS[cfg["interval"]]
    train = data[data["time"] < val_start - purge]
    val = data[data["time"] >= val_start]
    return train, val


def evaluate(cfg: dict, tm: TrainedModel, val: pd.DataFrame) -> dict:
    p = tm.proba(val)
    y = val["label"].to_numpy()
    cost = round_trip_cost(cfg)
    auc = float(roc_auc_score(y, p)) if 0 < y.sum() < len(y) else 0.5
    sel = p >= tm.meta["buy_threshold"]
    net = val["fwd"].to_numpy() - cost
    return {
        "auc": auc,
        "trades": int(sel.sum()),
        "expectancy": float(net[sel].mean()) if sel.any() else -1.0,
        "hit_rate": float((net[sel] > 0).mean()) if sel.any() else 0.0,
        "baseline_expectancy": float(net.mean()),
    }


def train_candidate(cfg: dict, frames: dict[str, pd.DataFrame], end_time: int | None = None,
                    feats: dict[str, pd.DataFrame] | None = None, version: int = 0):
    data = build_dataset(cfg, frames, end_time, feats)
    if data.empty:
        return None, None, {"error": "no data"}
    train, val = split(cfg, data)
    if len(train) < cfg["ml"]["min_train_rows"]:
        return None, val, {"error": f"not enough training rows ({len(train)} < {cfg['ml']['min_train_rows']})"}
    clf = HistGradientBoostingClassifier(
        max_iter=250, learning_rate=0.04, max_leaf_nodes=15, min_samples_leaf=80,
        l2_regularization=1.0, early_stopping=False, random_state=42)
    clf.fit(train[FEATURE_COLS].to_numpy(dtype=float), train["label"].to_numpy())
    tm = TrainedModel(clf, {"version": version})
    p_val = tm.proba(val)
    tm.meta.update({
        "created_at": int(time.time() * 1000),
        "train_end": int(train["time"].max()),
        "data_end": int(data["time"].max()),
        "train_rows": int(len(train)),
        "val_rows": int(len(val)),
        "base_rate": float(train["label"].mean()),
        "buy_threshold": float(np.quantile(p_val, cfg["ml"]["buy_quantile"])),
        "sell_threshold": float(np.quantile(p_val, cfg["ml"]["sell_quantile"])),
        "medians": {c: float(v) for c, v in train[FEATURE_COLS].median().items()},
        "horizon": cfg["ml"]["horizon"],
        "interval": cfg["interval"],
        "importances": {},
    })
    report = evaluate(cfg, tm, val)
    tm.meta["val"] = report
    return tm, val, report


def promotion_decision(cfg: dict, cand: TrainedModel, val: pd.DataFrame, report: dict,
                       active: TrainedModel | None) -> tuple[bool, str, float | None]:
    m = cfg["ml"]
    active_exp = None
    if active is not None:
        active_exp = evaluate(cfg, active, val)["expectancy"]
    if report["auc"] < m["min_auc"]:
        return False, f"AUC {report['auc']:.3f} below {m['min_auc']}", active_exp
    if report["trades"] < m["min_val_trades"]:
        return False, f"only {report['trades']} validation signals", active_exp
    if report["expectancy"] <= 0:
        return False, f"negative expectancy after costs ({report['expectancy']*100:.2f}%/signal)", active_exp
    if report["expectancy"] <= report["baseline_expectancy"]:
        return False, "does not beat random entry on validation window", active_exp
    if active_exp is not None and report["expectancy"] < active_exp + m["promotion_margin"]:
        return False, (f"expectancy {report['expectancy']*100:.2f}% does not beat active model "
                       f"v{active.version} ({active_exp*100:.2f}%)"), active_exp
    return True, (f"promoted: AUC {report['auc']:.3f}, expectancy {report['expectancy']*100:.2f}%/signal "
                  f"on {report['trades']} validation signals"), active_exp


class ModelManager:
    """Holds the active model; optional on-disk versioning."""

    def __init__(self, cfg: dict, model_dir: str | None = None):
        self.cfg = cfg
        self.dir = Path(model_dir) if model_dir else None
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
        self.active: TrainedModel | None = None

    def path_for(self, version: int) -> Path:
        return self.dir / f"model_v{version:04d}.joblib"

    def save(self, tm: TrainedModel) -> str:
        p = self.path_for(tm.version)
        joblib.dump({"model": tm.model, "meta": tm.meta}, p)
        return str(p)

    def load(self, path: str) -> TrainedModel:
        d = joblib.load(path)
        return TrainedModel(d["model"], d["meta"])

    def predict_row(self, row: pd.Series) -> float | None:
        tm = self.active
        if tm is None:
            return None
        x = row.reindex(FEATURE_COLS).to_numpy(dtype=float).reshape(1, -1)
        return float(tm.model.predict_proba(x)[0, 1])

    def explain_row(self, row: pd.Series, top: int = 3) -> list[dict]:
        """Local sensitivity: how much the probability moves if each feature were 'typical'."""
        tm = self.active
        if tm is None:
            return []
        x = row.reindex(FEATURE_COLS).to_numpy(dtype=float)
        base = tm.model.predict_proba(x.reshape(1, -1))[0, 1]
        med = tm.meta["medians"]
        X = np.repeat(x.reshape(1, -1), len(FEATURE_COLS), axis=0)
        for i, c in enumerate(FEATURE_COLS):
            X[i, i] = med.get(c, np.nan)
        p = tm.model.predict_proba(X)[:, 1]
        contrib = base - p  # positive => this feature's current value raises the probability
        merged: dict[str, float] = {}
        for c, v in zip(FEATURE_COLS, contrib):
            label = FEATURE_LABELS.get(c, c)
            merged[label] = merged.get(label, 0.0) + float(v)
        items = sorted(merged.items(), key=lambda kv: -abs(kv[1]))[:top]
        return [{"feature": k, "effect": v} for k, v in items if abs(v) > 1e-4]
