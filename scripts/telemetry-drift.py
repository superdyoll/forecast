"""What the telemetry alone says about drift (current + wind leeway), for
REPORT.md. Needs the private CSV (--csv, SCOUT_TELEMETRY_CSV or .env).

    .venv/bin/python scripts/telemetry-drift.py

1. Drift with no propulsion: Starlink rows in MANUAL with the load at most
   36 W (hotel + Starlink, no thrust), as 15-minute means of the velocity
   from speed and course over ground; and a fit of the 12.42 h tide to them.
2. Crab angle under way: course over ground against heading in AUTO.
3. Speed over ground at the same load in AUTO: the spread is the current.
"""

import argparse
import math
import statistics as st

from scout_forecast.data import load
from scout_forecast.env import csv_arg

NO_THRUST_W = 36.0  # hotel ~10 W + Starlink ~22 W + margin
BUCKET_S = 900
M2_S = 12.42 * 3600


def q(v, p):
    v = sorted(v)
    return v[int(p * (len(v) - 1))]


def tidal_r2(samples):
    """R² of value = c + a cos(wt) + b sin(wt), least squares."""
    w = 2 * math.pi / M2_S
    X = [(1.0, math.cos(w * t), math.sin(w * t)) for t, _ in samples]
    y = [v for _, v in samples]
    A = [
        [sum(x[i] * x[j] for x in X) for j in range(3)]
        + [sum(x[i] * v for x, v in zip(X, y))]
        for i in range(3)
    ]
    for i in range(3):  # Gauss-Jordan
        p = max(range(i, 3), key=lambda r: abs(A[r][i]))
        A[i], A[p] = A[p], A[i]
        for r in range(3):
            if r != i:
                f = A[r][i] / A[i][i]
                A[r] = [a - f * c for a, c in zip(A[r], A[i])]
    c = [A[i][3] / A[i][i] for i in range(3)]
    mean = st.mean(y)
    res = sum((v - (c[0] + c[1] * x[1] + c[2] * x[2])) ** 2 for x, v in zip(X, y))
    return 1 - res / sum((v - mean) ** 2 for v in y)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    csv_arg(ap)
    rows = [r for r in load(ap.parse_args().csv) if not r["flags"]]
    direct = [r for r in rows if r["link"] == "direct"]

    drifting = [
        r
        for r in direct
        if r["mode"] == 0
        and r["thruster_w"] is not None
        and r["thruster_w"] <= NO_THRUST_W
        and r["speed_ms"] is not None
        and r["course_deg"] is not None
    ]
    buckets = {}
    for r in drifting:
        c = math.radians(r["course_deg"])
        k = int(r["time"].timestamp() // BUCKET_S)
        buckets.setdefault(k, []).append(
            (r["speed_ms"] * math.cos(c), r["speed_ms"] * math.sin(c))
        )
    v = [
        (k * BUCKET_S, st.mean(n for n, _ in b), st.mean(e for _, e in b))
        for k, b in sorted(buckets.items())
        if len(b) >= 20
    ]
    speeds = [math.hypot(n, e) for _, n, e in v]
    mn, me = st.mean(n for _, n, _ in v), st.mean(e for _, _, e in v)
    days = len({t // 86400 for t, _, _ in v})
    print(
        f"no propulsion: {len(v)} x 15 min ({len(v) / 4:.0f} h) over {days} days; "
        f"drift median {q(speeds, 0.5):.2f} m/s, p10 {q(speeds, 0.1):.2f}, "
        f"p90 {q(speeds, 0.9):.2f}; mean {math.hypot(mn, me):.2f} m/s toward "
        f"{math.degrees(math.atan2(me, mn)) % 360:.0f} deg"
    )
    print(
        f"12.42 h tide explains R2 {tidal_r2([(t, n) for t, n, _ in v]):.2f} (north), "
        f"{tidal_r2([(t, e) for t, _, e in v]):.2f} (east)"
    )

    auto = [
        r
        for r in direct
        if r["mode"] == 10
        and r["armed"] == 1
        and r["speed_ms"] is not None
        and r["speed_ms"] > 0.3
        and r["heading_deg"] is not None
        and r["course_deg"] is not None
    ]
    crab = [abs((r["course_deg"] - r["heading_deg"] + 180) % 360 - 180) for r in auto]
    cross = [r["speed_ms"] * math.sin(math.radians(c)) for r, c in zip(auto, crab)]
    print(
        f"AUTO, {len(auto)} rows: |course - heading| median {q(crab, 0.5):.0f} deg, "
        f"p90 {q(crab, 0.9):.0f}; cross-track drift median {q(cross, 0.5):.2f} m/s, "
        f"p90 {q(cross, 0.9):.2f}"
    )
    same = [
        r["speed_ms"] for r in auto if r["thruster_w"] and 55 <= r["thruster_w"] <= 75
    ]
    print(
        f"AUTO at 55-75 W load, {len(same)} rows: speed over ground p10 "
        f"{q(same, 0.1):.2f}, median {q(same, 0.5):.2f}, p90 {q(same, 0.9):.2f} m/s"
    )


if __name__ == "__main__":
    main()
