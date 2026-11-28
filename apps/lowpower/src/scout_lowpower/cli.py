"""The `scout-lowpower` command: run the service, set the mission, inspect it.

    scout-lowpower set-mission --transit 60.30,2.20 60.50,2.20 \
        --arrive-by 2025-07-25T12:00Z
    scout-lowpower set-mission --station 60.50,2.20 --radius 50 --reserve 35
    scout-lowpower run --mavlink tcp:127.0.0.1:5760
    scout-lowpower status
    scout-lowpower plan --lat 60.2 --lon 2.2 --pct 55        # what-if, no autopilot

Configuration: CLI flags, else environment, else .env (environment wins):
SCOUT_MAVLINK, SCOUT_STATE_DIR, SCOUT_MISSION, SCOUT_PARAMS, SCOUT_STARLINK_RELAY.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import timedelta
from pathlib import Path

from scout_forecast import clock
from scout_forecast import energy as E
from scout_forecast.env import load_dotenv, state_dir

from . import planner as P
from .mission import Mission, distance_m
from .mission import load as load_mission
from .mission import save as save_mission
from .service import Service, Settings

log = logging.getLogger("scout_lowpower")
DECIDE_EVERY_S = 300.0


def _settings(a) -> Settings:
    st = Settings(state_dir=state_dir(a.state_dir))
    st.mavlink = (
        getattr(a, "mavlink", None) or os.environ.get("SCOUT_MAVLINK") or st.mavlink
    )
    st.power_link = (
        getattr(a, "power_link", None) or os.environ.get("SCOUT_POWER_LINK") or None
    )
    mission = getattr(a, "mission", None) or os.environ.get("SCOUT_MISSION")
    st.mission = Path(mission) if mission else None
    params = getattr(a, "params", None) or os.environ.get("SCOUT_PARAMS")
    st.params = Path(params) if params else None
    # 5 min of boat time between decisions; on a fast simulated clock that
    # is fewer real seconds (but not under 15 s).
    st.interval_s = getattr(a, "interval_s", None) or max(
        15.0, DECIDE_EVERY_S / clock.rate()
    )
    # Advisory mode (a first boat): decide and log, command nothing.
    st.dry_run = bool(getattr(a, "dry_run", False)) or os.environ.get(
        "SCOUT_DRY_RUN", ""
    ) not in ("", "0")
    relay = getattr(a, "starlink_relay", None)
    if relay is None and os.environ.get("SCOUT_STARLINK_RELAY"):
        relay = int(os.environ["SCOUT_STARLINK_RELAY"])
    st.starlink_relay = relay
    st.rearm = not getattr(a, "no_rearm", False)
    return st


def _params(st: Settings) -> E.Params:
    """--params / SCOUT_PARAMS, else the fitted model shipped with scout-forecast."""
    if not st.params:
        log.info("energy model: the fit shipped with scout-forecast (no --params)")
    return E.load_params(st.params)


def _deadline(a):
    """--arrive-by, or --arrive-in hours from now (simulated clock if set)."""
    if a.arrive_by:
        return clock.parse_iso(a.arrive_by)
    if a.arrive_in:
        return clock.now() + timedelta(hours=a.arrive_in)
    return None


def _pos(s: str):
    la, lo = (float(x) for x in s.split(","))
    return [la, lo]


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(
        prog="scout-lowpower", description="Scout low-power mission mode"
    )
    ap.add_argument(
        "--state-dir",
        help="default $SCOUT_STATE_DIR, $STATE_DIRECTORY or ./lowpower-state",
    )
    ap.add_argument("--mission", help="mission JSON (default <state-dir>/mission.json)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the service")
    r.add_argument(
        "--mavlink",
        help="tcp:HOST:PORT | udpin:HOST:PORT | udpout:HOST:PORT | serial:DEV:BAUD",
    )
    r.add_argument(
        "--power-link",
        help="separate BATTERY_STATUS source, e.g. udpin:0.0.0.0:14601 "
        "(default $SCOUT_POWER_LINK)",
    )
    r.add_argument(
        "--params", help="params.json from scout-evaluate (default $SCOUT_PARAMS)"
    )
    r.add_argument(
        "--interval-s",
        type=float,
        help="real seconds between decisions (default 300, divided by the "
        "simulated clock's rate, at least 15)",
    )
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and log, send no commands",
    )
    r.add_argument(
        "--no-rearm",
        action="store_true",
        help="don't re-arm after an autopilot restart",
    )
    r.add_argument(
        "--starlink-relay",
        type=int,
        help="relay number that powers Starlink (default $SCOUT_STARLINK_RELAY)",
    )

    sm = sub.add_parser("set-mission", help="write the mission file")
    g = sm.add_mutually_exclusive_group(required=True)
    g.add_argument("--transit", nargs="+", metavar="LAT,LON")
    g.add_argument("--station", metavar="LAT,LON")
    when = sm.add_mutually_exclusive_group()
    when.add_argument("--arrive-by", help="ISO time, e.g. 2025-07-25T12:00Z")
    when.add_argument(
        "--arrive-in",
        type=float,
        metavar="HOURS",
        help="deadline this many hours from now (on the simulated clock if set)",
    )
    sm.add_argument("--hold-until")
    sm.add_argument(
        "--reserve", type=float, default=30.0, help="minimum battery_pct (default 30)"
    )
    sm.add_argument("--radius", type=float, default=50.0)
    sm.add_argument("--drift-radius", type=float, default=500.0)

    sub.add_parser("status", help="print the current decision")

    pl = sub.add_parser("plan", help="what-if: run the planner without an autopilot")
    pl.add_argument("--params")
    pl.add_argument("--lat", type=float, required=True)
    pl.add_argument("--lon", type=float, required=True)
    pl.add_argument("--pct", type=float, required=True, help="battery_pct now")
    pl.add_argument("--time", help="ISO time (default now)")
    pl.add_argument("--next-wp", type=int, default=0)
    a = ap.parse_args(argv)

    fmt = (
        "%(levelname)s %(message)s"
        if os.environ.get("JOURNAL_STREAM")
        else "%(asctime)s %(levelname)s %(message)s"
    )
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format=fmt)
    try:
        st = _settings(a)
    except ValueError as e:  # a bad $SCOUT_CLOCK: say so, no traceback
        raise SystemExit(f"scout-lowpower: {e}")

    if a.cmd == "set-mission":
        m = Mission(
            kind="transit" if a.transit else "station",
            waypoints=[_pos(p) for p in a.transit] if a.transit else [],
            station=_pos(a.station) if a.station else None,
            radius_m=a.radius,
            drift_radius_m=a.drift_radius,
            arrive_by=_deadline(a),
            hold_until=clock.parse_iso(a.hold_until) if a.hold_until else None,
            min_reserve_pct=a.reserve,
        )
        save_mission(m, st.mission_path)
        print(f"mission {m.id} written to {st.mission_path}")
        return 0
    if a.cmd == "status":
        try:
            print(
                json.dumps(
                    json.loads((st.state_dir / "decision.json").read_text()), indent=1
                )
            )
        except OSError:
            print(f"no decision yet in {st.state_dir}")
            return 1
        return 0
    if a.cmd == "plan":
        try:
            m = load_mission(st.mission_path)
        except (OSError, ValueError, TypeError) as e:
            raise SystemExit(f"invalid mission {st.mission_path}: {e}")
        s = P.BoatState(
            time=clock.parse_iso(a.time) if a.time else clock.now(),
            lat=a.lat,
            lon=a.lon,
            battery_pct=a.pct,
            volt=st.pct_zero_v + a.pct * (st.pct_full_v - st.pct_zero_v) / 100,
            next_wp=a.next_wp,
        )
        plan = P.decide(s, m, _params(st))
        print(
            json.dumps(
                {k: v for k, v in plan.to_dict().items() if k != "forecast"}, indent=1
            )
        )
        if m.kind == "station":
            print(f"distance to station: {distance_m((a.lat, a.lon), m.station):.0f} m")
        return 0
    st.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        svc = Service(st, _params(st))
    except ValueError as e:  # a bad SCOUT_MAVLINK / --mavlink
        raise SystemExit(f"scout-lowpower: {e}")
    return svc.run()
