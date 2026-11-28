"""Low-power mission mode: the service that runs on the boat.

Started by `scout-lowpower run` (see cli.py).

What it does each cycle (default every 5 min, sooner on events):
  telemetry (MAVLink) -> BoatState -> planner.decide() -> commands to ArduPilot

Robustness:
  * Shore link down: nothing here needs shore. The mission, the energy
    parameters and the service state are local files.
  * Autopilot restart or link loss: heartbeats are tracked; a restart is
    detected from time_boot_ms going backwards or a heartbeat gap. On
    recovery it re-requests telemetry, checks the onboard mission against the
    one it uploaded (re-uploading if different), re-asserts mode and speed,
    and re-arms if the boat was armed before (--no-rearm to disable).
  * Service restart: state.json (atomic writes) holds the last plan, the
    mission it uploaded, the cloud observations, and the estimated current
    and top speed through the water.
  * Operator override: if someone switches the autopilot to a mode the
    service doesn't use (MANUAL, RTL, GUIDED...), it stops commanding until
    the mode is back in AUTO/HOLD/LOITER or a new mission is set.
  * systemd: READY/WATCHDOG/STATUS via $NOTIFY_SOCKET; state defaults to
    $STATE_DIRECTORY when systemd provides it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from scout_forecast import clock
from scout_forecast import energy as E

from . import current as C
from . import mavlink as M
from . import planner as P
from .mission import Mission, write_atomic
from .mission import load as load_mission

log = logging.getLogger("scout_lowpower")

SERVICE_MODES = (M.MODE_AUTO, M.MODE_HOLD, M.MODE_LOITER)
# The mode each action settles in. Transit (AUTO) and station-keeping
# (LOITER: AUTO reaching the final loiter item) both motor; in LOITER the boat
# points into the current, so the same estimate applies.
PLAN_MODES = {
    "transit": M.MODE_AUTO,
    "hold": M.MODE_LOITER,
    "pause": M.MODE_HOLD,
    "drift": M.MODE_HOLD,
}
HEARTBEAT_TIMEOUT_S = 5.0
REBOOT_GRACE_S = 60.0  # after a restart, the autopilot's own mode isn't an override
# After a reconnect, how long to wait for the boot clock (GLOBAL_POSITION_INT)
# that tells a reboot from a link blip, before trusting the armed state again.
BOOT_CHECK_S = 10.0
RESYNC_RETRY_S = 30.0  # after a failed mission upload
STREAMS_HZ = {
    "GLOBAL_POSITION_INT": 1,
    "SYS_STATUS": 1,
    "MISSION_CURRENT": 1,
    "VFR_HUD": 1,
    "BATTERY_STATUS": 0.5,
}


@dataclass
class Settings:
    mavlink: str = "tcp:127.0.0.1:5760"
    power_link: str | None = None  # separate source of BATTERY_STATUS, if any
    state_dir: Path = Path("lowpower-state")
    mission: Path | None = None
    params: Path | None = None
    interval_s: float = 300.0
    dry_run: bool = False
    rearm: bool = True
    starlink_relay: int | None = None  # relay number, if Starlink power is switchable
    starlink_period_s: float = 3600.0
    sysid: int = 1
    battery_id: int = 0  # BATTERY_STATUS ids: battery, whole-boat load, solar
    load_id: int = 1
    solar_id: int = 2
    pct_zero_v: float = 20.0  # Tether's battery_pct = (V - 20) * 20
    pct_full_v: float = 25.0
    # ArduPilot: + = discharging; we use + = charging.
    battery_current_sign: float = -1.0

    @property
    def mission_path(self) -> Path:
        return self.mission or self.state_dir / "mission.json"


# ------------------------------------------------------------------ telemetry
class Telemetry:
    """Latest values and short rolling windows of power, from MAVLink."""

    def __init__(self, st: Settings):
        self.st = st
        self.lat = self.lon = self.pos_t = None
        self.volt = self.volt_t = None
        self.battery_w = None
        self.load = deque()  # (monotonic t, W)
        self.solar = deque()
        self.mission_seq = None
        self.leg_t = None  # monotonic time the current mission item began
        self.sog = None
        self.ground = deque()  # (monotonic t, north m/s, east m/s) over ground
        self.headings = deque()  # (monotonic t, unit north, unit east)

    def update(self, msg: dict, now: float) -> None:
        n = msg["name"]
        if n == "GLOBAL_POSITION_INT" and (msg["lat"] or msg["lon"]):
            self.lat, self.lon, self.pos_t = msg["lat"] / 1e7, msg["lon"] / 1e7, now
            self.ground.append((now, msg["vx"] / 100, msg["vy"] / 100))
        elif (
            n == "SYS_STATUS"
            and 0 < msg["voltage_battery"] < 65535
            and self.volt_t is None
        ):
            self.volt = msg["voltage_battery"] / 1000
        elif n == "BATTERY_STATUS":
            v = msg["voltages"][0]
            v = v / 1000 if 0 < v < 65535 else None
            amps = (
                msg["current_battery"] / 100 if msg["current_battery"] != -1 else None
            )
            if msg["id"] == self.st.battery_id and v:
                self.volt, self.volt_t = v, now
                if amps is not None:
                    self.battery_w = self.st.battery_current_sign * amps * v
            elif msg["id"] == self.st.load_id and amps is not None:
                self.load.append((now, abs(amps) * (v or self.volt or E.NOMINAL_V)))
            elif msg["id"] == self.st.solar_id and amps is not None and v:
                self.solar.append((now, abs(amps) * v))
        elif n == "MISSION_CURRENT":
            if msg["seq"] != self.mission_seq:
                self.leg_t = now  # a new leg (or a new mission): a turn
            self.mission_seq = msg["seq"]
        elif n == "VFR_HUD":
            self.sog = msg["groundspeed"]
            h = math.radians(msg["heading"])
            self.headings.append((now, math.cos(h), math.sin(h)))
        # Keep what the longest window needs: a whole Starlink cycle
        # (_observe_current), at least 15 min.
        keep = max(900.0, self.st.starlink_period_s)
        for q in (self.load, self.solar, self.ground, self.headings):
            while q and now - q[0][0] > keep:
                q.popleft()

    @staticmethod
    def _window(q, window_s: float, now: float):
        """The values (without their time) of entries in the last window_s."""
        return [x[1:] for x in q if now - x[0] <= window_s]

    def mean(self, q, window_s: float, now: float):
        v = [w for (w,) in self._window(q, window_s, now)]
        return sum(v) / len(v) if v else None

    def mean_heading(self, window_s: float, now: float):
        """Heading over the window as a vector mean (degrees), or None. The
        window spans turns at waypoints: the last heading would misread it."""
        v = self._window(self.headings, window_s, now)
        if not v:
            return None
        n, e = sum(x[0] for x in v), sum(x[1] for x in v)
        if math.hypot(n, e) < 0.5 * len(v):  # turning a lot: no clear heading
            return None
        return math.degrees(math.atan2(e, n)) % 360

    def ground_velocity(self, window_s: float, now: float):
        v = self._window(self.ground, window_s, now)
        if not v:
            return None
        return sum(x[0] for x in v) / len(v), sum(x[1] for x in v) / len(v)

    def battery_pct(self) -> float | None:
        if self.volt is None:
            return None
        span = self.st.pct_full_v - self.st.pct_zero_v
        return min(100.0, max(0.0, (self.volt - self.st.pct_zero_v) * 100 / span))

    def fresh(self, now: float, max_age: float = 300) -> bool:
        return (
            self.pos_t is not None
            and now - self.pos_t < max_age
            and self.volt is not None
        )


class Autopilot:
    """Heartbeat tracking and restart detection."""

    def __init__(self):
        self.sysid = self.compid = None
        self.last_hb = None
        self.mode = None
        self.armed = False
        self.boot_ms = None
        self.online = False
        self.restarted_at = None
        # Set on (re)connect until the next boot clock arrives: until then a
        # quick reboot (shorter than the heartbeat timeout) can't be told apart
        # from a link blip, so its "disarmed" mustn't be taken as intended.
        self.boot_unknown_since = None

    def reconnected(self, now: float) -> None:
        self.boot_unknown_since = now

    def boot_unknown(self, now: float) -> bool:
        s = self.boot_unknown_since
        return s is not None and now - s < BOOT_CHECK_S

    def update(self, msg: dict, now: float) -> list[str]:
        events = []
        if msg["name"] == "HEARTBEAT":
            if msg["autopilot"] == M.MAV_AUTOPILOT_INVALID or msg["type"] in (
                M.MAV_TYPE_GCS,
                M.MAV_TYPE_ONBOARD_CONTROLLER,
            ):
                return events  # a GCS or companion, not the autopilot
            gap = None if self.last_hb is None else now - self.last_hb
            self.sysid, self.compid = msg["_sysid"], msg["_compid"]
            self.last_hb = now
            self.mode = msg["custom_mode"]
            self.armed = bool(msg["base_mode"] & M.MAV_MODE_FLAG_SAFETY_ARMED)
            if not self.online:
                self.online = True
                events.append("online" if gap is None else "regained")
            if self.mode == M.MODE_INITIALISING and "regained" not in events:
                events.append("initialising")
        elif msg["name"] == "GLOBAL_POSITION_INT" and msg["_compid"] == (
            self.compid or 1
        ):
            b = msg["time_boot_ms"]
            if self.boot_ms is not None and b + 200 < self.boot_ms:
                events.append("rebooted")
            self.boot_ms = b
            self.boot_unknown_since = None
        return events

    def restarted_recently(self, now: float) -> bool:
        """Within REBOOT_GRACE_S of a restart: its own mode isn't an override,
        and its "disarmed" isn't the operator's."""
        return (
            self.restarted_at is not None and now - self.restarted_at < REBOOT_GRACE_S
        )

    def check_timeout(self, now: float) -> bool:
        if (
            self.online
            and self.last_hb is not None
            and now - self.last_hb > HEARTBEAT_TIMEOUT_S
        ):
            self.online = False
            return True
        return False


# ---------------------------------------------------------------- systemd
def sd_notify(state: str) -> None:
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError:
        pass


# ----------------------------------------------------------------- service
class Service:
    def __init__(self, st: Settings, params: E.Params, cfg: P.Config | None = None):
        self.st, self.params, self.cfg = st, params, cfg or P.Config()
        self.link = M.Connection(st.mavlink, sysid=st.sysid)
        self.power = (
            M.Connection(st.power_link, sysid=st.sysid) if st.power_link else None
        )
        self.tel = Telemetry(st)
        self.ap = Autopilot()
        self.mission: Mission | None = None
        self.mission_mtime = None
        self.plan: P.Plan | None = None
        self.uploaded = None  # signature of the mission items onboard
        self.cloud_obs = []  # [epoch, observed W, clear-sky W], last 24 h
        self.current = (0.0, 0.0)  # estimated water current (north, east) m/s
        self.current_mean = (0.0, 0.0)  # its ~24 h average (residual drift)
        self.stw_max = None  # top speed through the water seen in practice
        self.was_armed = False
        self.override = False
        self.need_decide = True
        self.need_resync = True
        self.next_decide = 0.0
        self.prev_mode = None  # last observed autopilot mode
        self.mode_cmd_at = -1e9  # when we last commanded a mode change
        self.mode_cmd_want = None  # that mode, until a heartbeat confirms it
        self.link_opens = 0
        self.retry_resync_at = 0.0
        self.last_state: P.BoatState | None = None
        self.last = {}  # rate limiting: name -> monotonic time
        self.stop = False
        self.state_path = st.state_dir / "state.json"
        self._load_state()

    # -- persistence -------------------------------------------------------
    def _load_state(self):
        try:
            d = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return
        self.uploaded = d.get("uploaded")
        self.cloud_obs = d.get("cloud_obs", [])
        self.current = tuple(d.get("current") or (0.0, 0.0))
        self.current_mean = tuple(d.get("current_mean") or (0.0, 0.0))
        self.stw_max = d.get("stw_max")
        self.was_armed = d.get("was_armed", False)
        self.override = d.get("override", False)
        if d.get("plan"):
            try:
                self.plan = P.Plan.from_dict(d["plan"])
            except (KeyError, TypeError, ValueError):
                self.plan = None
        log.info(
            "restored state: plan=%s current=%.2f m/s override=%s",
            self.plan and self.plan.action,
            math.hypot(*self.current),
            self.override,
        )

    def _save_state(self):
        d = {
            "saved_at": _now().isoformat(),
            "uploaded": self.uploaded,
            "cloud_obs": self.cloud_obs[-200:],
            "current": list(self.current),
            "current_mean": list(self.current_mean),
            "stw_max": self.stw_max,
            "was_armed": self.was_armed,
            "override": self.override,
            "plan": self.plan.to_dict() if self.plan else None,
        }
        write_atomic(self.state_path, json.dumps(d, indent=1))

    def _publish(self, s: P.BoatState | None, history: bool = True):
        """decision.json for operators and the web page; history for hindcasts.

        Called at each decision (with history) and every 15 s for status
        (autopilot online/mode/armed, override), so the page isn't up to a
        decision interval stale.
        """
        self.last_state = s or self.last_state
        s = self.last_state
        d = {
            "updated": _now().isoformat(),
            "mission": self.mission.to_dict() if self.mission else None,
            "mission_id": self.mission.id if self.mission else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "autopilot": {
                "online": self.ap.online,
                "mode": M.ROVER_MODES.get(self.ap.mode, self.ap.mode),
                "armed": self.ap.armed,
            },
            "override": self.override,
            "current": {
                "north_ms": round(self.current[0], 3),
                "east_ms": round(self.current[1], 3),
                "speed_ms": round(math.hypot(*self.current), 3),
            },
            "stw_max_ms": None if self.stw_max is None else round(self.stw_max, 3),
            "cloud": self._cloud(),
            "state": s
            and {
                "time": s.time.isoformat(),
                "lat": s.lat,
                "lon": s.lon,
                "battery_pct": round(s.battery_pct, 2),
                "volt": s.volt,
            },
        }
        write_atomic(self.st.state_dir / "decision.json", json.dumps(d, indent=1))
        if history and s and self.plan:
            hist = self.st.state_dir / "history.jsonl"
            if hist.exists() and hist.stat().st_size > 5_000_000:
                os.replace(hist, hist.with_suffix(".jsonl.1"))
            mono = time.monotonic()
            load = self.tel.mean(self.tel.load, 300, mono)
            solar = self.tel.mean(self.tel.solar, 300, mono)
            with open(hist, "a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "time": s.time.isoformat(),
                            "battery_pct": round(s.battery_pct, 2),
                            "load_w": None if load is None else round(load, 1),
                            "solar_w": None if solar is None else round(solar, 1),
                            "sog": self.tel.sog,
                            "mode": M.ROVER_MODES.get(self.ap.mode, self.ap.mode),
                            "action": self.plan.action,
                            "speed_ms": self.plan.speed_ms,
                            "current_ms": [round(c, 3) for c in self.current],
                            "stw_max_ms": self.stw_max,
                            "min_pct": self.plan.min_pct,
                            "forecast": self.plan.forecast,
                        }
                    )
                    + "\n"
                )

    # -- main loop -----------------------------------------------------------
    def run(self) -> int:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: setattr(self, "stop", True))
            signal.signal(signal.SIGINT, lambda *_: setattr(self, "stop", True))
            signal.signal(
                signal.SIGHUP, lambda *_: setattr(self, "mission_mtime", None)
            )
        log.info(
            "starting: mavlink=%s power=%s state=%s mission=%s dry_run=%s clock=%s",
            self.st.mavlink,
            self.st.power_link,
            self.st.state_dir,
            self.st.mission_path,
            self.st.dry_run,
            _now().isoformat(timespec="seconds"),
        )
        sd_notify("READY=1")
        while not self.stop:
            now = time.monotonic()
            for msg in self.link.poll(0.4 if self.power else 0.5):
                self._on_message(msg, now)
            if self.power:
                for msg in self.power.poll(0.05):
                    self.tel.update(msg, time.monotonic())
            self._tick(time.monotonic())
            sd_notify("WATCHDOG=1")
        self._save_state()
        self.link.close("(service stopping)")
        if self.power:
            self.power.close("(service stopping)")
        log.info("stopped")
        return 0

    def _on_message(self, msg, now):
        self.tel.update(msg, now)
        for ev in self.ap.update(msg, now):
            if ev in ("regained", "rebooted", "initialising", "online"):
                level = logging.INFO if ev == "online" else logging.WARNING
                log.log(
                    level,
                    "autopilot %s (sysid %s, mode %s)",
                    ev,
                    self.ap.sysid,
                    M.ROVER_MODES.get(self.ap.mode, self.ap.mode),
                )
                self.need_resync = True
                self.prev_mode = None  # its current mode wasn't chosen by an operator
                if ev != "online":
                    self.ap.restarted_at = now
        if msg["name"] == "COMMAND_ACK" and msg["result"] != 0:
            log.warning(
                "autopilot rejected command %d (result %d)",
                msg["command"],
                msg["result"],
            )
        if msg["name"] == "STATUSTEXT":
            log.info("autopilot: %s", msg["text"])

    def _tick(self, now):
        if self._every("heartbeat", 1.0, now):
            self.link.send(
                "HEARTBEAT",
                type=M.MAV_TYPE_ONBOARD_CONTROLLER,
                autopilot=M.MAV_AUTOPILOT_INVALID,
                base_mode=0,
                custom_mode=0,
                system_status=4,
                mavlink_version=3,
            )
        if self.link.opens != self.link_opens:
            if self.link_opens:  # a reconnect, not the first connection
                self.ap.reconnected(now)
            self.link_opens = self.link.opens
            self.need_resync = True  # any reconnect: re-check mission, mode, speed
        if self.ap.check_timeout(now):
            self.prev_mode = None
            log.warning(
                "autopilot heartbeat lost (>%.0fs); will resync when it returns",
                HEARTBEAT_TIMEOUT_S,
            )
        if self._every("mission_check", 5.0, now):
            self._check_mission_file()
        if self.ap.online:
            self._track_mode(now)
            if not self.ap.restarted_recently(now) and not self.ap.boot_unknown(now):
                # Follows the operator; frozen just after a restart, and after
                # a reconnect until the boot clock says whether it rebooted.
                self.was_armed = self.ap.armed
        if (
            self.ap.online
            and self.need_resync
            and self.ap.mode != M.MODE_INITIALISING
            and now >= self.retry_resync_at
        ):
            self._resync(now)
        if self.mission and self._every("status", 15.0, now):
            self._publish(None, history=False)
        # Once a boat minute (every few real seconds on a fast simulated clock).
        if self._every("observe", max(5.0, 60.0 / clock.rate()), now):
            self._observe(now)
        if (self.need_decide or now >= self.next_decide) and self.mission:
            self._decide(now)
        if (
            self.ap.online
            and self.plan
            and not self.st.dry_run
            and not self.override
            and not self.ap.boot_unknown(now)  # reboot or operator? wait to see
        ):
            self._enforce(now)

    # -- mission file --------------------------------------------------------
    def _check_mission_file(self):
        path = self.st.mission_path
        try:
            mtime = path.stat().st_mtime
        except OSError:
            if self.mission is None and self._every(
                "no_mission_log", 600, time.monotonic()
            ):
                log.info("no mission at %s; waiting (scout-lowpower set-mission)", path)
            return
        if mtime == self.mission_mtime:
            return
        self.mission_mtime = mtime
        try:
            m = load_mission(path)
        except (OSError, ValueError, TypeError) as e:
            log.error(
                "mission file %s is invalid (%s); keeping the previous mission", path, e
            )
            return
        if self.mission is None or m.id != self.mission.id:
            log.info(
                "mission %s loaded: %s, reserve %.0f%%%s",
                m.id,
                m.kind,
                m.min_reserve_pct,
                f", arrive by {m.arrive_by:%Y-%m-%d %H:%MZ}" if m.arrive_by else "",
            )
            if self.override:
                log.info("new mission clears the operator override")
            self.override = False
            self.mission = m
            self.need_decide = True
            self.need_resync = True

    # -- autopilot sync ----------------------------------------------------
    def _resync(self, now):
        log.info("resyncing with autopilot")
        for name, hz in STREAMS_HZ.items():
            self._command(M.MAV_CMD_SET_MESSAGE_INTERVAL, M.MESSAGES[name].id, 1e6 / hz)
        if self.mission and not self.st.dry_run:
            items = self._mission_items()
            onboard = self._download_mission()
            if onboard != _signature(items):
                log.info(
                    "onboard mission differs (%s items); uploading %d items",
                    "unknown" if onboard is None else len(onboard),
                    len(items),
                )
                if self._upload_mission(items):
                    self.uploaded = _signature(items)
                    # A new mission starts at its first waypoint; the index the
                    # autopilot reported belonged to the old mission.
                    self.link.send("MISSION_SET_CURRENT", seq=1, **self._target())
                    self.tel.mission_seq = None
                else:
                    log.error(
                        "mission upload failed; retrying in %.0f s", RESYNC_RETRY_S
                    )
                    self.retry_resync_at = now + RESYNC_RETRY_S
                    return
            else:
                self.uploaded = onboard
            if (
                self.ap.restarted_recently(now)
                and self.st.rearm
                and self.was_armed
                and not self.ap.armed
                and not self.override
            ):
                log.warning(
                    "autopilot restarted while armed: re-arming to continue the mission"
                )
                self._command(M.MAV_CMD_COMPONENT_ARM_DISARM, 1)
        self.last.pop("speed", None)
        self.last.pop("mode", None)
        self.need_resync = False
        self.need_decide = True
        self._save_state()

    def _mission_items(self) -> list[tuple]:
        """(command, lat, lon, param3) items; item 0 is ArduPilot's home slot."""
        m = self.mission
        pts = m.waypoints if m.kind == "transit" else [m.station]
        items = [(M.MAV_CMD_NAV_WAYPOINT, pts[0][0], pts[0][1], 0.0)]
        items += [(M.MAV_CMD_NAV_WAYPOINT, la, lo, 0.0) for la, lo in pts]
        la, lo = pts[-1]
        items.append((M.MAV_CMD_NAV_LOITER_UNLIM, la, lo, float(m.radius_m)))
        return items

    def _target(self):
        return {
            "target_system": self.ap.sysid or 1,
            "target_component": self.ap.compid or 1,
        }

    def _wait_for(self, names, timeout, pred=lambda m: True):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for msg in self.link.poll(0.2):
                self._on_message(msg, time.monotonic())
                if msg["name"] in names and pred(msg):
                    return msg
            sd_notify("WATCHDOG=1")
        return None

    def _download_mission(self):
        self.link.send("MISSION_REQUEST_LIST", **self._target())
        cnt = self._wait_for(("MISSION_COUNT",), 3.0)
        if cnt is None:
            return None
        items = []
        for seq in range(cnt["count"]):
            for _ in range(3):
                self.link.send("MISSION_REQUEST_INT", seq=seq, **self._target())
                it = self._wait_for(
                    ("MISSION_ITEM_INT",), 1.5, lambda m, s=seq: m["seq"] == s
                )
                if it:
                    items.append(
                        (it["command"], it["x"] / 1e7, it["y"] / 1e7, it["param3"])
                    )
                    break
            else:
                return None
        self.link.send("MISSION_ACK", type=M.MAV_MISSION_ACCEPTED, **self._target())
        return _signature(items)

    def _upload_mission(self, items) -> bool:
        for attempt in range(3):
            self.link.send("MISSION_COUNT", count=len(items), **self._target())
            end = time.monotonic() + 10
            while time.monotonic() < end:
                msg = self._wait_for(
                    ("MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK"), 2.0
                )
                if msg is None:
                    break
                if msg["name"] == "MISSION_ACK":
                    if msg["type"] == M.MAV_MISSION_ACCEPTED:
                        log.info("mission uploaded (%d items)", len(items))
                        return True
                    log.warning("mission upload rejected (code %d)", msg["type"])
                    break
                seq = msg["seq"]
                if seq >= len(items):
                    continue
                cmd, la, lo, p3 = items[seq]
                self.link.send(
                    "MISSION_ITEM_INT",
                    seq=seq,
                    command=cmd,
                    frame=M.MAV_FRAME_GLOBAL_RELATIVE_ALT,
                    x=int(round(la * 1e7)),
                    y=int(round(lo * 1e7)),
                    z=0.0,
                    param3=p3,
                    current=0,
                    autocontinue=1,
                    **self._target(),
                )
            log.warning("mission upload attempt %d failed", attempt + 1)
        return False

    def _command(self, cmd, *params):
        self.link.command(self._target(), cmd, *params)

    def _param(self, name: str, value: float):
        self.link.param_set(self._target(), name, value)

    # -- observe / decide / enforce -----------------------------------------
    def _observe(self, now):
        """Learn the cloud factor (solar) and the water current."""
        t = self.tel
        if t.lat is None:
            return
        solar = t.mean(t.solar, 900, now)
        if solar is not None:
            clear = E.clear_sky_w(self.params, _now(), t.lat, t.lon)
            self.cloud_obs.append([_now().timestamp(), solar, clear])
        cutoff = _now().timestamp() - 86400
        self.cloud_obs = [o for o in self.cloud_obs if o[0] >= cutoff]
        self._observe_current(now)

    def _observe_current(self, now):
        """Estimate the water current (current.py): under thrust from measured
        propulsion power and heading, or with motors off from the drift."""
        t, pl = self.tel, self.plan
        if not pl or pl.since is None:
            return
        # Average over a whole Starlink cycle: propulsion = load - hotel -
        # Starlink, and Starlink comes in sessions, so a shorter window can be
        # all-on or all-off (+-15 W, more than the propulsion itself at low
        # speed). But not back past the plan's start or the current leg's
        # (current.window_s). Telemetry is in real seconds: scale by the rate.
        rate = clock.rate()
        since = (_now() - pl.since).total_seconds()
        if t.leg_t is not None:
            since = min(since, (now - t.leg_t) * rate)
        full = max(600.0, self.st.starlink_period_s)
        span = C.window_s(since, full)
        if span is None:
            return
        window = span / rate
        ground = t.ground_velocity(window, now)
        if ground is None:
            return
        load = t.mean(t.load, window, now)
        obs = C.observe(
            self.params,
            pl.action,
            pl.starlink_duty,
            self.ap.mode == PLAN_MODES.get(pl.action),
            self.ap.armed,
            ground,
            t.mean_heading(window, now),
            load,
        )
        if obs is None:
            return
        if pl.action == "transit" and pl.speed_ms and self.ap.armed:
            prop = load - E.base_load_w(self.params, pl.starlink_duty)
            self.stw_max = C.learn_top_speed(
                self.stw_max,
                pl.speed_ms,
                math.hypot(*ground),
                E.stw_for_power(self.params, prop),
                prop >= C.FULL_POWER * E.max_propulsion_w(self.params),
            )
        # A window shorter than a Starlink cycle is noisier: weigh it less.
        self.current = C.blend(self.current, obs, 0.3 * span / full)
        # Observed about once a boat minute: a ~24 h average of the same.
        self.current_mean = C.blend(self.current_mean, obs, 60.0 / 86400)
        log.debug(
            "current estimate %.2f m/s (n %.2f, e %.2f)",
            math.hypot(*self.current),
            *self.current,
        )

    def _cloud(self):
        return C.cloud_factor(self.cloud_obs)

    def _state(self, now) -> P.BoatState | None:
        t = self.tel
        if not t.fresh(now):
            return None
        next_wp = 0
        if (
            self.mission
            and self.mission.kind == "transit"
            and t.mission_seq is not None
        ):
            # Items: 0 home, 1..n waypoints, n+1 loiter at the last one.
            next_wp = min(max(t.mission_seq - 1, 0), len(self.mission.waypoints) - 1)
        return P.BoatState(
            time=_now(),
            lat=t.lat,
            lon=t.lon,
            battery_pct=t.battery_pct(),
            volt=t.volt,
            next_wp=next_wp,
            battery_w=t.battery_w,
            cloud=self._cloud(),
            current_ms=self.current,
            stw_max_ms=self.stw_max,
            current_mean_ms=self.current_mean,
            at_final=bool(
                self.mission
                and self.mission.kind == "transit"
                and t.mission_seq is not None
                and t.mission_seq > len(self.mission.waypoints)
            ),
        )

    def _decide(self, now):
        self.need_decide = False
        self.next_decide = now + self.st.interval_s
        s = self._state(now)
        if s is None:
            if self._every("stale_log", 300, now):
                log.warning("telemetry stale or missing; keeping the current plan")
            self.next_decide = now + 5  # cheap to check; decide as soon as it's back
            return
        new = P.decide(s, self.mission, self.params, self.cfg, prev=self.plan)
        changed = not new.same_control(self.plan)
        self.plan = new
        msg = (
            f"plan: {new.action}"
            + (f" {new.speed_ms:.1f} m/s" if new.speed_ms else "")
            + f", starlink duty {new.starlink_duty:.2f}, battery {s.battery_pct:.1f}%"
            + f", forecast low {new.min_pct:.1f}%"
            + (f", ETA {new.eta:%m-%d %H:%MZ}" if new.eta else "")
            + ("" if new.feasible else " [RESERVE AT RISK]")
            + " | "
            + "; ".join(new.reasons)
        )
        log.log(logging.WARNING if (changed or not new.feasible) else logging.INFO, msg)
        sd_notify(
            f"STATUS={new.action} {new.speed_ms or ''} battery {s.battery_pct:.0f}%"
        )
        if changed:
            self.last.pop("speed", None)
            self.last.pop("mode", None)
        self._save_state()
        self._publish(s)

    def _desired_mode(self):
        # hold = AUTO: the mission ends in LOITER_UNLIM at the station.
        return M.MODE_AUTO if self.plan.action in ("transit", "hold") else M.MODE_HOLD

    def _track_mode(self, now):
        """Detect operator overrides.

        An override is a *change* from a mode the service uses (AUTO, HOLD,
        LOITER) to one it doesn't, made by someone else: not the mode the
        autopilot happens to start in, not a restart, and not the heartbeats
        that still show the old mode just after our own command. Just after a
        reconnect, until the boot clock says whether the autopilot rebooted
        (Autopilot.boot_unknown), a changed mode can't be judged: wait.
        """
        if self.ap.boot_unknown(now):
            return
        mode, prev = self.ap.mode, self.prev_mode
        self.prev_mode = mode
        if mode == self.mode_cmd_want:
            self.mode_cmd_want = None  # confirmed: later changes aren't ours
        pending = self.mode_cmd_want is not None and now - self.mode_cmd_at < 5.0
        if mode in SERVICE_MODES:
            if self.override:
                log.info("autopilot back in %s: resuming control", M.ROVER_MODES[mode])
                self.override = False
        elif (
            prev in SERVICE_MODES
            and mode != M.MODE_INITIALISING
            and not pending
            and not self.override
            and self.plan
        ):
            log.warning(
                "autopilot switched to %s by someone else: operator override, "
                "not commanding until AUTO/HOLD/LOITER or a new mission",
                M.ROVER_MODES.get(mode, mode),
            )
            self.override = True
            self._save_state()

    def _enforce(self, now):
        """Re-assert the plan. Idempotent; rate limited."""
        if self.need_resync or self.uploaded is None or self.mission is None:
            return
        want = self._desired_mode()
        if self.ap.mode != want and self._every("mode", 5.0, now):
            log.info(
                "setting mode %s (was %s)",
                M.ROVER_MODES.get(want, want),
                M.ROVER_MODES.get(self.ap.mode, self.ap.mode),
            )
            self._command(
                M.MAV_CMD_DO_SET_MODE, M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED, want
            )
            self.mode_cmd_at = now
            self.mode_cmd_want = want
        pl = self.plan
        if pl.action == "transit" and pl.speed_ms and self._every("speed", 600.0, now):
            log.info("setting cruise speed %.1f m/s", pl.speed_ms)
            self._param("WP_SPEED", pl.speed_ms)  # persists across restarts
            self._command(M.MAV_CMD_DO_CHANGE_SPEED, 1, pl.speed_ms, -1)
        if pl.action in ("hold", "drift") and self._every("loiter_radius", 3600.0, now):
            self._param("LOIT_RADIUS", self.mission.radius_m)
        if self.st.starlink_relay is not None and self._every("relay", 60.0, now):
            period = self.st.starlink_period_s  # boat seconds, as power.py
            phase = (_now().timestamp() % period) / period
            on = phase < pl.starlink_duty
            self._command(
                M.MAV_CMD_DO_SET_RELAY, self.st.starlink_relay, 1 if on else 0
            )

    def _every(self, key, period, now) -> bool:
        if now - self.last.get(key, -1e9) >= period:
            self.last[key] = now
            return True
        return False


def _signature(items):
    return [
        [int(c), round(la, 6), round(lo, 6), round(float(p3), 1)]
        for c, la, lo, p3 in items[1:]
    ]


def _now():
    return clock.now()  # real UTC unless $SCOUT_CLOCK sets a simulated clock
