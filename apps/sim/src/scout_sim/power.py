"""Simulated electrical system: thruster and hotel load, solar input, battery.

Uses the same equations as the forecast model, but with its own "true"
parameters, deliberately ~10 % off the fitted ones (capacity, panel output,
hotel load, propulsion), so the service has to cope with model error rather
than forecasting a world built from its own assumptions.

* Propulsion power depends on speed *through the water* (ground velocity
  minus drift): pushing into a current costs energy, drifting with it is free.
* Starlink is on for `starlink_duty` of each `starlink_period_s`.
* battery_pct = 20 x V - 400, as Tether shows it; voltage includes sag/rise.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from scout_forecast import energy as E


@dataclass
class PowerTruth:
    # Against the shipped fit (fitted_params.json, run 20261127T233027Z); keep
    # these offsets if it is refitted.
    capacity_wh: float = 2500.0  # fitted: 2,300 (+9 %)
    sag_pct_per_a: float = 0.8  # fitted: 0.8
    solar_k: float = 380.0  # fitted: ~361 (+5 %)
    solar_gamma: float = 2.5  # fitted: 2.5
    solar_offset_h: float = -0.25  # fitted: -0.75 (sun half an hour off)
    hotel_w: float = 11.0  # fitted: 9.9
    starlink_w: float = 22.0
    propulsion_scale: float = 1.1  # x the fitted cubic
    esc_idle_w: float = 1.5  # armed, no thrust
    loss_w: float = 2.0
    starlink_duty: float = 0.25
    starlink_period_s: float = 3600.0
    volt_noise: float = 0.01


class PowerModel:
    def __init__(
        self, truth: PowerTruth, model: E.Params, pct0: float = 70.0, seed: int = 1
    ):
        self.t, self.model = truth, model
        self.p = E.Params(
            capacity_wh=truth.capacity_wh,
            sag_pct_per_a=truth.sag_pct_per_a,
            solar_k=truth.solar_k,
            solar_gamma=truth.solar_gamma,
            solar_offset_h=truth.solar_offset_h,
            loss_w=truth.loss_w,
        )
        self.rest = pct0
        self.pct = pct0
        self.volt = 20 + pct0 / 20
        self.rnd = random.Random(seed)

    def starlink_on(self, t: datetime) -> bool:
        period = self.t.starlink_period_s
        return (t.timestamp() % period) < self.t.starlink_duty * period

    def step(self, t, dt_s, lat, lon, u_water, armed, cloud) -> dict:
        tr = self.t
        starlink = self.starlink_on(t)
        load = tr.hotel_w + (tr.starlink_w if starlink else 0.0)
        prop = 0.0
        if armed:
            prop = tr.esc_idle_w + tr.propulsion_scale * E.propulsion_w(
                self.model, u_water
            )
        load += prop
        solar = E.clear_sky_w(self.p, t, lat, lon) * cloud
        v0 = self.volt
        self.rest, self.pct, net = E.battery_step(
            self.rest, solar - load - tr.loss_w, self.p, v0, dt_s / 3600
        )
        v = 20 + self.pct / 20 + self.rnd.gauss(0, tr.volt_noise)
        self.volt = round(v, 2)
        return {
            "load_w": load,
            "prop_w": prop,
            "solar_w": solar,
            "battery_w": net,
            "volt": self.volt,
            "pct": self.pct,
            "starlink": starlink,
        }
