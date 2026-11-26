"""Forecast Scout's battery from any start time in the data.

    forecast --csv telemetry.csv --from 2025-07-23T00:00Z --hours 24
    export SCOUT_TELEMETRY_CSV=telemetry.csv; forecast --from ... --hours 24
    forecast --from 2025-07-23T00:00Z --hours 24 --plan persist --json out.json

By default the model is fitted with the forecast window (plus the 24 h before
it) held out, so the forecast never sees the period it is scored on. Pass
--params to use a saved fit (from evaluate.py) instead.

--plan actual   use the recorded load as the operating plan (the low-power
                mode knows its own plan). Default.
--plan persist  blind: hold the mean load of the last 6 h.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .data import csv_arg

from . import energy as E


def parse_when(s: str) -> datetime:
    s = s.strip().replace("Z", "+00:00")
    t = datetime.fromisoformat(s)
    return (
        t.replace(tzinfo=timezone.utc)
        if t.tzinfo is None
        else t.astimezone(timezone.utc)
    )


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--from", dest="start", required=True, help="e.g. 2025-07-23T00:00Z"
    )
    ap.add_argument("--hours", type=float, default=24)
    csv_arg(ap)
    ap.add_argument("--plan", choices=["actual", "persist"], default="actual")
    ap.add_argument("--params", help="params.json from evaluate.py (skip fitting)")
    ap.add_argument("--every", type=float, default=1.0, help="print interval, hours")
    ap.add_argument("--json", help="also write the full forecast here")
    a = ap.parse_args(argv)

    g = E.build_grid(a.csv)
    t0 = parse_when(a.start)
    first, last = g["times"][0], g["times"][-1]
    if not first < t0 <= last:
        raise SystemExit(
            f"--from must be within {first:%Y-%m-%dT%H:%MZ}..{last:%Y-%m-%dT%H:%MZ}"
        )
    i = -(-int((t0 - first).total_seconds()) // E.STEP_S)  # first step at/after t0
    # Start from the latest reading at or before t0 (up to 1 h back).
    while (
        i > 0
        and None in (g["pct"][i - 1], g["volt"][i - 1])
        and (t0 - g["times"][i - 1]).total_seconds() < 3600
    ):
        i -= 1
    if i < 1 or g["pct"][i - 1] is None:
        raise SystemExit(f"no battery reading in the hour before {t0:%Y-%m-%dT%H:%MZ}")
    H = int(round(a.hours * E.STEPS_PER_H))
    L = 24 * E.STEPS_PER_H

    if a.params:
        p = E.Params.from_dict(json.loads(Path(a.params).read_text()))
        fitted = f"params from {a.params}"
    else:
        span = (i - L, i + H)
        origins = [
            o
            for o in E.usable_origins(g, L, 12 * E.STEPS_PER_H)
            if o + 12 * E.STEPS_PER_H <= span[0] or o - L >= span[1]
        ]
        p = E.fit(g, origins, 12 * E.STEPS_PER_H, exclude=[span])
        held = g["times"][max(span[0], 0)]
        fitted = f"fitted on data outside {held:%m-%d %H:%M}..+{(H + L) / 4:.0f} h"

    sim = E.simulate(g, p, i, H, plan=a.plan)
    n = len(g["times"])
    actual = [g["pct"][i + k] if i + k < n else None for k in range(H)]
    times = [g["times"][i - 1]]
    for k in range(H):
        times.append(times[0] + (k + 1) * (g["times"][1] - g["times"][0]))

    start_pct = g["pct"][i - 1]
    print(
        f"Forecast from {times[0]:%Y-%m-%d %H:%M}Z for {a.hours:g} h | "
        f"plan={a.plan} | {fitted}"
    )
    print(
        f"model: capacity {p.capacity_wh:.0f} Wh/100%, sag {p.sag_pct_per_a:.1f} %/A, "
        f"clear-sky solar {p.solar_k:.0f} W, cloud factor default {p.cloud_default:.2f}"
    )
    print(f"start: battery_pct {start_pct:.1f} ({g['volt'][i - 1]:.2f} V)\n")
    print(
        f"{'time (UTC)':16} {'forecast %':>10} {'actual %':>9} {'error':>6} "
        f"{'solar W':>8} {'load W':>7} {'net W':>6}"
    )
    step = max(1, int(round(a.every * E.STEPS_PER_H)))
    for k in range(step - 1, H, step):
        act = actual[k]
        err = f"{sim['pct'][k] - act:+6.1f}" if act is not None else f"{'-':>6}"
        act_s = f"{act:9.1f}" if act is not None else f"{'-':>9}"
        print(
            f"{times[k + 1]:%m-%d %H:%M}      {sim['pct'][k]:10.1f} {act_s} {err} "
            f"{sim['solar_w'][k]:8.0f} {sim['load_w'][k]:7.0f} {sim['net_w'][k]:6.0f}"
        )

    pairs = [(f, x) for f, x in zip(sim["pct"], actual) if x is not None]
    summary = {}
    if pairs:
        summary = {
            "mae": sum(abs(f - x) for f, x in pairs) / len(pairs),
            "mae_persistence": sum(abs(start_pct - x) for _, x in pairs) / len(pairs),
            "n": len(pairs),
        }
    e_in = sum(sim["solar_w"]) * E.STEP_H
    e_out = sum(sim["load_w"]) * E.STEP_H
    print(
        f"\nenergy over window: solar {e_in:.0f} Wh, load {e_out:.0f} Wh, "
        f"losses {p.loss_w * H * E.STEP_H:.0f} Wh"
    )
    if summary:
        print(
            f"MAE vs actual: {summary['mae']:.2f} points "
            f"(battery stays the same: {summary['mae_persistence']:.2f}) "
            f"over {summary['n']} steps"
        )
    if i + H > n:
        print(
            f"note: data ends at {last:%m-%d %H:%M}; later steps use the persisted load"
        )

    if a.json:
        Path(a.json).write_text(
            json.dumps(
                {
                    "start": times[0].isoformat(),
                    "hours": a.hours,
                    "plan": a.plan,
                    "params": p.to_dict(),
                    "summary": summary,
                    "steps": [
                        {
                            "time": times[k + 1].isoformat(),
                            "forecast_pct": sim["pct"][k],
                            "actual_pct": actual[k],
                            "solar_w": sim["solar_w"][k],
                            "load_w": sim["load_w"][k],
                            "net_w": sim["net_w"][k],
                        }
                        for k in range(H)
                    ],
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
