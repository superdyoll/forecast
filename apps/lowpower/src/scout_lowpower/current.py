"""The water current, from what the boat can measure. Pure functions.

ArduPilot holds the commanded speed *over ground* (SOG), but propulsion
power depends on speed *through the water* (STW). The difference is the
current (plus wind leeway), which Scout has no sensor for. It shows two ways:

* Under thrust: the load channel measures propulsion power, which gives STW
  (energy.stw_for_power), along the heading. current = ground velocity -
  water velocity.
* Motors off (drifting, paused, disarmed): current = ground velocity.

Vectors are (north, east) in m/s, pointing where the water goes.
"""

from __future__ import annotations

import math

from scout_forecast import energy as E


def from_thrust(p: E.Params, ground, heading_deg: float, propulsion_w: float):
    """Current while motoring: ground velocity minus the water velocity that
    the measured propulsion power holds along the heading."""
    stw = E.stw_for_power(p, propulsion_w)
    h = math.radians(heading_deg)
    return ground[0] - stw * math.cos(h), ground[1] - stw * math.sin(h)


def observe(
    p: E.Params,
    action: str,
    duty: float,
    mode_ok: bool,
    armed: bool,
    ground,
    heading_deg,
    load_w,
):
    """One observation of the current from a window of telemetry, or None.

    `mode_ok`: the autopilot is in the mode the plan asks for. Motoring
    (transit in AUTO, hold in LOITER, armed): from the propulsion power,
    which is the load minus hotel and Starlink at `duty`. Motors off
    (pause or drift in HOLD, or disarmed): the ground velocity itself.
    """
    if action in ("transit", "hold"):
        if not (mode_ok and armed) or heading_deg is None or load_w is None:
            return None
        return from_thrust(p, ground, heading_deg, load_w - E.base_load_w(p, duty))
    if action in ("pause", "drift") and (mode_ok or not armed):
        return tuple(ground)
    return None


FULL_POWER = 0.8  # of the model's top propulsion power: "flat out"
SETTLE_S = 120.0  # boat seconds after a change of plan or leg before averaging
MIN_SPAN_S = 300.0  # the shortest window worth an estimate


def window_s(since_change_s: float, full_s: float) -> float | None:
    """How many boat seconds of telemetry to average, or None for none yet.

    The whole of `full_s` (a Starlink cycle, so its sessions average out),
    but never reaching back past the last change of plan or leg, less
    SETTLE_S: a window across a turn mixes two legs' headings and ground
    velocities, and one across the start mixes in the boat at rest.
    """
    span = min(full_s, since_change_s - SETTLE_S)
    return span if span >= MIN_SPAN_S else None


def cloud_factor(obs) -> float | None:
    """Observed / clear-sky solar over [(time, observed W, clear-sky W), ...]
    (sun up only), or None with fewer than 4 observations."""
    obs = [o for o in obs if o[2] > 20]
    if len(obs) < 4:
        return None
    return sum(o[1] for o in obs) / sum(o[2] for o in obs)


def blend(old, new, weight: float):
    """Exponential smoothing of a current vector (new gets `weight`)."""
    return tuple(o + weight * (n - o) for o, n in zip(old, new))


def along_leg(v_cmd: float, unit, current, stw_max: float):
    """What commanding `v_cmd` over ground means on a leg with direction
    `unit`: returns (speed over ground, speed through the water).

    ArduPilot holds the track: the water velocity has to make up
    v * unit - current. It can't go slower than the water carries it (no
    reverse in AUTO), and it can't exceed stw_max through the water: then it
    makes what it can, and against a strong enough current, no headway.
    """
    along = current[0] * unit[0] + current[1] * unit[1]
    cross = -current[0] * unit[1] + current[1] * unit[0]
    sog = max(v_cmd, along)
    stw = math.hypot(sog - along, cross)
    if stw > stw_max:
        if abs(cross) >= stw_max:
            return min(along, 0.0), stw_max  # can't even hold the track
        sog, stw = along + math.sqrt(stw_max**2 - cross**2), stw_max
    return sog, stw


def learn_top_speed(cap, commanded: float, sog: float, stw: float, full_power: bool):
    """The hull's real top speed through the water, learnt while motoring.

    Making clearly less over ground than commanded at (near) full power
    means the boat is at its limit: the speed through the water it holds
    then (from the measured power) is its top speed. Short of the command at
    low power is not a limit (accelerating, or a window spanning the start).
    Going faster through the water than `cap` shows the cap is too low.
    Otherwise nothing is learnt. `cap` is None until the first observation.
    """
    if full_power and sog < commanded - 0.1:
        return stw if cap is None else cap + 0.3 * (stw - cap)
    if cap is not None and stw > cap:
        return stw
    return cap
