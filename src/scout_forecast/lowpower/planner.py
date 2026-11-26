"""Low-power decision logic. Pure functions: no I/O, no clock, no MAVLink.

Every cycle the service calls decide() with the boat's state, the operator's
mission and the fitted energy model. decide() forecasts the battery for each
candidate way of operating and picks one:

Transit (waypoints):
  candidates = each cruise speed in Config.speeds_ms, with normal or minimal
  Starlink duty. Load = hotel + Starlink x duty + propulsion(speed) x
  load_factor until arrival, then station-keeping at the last waypoint.
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
  hold (LOITER, small thrust) if feasible, else drift (HOLD, motors off) and
  motor back when outside drift_radius_m, again preferring normal comms.

Safety rules that bypass everything else:
  * battery_pct below reserve - floor_pct now: stop propulsion immediately.
  * Hysteresis: a speed is kept for min_dwell_s unless it stops being
    feasible, so the boat doesn't hunt between neighbouring speeds.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .. import energy as E
from .mission import Mission, distance_m, remaining_m


@dataclass
class Config:
    speeds_ms: tuple = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2)
    horizon_h: float = 24.0  # look-ahead when there's no deadline
    max_horizon_h: float = 96.0
    margin_pct: float = 5.0  # forecast must stay this far above the reserve
    comfort_pct: float = 15.0  # spare needed to choose speed over economy
    economy_tolerance_pct: float = 2.0  # when tight: fastest within this of best
    floor_pct: float = 3.0  # below reserve - floor now: stop propulsion
    starlink_duty_normal: float = 0.25  # fraction of time Starlink is on
    starlink_duty_min: float = 0.05
    min_dwell_s: float = 900.0
    station_hold_w: float = 5.0  # minimum station-keeping propulsion, W
    drift_return_fraction: float = 0.15  # share of drift time spent returning
    return_speed_ms: float = 0.6


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
    load_factor: float = 1.0  # observed / modelled propulsion power
    at_final: bool = (
        False  # the autopilot is on the loiter item after the last waypoint
    )


@dataclass
class Plan:
    action: str  # "transit", "pause", "hold" or "drift"
    speed_ms: float | None
    starlink_duty: float
    returning: bool  # drift: motoring back to the station
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
        from .mission import parse_time

        d = dict(d)
        d["eta"] = parse_time(d["eta"]) if d.get("eta") else None
        d["decided_at"] = parse_time(d["decided_at"])
        d["since"] = parse_time(d["since"]) if d.get("since") else None
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def same_control(self, other: Plan | None) -> bool:
        """True if applying this plan needs no new command to the autopilot."""
        return bool(
            other
            and other.mission_id == self.mission_id
            and other.action == self.action
            and other.speed_ms == self.speed_ms
            and other.starlink_duty == self.starlink_duty
            and other.returning == self.returning
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
            return _plan(opt, s, m, reserve_line, reasons, prev, cfg)
        opt = _pause_option(s, m, p, cfg, cfg.starlink_duty_min)
        return _plan(opt, s, m, reserve_line, reasons, prev, cfg)

    if m.kind == "station" or arrived:
        if arrived:
            reasons.append("arrived at the last waypoint: keeping station there")
        return _decide_station(s, m, p, cfg, prev, reserve_line, reasons)
    return _decide_transit(s, m, p, cfg, prev, reserve_line, reasons)


# ------------------------------------------------------------------ transit
def _decide_transit(s, m, p, cfg, prev, line, reasons) -> Plan:
    dist = remaining_m(m, (s.lat, s.lon), s.next_wp)
    deadline_h = (m.arrive_by - s.time).total_seconds() / 3600 if m.arrive_by else None
    for duty in (cfg.starlink_duty_normal, cfg.starlink_duty_min):
        opts = [_transit_option(s, m, p, cfg, v, duty, dist) for v in cfg.speeds_ms]
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
            if duty == cfg.starlink_duty_min:
                reasons.append(
                    "Starlink reduced to minimal duty: normal duty isn't feasible"
                )
            return _plan(best, s, m, line, reasons, prev, cfg)
        if feasible:  # can't make the deadline without breaching the reserve
            best = max(feasible, key=lambda o: o.speed)
            reasons.append(
                f"deadline can't be met without going below {line:.0f}%: "
                f"fastest safe speed {best.speed:.1f} m/s arrives "
                f"{best.hours_to_go - deadline_h:.1f} h late"
            )
            if duty == cfg.starlink_duty_min:
                reasons.append(
                    "Starlink reduced to minimal duty: normal duty isn't feasible"
                )
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


def _transit_option(s, m, p, cfg, v, duty, dist) -> _Option:
    hours = dist / v / 3600
    base = E.load_component(p, "hotel_w") + E.load_component(p, "starlink_w") * duty
    moving = base + E.propulsion_w(p, v) * s.load_factor
    holding = base + _station_w(p, cfg) * s.load_factor
    horizon = min(max(cfg.horizon_h, hours + 2), cfg.max_horizon_h)

    def load(k, t):
        return moving if (k + 0.5) * E.STEP_H < hours else holding

    sim = _project(s, p, load, horizon)
    return _Option("transit", v, duty, hours, min(sim["pct"]), sim["pct"][-1], sim)


def _pause_option(s, m, p, cfg, duty) -> _Option:
    base = E.load_component(p, "hotel_w") + E.load_component(p, "starlink_w") * duty
    sim = _project(s, p, lambda k, t: base, cfg.horizon_h)
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
                        f"active hold would go below {line:.0f}%: drifting with motors "
                        f"off, returning beyond {m.drift_radius_m:.0f} m "
                        f"(low point {opt.min_pct:.0f}%)"
                    )
                if duty == cfg.starlink_duty_min:
                    reasons.append(
                        "Starlink reduced to minimal duty: normal duty isn't feasible"
                    )
                return _plan(opt, s, m, line, reasons, prev, cfg)
    opt = _station_option(s, m, p, cfg, "drift", cfg.starlink_duty_min)
    reasons.append(
        f"no option keeps the forecast above {line:.0f}%: drifting with minimal "
        f"comms to save energy (low point {opt.min_pct:.0f}%)"
    )
    return _plan(opt, s, m, line, reasons, prev, cfg)


def _station_w(p, cfg) -> float:
    return max(E.load_component(p, "mode17_propulsion_w"), cfg.station_hold_w)


def _station_option(s, m, p, cfg, action, duty) -> _Option:
    base = E.load_component(p, "hotel_w") + E.load_component(p, "starlink_w") * duty
    if action == "hold":
        w = base + _station_w(p, cfg) * s.load_factor
    else:
        ret = E.propulsion_w(p, cfg.return_speed_ms) * s.load_factor
        w = base + cfg.drift_return_fraction * ret
    horizon = cfg.horizon_h
    if m.hold_until is not None:
        horizon = min(
            max((m.hold_until - s.time).total_seconds() / 3600, 1.0), cfg.max_horizon_h
        )
    sim = _project(s, p, lambda k, t: w, horizon)
    return _Option(action, None, duty, None, min(sim["pct"]), sim["pct"][-1], sim)


# ------------------------------------------------------------------ helpers
def _project(s, p, load_fn, hours) -> dict:
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
    returning = False
    if opt.action == "drift":
        target = m.station if m.kind == "station" else m.waypoints[-1]
        d = distance_m((s.lat, s.lon), target)
        was = bool(prev and prev.action == "drift" and prev.returning)
        # Return when beyond the drift radius; keep motoring until back inside
        # the loiter radius (hysteresis between the two radii).
        returning = d > m.drift_radius_m or (was and d > m.radius_m)
        if returning:
            reasons.append(f"{d:.0f} m from station: motoring back")
    eta = (
        s.time + timedelta(hours=opt.hours_to_go)
        if opt.hours_to_go is not None
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
        returning=returning,
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
