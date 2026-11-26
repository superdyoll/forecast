"""Extract and clean Scout telemetry exported from Tether.

Part of the scout-forecast package. Standard library only, so the same code
can run on the boat's computer.

Usage:
    scout-data --csv telemetry.csv -o data/clean.csv
    scout-data --csv telemetry.csv -o data/5min.csv --resample 300
    scout-data --csv telemetry.csv --summary

As a library:
    from scout_forecast.data import load, resample
    rows = load("telemetry.csv")

Cleaning rules (each from inspecting the raw file; see DATA.md for columns):
  * `null` -> None. Timestamps parsed as UTC (fractional seconds vary in length).
  * temperature_c is reported in centidegrees (e.g. 3120) -> divided by 100.
  * globalstar rows always carry battery_pct = 0.0 -> treated as missing.
  * Autopilot restart records (custom_mode 16 = INITIALISING) and other
    all-zero records carry lat/lon 0,0 and zeroed sensors -> zeros become None,
    row is kept and flagged `reboot` so restarts stay visible.
  * battery_pct == 0 or voltage == 0 are placeholders, not readings -> None.
  * Iridium (imt) normally arrives every ~5 min; rows arriving <2 s after the
    previous imt row are a flushed backlog whose created_at is not the sample
    time -> flagged `backlog`. Their positions/order are not trustworthy.
  * imt groundspeed is quantised to 0.2 m/s; values > 5 m/s (Starlink max is
    3.3 m/s) are artefacts of the backlog -> None.
  * Battery current sign: positive = charging, negative = discharging.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Iterable

# ArduPilot Rover mode numbers seen in this file. 17 is not a stock Rover
# mode; kept as an explicit unknown rather than guessed.
MODE_NAMES = {
    0: "MANUAL",
    4: "HOLD",
    5: "LOITER",
    10: "AUTO",
    11: "RTL",
    15: "GUIDED",
    16: "INITIALISING",
    17: "MODE_17",
}

FLOAT_COLS = [
    "latitude_deg",
    "longitude_deg",
    "heading_deg",
    "course_deg",
    "groundspeed_ms",
    "wp_latitude_deg",
    "wp_longitude_deg",
    "voltage_battery1_v",
    "current_battery1_a",
    "voltage_battery2_v",
    "current_battery2_a",
    "voltage_battery3_v",
    "current_battery3_a",
    "temperature_c",
    "battery_pct",
]
INT_COLS = ["custom_mode", "custom0_arming", "wp_num"]

IMT_BACKLOG_GAP_S = 2.0
MAX_PLAUSIBLE_SPEED_MS = 5.0

# Output column order for the cleaned per-message file.
OUT_COLS = [
    "time",
    "link",
    "lat",
    "lon",
    "heading_deg",
    "course_deg",
    "speed_ms",
    "mode",
    "mode_name",
    "armed",
    "wp_num",
    "wp_lat",
    "wp_lon",
    "battery_v",
    "battery_a",
    "thruster_v",
    "thruster_a",
    "solar_v",
    "solar_a",
    "battery_w",
    "thruster_w",
    "solar_w",
    "other_w",
    "temperature_c",
    "battery_pct",
    "flags",
]

RENAME = {
    "communication_mode": "link",
    "latitude_deg": "lat",
    "longitude_deg": "lon",
    "groundspeed_ms": "speed_ms",
    "custom_mode": "mode",
    "custom0_arming": "armed",
    "wp_latitude_deg": "wp_lat",
    "wp_longitude_deg": "wp_lon",
    "voltage_battery1_v": "battery_v",
    "current_battery1_a": "battery_a",
    "voltage_battery2_v": "thruster_v",
    "current_battery2_a": "thruster_a",
    "voltage_battery3_v": "solar_v",
    "current_battery3_a": "solar_a",
}


def parse_time(s: str) -> datetime:
    """Parse Tether's '2025-07-14 00:20:51.060558+00' (variable-length fraction)."""
    s = s.strip()
    if s.endswith("+00"):
        s = s[:-3]
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in s else "%Y-%m-%d %H:%M:%S"
    return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)


def _num(v: str, cast=float):
    if v is None or v == "" or v == "null":
        return None
    return cast(float(v))


