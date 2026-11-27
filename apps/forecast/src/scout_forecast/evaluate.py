"""Cross-validate the physical energy model. Standard library only.

Blocked, purged k-fold split: contiguous time blocks, with training windows
that overlap a test window removed. Every parameter is refitted on each fold's
training data.

    scout-evaluate --csv telemetry.csv
    scout-evaluate --csv small.csv --folds 3 \
        --lookback-h 6 --horizon-h 3

Reports MAE in battery_pct points at several lead times for:
  physical/actual  - recorded load used as the operating plan (what the
                     low-power mode would know, since it sets the plan)
  physical/persist - blind: hold the mean load of the last 6 h
  persistence      - battery_pct stays where it is
  seasonal_24h     - repeat the change seen 24 h earlier
Each method also gets a signed bias (forecast - actual, all lead times):
positive means the forecast is too optimistic.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import energy as E
from .env import csv_arg

LEADS_H = (1, 3, 6, 12, 24)


def blocked_folds(origins, k, lookback, horizon):
    lo, hi = origins[0], origins[-1] + 1
    edges = [lo + (hi - lo) * f // k for f in range(k + 1)]
    for f in range(k):
        test = [o for o in origins if edges[f] <= o < edges[f + 1]]
        if not test:
            continue
        t_lo, t_hi = test[0], test[-1]
        train = [o for o in origins if no_overlap(o, t_lo, t_hi, lookback, horizon)]
        yield f, train, test, (t_lo - lookback, t_hi + horizon)


def no_overlap(o, t_lo, t_hi, lookback, horizon):
    return o + horizon <= t_lo - lookback or o - lookback >= t_hi + horizon


def predictions(g, p, origins, horizon, sinel):
    """Per-method list of (origin, [predicted change per step])."""
    soc = g["pct"]
    period = 24 * E.STEPS_PER_H
    out = {
        "physical_actual": [],
        "physical_persist": [],
        "persistence": [],
        "seasonal_24h": [],
    }
    for i in origins:
        base = soc[i - 1]
        for plan in ("actual", "persist"):
            sim = E.simulate(g, p, i, horizon, plan=plan, sinel=sinel)
            out[f"physical_{plan}"].append([v - base for v in sim["pct"]])
        out["persistence"].append([0.0] * horizon)
        lo = i - 1 - period
        past = None
        if lo >= 0:
            seg = soc[lo : lo + 1 + horizon]
            if len(seg) == horizon + 1 and None not in seg:
                past = [v - seg[0] for v in seg[1:]]
        out["seasonal_24h"].append(past)
    return out


def mae(g, origins, preds, horizon):
    rep = {}
    errs_all, signed, by_step = [], [], [[] for _ in range(horizon)]
    for i, pr in zip(origins, preds):
        if pr is None:
            continue
        base = g["pct"][i - 1]
        for k in range(horizon):
            d = pr[k] - (g["pct"][i + k] - base)
            errs_all.append(abs(d))
            signed.append(d)
            by_step[k].append(abs(d))
    rep["all"] = _mean(errs_all)
    rep["bias"] = _mean(signed)
    for h in LEADS_H:
        s = h * E.STEPS_PER_H
        if s <= horizon:
            rep[f"{h}h"] = _mean(by_step[s - 1])
    return rep


def _mean(v):
    return sum(v) / len(v) if v else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    csv_arg(ap)
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument(
        "--lookback-h",
        type=float,
        default=24,
        help="history required before each window; only used to select windows",
    )
    ap.add_argument("--horizon-h", type=float, default=12)
    ap.add_argument(
        "--stride",
        type=int,
        default=4,
        help="subsample training origins when fitting capacity/sag",
    )
    ap.add_argument(
        "--out", default="runs/physical", help="relative to the current directory"
    )
    a = ap.parse_args(argv)

    L = int(a.lookback_h * E.STEPS_PER_H)
    H = int(a.horizon_h * E.STEPS_PER_H)
    g = E.build_grid(a.csv)
    origins = E.usable_origins(g, L, H)
    if not origins:
        raise SystemExit("no complete windows: shorten --lookback-h/--horizon-h")
    _log(
        f"grid: {len(g['times'])} steps | windows: {len(origins)} "
        f"(lookback {L}, horizon {H})"
    )

    folds, methods = [], None
    for f, train, test, span in blocked_folds(origins, a.folds, L, H):
        if len(train) < 8:
            _log(f"fold {f}: skipped, {len(train)} training windows")
            continue
        p = E.fit(g, train, H, exclude=[span], stride=a.stride)
        sinel = E.sin_elevation_series(g, p.solar_offset_h)
        preds = predictions(g, p, test, H, sinel)
        methods = list(preds)
        r = {
            "fold": f,
            "test_span": f"{g['times'][test[0]]:%m-%d %H:%M}.."
            f"{g['times'][test[-1]]:%m-%d %H:%M}",
            "n_train": len(train),
            "n_test": len(test),
            "params": {k: v for k, v in p.to_dict().items() if k != "load"},
        }
        for m in methods:
            r[m] = mae(g, test, preds[m], H)
        folds.append(r)
        _log(
            f"fold {f} [{r['test_span']}] cap={p.capacity_wh:.0f}Wh "
            f"sag={p.sag_pct_per_a:.1f} solar_k={p.solar_k:.0f} "
            f"off={p.solar_offset_h:+.2f}h cloud={p.cloud_default:.2f} | "
            + " ".join(f"{m}={_f(r[m]['all'])}" for m in methods)
        )
    if not folds:
        raise SystemExit("no fold had enough training data")

    summary = {m: _avg([r[m] for r in folds]) for m in methods}
    _log("mean over folds (MAE, battery_pct points):")
    for m, v in summary.items():
        _log(f"  {m:17s} " + "  ".join(f"{h}={_f(x)}" for h, x in v.items()))

    # Final parameters on all data, for the forecast CLI and the service.
    final = E.fit(g, origins, H, stride=a.stride)
    run = Path(a.out) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run.mkdir(parents=True, exist_ok=True)
    (run / "params.json").write_text(json.dumps(final.to_dict(), indent=2))
    (run / "results.json").write_text(
        json.dumps(
            {
                "config": vars(a),
                "n_windows": len(origins),
                "folds": folds,
                "summary": summary,
            },
            indent=2,
        )
    )
    _log(
        "all-data fit: "
        + json.dumps({k: _round(v) for k, v in final.to_dict().items()})
    )
    _log(f"wrote {run}")
    return 0


def _avg(reports):
    return {
        k: _mean([r[k] for r in reports if r.get(k) is not None]) for k in reports[0]
    }


def _round(v):
    if isinstance(v, float):
        return round(v, 2)
    if isinstance(v, dict):
        return {k: _round(x) for k, x in v.items()}
    return v


def _f(v):
    return "n/a" if v is None else f"{v:.2f}"


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
