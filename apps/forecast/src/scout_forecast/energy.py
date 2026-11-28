"""Physical energy-balance model for Scout. Standard library only.

    battery power = solar - load - losses
    stored energy += battery power * dt       (capped when full, floored at empty)
    battery_pct    = stored % + sag * battery current

Every component is fitted from the telemetry (see REPORT.md):

* Load: the channel Tether calls "thruster" is the whole boat's load and is
  reported on Starlink *and* Iridium, so it is known at every step. For a
  hindcast with a known operating plan the recorded load is used. A blind
  forecast holds the mean load of the previous few hours. The load components
  (hotel, Starlink, propulsion by mode and speed) are fitted for the energy
  breakdown and for the low-power mode's "what if" planning.
* Solar: clear-sky curve k * sin(sun elevation)^gamma, using the boat's
  position and a fitted clock offset (positions/times in the file are
  shifted), scaled by a cloud factor. The factor is the observed/clear-sky
  ratio over the last 24 h where solar was reported, relaxing towards the
  training-set average with time constant `cloud_tau_h`.
* Battery: battery_pct is 20*V - 400, a voltage reading. It is modelled as
  a "rested" level that moves with net energy (capacity_wh per 100 points),
  plus an instantaneous sag/rise of `sag_pct_per_a` per amp of battery
  current. capacity_wh and sag are fitted by minimising multi-step forecast
  error on the training data.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .data import load, resample

log = logging.getLogger("scout_forecast")
STEP_S = 900
STEP_H = STEP_S / 3600
STEPS_PER_H = 3600 // STEP_S
NOMINAL_V = 23.5  # battery voltage to assume when none is reported
UNDER_WAY_MODES = (10, 17)  # AUTO and MODE_17 (looks like station-keeping)
# Columns that must be present for a step to be usable as forecast
# input/target.
REQUIRED = ("pct", "volt", "load_w", "speed", "armed", "under_way")


# --------------------------------------------------------------------------- grid
def build_grid(csv_path: str, max_gap_steps: int = 4) -> dict:
    """15-minute grid of the quantities the model uses. Missing = None."""
    buckets = resample(load(csv_path, include_globalstar=False), STEP_S)
    if not buckets:
        raise SystemExit(f"no usable rows in {csv_path}")
    t0 = int(buckets[0]["time"].timestamp())
    n = (int(buckets[-1]["time"].timestamp()) - t0) // STEP_S + 1
    keys = (
        "pct volt load_w speed armed mode under_way solar_w battery_w lat lon"
    ).split()
    g = {k: [None] * n for k in keys}
    g["starlink"] = [False] * n
    g["times"] = [
        datetime.fromtimestamp(t0 + i * STEP_S, timezone.utc) for i in range(n)
    ]
    for b in buckets:
        i = (int(b["time"].timestamp()) - t0) // STEP_S
        g["pct"][i] = b["battery_pct"]
        g["volt"][i] = b["battery_v"]
        g["load_w"][i] = b["thruster_w"]  # whole-boat load (see REPORT.md)
        g["speed"][i] = b["speed_ms"]
        g["armed"][i] = b["armed"]
        g["mode"][i] = b["mode"]
        if b["mode"] is not None:
            g["under_way"][i] = 1 if b["mode"] in UNDER_WAY_MODES else 0
        g["solar_w"][i] = b["solar_w"]
        g["battery_w"][i] = b["battery_w"]
        g["lat"][i], g["lon"][i] = b["lat"], b["lon"]
        g["starlink"][i] = b["n_direct"] > 0

    for k in ("pct", "volt", "load_w", "speed"):
        g[k] = fill_gaps(g[k], max_gap_steps, hold=False)
    for k in ("armed", "mode", "under_way"):
        g[k] = fill_gaps(g[k], max_gap_steps, hold=True)
    # Position only feeds sun elevation; hold it across any gap.
    for k in ("lat", "lon"):
        g[k] = _hold_everywhere(g[k])
    return g


def fill_gaps(x: list, max_gap: int, hold: bool) -> list:
    """Interpolate (or hold the last value) across None runs <= max_gap."""
    x = list(x)
    i, n = 0, len(x)
    while i < n:
        if x[i] is not None:
            i += 1
            continue
        j = i
        while j < n and x[j] is None:
            j += 1
        if 0 < i and j < n and j - i <= max_gap:
            for k in range(i, j):
                if hold:
                    x[k] = x[i - 1]
                else:
                    f = (k - i + 1) / (j - i + 1)
                    x[k] = x[i - 1] + f * (x[j] - x[i - 1])
        i = j
    return x


def _hold_everywhere(x: list) -> list:
    x = list(x)
    last = next((v for v in x if v is not None), 0.0)
    for i, v in enumerate(x):
        x[i] = last = v if v is not None else last
    return x


def usable_origins(g: dict, lookback: int, horizon: int) -> list[int]:
    """Origins i whose steps [i-lookback, i+horizon) all have REQUIRED data.

    Origin i forecasts steps [i, i+horizon) from the state at step i-1.
    """
    n = len(g["times"])
    ok = [all(g[k][j] is not None for k in REQUIRED) for j in range(n)]
    csum = [0]
    for v in ok:
        csum.append(csum[-1] + v)
    span = lookback + horizon
    return [
        i
        for i in range(lookback, n - horizon + 1)
        if csum[i + horizon] - csum[i - lookback] == span
    ]


# ------------------------------------------------------------------------- solar
def sun_elevation(t: datetime, lat: float, lon: float) -> float:
    """Solar elevation in degrees (NOAA approximation, ~0.5 deg accuracy)."""
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (
        0.006918
        - 0.399912 * math.cos(g)
        + 0.070257 * math.sin(g)
        - 0.006758 * math.cos(2 * g)
        + 0.000907 * math.sin(2 * g)
    )
    eot = 229.18 * (
        0.000075
        + 0.001868 * math.cos(g)
        - 0.032077 * math.sin(g)
        - 0.014615 * math.cos(2 * g)
        - 0.040849 * math.sin(2 * g)
    )
    ha = math.radians((hour * 60 + eot + 4 * lon) / 4 - 180)
    la = math.radians(lat)
    s = math.sin(la) * math.sin(decl) + math.cos(la) * math.cos(decl) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


def sin_elevation_series(g: dict, offset_h: float) -> list[float]:
    """max(0, sin(elevation)) at the centre of each step, clock shifted."""
    half = timedelta(seconds=STEP_S / 2) + timedelta(hours=offset_h)
    return [
        max(0.0, math.sin(math.radians(sun_elevation(t + half, la, lo))))
        for t, la, lo in zip(g["times"], g["lat"], g["lon"])
    ]


# ------------------------------------------------------------------------ params
@dataclass
class Params:
    solar_k: float = 250.0  # clear-sky watts at sin(elev)=1
    solar_gamma: float = 1.0
    solar_offset_h: float = 0.0
    cloud_default: float = 0.5  # mean observed / clear-sky
    cloud_tau_h: float = 12.0
    capacity_wh: float = 1900.0  # energy per 100 battery_pct points
    sag_pct_per_a: float = 1.2
    loss_w: float = 2.0
    persist_hours: float = 6.0  # blind plan: hold mean load of the last N h
    load: dict = field(default_factory=dict)  # fitted components, for reports

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Params:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


def _allowed(n: int, exclude: list[tuple[int, int]]) -> list[bool]:
    ok = [True] * n
    for lo, hi in exclude:
        for j in range(max(lo, 0), min(hi, n)):
            ok[j] = False
    return ok


def fit(
    g: dict,
    origins: list[int],
    horizon: int,
    exclude: list[tuple[int, int]] = (),
    stride: int = 4,
) -> Params:
    """Fit every parameter on steps outside `exclude` (half-open index ranges).

    `origins` must already be restricted to windows that don't overlap the
    excluded spans.
    """
    p = Params()
    n = len(g["times"])
    ok = _allowed(n, list(exclude))
    _fit_solar(g, ok, p)
    _fit_load(g, ok, p)
    sinel = sin_elevation_series(g, p.solar_offset_h)
    _fit_battery(g, origins[::stride] or origins, horizon, sinel, p)
    return p


def _fit_solar(g, ok, p: Params) -> None:
    obs = [j for j, v in enumerate(g["solar_w"]) if v is not None and ok[j]]
    if len(obs) < 20:
        return  # keep defaults
    best = None
    for off in [x / 4 for x in range(-12, 9)]:  # -3 h .. +2 h
        s = sin_elevation_series(g, off)
        for gamma in (1.0, 1.25, 1.5, 1.75, 2.0, 2.5):
            xs = [s[j] ** gamma for j in obs]
            ys = [g["solar_w"][j] for j in obs]
            c = _corr(xs, ys)
            if best is None or c > best[0]:
                best = (c, off, gamma, s)
    _, p.solar_offset_h, p.solar_gamma, s = best
    # Clear-sky scale: 90th percentile of P / sin^gamma when the sun is up.
    ratios = sorted(
        g["solar_w"][j] / s[j] ** p.solar_gamma for j in obs if s[j] > 0.17
    )  # elevation > ~10 deg
    if ratios:
        p.solar_k = ratios[int(0.9 * (len(ratios) - 1))]
    clear = [p.solar_k * s[j] ** p.solar_gamma for j in obs]
    tot_clear = sum(c for c in clear if c > 20)
    tot_obs = sum(g["solar_w"][j] for j, c in zip(obs, clear) if c > 20)
    if tot_clear > 0:
        p.cloud_default = tot_obs / tot_clear


def _fit_load(g, ok, p: Params) -> None:
    n = len(g["times"])
    L = g["load_w"]

    def med(pred):
        v = [L[j] for j in range(n) if ok[j] and L[j] is not None and pred(j)]
        return (statistics.median(v), len(v)) if v else (None, 0)

    hotel, n_h = med(lambda j: g["under_way"][j] == 0 and not g["starlink"][j])
    with_sl, _ = med(lambda j: g["under_way"][j] == 0 and g["starlink"][j])
    comps = {"hotel_w": hotel, "hotel_n": n_h}
    if hotel is not None and with_sl is not None:
        comps["starlink_w"] = with_sl - hotel
    for m, name in ((10, "auto"), (17, "mode17")):
        v, k = med(lambda j: g["mode"][j] == m and not g["starlink"][j])
        if v is not None and hotel is not None:
            comps[f"{name}_propulsion_w"] = v - hotel
            comps[f"{name}_n"] = k
    # Propulsion vs speed over ground (AUTO, Starlink off), 0.2 m/s bins.
    curve = []
    for lo in [x / 10 for x in range(0, 20, 2)]:
        v, k = med(
            lambda j: g["mode"][j] == 10
            and not g["starlink"][j]
            and g["speed"][j] is not None
            and lo <= g["speed"][j] < lo + 0.2
        )
        if k >= 10 and hotel is not None:
            curve.append([round(lo + 0.1, 2), round(v - hotel, 1), k])
    comps["auto_propulsion_by_sog"] = curve  # [m/s, W, n]
    sog = [
        g["speed"][j]
        for j in range(n)
        if ok[j]
        and g["mode"][j] == 10
        and not g["starlink"][j]
        and g["speed"][j] is not None
    ]
    if sog:
        comps["auto_sog_median"] = statistics.median(sog)
    p.load = comps
    # Unaccounted power when all three channels are present.
    res = [
        g["solar_w"][j] - L[j] - g["battery_w"][j]
        for j in range(n)
        if ok[j] and None not in (g["solar_w"][j], L[j], g["battery_w"][j])
    ]
    if len(res) > 20:
        p.loss_w = max(0.0, statistics.median(res))


def _fit_battery(g, origins, horizon, sinel, p: Params) -> None:
    if not origins:
        return

    def score(cap, sag):
        q = Params(**{**p.to_dict(), "capacity_wh": cap, "sag_pct_per_a": sag})
        err, cnt = 0.0, 0
        for i in origins:
            pred = simulate(g, q, i, horizon, sinel=sinel)["pct"]
            for k, v in enumerate(pred):
                a = g["pct"][i + k]
                if a is not None:
                    err += abs(v - a)
                    cnt += 1
        return err / max(cnt, 1)

    best = min(
        (score(c, s), c, s)
        for c in range(600, 4201, 300)
        for s in (0, 0.6, 1.2, 1.8, 2.4)
    )
    _, c0, s0 = best
    # Refine: capacity to 100 Wh, sag to 0.1 point/A (0.3 steps missed the
    # optimum between them on test data with a known answer).
    sags = sorted({round(max(0.0, s0 + d / 10), 2) for d in range(-3, 4)})
    best = min(
        (score(c, s), c, s)
        for c in range(max(300, c0 - 200), c0 + 201, 100)
        for s in sags
    )
    _, p.capacity_wh, p.sag_pct_per_a = best


# --------------------------------------------------------------------- simulate
def cloud_factor(g, i: int, sinel: list[float], p: Params) -> float:
    """Observed/clear-sky solar over the 24 h before step i, or the default."""
    obs = clear = 0.0
    k = 0
    for j in range(max(0, i - 24 * STEPS_PER_H), i):
        c = p.solar_k * sinel[j] ** p.solar_gamma
        if g["solar_w"][j] is not None and c > 20:
            obs += g["solar_w"][j]
            clear += c
            k += 1
    return obs / clear if k >= 4 and clear > 0 else p.cloud_default


def persist_load(g, i: int, p: Params) -> float:
    n = int(p.persist_hours * STEPS_PER_H)
    v = [x for x in g["load_w"][max(0, i - n) : i] if x is not None]
    if v:
        return sum(v) / len(v)
    return p.load.get("hotel_w") or 10.0


def simulate(
    g: dict,
    p: Params,
    i: int,
    horizon: int,
    plan: str = "actual",
    sinel: list[float] | None = None,
) -> dict:
    """Forecast steps [i, i+horizon) from the state at step i-1.

    plan="actual": use the recorded load (a known operating plan).
    plan="persist": hold the mean load of the last `persist_hours`.
    Steps beyond the end of the grid use the persisted load and the clock.
    """
    if sinel is None:
        sinel = sin_elevation_series(g, p.solar_offset_h)
    n = len(g["times"])
    pct0, v0 = g["pct"][i - 1], g["volt"][i - 1] or NOMINAL_V
    cf0 = cloud_factor(g, i, sinel, p)
    hold = persist_load(g, i, p)

    def solar_at(j, h):
        cf = p.cloud_default + (cf0 - p.cloud_default) * math.exp(-h / p.cloud_tau_h)
        if j < n:
            return p.solar_k * sinel[j] ** p.solar_gamma * cf
        # Beyond the grid: the clock and the last position.
        mid = g["times"][-1] + timedelta(seconds=STEP_S * (j - n + 1.5))
        return clear_sky_w(p, mid, g["lat"][-1], g["lon"][-1]) * cf

    # Starting rested level: remove the sag/rise present at the last reading.
    if g["battery_w"][i - 1] is not None:
        net0 = g["battery_w"][i - 1]
    else:
        load0 = g["load_w"][i - 1] if g["load_w"][i - 1] is not None else hold
        net0 = solar_at(i - 1, 0) - load0 - p.loss_w
    rest = pct0 - p.sag_pct_per_a * net0 / v0

    out = {"pct": [], "solar_w": [], "load_w": [], "net_w": []}
    for k in range(horizon):
        j = i + k
        solar = solar_at(j, (k + 0.5) * STEP_H)
        if plan == "actual" and j < n and g["load_w"][j] is not None:
            load_w = g["load_w"][j]
        else:
            load_w = hold
        rest, pct, net = battery_step(rest, solar - load_w - p.loss_w, p, v0, STEP_H)
        out["pct"].append(pct)
        out["solar_w"].append(solar)
        out["load_w"].append(load_w)
        out["net_w"].append(net)
    return out


def battery_step(rest: float, net: float, p: Params, v0: float, step_h: float):
    """Advance the rested level by one step of net power (W).

    Returns (rest, observed battery_pct, net actually stored).
    """
    if rest >= 100 and net > 0:
        net = 0.0  # charger full: surplus solar is curtailed
    rest = min(100.0, max(0.0, rest + 100 * net * step_h / p.capacity_wh))
    pct = min(100.0, max(0.0, rest + p.sag_pct_per_a * net / v0))
    return rest, pct, net


# ----------------------------------------------------------- live projection
# Defaults used when params.json lacks a fitted component (values from the
# 17-day fit; see REPORT.md).
LOAD_DEFAULTS = {
    "hotel_w": 10.0,
    "starlink_w": 22.0,
    "auto_propulsion_w": 60.0,
    "auto_sog_median": 0.8,
    "mode17_propulsion_w": 5.0,
    # AUTO propulsion over ground levels off at ~73 W from 1.1 m/s: read as the
    # throttle limit, i.e. the boat's top speed through the water.
    "max_propulsion_w": 73.0,
}


def fitted_params() -> Params:
    """The model fitted on all 17 days of telemetry, shipped with the package
    (fitted_params.json). The default whenever no params.json is configured."""
    from importlib.resources import files

    text = files("scout_forecast").joinpath("fitted_params.json").read_text()
    return Params.from_dict(json.loads(text))


def load_params(path) -> Params:
    """params.json at `path`, else (missing, unreadable or None) the shipped fit.
    Never raises: under systemd a bad file must not crash-loop the planner."""
    if path:
        try:
            return Params.from_dict(json.loads(Path(path).read_text()))
        except (OSError, ValueError, TypeError) as e:
            log.error(
                "can't read energy params %s (%s); using the shipped fit", path, e
            )
    return fitted_params()


def load_component(p: Params, name: str) -> float:
    v = p.load.get(name)
    return float(v) if v is not None else LOAD_DEFAULTS[name]


def base_load_w(p: Params, starlink_duty: float) -> float:
    """Hotel load plus Starlink at the given duty: everything but propulsion."""
    return (
        load_component(p, "hotel_w") + load_component(p, "starlink_w") * starlink_duty
    )


def max_propulsion_w(p: Params) -> float:
    """The most propulsion AUTO ever drew: the plateau of the fitted
    power-over-ground bins (with at least 20 steps), else the default."""
    bins = p.load.get("auto_propulsion_by_sog") or []
    seen = [w for _, w, n in bins if n >= 20]
    return max(seen) if seen else LOAD_DEFAULTS["max_propulsion_w"]


def _cubic_ref(p: Params) -> tuple[float, float]:
    """The point the propulsion cubic is pinned to: (speed m/s, power W)."""
    return (
        max(load_component(p, "auto_sog_median"), 0.1),
        max(load_component(p, "auto_propulsion_w"), 1e-6),
    )


def stw_for_power(p: Params, watts: float) -> float:
    """Speed through the water that `watts` of propulsion holds: the inverse
    of propulsion_w. Used to read speed through the water off the load."""
    ref_v, ref_w = _cubic_ref(p)
    return ref_v * (max(watts, 0.0) / ref_w) ** (1 / 3)


def max_stw_ms(p: Params) -> float:
    """The boat's top speed through the water (~0.85 m/s with the fitted model)."""
    return stw_for_power(p, max_propulsion_w(p))


