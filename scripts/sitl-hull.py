"""SITL experiment: ArduPilot 4.0 sailboat-motor's hull, driven in MANUAL with a
fixed RC throttle (no service, no scout-sim). Measures its speed through the
water against throttle, whether SIM_TIDE moves it in full, and how it drifts
at zero throttle and in HOLD. Results in REPORT.md and AGENTS.md.

    rate=5; SCOUT_CLOCK=x$rate SCOUT_SITL_PARM_DIR=apps/sim/sitl \
        packaging/sitl-container.sh &      # wait for "Waiting for connection"
    .venv/bin/python scripts/sitl-hull.py runs/sitl-hull.jsonl $rate
    docker stop scout-sitl
"""

import json
import math
import sys
import time

from scout_lowpower import mavlink as M

M.MESSAGES["RC_CHANNELS_OVERRIDE"] = M._Msg(
    "RC_CHANNELS_OVERRIDE",
    70,
    124,
    " ".join(f"chan{i}_raw:H" for i in range(1, 9))
    + " target_system:B target_component:B",
)
T = {"target_system": 1, "target_component": 1}
out, rate = sys.argv[1], float(sys.argv[2])
c = M.Connection("tcp:127.0.0.1:5760", sysid=255, compid=190)
st = {"vel": None, "hdg": None, "thr": None, "armed": False, "mode": None}


def pump(seconds, rc=None):
    """Read telemetry for `seconds` (real), sending RC throttle `rc` (PWM)."""
    end, nxt, samples = time.monotonic() + seconds, 0.0, []
    while time.monotonic() < end:
        if rc and time.monotonic() > nxt:
            c.send("RC_CHANNELS_OVERRIDE", chan1_raw=1500, chan3_raw=rc, **T)
            nxt = time.monotonic() + 0.1
        for m in c.poll(0.05):
            n = m["name"]
            if n == "HEARTBEAT" and m["autopilot"] != M.MAV_AUTOPILOT_INVALID:
                st["armed"] = bool(m["base_mode"] & M.MAV_MODE_FLAG_SAFETY_ARMED)
                st["mode"] = m["custom_mode"]
            elif n == "GLOBAL_POSITION_INT":
                st["vel"] = (m["vx"] / 100, m["vy"] / 100)
                if st["hdg"] is not None:
                    samples.append((st["vel"], st["hdg"], st["thr"]))
            elif n == "VFR_HUD":
                st["hdg"], st["thr"] = m["heading"], m["throttle"]
    return samples


def tide(speed, frm):
    c.param_set(T, "SIM_TIDE_SPEED", speed)
    c.param_set(T, "SIM_TIDE_DIR", frm)  # where it comes FROM
    return -math.cos(math.radians(frm)) * speed, -math.sin(math.radians(frm)) * speed


def measure(f, label, pwm=None, tide_ne=(0.0, 0.0)):
    """Settle 90 boat s, then average 60 boat s."""
    pump(90 / rate, rc=pwm)
    s = pump(60 / rate, rc=pwm)
    vn = sum(x[0][0] for x in s) / len(s)
    ve = sum(x[0][1] for x in s) / len(s)
    hn = sum(math.cos(math.radians(x[1])) for x in s)
    he = sum(math.sin(math.radians(x[1])) for x in s)
    rec = dict(
        label=label,
        throttle=sum(x[2] for x in s) / len(s),
        sog=round(math.hypot(vn, ve), 3),
        vn=round(vn, 3),
        ve=round(ve, 3),
        heading=round(math.degrees(math.atan2(he, hn)) % 360, 1),
        # Speed through the water if the tide applied in full.
        stw_full_tide=round(math.hypot(vn - tide_ne[0], ve - tide_ne[1]), 3),
    )
    print(json.dumps(rec), flush=True)
    f.write(json.dumps(rec) + "\n")


pump(3)
for name, hz in (("GLOBAL_POSITION_INT", 4), ("VFR_HUD", 4)):
    c.command(T, M.MAV_CMD_SET_MESSAGE_INTERVAL, M.MESSAGES[name].id, 1e6 / hz)
c.param_set(T, "SIM_SPEEDUP", rate)
pump(20 / rate + 5)  # EKF and GPS
c.command(T, M.MAV_CMD_DO_SET_MODE, M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, 0)  # MANUAL
while not st["armed"]:
    c.command(T, M.MAV_CMD_COMPONENT_ARM_DISARM, 1, 21196)  # forced
    pump(3, rc=1500)

with open(out, "w") as f:
    # Heading north: still water, tide from behind, from ahead, from the beam.
    for speed, frm in ((0.0, 0), (0.5, 180), (0.5, 0), (0.5, 90)):
        t = tide(speed, frm)
        for pct in (10, 20, 40, 60, 80, 100):
            label = f"RC {pct} %, tide {speed} from {frm}"
            measure(f, label, 1500 + 5 * pct, t)
    t = tide(0.5, 0)
    measure(f, "RC 0, tide 0.5 from 0", 1500, t)
    c.send("RC_CHANNELS_OVERRIDE", chan1_raw=0, chan3_raw=0, **T)  # release
    c.command(T, M.MAV_CMD_DO_SET_MODE, M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, 4)
    measure(f, "HOLD, tide 0.5 from 0", None, t)
c.command(T, M.MAV_CMD_COMPONENT_ARM_DISARM, 0, 21196)
pump(1)
