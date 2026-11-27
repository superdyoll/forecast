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


def from_drift(ground):
    """Current while drifting with the motors off: the ground velocity."""
    return ground[0], ground[1]


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


def learn_top_speed(cap, commanded: float, sog: float, stw: float):
    """The hull's real top speed through the water, learnt while motoring.

    Making clearly less over ground than commanded means the boat is at its
    limit: the speed through the water it holds then (from the measured
    power) is its top speed. Making the commanded speed at a higher water
    speed than `cap` shows the cap is too low. Otherwise nothing is learnt.
    `cap` is None until the first observation.
    """
    if sog < commanded - 0.1:
        return stw if cap is None else cap + 0.3 * (stw - cap)
    if cap is not None and stw > cap:
        return stw
    return cap