def propulsion_w(p: Params, speed_ms: float) -> float:
    """Propulsion power to hold `speed_ms` through the water.

    Drag on a displacement hull goes as v^2, so power goes as v^3. The curve
    is pinned to the fitted AUTO propulsion at the median AUTO speed. The
    speed-over-ground curve in params is not used directly: it flattens above
    ~0.7 m/s because current, not thrust, adds the extra speed.
    """
    ref_v, ref_w = _cubic_ref(p)
    return ref_w * (max(speed_ms, 0.0) / ref_v) ** 3


def clear_sky_w(p: Params, t: datetime, lat: float, lon: float) -> float:
    e = sun_elevation(t + timedelta(hours=p.solar_offset_h), lat, lon)
    s = max(0.0, math.sin(math.radians(e)))
    return p.solar_k * s**p.solar_gamma


def project(
    p: Params,
    t0: datetime,
    lat: float,
    lon: float,
    pct0: float,
    volt0: float,
    load_fn,
    hours: float,
    cloud: float | None = None,
    net0: float | None = None,
    step_s: int = STEP_S,
) -> dict:
    """Forward-simulate the battery from a live state under a load plan.

    load_fn(k, t) gives the boat's load in W for step k, which starts at t.
    `cloud` is the current observed/clear-sky ratio (None: training average);
    it relaxes to the average with time constant cloud_tau_h. `net0` is the
    battery power at t0 if measured, used to remove the current sag/rise.
    Same battery equations as simulate().
    """
    step_h = step_s / 3600
    cf0 = p.cloud_default if cloud is None else cloud
    v0 = volt0 or NOMINAL_V
    if net0 is None:
        net0 = clear_sky_w(p, t0, lat, lon) * cf0 - load_fn(0, t0) - p.loss_w
    rest = pct0 - p.sag_pct_per_a * net0 / v0
    out = {"time": [], "pct": [], "solar_w": [], "load_w": [], "net_w": []}
    for k in range(int(math.ceil(hours / step_h))):
        t = t0 + timedelta(seconds=k * step_s)
        h = (k + 0.5) * step_h
        cf = p.cloud_default + (cf0 - p.cloud_default) * math.exp(-h / p.cloud_tau_h)
        solar = clear_sky_w(p, t + timedelta(seconds=step_s / 2), lat, lon) * cf
        load_w = load_fn(k, t)
        rest, pct, net = battery_step(rest, solar - load_w - p.loss_w, p, v0, step_h)
        out["time"].append(t + timedelta(seconds=step_s))
        out["pct"].append(pct)
        out["solar_w"].append(solar)
        out["load_w"].append(load_w)
        out["net_w"].append(net)
    return out


def _corr(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx > 0 and syy > 0 else -1.0
