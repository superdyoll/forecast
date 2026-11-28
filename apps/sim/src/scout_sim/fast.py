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

from scout_forecast import clock
from scout_forecast import energy as E
from scout_forecast.evaluate import LEADS_H
from scout_lowpower import current as C
from scout_lowpower import planner as P
from scout_lowpower.mission import EARTH_R, Mission, distance_m, unit

from .environment import Environment, add_storm_args, env_config
from .power import PowerModel, PowerTruth

STEP_S = 60  # physics step (boat seconds)
DECIDE_S = 300  # as the service
OBSERVE_S = 60  # cloud and current updates: once a boat minute, as the service
HULL_TOP_STW = 0.8  # m/s through the water (--hull-top-stw; SITL's is ~0.3)
WP_RADIUS_M = 15.0


def move(lat, lon, vn, ve, dt):
    lat2 = lat + math.degrees(vn * dt / EARTH_R)
    lon2 = lon + math.degrees(ve * dt / (EARTH_R * math.cos(math.radians(lat))))
    return lat2, lon2


def unit_to(a, b):
    u, d = unit(a, b)
    return u if d > 0.1 else (0.0, 0.0)


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
        elif action in ("hold", "transit"):
            home = m.station if m.kind == "station" else m.waypoints[-1]
            if distance_m(pos, home) > m.radius_m:
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
    start = clock.parse_iso(a.start)
    env = Environment(env_config(a.seed, a.storm, a.storm_cloud), start)
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
    with (
        open(out / "truth.jsonl", "w") as truth_f,
        open(out / "history.jsonl", "w") as hist_f,
    ):
        plan, current, cloud_obs, stw_max = None, (0.0, 0.0), [], None
        current_mean = (0.0, 0.0)
        recent = []  # (t, load W, solar W, ground, heading, throttle %)
        leg, leg_t = (0, False), start  # (next_wp, at_final) and when it began
        action_t = start  # when the plan's action last changed
        truth_pct = {}  # minute -> battery reading
        rows, actions = [], {}
        t = start
        end = start + timedelta(days=a.days)
        k = 0
        while t < end:
            drift = env.drift(t)
            stw, ground, heading = boat.step(plan, drift, STEP_S)
            if (boat.next_wp, boat.at_final) != leg:
                leg, leg_t = (boat.next_wp, boat.at_final), t
            motors = plan is not None and plan.action in ("transit", "hold")
            pw = power.step(t, STEP_S, boat.lat, boat.lon, stw, motors, env.cloud(t))
            # Throttle as ArduPilot reports it: full only at the hull's limit.
            throttle = 100.0 * min(1.0, stw / boat.top)
            recent.append((t, pw["load_w"], pw["solar_w"], ground, heading, throttle))
            # A whole Starlink cycle, as the service averages (_observe_current).
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
                # As the service: a Starlink cycle, not back past the plan's
                # or the leg's start (current.window_s).
                span = None
                if plan:
                    since = (t - max(action_t, leg_t)).total_seconds()
                    span = C.window_s(since, 3600.0)
                win = [r for r in recent if span and (t - r[0]).total_seconds() <= span]
                if win:
                    g = tuple(sum(r[3][i] for r in win) / len(win) for i in (0, 1))
                    hn = sum(math.cos(math.radians(r[4])) for r in win)
                    he = sum(math.sin(math.radians(r[4])) for r in win)
                    heading = math.degrees(math.atan2(he, hn)) % 360  # vector mean
                    # The service switches Starlink here (the power model follows
                    # its advice), so it knows when Starlink was on.
                    prop = C.propulsion_w(
                        params,
                        [(r[0].timestamp(), r[1]) for r in win],
                        plan.starlink_duty,
                        power.t.starlink_period_s,
                    )
                    # The boat always does what the plan asks: right mode, and
                    # armed exactly when the motors run.
                    obs = C.observe(params, plan.action, True, motors, g, heading, prop)
                    if obs is not None:
                        current = C.blend(current, obs, 0.3 * span / 3600.0)
                        current_mean = C.blend(current_mean, obs, OBSERVE_S / 86400)
                    if plan.action == "transit" and plan.speed_ms:
                        stw_max = C.learn_top_speed(
                            stw_max,
                            plan.speed_ms,
                            math.hypot(*g),
                            E.stw_for_power(params, prop),
                            sum(r[5] for r in win) / len(win),
                        )
            if k % (DECIDE_S // STEP_S) == 0:
                cloud = C.cloud_factor(cloud_obs)
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
                new = P.decide(s, m, params, prev=plan)
                if plan is None or new.action != plan.action:
                    action_t = t  # as the service: the window starts again
                plan = new
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
    add_storm_args(ap)
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
