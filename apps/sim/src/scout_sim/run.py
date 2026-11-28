"""scout-sim: the environment and power system around ArduPilot SITL.

    export SCOUT_CLOCK=$(scout-sim clock --start 2025-07-23T06:00Z)
    scout-sim run --sitl tcp:127.0.0.1:5762 --power-out udpout:127.0.0.1:14601 --arm

ArduPilot 4.0 SITL (the radarku/ardupilot-sitl image) has no external-physics
backend, but its boat model takes a water-current vector (SIM_TIDE_SPEED/DIR,
applied while armed). So this process:

  * computes drift (current + wind leeway) from environment.py and pushes it
    into SITL as SIM_TIDE every few seconds: ArduPilot's real navigation and
    speed controllers then have to fight it;
  * reads the boat's ground velocity back, takes away the drift to get speed
    through the water, and runs power.py (thruster + hotel + Starlink load,
    solar from sun position and cloud, battery);
  * sends BATTERY_STATUS id 0 (battery), 1 (whole-boat load) and 2 (solar),
    as the boat's power system does, to the low-power service's power link;
  * writes ground truth (sim-truth.jsonl) for comparing forecasts.

Its first link to SITL is separate from the service's (port 5762 vs 5760).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import time
from pathlib import Path

from scout_forecast import clock
from scout_forecast import energy as E
from scout_forecast.env import load_dotenv
from scout_lowpower import mavlink as M

from . import fast
from .environment import Environment, add_storm_args, env_config
from .power import PowerModel, PowerTruth

log = logging.getLogger("scout_sim")
SITL = {"target_system": 1, "target_component": 1}


class Sim:
    def __init__(self, a, model: E.Params):
        self.a = a
        self.sitl = M.Connection(
            a.sitl, sysid=1, compid=M.MAV_COMP_ID_ONBOARD_COMPUTER + 1
        )
        self.out = M.Connection(
            a.power_out, sysid=1, compid=M.MAV_COMP_ID_ONBOARD_COMPUTER + 2
        )
        start = clock.now()
        self.env = Environment(env_config(a.seed, a.storm, a.storm_cloud), start)
        self.power = PowerModel(PowerTruth(), model, pct0=a.pct0, seed=a.seed)
        self.truth_path = Path(a.truth)
        self.lat = self.lon = None
        self.vel = (0.0, 0.0)
        self.heading = self.throttle = None  # VFR_HUD: degrees, %
        self.armed = False
        self.mode = None
        self.boot_ms = None
        self.last_hb = None
        self.ready_at = None
        self.armed_once = False
        self.last = {}
        self.sim_t = start
        self.opens = 0
        self._pace_ref = None  # (boot ms, monotonic s) for check_pace
        self.stop = False

    def every(self, key, period, now):
        if now - self.last.get(key, -1e9) >= period:
            self.last[key] = now
            return True
        return False

    def run(self):
        log.info(
            "scout-sim: sitl=%s power-out=%s clock=%s (x%g) truth=%s",
            self.a.sitl,
            self.a.power_out,
            self.sim_t.isoformat(timespec="seconds"),
            clock.rate(),
            self.truth_path,
        )
        while not self.stop:
            for m in self.sitl.poll(0.2):
                self.on_message(m)
            now = time.monotonic()
            if self.sitl.opens != self.opens:
                self.opens = self.sitl.opens
                self.request_streams()
            if self.every("heartbeat", 1.0, now):
                self.sitl.send(
                    "HEARTBEAT",
                    type=M.MAV_TYPE_ONBOARD_CONTROLLER,
                    autopilot=M.MAV_AUTOPILOT_INVALID,
                    system_status=4,
                    mavlink_version=3,
                )
            if self.every("step", 1.0, now):
                self.step()
            if self.every("tide", self.a.tide_every, now):
                self.push_drift()
            if (
                self.a.arm
                and not self.armed_once
                and self.ready_at
                and now > self.ready_at
            ):
                if self.armed:
                    self.armed_once = True
                    log.info("vehicle armed (operator action done)")
                elif self.every("arm", 5.0, now):
                    log.info("arming vehicle (operator action)")
                    self.command(M.MAV_CMD_COMPONENT_ARM_DISARM, 1)
        self.sitl.close("(scout-sim stopping)")
        self.out.close("(scout-sim stopping)")

    def command(self, cmd, *p):
        self.sitl.command(SITL, cmd, *p)

    def request_streams(self):
        for name, hz in (("GLOBAL_POSITION_INT", 2), ("SYS_STATUS", 1), ("VFR_HUD", 1)):
            self.command(M.MAV_CMD_SET_MESSAGE_INTERVAL, M.MESSAGES[name].id, 1e6 / hz)
        # Keep SITL's physics in step with the shared clock's rate.
        self.sitl.param_set(SITL, "SIM_SPEEDUP", clock.rate())

    def on_message(self, m):
        n = m["name"]
        now = time.monotonic()
        if n == "HEARTBEAT" and m["autopilot"] != M.MAV_AUTOPILOT_INVALID:
            if self.last_hb is None or now - self.last_hb > 5:
                log.info(
                    "SITL heartbeat (mode %s)",
                    M.ROVER_MODES.get(m["custom_mode"], m["custom_mode"]),
                )
                self.request_streams()
                self.ready_at = now + self.a.arm_delay
            self.last_hb = now
            self.armed = bool(m["base_mode"] & M.MAV_MODE_FLAG_SAFETY_ARMED)
            self.mode = m["custom_mode"]
        elif n == "GLOBAL_POSITION_INT" and (m["lat"] or m["lon"]):
            if self.boot_ms is not None and m["time_boot_ms"] + 200 < self.boot_ms:
                log.warning("SITL restarted: re-requesting telemetry")
                self.request_streams()
            self.boot_ms = m["time_boot_ms"]
            self.check_pace(m["time_boot_ms"], now)
            self.lat, self.lon = m["lat"] / 1e7, m["lon"] / 1e7
            self.vel = (m["vx"] / 100, m["vy"] / 100)
        elif n == "VFR_HUD":  # for the truth: does the hull move along its yaw?
            self.heading, self.throttle = m["heading"], m["throttle"]
        elif n == "STATUSTEXT" and self.a.verbose:
            log.info("SITL: %s", m["text"])

    def check_pace(self, boot_ms: int, now: float):
        """Warn when SITL's physics can't keep up with the shared clock (seen
        with x86 emulation on Apple silicon: x5.5 at SIM_RATE=20). The boat
        then covers ground too slowly in clock time: arrivals look late."""
        ref = self._pace_ref
        if ref is None or boot_ms < ref[0]:
            self._pace_ref = (boot_ms, now)
            return
        real = now - ref[1]
        if real < 60:
            return
        achieved = (boot_ms - ref[0]) / 1000 / real
        want = clock.rate()
        if achieved < 0.8 * want:
            log.warning(
                "SITL runs at x%.1f but the clock assumes x%g: the boat lags the "
                "clock (arrivals look late); use a lower SIM_RATE (e.g. %d)",
                achieved,
                want,
                max(1, int(achieved)),
            )
        self._pace_ref = (boot_ms, now)

    def push_drift(self):
        dn, de = self.env.drift(clock.now())
        spd = math.hypot(dn, de)
        # SITL: tide velocity north = -cos(dir) * speed, i.e. dir = where it comes FROM.
        frm = math.degrees(math.atan2(-de, -dn)) % 360 if spd > 1e-3 else 0.0
        self.sitl.param_set(SITL, "SIM_TIDE_SPEED", spd)
        self.sitl.param_set(SITL, "SIM_TIDE_DIR", frm)

    def step(self):
        t = clock.now()
        dt = max(0.0, (t - self.sim_t).total_seconds())
        self.sim_t = t
        lat = self.lat if self.lat is not None else self.a.lat
        lon = self.lon if self.lon is not None else self.a.lon
        dn, de = self.env.drift(t)
        # Through-water speed. SITL only applies drift while armed.
        if self.armed:
            u = math.hypot(self.vel[0] - dn, self.vel[1] - de)
        else:
            u = math.hypot(*self.vel)
        cloud = self.env.cloud(t)
        p = self.power.step(t, dt, lat, lon, u, self.armed, cloud)
        v = p["volt"]
        mv = int(v * 1000)

        def amps(w, volts):  # centiamps, as BATTERY_STATUS carries them
            return int(round(w / volts * 100))

        self.out.send(
            "BATTERY_STATUS",
            id=0,
            voltages=[mv] + [65535] * 9,
            current_battery=amps(-p["battery_w"], v),
            battery_remaining=int(p["pct"]),
        )
        self.out.send(
            "BATTERY_STATUS",
            id=1,
            voltages=[mv] + [65535] * 9,
            current_battery=amps(p["load_w"], v),
        )
        panel_v = 45.0 if p["solar_w"] > 1 else 5.0
        self.out.send(
            "BATTERY_STATUS",
            id=2,
            voltages=[int(panel_v * 1000)] + [65535] * 9,
            current_battery=amps(p["solar_w"], panel_v),
        )
        if self.every(
            "truth", self.a.truth_every / max(clock.rate(), 1e-6), time.monotonic()
        ):
            wspd, wfrom = self.env.wind(t)
            cn, ce = self.env.current(t)
            rec = {
                "time": t.isoformat(),
                "lat": lat,
                "lon": lon,
                "sog": round(math.hypot(*self.vel), 3),
                "heading": self.heading,
                "throttle": self.throttle,
                "u_water": round(u, 3),
                "current_n": round(cn, 3),
                "current_e": round(ce, 3),
                "drift_n": round(dn, 3),
                "drift_e": round(de, 3),
                "wind_ms": round(wspd, 2),
                "wind_from": round(wfrom),
                "cloud": round(cloud, 3),
                "storm": round(self.env.storm(t), 2),
                "armed": self.armed,
                "mode": M.ROVER_MODES.get(self.mode, self.mode),
                **{
                    k: (round(x, 2) if isinstance(x, float) else x)
                    for k, x in p.items()
                },
            }
            self.truth_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.truth_path, "a") as fh:
                fh.write(json.dumps(rec) + "\n")


def main(argv=None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(prog="scout-sim", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("clock", help="print a SCOUT_CLOCK value to export")
    c.add_argument(
        "--start", required=True, help="simulated start, e.g. 2025-07-23T06:00Z"
    )
    c.add_argument(
        "--rate", type=float, default=1.0, help="simulated seconds per real second"
    )
    r = sub.add_parser("run", help="run the environment and power simulation")
    r.add_argument(
        "--sitl", default=os.environ.get("SCOUT_SIM_SITL", "tcp:127.0.0.1:5762")
    )
    r.add_argument(
        "--power-out",
        default=os.environ.get("SCOUT_SIM_POWER_OUT", "udpout:127.0.0.1:14601"),
    )
    r.add_argument(
        "--truth", default=os.environ.get("SCOUT_SIM_TRUTH", "sim-truth.jsonl")
    )
    r.add_argument(
        "--params",
        default=os.environ.get("SCOUT_PARAMS"),
        help="fitted params.json; the sim perturbs it to make its 'truth'",
    )
    r.add_argument("--seed", type=int, default=1)
    r.add_argument("--pct0", type=float, default=70.0, help="starting battery_pct")
    r.add_argument(
        "--lat", type=float, default=60.2, help="used until SITL reports position"
    )
    r.add_argument("--lon", type=float, default=2.2)
    r.add_argument(
        "--arm", action="store_true", help="arm once SITL is ready (operator action)"
    )
    r.add_argument(
        "--arm-delay", type=float, default=40.0, help="seconds after first heartbeat"
    )
    r.add_argument(
        "--tide-every", type=float, default=5.0, help="seconds between drift updates"
    )
    r.add_argument(
        "--truth-every",
        type=float,
        default=60.0,
        help="simulated seconds per truth line",
    )
    add_storm_args(r)
    r.add_argument("-v", "--verbose", action="store_true")
    f = sub.add_parser(
        "fast", help="fast-time simulation without SITL (days in seconds)"
    )
    fast.add_args(f)
    sc = sub.add_parser("score", help="score SITL runs (run_sitl.sh / sim-batch.sh)")
    sc.add_argument("runs", nargs="+", type=Path, help="run directories")
    a = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if a.cmd == "clock":
        print(clock.make(clock.parse_iso(a.start), a.rate))
        return 0
    if a.cmd == "fast":
        print(json.dumps(fast.run(a), indent=1))
        return 0
    if a.cmd == "score":
        from .score import score, table

        print(table([score(r) for r in a.runs]))
        return 0
    model = E.load_params(a.params)
    try:
        Sim(a, model).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
