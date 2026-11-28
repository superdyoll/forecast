"""A synthetic Tether CSV for the tests of scout-forecast and scout-web (the
real export is private and never in the repository).

The battery follows the model's own equations with known parameters
(TRUTH), so a fit should find them again. On top of that it carries one
example of each quirk the loader cleans up (see scout_forecast.data):
an Iridium backlog flush, an autopilot restart with zeroed fields,
Globalstar's placeholder battery_pct, centidegree temperatures and an
impossible Iridium speed.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

from . import energy as E
from .data import FLOAT_COLS, INT_COLS

START = datetime(2025, 7, 14, tzinfo=timezone.utc)
LAT, LON = 60.2, 2.2
TRUTH = E.Params(
    solar_k=240.0,
    solar_gamma=1.5,
    solar_offset_h=0.0,
    cloud_default=0.6,
    capacity_wh=2000.0,
    sag_pct_per_a=0.9,
    loss_w=2.0,
)
HOTEL_W = 10.0
STARLINK_W = 22.0
COLUMNS = ["created_at", "communication_mode"] + FLOAT_COLS + INT_COLS
REBOOT_AT = START + timedelta(hours=30)
BACKLOG_AT = START + timedelta(hours=40)


def tether_time(t: datetime, frac_digits: int = 6) -> str:
    """Tether's format: '2025-07-14 00:20:51.060558+00', fraction varying."""
    s = t.strftime("%Y-%m-%d %H:%M:%S.%f")[: 20 + frac_digits]
    return s + "+00"


def _row(t, link, **kw):
    r = {c: "null" for c in COLUMNS}
    r["created_at"] = tether_time(t, frac_digits=1 + t.minute % 6)
    r["communication_mode"] = link
    r.update({k: str(v) for k, v in kw.items()})
    return r


def rows(days: float = 3.0, step_s: int = 300, pct0: float = 70.0) -> list[dict]:
    """An Iridium row every `step_s` (no solar, no battery current); a
    Starlink row alongside it (with both) for the first 15 min of each hour,
    one grid step; plus the quirks. Times are receipt times, in order except
    the backlog flush."""
    out, rest = [], pct0
    n = int(days * 86400 / step_s)
    for k in range(n):
        t = START + timedelta(seconds=k * step_s)
        solar = E.clear_sky_w(TRUTH, t, LAT, LON) * TRUTH.cloud_default
        # AUTO by day, MODE_17 in the evening, HOLD (hotel load only) at night.
        mode = 10 if 6 <= t.hour < 18 else (17 if t.hour < 22 else 4)
        under_way = mode == 10
        load = HOTEL_W + {10: 40.0, 17: 4.5, 4: 0.0}[mode]
        if t.minute < 15:
            load += STARLINK_W
        v = (rest + 400) / 20
        net = solar - load - TRUTH.loss_w
        rest, pct, net = E.battery_step(rest, net, TRUTH, v, step_s / 3600)
        v = (pct + 400) / 20
        common = dict(
            latitude_deg=LAT,
            longitude_deg=LON,
            heading_deg=350 if k % 2 else 10,  # circular mean is north
            course_deg=0,
            groundspeed_ms=0.8 if under_way else 0.1,
            voltage_battery1_v=round(v, 3),
            current_battery2_a=round(load / v, 3),
            temperature_c=3120,  # centidegrees: 31.2 C
            battery_pct=round(pct, 1),
            custom_mode=mode,
            custom0_arming=1,
            wp_num=1,
        )
        if t.minute < 15:  # Starlink comes in sessions, like the real file
            out.append(
                _row(
                    t,
                    "direct",
                    **common,
                    current_battery1_a=round(net / v, 3),
                    voltage_battery2_v=round(v, 3),
                    voltage_battery3_v=40.0,
                    current_battery3_a=round(solar / 40.0, 3),
                )
            )
        out.append(_row(t + timedelta(seconds=7), "imt", **common))
        if k % 12 == 0:
            out.append(
                _row(
                    t + timedelta(seconds=11),
                    "globalstar",
                    latitude_deg=LAT,
                    longitude_deg=LON,
                    battery_pct=0.0,  # always a placeholder on Globalstar
                )
            )
    # An autopilot restart: mode 16, position 0,0, zeroed sensors.
    out.append(
        _row(
            REBOOT_AT + timedelta(seconds=30),
            "imt",
            latitude_deg=0,
            longitude_deg=0,
            groundspeed_ms=0,
            voltage_battery1_v=0,
            battery_pct=0,
            temperature_c=0,
            custom_mode=16,
            custom0_arming=0,
        )
    )
    # An Iridium backlog flush: rows 1 s apart, one with an impossible speed.
    for j in range(3):
        out.append(
            _row(
                BACKLOG_AT + timedelta(seconds=40 + j),
                "imt",
                latitude_deg=LAT,
                longitude_deg=LON,
                groundspeed_ms=7.4 if j == 1 else 0.6,
                voltage_battery1_v=24.0,
                battery_pct=80.0,
                custom_mode=10,
            )
        )
    return out


def write(path, **kw) -> str:
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows(**kw))
    return str(path)