def _clean(raw: dict) -> dict:
    r = {"time": parse_time(raw["created_at"]), "link": raw["communication_mode"]}
    for c in FLOAT_COLS:
        r[RENAME.get(c, c)] = _num(raw.get(c))
    for c in INT_COLS:
        r[RENAME.get(c, c)] = _num(raw.get(c), int)
    flags = []

    # Restart / placeholder records: position 0,0 and zeroed sensors.
    if r["lat"] == 0 and r["lon"] == 0:
        flags.append("reboot" if r["mode"] == 16 else "zero_record")
        for k in (
            "lat",
            "lon",
            "heading_deg",
            "course_deg",
            "speed_ms",
            "wp_lat",
            "wp_lon",
            "temperature_c",
            "battery_a",
            "solar_v",
            "solar_a",
            "thruster_a",
        ):
            r[k] = None
    elif r["mode"] == 16:
        flags.append("reboot")
    if r["wp_lat"] == 0 and r["wp_lon"] == 0:
        r["wp_lat"] = r["wp_lon"] = None

    for k in ("battery_pct", "battery_v", "thruster_v"):
        if r[k] == 0:
            r[k] = None
    if r["temperature_c"] is not None:
        r["temperature_c"] = r["temperature_c"] / 100.0
    if r["speed_ms"] is not None and r["speed_ms"] > MAX_PLAUSIBLE_SPEED_MS:
        r["speed_ms"] = None
        flags.append("bad_speed")

    r["mode_name"] = (
        MODE_NAMES.get(r["mode"], str(r["mode"])) if r["mode"] is not None else None
    )

    # Power (W). Tether labels channel 2 "thruster", but it measures the whole
    # boat's load (it equals battery discharge at night). It shares the battery
    # bus (v1 == v2 in ~93% of Starlink rows); Iridium sends its current but
    # no voltage, so fall back to battery voltage.
    bus_v = r["thruster_v"] or r["battery_v"]
    r["battery_w"] = _mul(r["battery_v"], r["battery_a"])
    r["thruster_w"] = _mul(bus_v, r["thruster_a"])
    r["solar_w"] = _mul(r["solar_v"], r["solar_a"])  # panel side, before MPPT
    # Residual: conversion losses + timing mismatch (~2 W on average).
    if None not in (r["solar_w"], r["thruster_w"], r["battery_w"]):
        r["other_w"] = r["solar_w"] - r["thruster_w"] - r["battery_w"]
    else:
        r["other_w"] = None

    r["flags"] = flags
    return r


def _mul(a, b):
    return None if a is None or b is None else a * b


def load(path: str | Path, include_globalstar: bool = True) -> list[dict]:
    """Return cleaned rows sorted by receipt time."""
    with open(path, newline="") as fh:
        rows = [_clean(raw) for raw in csv.DictReader(fh)]
    rows.sort(key=lambda r: r["time"])

    # Flag imt backlog flushes.
    prev_imt = None
    for r in rows:
        if r["link"] != "imt":
            continue
        if (
            prev_imt is not None
            and (r["time"] - prev_imt).total_seconds() < IMT_BACKLOG_GAP_S
        ):
            r["flags"].append("backlog")
        prev_imt = r["time"]

    if not include_globalstar:
        rows = [r for r in rows if r["link"] != "globalstar"]
    return rows


# Fields averaged when resampling. Angles use a circular mean.
_MEAN_FIELDS = [
    "lat",
    "lon",
    "speed_ms",
    "battery_v",
    "battery_a",
    "thruster_a",
    "solar_v",
    "solar_a",
    "battery_w",
    "thruster_w",
    "solar_w",
    "other_w",
    "temperature_c",
    "battery_pct",
]
_ANGLE_FIELDS = ["heading_deg", "course_deg"]
_LAST_FIELDS = ["mode", "mode_name", "armed", "wp_num", "wp_lat", "wp_lon"]

RESAMPLE_COLS = (
    ["time", "n", "n_direct", "n_imt"]
    + _MEAN_FIELDS
    + _ANGLE_FIELDS
    + _LAST_FIELDS
    + ["flags"]
)


def resample(
    rows: Iterable[dict], seconds: int = 300, skip_flags=("backlog",)
) -> list[dict]:
    """Bucket rows into fixed intervals (start-labelled).

    Rows carrying any flag in `skip_flags` are excluded from the means (their
    timestamps aren't sample times). Buckets with no data are omitted; callers
    should treat missing buckets as gaps, not zeros.
    """
    buckets: dict[int, list[dict]] = {}
    for r in rows:
        if any(f in skip_flags for f in r["flags"]):
            continue
        key = int(r["time"].timestamp()) // seconds * seconds
        buckets.setdefault(key, []).append(r)

    out = []
    for key in sorted(buckets):
        group = buckets[key]
        o = {
            "time": datetime.fromtimestamp(key, timezone.utc),
            "n": len(group),
            "n_direct": sum(r["link"] == "direct" for r in group),
            "n_imt": sum(r["link"] == "imt" for r in group),
        }
        for f in _MEAN_FIELDS:
            vals = [r[f] for r in group if r[f] is not None]
            o[f] = sum(vals) / len(vals) if vals else None
        for f in _ANGLE_FIELDS:
            vals = [math.radians(r[f]) for r in group if r[f] is not None]
            if vals:
                deg = math.degrees(
                    math.atan2(sum(map(math.sin, vals)), sum(map(math.cos, vals)))
                )
                o[f] = deg % 360
            else:
                o[f] = None
        for f in _LAST_FIELDS:
            o[f] = next((r[f] for r in reversed(group) if r[f] is not None), None)
        o["flags"] = sorted({f for r in group for f in r["flags"]})
        out.append(o)
    return out


