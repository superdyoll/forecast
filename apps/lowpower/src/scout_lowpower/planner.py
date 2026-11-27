"""Low-power decision logic. Pure functions: no I/O, no clock, no MAVLink.

Every cycle the service calls decide() with the boat's state, the operator's
mission and the fitted energy model. decide() forecasts the battery for each
candidate way of operating and picks one:

Transit (waypoints):
  candidates = each cruise speed in Config.speeds_ms, with normal or minimal
  Starlink duty. The speed is over ground (ArduPilot holds SOG); with the
  estimated current, each leg gives the speed through the water it takes and
  the ground speed actually reachable at the hull's top speed through the
  water (current.along_leg). Load = hotel + Starlink x duty + propulsion(speed
  through the water) until arrival, then station-keeping at the last waypoint.
  * feasible = the forecast never drops below reserve + margin.
  * With arrive_by: the feasible speed that arrives in time with the most
    battery to spare (usually the slowest that makes it). If none arrives in
    time, the fastest feasible speed: reserve outranks deadline.
  * Without arrive_by: the fastest speed that still leaves `comfort_pct`
    above the reserve margin, else the fastest feasible speed within
    `economy_tolerance_pct` of the one that leaves most battery.
  * Nothing feasible: pause (HOLD, motors off) and let the sun recharge the
    battery. Re-evaluated every cycle, so transit resumes once it is safe.
  Minimal Starlink duty is used only when normal duty isn't feasible.

Station (or arrived at the last waypoint):
  hold (LOITER, thrust enough to stem the current) if feasible, else drift
  (HOLD, motors off) until holding is affordable again; holding then brings
  the boat back. Drifting and motoring back costs at least as much as holding
  (propulsion grows with the cube of speed: (d/v) P(v) >= P(d)), so when
  holding isn't affordable, returning isn't either. Normal comms preferred.

Safety rules that bypass everything else:
  * battery_pct below reserve - floor_pct now: stop propulsion immediately.
  * Hysteresis: a speed is kept for min_dwell_s unless it stops being
    feasible, so the boat doesn't hunt between neighbouring speeds.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta

from scout_forecast import energy as E
from scout_forecast.clock import parse_iso

from . import current as C
from .mission import Mission, distance_m, legs, remaining_m


@dataclass
class Config:
    speeds_ms: tuple = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2)
    # Look-ahead when there's no deadline. 48 h, not 24: in a 3-day storm
    # (scout-sim fast --storm), 24 h let it hold station into the gale on day 1
    # and run below the reserve on day 3.
    horizon_h: float = 48.0
    max_horizon_h: float = 96.0
    margin_pct: float = 5.0  # forecast must stay this far above the reserve
    comfort_pct: float = 15.0  # spare needed to choose speed over economy
    economy_tolerance_pct: float = 2.0  # when tight: fastest within this of best
    floor_pct: float = 3.0  # below reserve - floor now: stop propulsion
    starlink_duty_normal: float = 0.25  # fraction of time Starlink is on
    starlink_duty_min: float = 0.05
    min_dwell_s: float = 900.0
    station_hold_w: float = 5.0  # minimum station-keeping propulsion, W
    min_progress_ms: float = 0.05  # less over ground than this is no headway
    # No headway: price motoring into it this long (a tide turns).
    stem_hours: float = 6.0
    # Bad weather persists: with the recent cloud factor below the long-run
    # average, plan as if it relaxes back over this long, not cloud_tau_h.
    bad_weather_tau_h: float = 96.0
    # In forecasts the current relaxes from now to its mean over this long
    # (a semi-diurnal tide turns in ~6 h), instead of staying at today's peak.
    current_tau_h: float = 3.0


@dataclass
class BoatState:
    time: datetime
    lat: float
    lon: float
    battery_pct: float
    volt: float
    next_wp: int = 0  # 0-based index of the waypoint being steered to
    battery_w: float | None = None  # measured battery power (+ charging)
    cloud: float | None = None  # observed / clear-sky solar ratio, recent
    # Estimated water current (north, east) m/s, where the water goes
    # (current.py). ArduPilot's speeds are over ground; power is through water.
    current_ms: tuple = (0.0, 0.0)
    # Its slow average (~a day): what the current relaxes to in forecasts,
    # since a tidal current peaks and slackens every few hours.
    current_mean_ms: tuple = (0.0, 0.0)
    # Top speed through the water seen in practice (current.learn_top_speed);
    # None until learnt. The planner uses the lower of this and the model's.
    stw_max_ms: float | None = None
    # The autopilot is on the loiter item after the last waypoint.
    at_final: bool = False


@dataclass
class Plan:
    action: str  # "transit", "pause", "hold" or "drift"
    speed_ms: float | None
    starlink_duty: float
    feasible: bool
    min_pct: float
    end_pct: float
    eta: datetime | None
    decided_at: datetime
    mission_id: str
    reasons: list = field(default_factory=list)
    since: datetime | None = None  # when the current control (action/speed/duty) began
    forecast: list = field(default_factory=list)  # [[iso time, pct], ...] hourly

    def to_dict(self) -> dict:
        d = asdict(self)
        d["eta"] = self.eta.isoformat() if self.eta else None
        d["decided_at"] = self.decided_at.isoformat()
        d["since"] = self.since.isoformat() if self.since else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Plan:
        d = dict(d)
        d["eta"] = parse_iso(d["eta"]) if d.get("eta") else None
        d["decided_at"] = parse_iso(d["decided_at"])
        d["since"] = parse_iso(d["since"]) if d.get("since") else None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def same_control(self, other: Plan | None) -> bool:
        """True if applying this plan needs no new command to the autopilot."""
        return bool(
            other
            and other.mission_id == self.mission_id
            and other.action == self.action
            and other.speed_ms == self.speed_ms
            and other.starlink_duty == self.starlink_duty
        )


@dataclass
class _Option:
    action: str
    speed: float | None
    duty: float
    hours_to_go: float | None
    min_pct: float
    end_pct: float
    sim: dict


def decide(
    s: BoatState,
    m: Mission,
    p: E.Params,
    cfg: Config | None = None,
    prev: Plan | None = None,
) -> Plan:
    cfg = cfg or Config()
    reserve_line = m.min_reserve_pct + cfg.margin_pct
    if prev is not None and prev.mission_id != m.id:
        prev = None  # new mission: no hysteresis from the old one
    pos = (s.lat, s.lon)
    # Arrival latches: once the transit is done the boat keeps station, even
    # when loitering carries it beyond arrival_radius_m of the last waypoint.
    # ...but only near the final waypoint, so a bad index can't latch it.
    near_end = (
        m.kind == "transit" and distance_m(pos, m.waypoints[-1]) <= m.drift_radius_m
    )
    was_arrived = prev is not None and prev.action in ("hold", "drift")
    arrived = m.kind == "transit" and (
        remaining_m(m, pos, s.next_wp) <= m.arrival_radius_m
        or (near_end and (s.at_final or was_arrived))
    )
    reasons = []

    if s.battery_pct < m.min_reserve_pct - cfg.floor_pct:
        reasons.append(
            f"battery {s.battery_pct:.1f}% is below reserve {m.min_reserve_pct:.0f}% "
            f"- {cfg.floor_pct:.0f}: propulsion stopped to recharge"
        )
        if m.kind == "station" or arrived:
            opt = _station_option(s, m, p, cfg, "drift", cfg.starlink_duty_min)
        else:
            opt = _pause_option(s, m, p, cfg, cfg.starlink_duty_min)
        return _plan(opt, s, m, reserve_line, reasons, prev, cfg)

    if m.kind == "station" or arrived:
        if arrived:
            reasons.append("arrived at the last waypoint: keeping station there")
        return _decide_station(s, m, p, cfg, prev, reserve_line, reasons)
    return _decide_transit(s, m, p, cfg, prev, reserve_line, reasons)


# ------------------------------------------------------------------ transit
def _decide_transit(s, m, p, cfg, prev, line, reasons) -> Plan:
    route = legs(m, (s.lat, s.lon), s.next_wp)
    deadline_h = (m.arrive_by - s.time).total_seconds() / 3600 if m.arrive_by else None
    stw_max = _stw_max(s, p)
    cur = math.hypot(*s.current_ms)
    if cur >= 0.05 and route:
        u = route[0][1]
        along = s.current_ms[0] * u[0] + s.current_ms[1] * u[1]
        reasons.append(
            f"current {cur:.2f} m/s, {abs(along):.2f} m/s "
            f"{'with' if along >= 0 else 'against'} the boat on this leg "
            f"(top speed through the water {stw_max:.2f} m/s)"
        )
    for duty in (cfg.starlink_duty_normal, cfg.starlink_duty_min):
        opts = _distinct(
            [_transit_option(s, m, p, cfg, v, duty, route) for v in cfg.speeds_ms]
        )
        moving = [o for o in opts if math.isfinite(o.hours_to_go)]
        if not moving:
            # No speed makes headway. Motoring flat out loses the least ground
            # until the current turns; pause only if that isn't affordable.
            stem = max(opts, key=lambda o: o.speed)
            if stem.min_pct >= line:
                reasons.append(
                    f"no headway: the current ({cur:.2f} m/s) beats the boat's top "
                    f"speed through the water ({stw_max:.2f} m/s); motoring into it "
                    f"to hold ground until it eases (low point {stem.min_pct:.0f}%)"
                )
                return _plan(stem, s, m, line, reasons, prev, cfg)
            if duty == cfg.starlink_duty_min:
                opt = _pause_option(s, m, p, cfg, cfg.starlink_duty_min)
                reasons.append(
                    f"no headway against {cur:.2f} m/s and no energy to hold ground: "
                    f"pausing (low point {opt.min_pct:.0f}%)"
                )
                return _plan(opt, s, m, line, reasons, prev, cfg)
            continue
        opts = moving
        feasible = [o for o in opts if o.min_pct >= line]
        on_time = [
            o for o in feasible if deadline_h is None or o.hours_to_go <= deadline_h
        ]
        if on_time:
            if deadline_h is not None:
                best = max(on_time, key=lambda o: (round(o.min_pct, 1), -o.speed))
                why = (
                    f"{best.speed:.1f} m/s arrives {best.hours_to_go:.1f} h from now, "
                    f"{deadline_h - best.hours_to_go:.1f} h before the deadline, "
                    f"with the most battery to spare (low point {best.min_pct:.0f}%)"
                )
            else:
                comfy = [o for o in on_time if o.min_pct >= line + cfg.comfort_pct]
                if comfy:
                    best = max(comfy, key=lambda o: o.speed)
                    why = (
                        f"no deadline and energy to spare: fastest speed "
                        f"({best.speed:.1f} m/s) keeping the low point at "
                        f"{best.min_pct:.0f}% (>= {line + cfg.comfort_pct:.0f}%)"
                    )
                else:
                    top = max(o.min_pct for o in on_time)
                    near = [
                        o
                        for o in on_time
                        if o.min_pct >= top - cfg.economy_tolerance_pct
                    ]
                    best = max(near, key=lambda o: o.speed)
                    why = (
                        f"energy is tight: fastest speed within "
                        f"{cfg.economy_tolerance_pct:.0f} points of the most "
                        f"economical "
                        f"({best.speed:.1f} m/s), low point {best.min_pct:.0f}%"
                    )
            best = _hysteresis(best, opts, prev, s, cfg, line, deadline_h, reasons)
            reasons.append(why)
            _note_min_duty(reasons, duty, cfg)
            return _plan(best, s, m, line, reasons, prev, cfg)
        if feasible:  # can't make the deadline without breaching the reserve
            best = max(feasible, key=lambda o: o.speed)
            if deadline_h <= 0:
                why = f"the deadline passed {-deadline_h:.1f} h ago"
            else:
                why = f"deadline can't be met without going below {line:.0f}%"
            reasons.append(
                f"{why}: fastest safe speed {best.speed:.1f} m/s arrives "
                f"{best.hours_to_go - deadline_h:.1f} h late"
            )
            _note_min_duty(reasons, duty, cfg)
            return _plan(best, s, m, line, reasons, prev, cfg)
    opt = _pause_option(s, m, p, cfg, cfg.starlink_duty_min)
    reasons.append(
        f"no speed keeps the forecast above {line:.0f}%: pausing (motors off, "
        f"minimal comms) to recharge; low point while paused {opt.min_pct:.0f}%"
    )
    return _plan(opt, s, m, line, reasons, prev, cfg)


def _hysteresis(best, opts, prev, s, cfg, line, deadline_h, reasons):
    if prev is None or prev.action != "transit" or prev.speed_ms is None:
        return best
    since = prev.since or prev.decided_at  # when that speed was set
    if (s.time - since).total_seconds() >= cfg.min_dwell_s:
        return best
    keep = next((o for o in opts if o.speed == prev.speed_ms), None)
    ok = keep is not None and keep.min_pct >= line
    if ok and deadline_h is not None:
        ok = keep.hours_to_go <= deadline_h
    if ok and keep.speed != best.speed:
        reasons.append(
            f"keeping {keep.speed:.1f} m/s (changed less than "
            f"{cfg.min_dwell_s / 60:.0f} min ago)"
        )
        return keep
    return best


def _transit_option(s, m, p, cfg, v, duty, route) -> _Option:
    """Command `v` over ground along `route` (legs): time and load per leg."""
    stw_max = _stw_max(s, p)
    base = E.base_load_w(p, duty)
    segments, hours = [], 0.0  # (end hour, load W)
    for length, unit in route:
        # The current where the boat is mid-leg. That time depends on the
        # speed it actually makes, which depends on the current: iterate,
        # from the speed it makes at the leg's start. (Timing it by the
        # commanded speed made commands beyond reach look slower.)
        sog, stw = C.along_leg(v, unit, _current_at(s, hours, cfg), stw_max)
        for _ in range(20):
            mid = hours + 0.5 * length / max(sog, cfg.min_progress_ms) / 3600
            was = sog
            sog, stw = C.along_leg(v, unit, _current_at(s, mid, cfg), stw_max)
            if abs(sog - was) < 1e-9:
                break
        if sog < cfg.min_progress_ms:
            hours = math.inf
            break
        hours += length / sog / 3600
        segments.append((hours, base + E.propulsion_w(p, stw)))
    if not math.isfinite(hours):  # no headway: flat out for stem_hours, then hold
        segments = [(cfg.stem_hours, base + E.propulsion_w(p, stw_max))]
    span = hours if math.isfinite(hours) else cfg.stem_hours
    horizon = min(max(cfg.horizon_h, span + 2), cfg.max_horizon_h)

    def load(k, t):
        h = (k + 0.5) * E.STEP_H
        for end, w in segments:
            if h < end:
                return w
        return base + _station_w(p, cfg, _current_at(s, h, cfg), stw_max)

    sim = _project(s, p, load, horizon, cfg)
    return _Option("transit", v, duty, hours, min(sim["pct"]), sim["pct"][-1], sim)


def _current_at(s, hours: float, cfg: Config):
    """The current `hours` ahead: relaxing from now to its slow mean."""
    k = math.exp(-hours / cfg.current_tau_h) if cfg.current_tau_h > 0 else 1.0
    return tuple(m + (c - m) * k for c, m in zip(s.current_ms, s.current_mean_ms))


def _stw_max(s, p) -> float:
    model = E.max_stw_ms(p)
    return model if s.stw_max_ms is None else max(0.05, min(model, s.stw_max_ms))


def _distinct(opts):
    """Speeds the current or the hull's top speed make identical: keep the
    lowest command (asking for more than is reachable changes nothing)."""
    out = []
    for o in opts:
        same = math.isfinite(o.hours_to_go) and any(
            abs(o.hours_to_go - k.hours_to_go) < 1e-6
            and abs(o.min_pct - k.min_pct) < 1e-6
            for k in out
        )
        if not same:
            out.append(o)
    return out


def _pause_option(s, m, p, cfg, duty) -> _Option:
    base = E.base_load_w(p, duty)
    sim = _project(s, p, lambda k, t: base, cfg.horizon_h, cfg)
    return _Option("pause", None, duty, None, min(sim["pct"]), sim["pct"][-1], sim)


# ------------------------------------------------------------------ station
def _decide_station(s, m, p, cfg, prev, line, reasons) -> Plan:
    for duty in (cfg.starlink_duty_normal, cfg.starlink_duty_min):
        for action in ("hold", "drift"):
            opt = _station_option(s, m, p, cfg, action, duty)
            if opt.min_pct >= line:
                if action == "hold":
                    reasons.append(
                        f"holding station keeps the low point at "
                        f"{opt.min_pct:.0f}% (>= {line:.0f}%)"
                    )
                else:
                    reasons.append(
                        f"holding station would go below {line:.0f}%: drifting with "
                        f"motors off until holding is affordable again "
                        f"(low point {opt.min_pct:.0f}%)"
                    )
                _note_min_duty(reasons, duty, cfg)
                return _plan(opt, s, m, line, reasons, prev, cfg)
    opt = _station_option(s, m, p, cfg, "drift", cfg.starlink_duty_min)
    reasons.append(
        f"no option keeps the forecast above {line:.0f}%: drifting with motors off "
        f"and minimal comms to save energy (low point {opt.min_pct:.0f}%)"
    )
    return _plan(opt, s, m, line, reasons, prev, cfg)


def _station_w(p, cfg, current, stw_max) -> float:
    """Station-keeping propulsion: at least the observed MODE_17 draw, and
    enough to stem the current (capped at the hull's top speed)."""
    stem = E.propulsion_w(p, min(math.hypot(*current), stw_max))
    return max(E.load_component(p, "mode17_propulsion_w"), cfg.station_hold_w, stem)


def _station_option(s, m, p, cfg, action, duty) -> _Option:
    base = E.base_load_w(p, duty)

    stw_max = _stw_max(s, p)

    def load(k, t):
        if action == "drift":  # motors off
            return base
        h = (k + 0.5) * E.STEP_H
        return base + _station_w(p, cfg, _current_at(s, h, cfg), stw_max)

    horizon = cfg.horizon_h
    if m.hold_until is not None:
        horizon = min(
            max((m.hold_until - s.time).total_seconds() / 3600, 1.0), cfg.max_horizon_h
        )
    sim = _project(s, p, load, horizon, cfg)
    return _Option(action, None, duty, None, min(sim["pct"]), sim["pct"][-1], sim)


# ------------------------------------------------------------------ helpers
def _note_min_duty(reasons, duty, cfg) -> None:
    if duty == cfg.starlink_duty_min:
        reasons.append("Starlink reduced to minimal duty: normal duty isn't feasible")


def _project(s, p, load_fn, hours, cfg: Config) -> dict:
    if s.cloud is not None and s.cloud < p.cloud_default:
        p = replace(p, cloud_tau_h=max(p.cloud_tau_h, cfg.bad_weather_tau_h))
    return E.project(
        p,
        s.time,
        s.lat,
        s.lon,
        s.battery_pct,
        s.volt,
        load_fn,
        hours,
        cloud=s.cloud,
        net0=s.battery_w,
    )


def _plan(opt: _Option, s, m, line, reasons, prev, cfg) -> Plan:
    eta = (
        s.time + timedelta(hours=opt.hours_to_go)
        if opt.hours_to_go is not None and math.isfinite(opt.hours_to_go)
        else None
    )
    sim = opt.sim
    hourly = [
        [t.isoformat(), round(v, 2)]
        for k, (t, v) in enumerate(zip(sim["time"], sim["pct"]))
        if (k + 1) % E.STEPS_PER_H == 0
    ]
    plan = Plan(
        action=opt.action,
        speed_ms=opt.speed,
        starlink_duty=opt.duty,
        feasible=opt.min_pct >= line,
        min_pct=round(opt.min_pct, 2),
        end_pct=round(opt.end_pct, 2),
        eta=eta,
        decided_at=s.time,
        mission_id=m.id,
        reasons=reasons,
        forecast=hourly,
    )
    plan.since = (prev.since or prev.decided_at) if plan.same_control(prev) else s.time
    return plan
