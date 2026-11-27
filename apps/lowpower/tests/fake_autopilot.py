"""A fake ArduPilot Rover for testing the low-power service without SITL.

Serves MAVLink on TCP (as SITL does on 5760), streams heartbeat, position
and battery telemetry, and implements the mission upload/download protocol,
DO_SET_MODE, arming, DO_CHANGE_SPEED and PARAM_SET. restart() simulates an
autopilot reboot (link drops; mission and params persist like EEPROM; comes
back disarmed in MANUAL). stop()/start() simulate the autopilot being gone.
"""

from __future__ import annotations

import socket
import threading
import time

from scout_lowpower import mavlink as M


class FakeRover:
    def __init__(
        self, port: int = 0, lat=60.20, lon=2.20, volt=23.0, position_delay_s=0.0
    ):
        self.lat, self.lon, self.volt = lat, lon, volt
        # Seconds after boot before position (with its boot clock) streams: a
        # real autopilot's heartbeat and position are separate streams, so the
        # service can see the heartbeat of a rebooted autopilot first.
        self.position_delay_s = position_delay_s
        self.mode, self.armed = 0, False
        self.mission = []  # (command, lat, lon, param3)
        self.params = {}
        self.speed = None
        self.commands = []  # (command, params) log
        self.boot = time.monotonic()
        self.codec = M.Codec(1, 1)
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", port))
        self.port = self.server.getsockname()[1]
        self.conn = None
        self.running = False
        self.lock = threading.Lock()
        self._upload = None  # (count, items)
        self.errors = []
        self.seq = 1

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        self.running = True
        self.server.listen(1)
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()
        return self

    def stop(self):
        """Autopilot gone: drop the link and stop answering."""
        self.running = False
        self.thread.join(timeout=3)
        if self.conn:
            self.conn.close()
            self.conn = None

    def restart(self, down_s: float = 2.0):
        """Reboot: link drops, comes back disarmed in MANUAL, boot clock reset."""
        self.stop()
        time.sleep(down_s)
        self.mode, self.armed, self.speed = 0, False, None
        self.boot = time.monotonic()
        self.start()

    def close(self):
        self.stop()
        self.server.close()

    # -- protocol ------------------------------------------------------------
    def send(self, name, **kw):
        if self.conn:
            try:
                self.conn.sendall(self.codec.pack(name, **kw))
            except OSError as e:
                self.errors.append(f"send {name}: {e!r}")

    def _loop(self):
        self.server.settimeout(0.2)
        last_stream = 0.0
        while self.running:
            if self.conn is None:
                try:
                    self.conn, _ = self.server.accept()
                    self.conn.settimeout(0.05)
                except socket.timeout:
                    continue
            now = time.monotonic()
            if now - last_stream >= 0.5:
                last_stream = now
                self._stream(now)
            try:
                data = self.conn.recv(4096)
                if not data:
                    self.conn.close()
                    self.conn = None
                    continue
            except socket.timeout:
                continue
            except OSError as e:
                self.errors.append(f"recv: {e!r}")
                self.conn.close()
                self.conn = None
                continue
            for msg in self.codec.feed(data):
                with self.lock:
                    self._handle(msg)

    def _stream(self, now):
        base = (
            M.MAV_MODE_FLAG_SAFETY_ARMED if self.armed else 0
        ) | M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED
        self.send(
            "HEARTBEAT",
            custom_mode=self.mode,
            type=11,
            autopilot=3,
            base_mode=base,
            system_status=4,
            mavlink_version=3,
        )
        if now - self.boot >= self.position_delay_s:
            self.send(
                "GLOBAL_POSITION_INT",
                time_boot_ms=int((now - self.boot) * 1000),
                lat=int(self.lat * 1e7),
                lon=int(self.lon * 1e7),
            )
        mv = int(self.volt * 1000)
        self.send(
            "SYS_STATUS", voltage_battery=mv, current_battery=150, battery_remaining=60
        )
        self.send(
            "BATTERY_STATUS", id=0, voltages=[mv] + [65535] * 9, current_battery=150
        )
        self.send(
            "BATTERY_STATUS", id=1, voltages=[mv] + [65535] * 9, current_battery=200
        )
        self.send(
            "BATTERY_STATUS", id=2, voltages=[40000] + [65535] * 9, current_battery=50
        )
        self.send("MISSION_CURRENT", seq=self.seq)

    def _handle(self, m):
        n = m["name"]
        if n == "COMMAND_LONG":
            self.commands.append((m["command"], m["param1"], m["param2"]))
            if m["command"] == M.MAV_CMD_DO_SET_MODE:
                self.mode = int(m["param2"])
            elif m["command"] == M.MAV_CMD_COMPONENT_ARM_DISARM:
                self.armed = m["param1"] == 1
            elif m["command"] == M.MAV_CMD_DO_CHANGE_SPEED:
                self.speed = m["param2"]
            self.send("COMMAND_ACK", command=m["command"], result=0)
        elif n == "PARAM_SET":
            self.params[m["param_id"]] = m["param_value"]
            self.send(
                "PARAM_VALUE",
                param_id=m["param_id"],
                param_value=m["param_value"],
                param_type=m["param_type"],
                param_count=1,
            )
        elif n == "MISSION_COUNT":
            self._upload = (m["count"], [])
            self.send(
                "MISSION_REQUEST_INT", seq=0, target_system=255, target_component=191
            )
        elif n == "MISSION_ITEM_INT" and self._upload:
            if m["frame"] not in (0, 3):  # like ArduPilot 4.0: no *_INT frames
                self._upload = None
                self.send(
                    "MISSION_ACK", type=2, target_system=255, target_component=191
                )
                return
            count, items = self._upload
            if m["seq"] == len(items):
                items.append((m["command"], m["x"] / 1e7, m["y"] / 1e7, m["param3"]))
            if len(items) < count:
                self.send(
                    "MISSION_REQUEST_INT",
                    seq=len(items),
                    target_system=255,
                    target_component=191,
                )
            else:
                self.mission = items
                self._upload = None
                self.send(
                    "MISSION_ACK", type=0, target_system=255, target_component=191
                )
        elif n == "MISSION_SET_CURRENT":
            self.seq = m["seq"]
        elif n == "MISSION_REQUEST_LIST":
            self.send(
                "MISSION_COUNT",
                count=len(self.mission),
                target_system=255,
                target_component=191,
            )
        elif n == "MISSION_REQUEST_INT" and m["seq"] < len(self.mission):
            c, la, lo, p3 = self.mission[m["seq"]]
            self.send(
                "MISSION_ITEM_INT",
                seq=m["seq"],
                command=c,
                x=int(round(la * 1e7)),
                y=int(round(lo * 1e7)),
                param3=p3,
                frame=6,
                autocontinue=1,
                target_system=255,
                target_component=191,
            )


if __name__ == "__main__":
    # Standalone, for the package smoke test: python3 fake_autopilot.py [PORT]
    import sys

    rover = FakeRover(port=int(sys.argv[1]) if len(sys.argv) > 1 else 5760).start()
    rover.armed = True
    print(f"fake rover on tcp:127.0.0.1:{rover.port}", flush=True)
    last = None
    while True:  # log state changes, so the test can read them from the journal
        time.sleep(0.5)
        with rover.lock:
            now = f"mode={rover.mode} armed={rover.armed} items={len(rover.mission)}"
        if now != last:
            print(now, flush=True)
            last = now