def write_csv(rows: list[dict], path: str | Path, cols: list[str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([_fmt(r.get(c)) for c in cols])


def _fmt(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(v, float):
        return f"{v:.7g}"
    if isinstance(v, list):
        return ";".join(v)
    return v


def summary(rows: list[dict]) -> str:
    lines = [
        f"rows: {len(rows)}",
        f"span: {rows[0]['time']:%Y-%m-%d %H:%M} -> "
        f"{rows[-1]['time']:%Y-%m-%d %H:%M} UTC",
    ]
    lines.append(
        "by link: "
        + ", ".join(
            f"{k}={v}" for k, v in Counter(r["link"] for r in rows).most_common()
        )
    )
    lines.append(
        "by mode: "
        + ", ".join(
            f"{k}={v}" for k, v in Counter(r["mode_name"] for r in rows).most_common()
        )
    )
    flags = Counter(f for r in rows for f in r["flags"])
    lines.append(
        "flags: " + (", ".join(f"{k}={v}" for k, v in flags.most_common()) or "none")
    )
    reboots = sorted(
        {f"{r['time']:%m-%d %H:%M}" for r in rows if "reboot" in r["flags"]}
    )
    if reboots:
        lines.append("autopilot restarts: " + ", ".join(reboots))
    last_direct = max((r["time"] for r in rows if r["link"] == "direct"), default=None)
    if last_direct:
        lines.append(f"last Starlink message: {last_direct:%Y-%m-%d %H:%M} UTC")
    for f in ("battery_pct", "battery_v", "solar_w", "thruster_w", "temperature_c"):
        vals = [r[f] for r in rows if r[f] is not None]
        if vals:
            mean = sum(vals) / len(vals)
            lines.append(
                f"{f}: n={len(vals)} min={min(vals):.2f} "
                f"mean={mean:.2f} max={max(vals):.2f}"
            )
    return "\n".join(lines)


CSV_ENV = "SCOUT_TELEMETRY_CSV"
_dotenv_dir: Path | None = None  # directory of the .env that was loaded
_dotenv_keys: set = set()  # variables that came from .env, not the shell


def find_dotenv() -> Path | None:
    """Nearest .env above the current directory, else above the running script."""
    starts = [Path.cwd()]
    if sys.argv and sys.argv[0]:
        starts.append(Path(sys.argv[0]).resolve().parent)
    for start in starts:
        for d in (start, *start.parents):
            if (d / ".env").is_file():
                return d / ".env"
    return None


def load_dotenv(path: str | Path | None = None) -> Path | None:
    """Load KEY=VALUE lines from a .env file into os.environ.

    Variables already set in the environment win. Supports blank lines,
    `# comments`, an optional `export ` prefix and quoted values. Returns
    the file loaded, or None.
    """
    global _dotenv_dir
    f = Path(path) if path else find_dotenv()
    if f is None or not f.is_file():
        return None
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key[7:].strip() if key.startswith("export ") else key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value
            _dotenv_keys.add(key)
    _dotenv_dir = f.parent.resolve()
    return f


def csv_default() -> str | None:
    """$SCOUT_TELEMETRY_CSV from the shell or .env, or None. Never a built-in path.

    A relative path from .env is relative to the .env file; one from the
    shell is relative to the current directory.
    """
    if _dotenv_dir is None:
        load_dotenv()
    v = os.environ.get(CSV_ENV)
    if v and CSV_ENV in _dotenv_keys and _dotenv_dir and not Path(v).is_absolute():
        v = str(_dotenv_dir / v)
    return v or None


def csv_arg(ap: argparse.ArgumentParser) -> None:
    """Add --csv. Defaults to $SCOUT_TELEMETRY_CSV from the environment or a .env.

    There is no built-in path. A relative path in .env is taken relative to
    the .env file; one from the shell is relative to the current directory.
    """
    default = csv_default()
    ap.add_argument(
        "--csv",
        default=default,
        required=not default,
        help=f"raw Tether CSV export (default: ${CSV_ENV}, also read from .env)",
    )


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    csv_arg(p)
    p.add_argument("-o", "--out", help="write cleaned CSV here")
    p.add_argument(
        "--resample",
        type=int,
        metavar="SECONDS",
        help="bucket into fixed intervals (excludes backlog rows)",
    )
    p.add_argument(
        "--no-globalstar",
        action="store_true",
        help="drop backup-tracker rows (position only)",
    )
    p.add_argument("--summary", action="store_true", help="print a summary")
    a = p.parse_args(argv)

    rows = load(a.csv, include_globalstar=not a.no_globalstar)
    if a.summary or not a.out:
        print(summary(rows))
    if a.out:
        if a.resample:
            out = resample(rows, a.resample)
            write_csv(out, a.out, RESAMPLE_COLS)
        else:
            out = rows
            write_csv(out, a.out, OUT_COLS)
        print(f"wrote {len(out)} rows -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
