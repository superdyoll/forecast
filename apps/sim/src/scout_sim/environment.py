"""The simulated sea and sky: current, wind and cloud. Seeded, so runs repeat.

* Current: a mean drift plus a semi-diurnal (M2, 12.42 h) tidal ellipse,
  the dominant pattern in the northern North Sea.
* Wind: speed and direction as mean-reverting random processes (hourly
  knots, Ornstein-Uhlenbeck), so weather changes over hours, not seconds.
  Wind pushes the hull downwind at `leeway` x wind speed.
* Cloud: the fraction of clear-sky solar that reaches the panels, also
  mean-reverting, clipped to [0.05, 1]. Its average matches the fitted
  cloud factor (~0.6).

* Storm (optional): from storm_start_h for storm_hours, ramping in and out
  over storm_ramp_h, cloud falls to storm_cloud and wind rises to
  storm_wind_ms (an Atlantic low: days of overcast and gales).

Drift (current + leeway) is what the boat feels as SIM_TIDE in SITL.
Vectors are (north, east) in m/s, pointing where the water/wind goes.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime


@dataclass
class EnvConfig:
    seed: int = 1
    current_mean_ms: float = 0.08
    current_mean_toward_deg: float = 30.0
    tide_amp_ms: float = 0.25  # major-axis amplitude
    tide_minor_ratio: float = 0.3
    tide_axis_toward_deg: float = 15.0
    tide_period_h: float = 12.42
    wind_mean_ms: float = 6.0
    wind_sd_ms: float = 2.5
    wind_from_deg: float = 240.0
    wind_dir_sd_deg: float = 40.0
    wind_tau_h: float = 8.0
    leeway: float = 0.03
    cloud_mean: float = 0.6
    cloud_sd: float = 0.25
    cloud_tau_h: float = 5.0
    days: int = 40
    storm_start_h: float | None = None  # hours after the start
    storm_hours: float = 72.0
    storm_ramp_h: float = 6.0
    storm_cloud: float = 0.08  # fraction of clear-sky solar under the storm
    storm_wind_ms: float = 20.0


def env_config(seed: int, storm: str | None, storm_cloud: float) -> EnvConfig:
    """EnvConfig, with a storm from `storm` = 'START_H:HOURS[:RAMP_H]' if given.
    Without RAMP_H it ramps in and out over the default 6 h, but never over
    more than a quarter of the storm, so a short one still reaches full."""
    if not storm:
        return EnvConfig(seed=seed)
    parts = [float(x) for x in storm.split(":")]
    if len(parts) not in (2, 3) or parts[1] <= 0:
        raise ValueError(f"storm {storm!r}: expected START_H:HOURS[:RAMP_H]")
    h0, hours = parts[:2]
    ramp = parts[2] if len(parts) == 3 else min(EnvConfig.storm_ramp_h, hours / 4)
    return EnvConfig(
        seed=seed,
        storm_start_h=h0,
        storm_hours=hours,
        storm_ramp_h=ramp,
        storm_cloud=storm_cloud,
    )


def add_storm_args(ap) -> None:
    ap.add_argument(
        "--storm",
        metavar="START_H:HOURS[:RAMP_H]",
        help="overcast and gales from START_H for HOURS (24:72: day 2, for 3 days)",
    )
    ap.add_argument(
        "--storm-cloud",
        type=float,
        default=0.08,
        help="fraction of clear-sky solar under the storm",
    )


class Environment:
    def __init__(self, cfg: EnvConfig, start: datetime):
        self.cfg, self.start = cfg, start
        rnd = random.Random(cfg.seed)
        n = cfg.days * 24 + 2
        self._wind_spd = _ou(rnd, n, cfg.wind_mean_ms, cfg.wind_sd_ms, cfg.wind_tau_h)
        self._wind_dir = _ou(
            rnd, n, cfg.wind_from_deg, cfg.wind_dir_sd_deg, cfg.wind_tau_h
        )
        self._cloud = _ou(rnd, n, cfg.cloud_mean, cfg.cloud_sd, cfg.cloud_tau_h)
        self._tide_phase = rnd.uniform(0, 2 * math.pi)

    def _hours(self, t: datetime) -> float:
        return max(0.0, (t - self.start).total_seconds() / 3600)

    def current(self, t: datetime) -> tuple[float, float]:
        c = self.cfg
        h = self._hours(t)
        ph = 2 * math.pi * h / c.tide_period_h + self._tide_phase
        major = c.tide_amp_ms * math.cos(ph)
        minor = c.tide_amp_ms * c.tide_minor_ratio * math.sin(ph)
        ax = math.radians(c.tide_axis_toward_deg)
        n = major * math.cos(ax) - minor * math.sin(ax)
        e = major * math.sin(ax) + minor * math.cos(ax)
        m = math.radians(c.current_mean_toward_deg)
        return n + c.current_mean_ms * math.cos(m), e + c.current_mean_ms * math.sin(m)

    def storm(self, t: datetime) -> float:
        """How much of the storm is on at t: 0 (none) to 1 (full)."""
        c = self.cfg
        if c.storm_start_h is None:
            return 0.0
        h = self._hours(t) - c.storm_start_h
        if h <= 0 or h >= c.storm_hours:
            return 0.0
        ramp = max(c.storm_ramp_h, 1e-6)
        return min(1.0, h / ramp, (c.storm_hours - h) / ramp)

    def wind(self, t: datetime) -> tuple[float, float]:
        """(speed m/s, direction it blows FROM, degrees)."""
        h = self._hours(t)
        spd = max(0.0, _interp(self._wind_spd, h))
        k = self.storm(t)
        spd = spd * (1 - k) + self.cfg.storm_wind_ms * k
        return spd, _interp(self._wind_dir, h) % 360

    def drift(self, t: datetime) -> tuple[float, float]:
        cn, ce = self.current(t)
        spd, frm = self.wind(t)
        to = math.radians(frm + 180)
        lw = self.cfg.leeway * spd
        return cn + lw * math.cos(to), ce + lw * math.sin(to)

    def cloud(self, t: datetime) -> float:
        base = min(1.0, max(0.05, _interp(self._cloud, self._hours(t))))
        k = self.storm(t)
        return base * (1 - k) + self.cfg.storm_cloud * k


def _ou(rnd, n, mean, sd, tau_h):
    a = math.exp(-1.0 / tau_h)
    s = sd * math.sqrt(1 - a * a)
    x, out = mean, []
    for _ in range(n):
        out.append(x)
        x = mean + (x - mean) * a + s * rnd.gauss(0, 1)
    return out


def _interp(series, h):
    i = min(int(h), len(series) - 2)
    f = min(max(h - i, 0.0), 1.0)
    return series[i] * (1 - f) + series[i + 1] * f
