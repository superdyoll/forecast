#!/usr/bin/env python3
"""Train an LSTM to forecast Scout's battery state of charge (SoC).

Given the last `--lookback-h` hours of telemetry, the model predicts the change
in battery_pct at every step of the next `--horizon-h` hours. It is evaluated
with blocked, purged k-fold cross-validation and compared against two
baselines. A model that can't beat the baselines isn't worth using.

    python3 model/train_lstm.py scout_telemetry_2025-07.csv
    python3 model/train_lstm.py small.csv --lookback-h 6 --horizon-h 3 \
        --folds 3 --epochs 3                                  # quick smoke test

Data-driven choices (see AGENTS.md "Data facts"):

* 15-minute grid. Iridium arrives every ~5 min and is the only link after
  07-27, so 15 min always has data; Starlink's ~1 Hz bursts average out.
  The 17-day file gives ~1,630 steps, nearly all gaps <= 1 h.
* Backlog rows are dropped by scout_data.resample (receipt time != sample
  time). Gaps <= --max-gap-steps are interpolated; windows touching a longer
  gap are skipped, never zero-filled.
* Solar power is missing whenever only Iridium is up (the last ~3.5 days).
  It is fed as 0 plus a `solar_seen` mask so the network can tell "no
  reading" from "night". Time of day (sin/cos) carries the diurnal cycle at
  60-62 N in July (short nights, long days) when solar isn't reported.
* Target is the SoC *change* relative to the last observed value, not
  absolute SoC: it removes the level, which drifts across the deployment.
  SoC is quantised to 0.2 % with occasional jumps of 5-7 % (e.g. around
  autopilot restarts), so the loss is Huber, not MSE.
* ~17 days is very little data and windows overlap heavily, so the
  effective sample size is roughly one per day. The network is kept small
  (one LSTM layer, 32 units), with dropout, L2 and early stopping.
* Random k-fold would leak: neighbouring windows share most of their
  inputs. Folds are contiguous time blocks, and training windows that
  overlap a test block's inputs or targets are purged. Early stopping uses
  the last contiguous slice of the training data, purged the same way.
* Scalers are fitted on each fold's training windows only.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from scout_data import load, resample  # noqa: E402

FEATURES = [
    "battery_pct",
    "battery_v",
    "thruster_w",
    "speed_ms",
    "solar_w",
    "solar_seen",
    "armed",
    "under_way",  # AUTO or MODE_17: the boat is following a mission
    "hour_sin",
    "hour_cos",
]
TARGET = "battery_pct"


# --------------------------------------------------------------------------- data
def build_grid(csv_path: str, step_s: int, max_gap_steps: int) -> dict:
    """Resample telemetry onto a regular grid. Missing steps are NaN."""
    buckets = resample(load(csv_path, include_globalstar=False), step_s)
    if not buckets:
        raise SystemExit(f"no usable rows in {csv_path}")
    t0 = int(buckets[0]["time"].timestamp())
    n = (int(buckets[-1]["time"].timestamp()) - t0) // step_s + 1
    times = t0 + step_s * np.arange(n)
    cols = {k: np.full(n, np.nan) for k in FEATURES}

    for b in buckets:
        i = (int(b["time"].timestamp()) - t0) // step_s
        for k in ("battery_pct", "battery_v", "thruster_w", "speed_ms", "armed"):
            if b[k] is not None:
                cols[k][i] = b[k]
        cols["solar_seen"][i] = 1.0 if b["solar_w"] is not None else 0.0
        cols["solar_w"][i] = b["solar_w"] if b["solar_w"] is not None else 0.0
        if b["mode"] is not None:
            cols["under_way"][i] = 1.0 if b["mode"] in (10, 17) else 0.0

    for k in FEATURES:
        if k in ("hour_sin", "hour_cos"):
            continue
        binary = k in ("armed", "under_way", "solar_seen")
        cols[k] = _fill_short_gaps(cols[k], max_gap_steps, hold=binary)

    hours = (times % 86400) / 3600.0
    cols["hour_sin"] = np.sin(2 * math.pi * hours / 24)
    cols["hour_cos"] = np.cos(2 * math.pi * hours / 24)
    return {"times": times, "X": np.stack([cols[k] for k in FEATURES], axis=1)}


def _fill_short_gaps(x: np.ndarray, max_gap: int, hold: bool) -> np.ndarray:
    """Interpolate (or hold the last value) across NaN runs <= max_gap."""
    x = x.copy()
    isnan = np.isnan(x)
    i, n = 0, len(x)
    while i < n:
        if not isnan[i]:
            i += 1
            continue
        j = i
        while j < n and isnan[j]:
            j += 1
        if 0 < i and j < n and (j - i) <= max_gap:
            if hold:
                x[i:j] = x[i - 1]
            else:
                x[i:j] = np.linspace(x[i - 1], x[j], j - i + 2)[1:-1]
        i = j
    return x


def make_windows(grid: dict, lookback: int, horizon: int):
    """Return X (N, lookback, F), y (N, horizon), origin indices (N,).

    Origin i means: inputs are steps [i-lookback, i), targets are SoC at
    steps [i, i+horizon) minus SoC at step i-1.
    """
    X, ti = grid["X"], FEATURES.index(TARGET)
    ok = np.isfinite(X).all(axis=1)
    # Count of finite rows up to each index, so a range check is O(1).
    csum = np.concatenate([[0], np.cumsum(ok)])
    xs, ys, origins = [], [], []
    for i in range(lookback, len(X) - horizon + 1):
        if csum[i + horizon] - csum[i - lookback] != lookback + horizon:
            continue
        xs.append(X[i - lookback : i])
        ys.append(X[i : i + horizon, ti] - X[i - 1, ti])
        origins.append(i)
    if not xs:
        raise SystemExit("no complete windows: shorten --lookback-h/--horizon-h")
    return np.array(xs, "float32"), np.array(ys, "float32"), np.array(origins)


def baselines(grid: dict, origins: np.ndarray, horizon: int, period: int):
    """Persistence (no change) and seasonal-naive (repeat the change seen
    `period` steps earlier). Seasonal is NaN where history is missing."""
    soc = grid["X"][:, FEATURES.index(TARGET)]
    persist = np.zeros((len(origins), horizon), "float32")
    seasonal = np.full((len(origins), horizon), np.nan, "float32")
    for n, i in enumerate(origins):
        lo = i - 1 - period
        if lo >= 0 and i - 1 - period + horizon < len(soc):
            past = soc[lo + 1 : lo + 1 + horizon] - soc[lo]
            if np.isfinite(past).all():
                seasonal[n] = past
    return persist, seasonal


# ------------------------------------------------------------------- validation
def blocked_folds(origins: np.ndarray, k: int, lookback: int, horizon: int):
    """Yield (train_idx, test_idx) over contiguous time blocks with purging.

    A training window [o-lookback, o+horizon) is dropped if it overlaps any
    test window's span, so no target value is seen in training inputs.
    """
    lo, hi = origins.min(), origins.max() + 1
    edges = np.linspace(lo, hi, k + 1).astype(int)
    for f in range(k):
        test = (origins >= edges[f]) & (origins < edges[f + 1])
        if not test.any():
            continue
        t_lo, t_hi = origins[test].min(), origins[test].max()
        train = ~test & _no_overlap(origins, t_lo, t_hi, lookback, horizon)
        yield np.where(train)[0], np.where(test)[0]


def _no_overlap(origins, t_lo, t_hi, lookback, horizon):
    # Window spans: [o-lookback, o+horizon). Test spans: [t_lo-lookback, t_hi+horizon).
    return (origins + horizon <= t_lo - lookback) | (
        origins - lookback >= t_hi + horizon
    )


def split_validation(train_idx, origins, frac, lookback, horizon):
    """Hold out the latest `frac` of a fold's training windows for early
    stopping, purging the overlap. Falls back to no validation if too small."""
    o = origins[train_idx]
    cut = np.quantile(o, 1 - frac)
    val = train_idx[o >= cut]
    if len(val) < 8:
        return train_idx, None
    keep = _no_overlap(o, origins[val].min(), origins[val].max(), lookback, horizon)
    fit = train_idx[keep & (o < cut)]
    return (fit, val) if len(fit) >= 16 else (train_idx, None)


# ------------------------------------------------------------------------ model
class Scaler:
    def fit(self, X, y):
        flat = X.reshape(-1, X.shape[-1])
        self.x_mean, self.x_std = flat.mean(0), np.maximum(flat.std(0), 1e-6)
        self.y_std = float(max(y.std(), 1e-6))
        return self

    def x(self, X):
        return ((X - self.x_mean) / self.x_std).astype("float32")

    def to_dict(self):
        return {
            "features": FEATURES,
            "x_mean": self.x_mean.tolist(),
            "x_std": self.x_std.tolist(),
            "y_std": self.y_std,
        }


def build_model(lookback, n_features, horizon, a):
    import keras
    from keras import layers, regularizers

    model = keras.Sequential(
        [
            keras.Input((lookback, n_features)),
            layers.LSTM(
                a.units,
                dropout=a.dropout,
                kernel_regularizer=regularizers.l2(a.l2),
                recurrent_regularizer=regularizers.l2(a.l2),
            ),
            layers.Dropout(a.dropout),
            layers.Dense(horizon),
        ]
    )
    model.compile(
        optimizer=keras.optimizers.Adam(a.lr, clipnorm=1.0),
        loss=keras.losses.Huber(delta=1.0),  # in scaled units of y_std
    )
    return model


def fit_one(X, y, fit_idx, val_idx, a, lookback, horizon):
    import keras

    scaler = Scaler().fit(X[fit_idx], y[fit_idx])
    model = build_model(lookback, X.shape[-1], horizon, a)
    val = None
    if val_idx is not None:
        val = (scaler.x(X[val_idx]), y[val_idx] / scaler.y_std)
    cb = [
        keras.callbacks.EarlyStopping(
            monitor="val_loss" if val else "loss",
            patience=a.patience,
            restore_best_weights=True,
        ),
        keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss" if val else "loss",
            factor=0.5,
            patience=max(a.patience // 3, 1),
        ),
    ]
    hist = model.fit(
        scaler.x(X[fit_idx]),
        y[fit_idx] / scaler.y_std,
        validation_data=val,
        epochs=a.epochs,
        batch_size=a.batch,
        shuffle=True,  # safe: windows are already cut and purged
        callbacks=cb,
        verbose=a.verbose,
    )
    return model, scaler, len(hist.history["loss"])


def predict(model, scaler, X):
    return model.predict(scaler.x(X), verbose=0) * scaler.y_std


def mae_report(y, pred, steps_per_h, horizon):
    """MAE (SoC percentage points), overall and at a few lead times."""
    err = np.abs(pred - y)
    out = {"all": _nanmean(err)}
    for h in (1, 3, 6, 12, 24):
        s = h * steps_per_h
        if s <= horizon:
            out[f"{h}h"] = _nanmean(err[:, s - 1])
    return out


def _nanmean(a):
    a = a[np.isfinite(a)]
    return float(a.mean()) if a.size else None


# -------------------------------------------------------------------------- main
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("csv", help="raw Tether CSV export")
    p.add_argument("--step-min", type=int, default=15)
    p.add_argument("--lookback-h", type=float, default=24)
    p.add_argument("--horizon-h", type=float, default=12)
    p.add_argument("--max-gap-steps", type=int, default=4, help="fill gaps <= this")
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--val-frac", type=float, default=0.15)
    p.add_argument("--units", type=int, default=32)
    p.add_argument("--dropout", type=float, default=0.2)
    p.add_argument("--l2", type=float, default=1e-4)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--epochs", type=int, default=200)
    p.add_argument("--patience", type=int, default=15)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--out", default=str(REPO / "model" / "runs"))
    p.add_argument("--no-final", action="store_true", help="skip the all-data fit")
    p.add_argument("--verbose", type=int, default=0, choices=[0, 1, 2])
    a = p.parse_args(argv)

    import keras

    keras.utils.set_random_seed(a.seed)

    step_s = a.step_min * 60
    steps_per_h = 3600 // step_s
    lookback = int(round(a.lookback_h * steps_per_h))
    horizon = int(round(a.horizon_h * steps_per_h))

    grid = build_grid(a.csv, step_s, a.max_gap_steps)
    X, y, origins = make_windows(grid, lookback, horizon)
    persist, seasonal = baselines(grid, origins, horizon, period=24 * steps_per_h)
    _log(
        f"grid: {len(grid['times'])} steps of {a.step_min} min | "
        f"windows: {len(X)} (lookback {lookback}, horizon {horizon}) | "
        f"features: {len(FEATURES)}"
    )

    folds = []
    for f, (train_idx, test_idx) in enumerate(
        blocked_folds(origins, a.folds, lookback, horizon)
    ):
        span = _span(grid, origins[test_idx], step_s)
        if len(train_idx) < 16:
            _log(f"fold {f}: skipped, only {len(train_idx)} training windows ({span})")
            continue
        fit_idx, val_idx = split_validation(
            train_idx, origins, a.val_frac, lookback, horizon
        )
        model, scaler, epochs = fit_one(X, y, fit_idx, val_idx, a, lookback, horizon)
        yt = y[test_idx]
        r = {
            "fold": f,
            "test_span": span,
            "n_train": int(len(fit_idx)),
            "n_val": 0 if val_idx is None else int(len(val_idx)),
            "n_test": int(len(test_idx)),
            "epochs": epochs,
            "lstm": mae_report(
                yt, predict(model, scaler, X[test_idx]), steps_per_h, horizon
            ),
            "persistence": mae_report(yt, persist[test_idx], steps_per_h, horizon),
            "seasonal_24h": mae_report(yt, seasonal[test_idx], steps_per_h, horizon),
        }
        folds.append(r)
        _log(
            f"fold {f} [{span}] train={r['n_train']} test={r['n_test']} "
            f"epochs={epochs} | MAE %pts lstm={_f(r['lstm']['all'])} "
            f"persist={_f(r['persistence']['all'])} "
            f"seasonal={_f(r['seasonal_24h']['all'])}"
        )

    if not folds:
        raise SystemExit(
            "no fold had enough training data; use fewer folds or a shorter window"
        )

    summary = {
        k: _mean_reports([r[k] for r in folds])
        for k in ("lstm", "persistence", "seasonal_24h")
    }
    _log("mean over folds (MAE, SoC % points):")
    for k, v in summary.items():
        _log(f"  {k:13s} " + "  ".join(f"{h}={_f(m)}" for h, m in v.items()))

    run = Path(a.out) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run.mkdir(parents=True, exist_ok=True)
    result = {
        "config": vars(a) | {"lookback_steps": lookback, "horizon_steps": horizon},
        "features": FEATURES,
        "n_windows": int(len(X)),
        "folds": folds,
        "summary": summary,
    }

    if not a.no_final:
        fit_idx, val_idx = split_validation(
            np.arange(len(X)), origins, a.val_frac, lookback, horizon
        )
        model, scaler, epochs = fit_one(X, y, fit_idx, val_idx, a, lookback, horizon)
        model.save(run / "lstm_soc.keras")
        (run / "scaler.json").write_text(json.dumps(scaler.to_dict(), indent=2))
        result["final_epochs"] = epochs
        _log(f"final model trained on all windows ({epochs} epochs)")

    (run / "results.json").write_text(json.dumps(result, indent=2))
    _log(f"wrote {run}")
    return 0


def _mean_reports(reports):
    keys = reports[0].keys()
    out = {}
    for k in keys:
        vals = [r[k] for r in reports if r.get(k) is not None]
        out[k] = float(np.mean(vals)) if vals else None
    return out


def _span(grid, o, step_s):
    t = grid["times"]
    a = datetime.fromtimestamp(int(t[o.min()]), timezone.utc)
    b = datetime.fromtimestamp(int(t[o.max()]), timezone.utc)
    return f"{a:%m-%d %H:%M}..{b:%m-%d %H:%M}"


def _f(v):
    return "n/a" if v is None else f"{v:.2f}"


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
