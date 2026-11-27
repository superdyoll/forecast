"""Fast-time simulation without SITL, for scenarios that last days (storms).

    scout-sim fast --days 4 --mission station --storm 24:72 --out sim-run/storm

The same pieces as the SITL run where it matters: the environment
(environment.py), the power system's truth (power.py, ~10 % off the fitted
model), and the low-power service's real planner and current estimator
(scout_lowpower.planner / current). What's simplified is the boat: instead of
ArduPilot and SITL's hull, it does what the plan asks within the hull's top
speed through the water (current.along_leg), holds its track, and drifts with
the water when the motors are off. No MAVLink, no clock: hours run in seconds.

The service's telemetry habits are copied: decisions every 5 min of boat time,
the cloud factor from the last 24 h of solar readings, and the current from
measured propulsion power (motoring) or from the drift (motors off).
Output: truth.jsonl (every minute), history.jsonl (every decision, as the
service writes it) and summary.json, which scores every forecast against what
followed, as the web page does.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from scout_forecast import energy as E
from scout_lowpower import current as C
from scout_lowpower import planner as P
from scout_lowpower.mission import EARTH_R, Mission, distance_m, parse_time

from .environment import EnvConfig, Environment
from .power import PowerModel, PowerTruth

STEP_S = 60  # physics step (boat seconds)
DECIDE_S = 300  # as the service
OBSERVE_S = 900  # cloud and current updates
HULL_TOP_STW = 0.8  # m/s through the water (--hull-top-stw; SITL's is ~0.3)
WP_RADIUS_M = 15.0
LEADS_H = (1, 3, 6, 12, 24)


def move(lat, lon, vn, ve, dt):
    lat2 = lat + math.degrees(vn * dt / EARTH_R)
    lon2 = lon + math.degrees(ve * dt / (EARTH_R * math.cos(math.radians(lat))))
    return lat2, lon2


def unit_to(a, b):
    n = math.radians(b[0] - a[0]) * EARTH_R
    e = math.radians(b[1] - a[1]) * EARTH_R * math.cos(math.radians(a[0]))
    d = math.hypot(n, e)
    return (n / d, e / d) if d > 0.1 else (0.0, 0.0)


class Boat:
    """Position and what the motors are doing, one plan at a time."""

    def __init__(self, lat, lon, mission: Mission, top_stw: float = HULL_TOP_STW):
        self.lat, self.lon, self.m, self.top = lat, lon, mission, top_stw
        self.next_wp, self.at_final = 0, False

    def step(self, plan: P.Plan | None, drift, dt):
        """Returns (speed through the water, ground velocity, heading deg)."""
        m, pos = self.m, (self.lat, self.lon)
        stw, ground = 0.0, drift
        action = plan.action if plan else "pause"
        if action == "transit" and not self.at_final:
            target = m.waypoints[self.next_wp]
            u = unit_to(pos, target)
            sog, stw = C.along_leg(plan.speed_ms, u, drift, self.top)
            ground = (sog * u[0], sog * u[1])
            if distance_m(pos, target) <= max(WP_RADIUS_M, sog * dt):
                self.lat, self.lon = target
                if self.next_wp < len(m.waypoints) - 1:
                    self.next_wp += 1
                else:
                    self.at_final = True
                return stw, ground, _heading(ground, drift, u)
        elif action in ("hold", "transit") or (action == "drift" and plan.returning):
            home = m.station if m.kind == "station" else m.waypoints[-1]
            if action == "drift" or distance_m(pos, home) > m.radius_m:
                u = unit_to(pos, home)
                sog, stw = C.along_leg(0.6, u, drift, self.top)
                ground = (sog * u[0], sog * u[1])
            else:  # station-keeping: stem the drift if the hull can
                need = math.hypot(*drift)
                stw = min(need, self.top)
                k = 0.0 if need <= self.top else (need - self.top) / need
                ground = (drift[0] * k, drift[1] * k)
        self.lat, self.lon = move(self.lat, self.lon, ground[0], ground[1], dt)
        return stw, ground, _heading(ground, drift, (1.0, 0.0))


def _heading(ground, drift, fallback):
    w = (ground[0] - drift[0], ground[1] - drift[1])
    if math.hypot(*w) < 1e-3:
        w = fallback
    return math.degrees(math.atan2(w[1], w[0])) % 360


def run(a) -> dict:
    start = parse_time(a.start)
    env_cfg = EnvConfig(seed=a.seed)
    if a.storm:
        h0, hours = (float(x) for x in a.storm.split(":")[:2])
        env_cfg = replace(
            env_cfg, storm_start_h=h0, storm_hours=hours, storm_cloud=a.storm_cloud
        )
    env = Environment(env_cfg, start)
    params = E.fitted_params()
    power = PowerModel(PowerTruth(), params, pct0=a.pct0, seed=a.seed)
    lat, lon = 60.2, 2.2
    if a.mission == "station":
        m = Mission(
            kind="station",
            station=[lat, lon],
            radius_m=50,
            drift_radius_m=500,
            min_reserve_pct=a.reserve,
        )
    else:
        m = Mission(
            kind="transit",
            waypoints=[[60.215, 2.210], [60.230, 2.240], [60.240, 2.200]],
            arrive_by=start + timedelta(hours=a.arrive_in) if a.arrive_in else None,
            min_reserve_pct=a.reserve,
        )
    boat = Boat(lat, lon, m, a.hull_top_stw)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    truth_f = open(out / "truth.jsonl", "w")
    hist_f = open(out / "history.jsonl", "w")
    plan, current, cloud_obs, stw_max = None, (0.0, 0.0), [], None
    current_mean = (0.0, 0.0)
    recent = []  # (t, load W, solar W, ground)
    truth_pct = {}  # minute -> battery reading
    rows, actions = [], {}
    t = start
    end = start + timedelta(days=a.days)
    k = 0
    while t < end:
        drift = env.drift(t)
        stw, ground, heading = boat.step(plan, drift, STEP_S)
        motors = (
            plan is not None
            and plan.action in ("transit", "hold")
            or bool(plan and plan.returning)
        )
        pw = power.step(t, STEP_S, boat.lat, boat.lon, stw, motors, env.cloud(t))
        recent.append((t, pw["load_w"], pw["solar_w"], ground, heading))
        # A whole Starlink cycle, as the service averages (see its _observe_current).
        recent = [r for r in recent if (t - r[0]).total_seconds() <= 3600]
        truth_pct[int(t.timestamp() // 60)] = pw["pct"]
        truth_f.write(
            json.dumps(
                {
                    "time": t.isoformat(),
                    "pct": round(pw["pct"], 2),
                    "solar_w": round(pw["solar_w"], 1),
                    "load_w": round(pw["load_w"], 1),
                    "stw": round(stw, 3),
                    "drift": [round(x, 3) for x in drift],
                    "cloud": round(env.cloud(t), 3),
                    "storm": round(env.storm(t), 2),
                    "lat": round(boat.lat, 6),
                    "lon": round(boat.lon, 6),
                }
            )
            + "\n"
        )
        if k % (OBSERVE_S // STEP_S) == 0 and recent:
            solar = sum(r[2] for r in recent) / len(recent)
            clear = E.clear_sky_w(params, t, boat.lat, boat.lon)
            cloud_obs = [o for o in cloud_obs if (t - o[0]).total_seconds() < 86400]
            cloud_obs.append((t, solar, clear))
            settled = plan and plan.since and (t - plan.since).total_seconds() >= 600
            if settled:
                g = tuple(sum(r[3][i] for r in recent) / len(recent) for i in (0, 1))
                hn = sum(math.cos(math.radians(r[4])) for r in recent)
                he = sum(math.sin(math.radians(r[4])) for r in recent)
                heading = math.degrees(math.atan2(he, hn)) % 360  # vector mean
                load = sum(r[1] for r in recent) / len(recent)
                if plan.action in ("transit", "hold"):
                    base = (
                        E.load_component(params, "hotel_w")
                        + E.load_component(params, "starlink_w") * plan.starlink_duty
                    )
                    obs = C.from_thrust(params, g, heading, load - base)
                    current = C.blend(current, obs, 0.3)
                    current_mean = C.blend(current_mean, obs, OBSERVE_S / 86400)
                    if plan.action == "transit" and plan.speed_ms:
                        stw_max = C.learn_top_speed(
                            stw_max,
                            plan.speed_ms,
                            math.hypot(*g),
                            E.stw_for_power(params, load - base),
                        )
                elif plan.action in ("pause", "drift") and not plan.returning:
                    current = C.blend(current, C.from_drift(g), 0.3)
                    current_mean = C.blend(
                        current_mean, C.from_drift(g), OBSERVE_S / 86400
                    )
        if k % (DECIDE_S // STEP_S) == 0:
            obs = [o for o in cloud_obs if o[2] > 20]
            cloud = (
                sum(o[1] for o in obs) / sum(o[2] for o in obs)
                if len(obs) >= 4
                else None
            )
            s = P.BoatState(
                time=t,
                lat=boat.lat,
                lon=boat.lon,
                battery_pct=pw["pct"],
                volt=pw["volt"],
                next_wp=boat.next_wp,
                battery_w=pw["battery_w"],
                cloud=cloud,
                current_ms=current if a.estimate_current else (0.0, 0.0),
                stw_max_ms=stw_max if a.estimate_current else None,
                current_mean_ms=current_mean if a.estimate_current else (0.0, 0.0),
                at_final=boat.at_final,
            )
            plan = P.decide(s, m, params, prev=plan)
            # As if the service switches Starlink with a relay: follow its advice.
            power.t = replace(power.t, starlink_duty=plan.starlink_duty)
            actions[plan.action] = actions.get(plan.action, 0) + 1
            row = {
                "time": t.isoformat(),
                "battery_pct": round(pw["pct"], 2),
                "action": plan.action,
                "speed_ms": plan.speed_ms,
                "starlink_duty": plan.starlink_duty,
                "min_pct": plan.min_pct,
                "eta": plan.eta.isoformat() if plan.eta else None,
                "cloud_est": cloud,
                "current_ms": [round(x, 3) for x in current],
                "stw_max_ms": stw_max,
                "reasons": plan.reasons,
                "forecast": plan.forecast,
            }
            rows.append(row)
            hist_f.write(json.dumps(row) + "\n")
        t += timedelta(seconds=STEP_S)
        k += 1
    truth_f.close()
    hist_f.close()
    summary = summarise(rows, truth_pct, m, actions, start)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


def summarise(rows, truth_pct, m, actions, start) -> dict:
    def actual(iso):
        minute = int(datetime.fromisoformat(iso).timestamp() // 60)
        return truth_pct.get(minute)

    scores = {}
    for lead in LEADS_H:
        err, per, bias = [], [], []
        for r in rows:
            if len(r["forecast"]) < lead:
                continue
            ft, fp = r["forecast"][lead - 1]
            act = actual(ft)
            if act is None:
                continue
            err.append(abs(fp - act))
            bias.append(fp - act)
            per.append(abs(r["battery_pct"] - act))
        if err:
            scores[f"{lead}h"] = {
                "n": len(err),
                "mae": round(sum(err) / len(err), 2),
                "bias": round(sum(bias) / len(bias), 2),
                "mae_persistence": round(sum(per) / len(per), 2),
            }
    pcts = list(truth_pct.values())
    below = sum(1 for v in pcts if v < m.min_reserve_pct) / 60
    total = sum(actions.values()) or 1
    arrived = next((r["time"] for r in rows if r["action"] in ("hold", "drift")), None)
    return {
        "battery_min_pct": round(min(pcts), 2),
        "battery_end_pct": round(pcts[-1], 2),
        "hours_below_reserve": round(below, 2),
        "reserve_pct": m.min_reserve_pct,
        "actions_share": {k: round(v / total, 3) for k, v in sorted(actions.items())},
        "arrive_by": m.arrive_by.isoformat() if m.arrive_by else None,
        "arrived": arrived if m.kind == "transit" else None,
        "forecast_error": scores,
    }


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--start", default="2025-07-23T06:00Z")
    ap.add_argument("--days", type=float, default=2.0)
    ap.add_argument("--mission", choices=("transit", "station"), default="transit")
    ap.add_argument("--arrive-in", type=float, default=4.0, metavar="HOURS")
    ap.add_argument("--reserve", type=float, default=30.0)
    ap.add_argument("--pct0", type=float, default=70.0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument(
        "--storm", metavar="START_H:HOURS", help="e.g. 24:72: from day 2, for 3 days"
    )
    ap.add_argument("--storm-cloud", type=float, default=0.08)
    ap.add_argument(
        "--no-current-estimate",
        dest="estimate_current",
        action="store_false",
        help="plan as if there were no current (the old behaviour), for comparison",
    )
    ap.add_argument(
        "--hull-top-stw",
        type=float,
        default=HULL_TOP_STW,
        help="the boat's real top speed through the water, m/s",
    )
    ap.add_argument("--out", default="sim-run/fast")
