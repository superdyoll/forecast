"""Minimal MAVLink v2 for the low-power service. Standard library only.

Covers only the messages the service uses (layouts and CRC extras from the
common/ardupilotmega definitions; tests check them byte-for-byte against
pymavlink when it is installed). Receives v1 and v2 frames, sends v2.
Signed frames are accepted but not verified.

Connections reconnect by themselves: `tcp:` (e.g. SITL on 5760), `udpin:`
(listen; replies go to the last sender), `udpout:` and `serial:` (raw termios,
no pyserial).
"""

from __future__ import annotations

import logging
import os
import select
import socket
import struct
import time

log = logging.getLogger(__name__)

# name: (msgid, crc_extra, [(field, struct code, count)]) in wire order,
# extension fields included at the end.
_DEFS = {
    "HEARTBEAT": (
        0,
        50,
        "custom_mode:I type:B autopilot:B base_mode:B system_status:B mavlink_version:B",
    ),
    "SYS_STATUS": (
        1,
        124,
        "sensors_present:I sensors_enabled:I sensors_health:I load:H voltage_battery:H current_battery:h drop_rate_comm:H errors_comm:H errors_count1:H errors_count2:H errors_count3:H errors_count4:H battery_remaining:b sensors_present_ext:I sensors_enabled_ext:I sensors_health_ext:I",
    ),
    "PARAM_VALUE": (
        22,
        220,
        "param_value:f param_count:H param_index:H param_id:16s param_type:B",
    ),
    "PARAM_SET": (
        23,
        168,
        "param_value:f target_system:B target_component:B param_id:16s param_type:B",
    ),
    "GLOBAL_POSITION_INT": (
        33,
        104,
        "time_boot_ms:I lat:i lon:i alt:i relative_alt:i vx:h vy:h vz:h hdg:H",
    ),
    "MISSION_REQUEST": (
        40,
        230,
        "seq:H target_system:B target_component:B mission_type:B",
    ),
    "MISSION_CURRENT": (42, 28, "seq:H total:H mission_state:B mission_mode:B"),
    "MISSION_REQUEST_LIST": (
        43,
        132,
        "target_system:B target_component:B mission_type:B",
    ),
    "MISSION_COUNT": (
        44,
        221,
        "count:H target_system:B target_component:B mission_type:B",
    ),
    "MISSION_ACK": (
        47,
        153,
        "target_system:B target_component:B type:B mission_type:B",
    ),
    "MISSION_REQUEST_INT": (
        51,
        196,
        "seq:H target_system:B target_component:B mission_type:B",
    ),
    "MISSION_ITEM_INT": (
        73,
        38,
        "param1:f param2:f param3:f param4:f x:i y:i z:f seq:H command:H target_system:B target_component:B frame:B current:B autocontinue:B mission_type:B",
    ),
    "VFR_HUD": (74, 20, "airspeed:f groundspeed:f alt:f climb:f heading:h throttle:H"),
    "COMMAND_LONG": (
        76,
        152,
        "param1:f param2:f param3:f param4:f param5:f param6:f param7:f command:H target_system:B target_component:B confirmation:B",
    ),
    "COMMAND_ACK": (
        77,
        143,
        "command:H result:B progress:B result_param2:i target_system:B target_component:B",
    ),
    "BATTERY_STATUS": (
        147,
        154,
        "current_consumed:i energy_consumed:i temperature:h voltages:H*10 current_battery:h id:B battery_function:B type:B battery_remaining:b time_remaining:i charge_state:B voltages_ext:H*4 mode:B fault_bitmask:I",
    ),
    "STATUSTEXT": (253, 83, "severity:B text:50s id:H chunk_seq:B"),
}


class _Msg:
    def __init__(self, name, msgid, crc_extra, spec):
        self.name, self.id, self.crc_extra = name, msgid, crc_extra
        self.fields = []  # (name, code, count)
        fmt = "<"
        for item in spec.split():
            fname, code = item.split(":")
            count = 1
            if "*" in code:
                code, n = code.split("*")
                count = int(n)
            self.fields.append((fname, code, count))
            fmt += f"{count}{code}" if count > 1 else code
        self.struct = struct.Struct(fmt)


MESSAGES = {n: _Msg(n, *d) for n, d in _DEFS.items()}
BY_ID = {m.id: m for m in MESSAGES.values()}

# Constants used by the service.
MAV_TYPE_GCS = 6
MAV_TYPE_ONBOARD_CONTROLLER = 18
MAV_AUTOPILOT_INVALID = 8
MAV_COMP_ID_AUTOPILOT1 = 1
MAV_COMP_ID_ONBOARD_COMPUTER = 191
MAV_MODE_FLAG_SAFETY_ARMED = 128
MAV_MODE_FLAG_CUSTOM_MODE_ENABLED = 1
MAV_CMD_NAV_WAYPOINT = 16
MAV_CMD_NAV_LOITER_UNLIM = 17
MAV_CMD_DO_SET_MODE = 176
MAV_CMD_DO_CHANGE_SPEED = 178
MAV_CMD_DO_SET_RELAY = 181
MAV_CMD_COMPONENT_ARM_DISARM = 400
MAV_CMD_SET_MESSAGE_INTERVAL = 511
MAV_FRAME_GLOBAL_RELATIVE_ALT_INT = 6
MAV_MISSION_ACCEPTED = 0
MAV_PARAM_TYPE_REAL32 = 9
ROVER_MODES = {
    0: "MANUAL",
    3: "STEERING",
    4: "HOLD",
    5: "LOITER",
    10: "AUTO",
    11: "RTL",
    12: "SMART_RTL",
    15: "GUIDED",
    16: "INITIALISING",
}
MODE_HOLD, MODE_LOITER, MODE_AUTO, MODE_INITIALISING = 4, 5, 10, 16


def crc_x25(data: bytes, crc: int = 0xFFFF) -> int:
    for b in data:
        tmp = b ^ (crc & 0xFF)
        tmp = (tmp ^ (tmp << 4)) & 0xFF
        crc = ((crc >> 8) ^ (tmp << 8) ^ (tmp << 3) ^ (tmp >> 4)) & 0xFFFF
    return crc


def encode_payload(name: str, **values) -> bytes:
    m = MESSAGES[name]
    args = []
    for fname, code, count in m.fields:
        default = b"" if code.endswith("s") else ([] if count > 1 else 0)
        v = values.get(fname, default)
        if code.endswith("s") and isinstance(v, str):
            v = v.encode()
        if count > 1:
            v = list(v) + [0] * (count - len(v))
            args.extend(v[:count])
        else:
            args.append(v)
    return m.struct.pack(*args)


def decode_payload(m: _Msg, payload: bytes) -> dict:
    payload = payload.ljust(m.struct.size, b"\0")[: m.struct.size]
    vals = list(m.struct.unpack(payload))
    out, i = {"name": m.name}, 0
    for fname, code, count in m.fields:
        if count > 1:
            out[fname] = tuple(vals[i : i + count])
            i += count
        else:
            v = vals[i]
            if code.endswith("s"):
                v = v.split(b"\0", 1)[0].decode(errors="replace")
            out[fname] = v
            i += 1
    return out


class Codec:
    """Frames outgoing messages and parses an incoming byte stream."""

    def __init__(self, sysid: int, compid: int):
        self.sysid, self.compid, self.seq = sysid, compid, 0
        self.buf = bytearray()

    def pack(self, name: str, **values) -> bytes:
        m = MESSAGES[name]
        payload = encode_payload(name, **values)
        payload = payload.rstrip(b"\0") or b"\0"  # v2 trailing-zero truncation
        hdr = struct.pack(
            "<BBBBBBBHB",
            0xFD,
            len(payload),
            0,
            0,
            self.seq,
            self.sysid,
            self.compid,
            m.id & 0xFFFF,
            m.id >> 16,
        )
        self.seq = (self.seq + 1) & 0xFF
        crc = crc_x25(hdr[1:] + payload)
        crc = crc_x25(bytes([m.crc_extra]), crc)
        return hdr + payload + struct.pack("<H", crc)

    def feed(self, data: bytes) -> list[dict]:
        """Add bytes; return complete, CRC-valid messages we know."""
        self.buf.extend(data)
        out = []
        while True:
            msg = self._next()
            if msg is None:
                return out
            if msg is not False:
                out.append(msg)

    def _next(self):
        b = self.buf
        start = next((i for i, c in enumerate(b) if c in (0xFD, 0xFE)), None)
        if start is None:
            b.clear()
            return None
        del b[:start]
        if len(b) < 2:
            return None
        if b[0] == 0xFD:
            if len(b) < 10:
                return None
            plen, incompat = b[1], b[2]
            total = 10 + plen + 2 + (13 if incompat & 1 else 0)
            if len(b) < total:
                return None
            msgid = b[7] | (b[8] << 8) | (b[9] << 16)
            sysid, compid = b[5], b[6]
            body, payload = bytes(b[1 : 10 + plen]), bytes(b[10 : 10 + plen])
            crc_pos = 10 + plen
        else:
            plen = b[1]
            total = 6 + plen + 2
            if len(b) < total:
                return None
            msgid, sysid, compid = b[5], b[3], b[4]
            body, payload = bytes(b[1 : 6 + plen]), bytes(b[6 : 6 + plen])
            crc_pos = 6 + plen
        m = BY_ID.get(msgid)
        if m is None:
            # Unknown id: its CRC can't be checked, so it may be a false start
            # inside a corrupted frame. Drop only the start byte and rescan;
            # skipping `total` bytes could swallow valid frames behind it.
            del b[:1]
            return False
        crc = crc_x25(bytes([m.crc_extra]), crc_x25(body))
        if crc != struct.unpack_from("<H", b, crc_pos)[0]:
            del b[:1]  # bad CRC: resynchronise on the next start byte
            return False
        del b[:total]
        msg = decode_payload(m, payload)
        msg["_sysid"], msg["_compid"] = sysid, compid
        return msg


class Connection:
    """A self-reconnecting MAVLink link. Call poll() often; send() any time."""

    def __init__(
        self, url: str, sysid: int = 1, compid: int = MAV_COMP_ID_ONBOARD_COMPUTER
    ):
        self.url = url
        self.codec = Codec(sysid, compid)
        self.sock = None
        self.fd = None
        self.peer = None
        self.next_try = 0.0
        self.backoff = 1.0
        self.opens = 0  # incremented on every (re)connect
        self.kind, _, rest = url.partition(":")
        if self.kind not in ("tcp", "udpin", "udpout", "serial"):
            raise ValueError(
                f"unsupported MAVLink URL {url!r} (tcp:, udpin:, udpout:, serial:)"
            )
        self.rest = rest

    @property
    def connected(self) -> bool:
        return self.sock is not None or self.fd is not None

    def _open(self):
        if self.kind == "serial":
            import termios
            import tty

            dev, _, baud = self.rest.partition(":")
            fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            tty.setraw(fd)
            attrs = termios.tcgetattr(fd)
            speed = getattr(termios, f"B{baud or 57600}")
            attrs[4] = attrs[5] = speed
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            self.fd = fd
            return
        host, _, port = self.rest.rpartition(":")
        addr = (host or "0.0.0.0", int(port))
        if self.kind == "tcp":
            s = socket.create_connection(addr, timeout=3)
            s.setblocking(False)
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setblocking(False)
            if self.kind == "udpin":
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(addr)
            else:
                self.peer = addr
        self.sock = s

    def _ensure(self) -> bool:
        if self.connected:
            return True
        now = time.monotonic()
        if now < self.next_try:
            return False
        try:
            self._open()
            log.info("MAVLink link open: %s", self.url)
            self.backoff = 1.0
            self.opens += 1
            return True
        except OSError as e:
            log.warning(
                "MAVLink link %s unavailable (%s); retry in %.0fs",
                self.url,
                e,
                self.backoff,
            )
            self.next_try = now + self.backoff
            self.backoff = min(self.backoff * 2, 30.0)
            return False

    def close(self, why: str = ""):
        if self.connected:
            log.warning("MAVLink link closed: %s %s", self.url, why)
        for closer in (
            lambda: self.sock and self.sock.close(),
            lambda: self.fd is not None and os.close(self.fd),
        ):
            try:
                closer()
            except OSError:
                pass
        self.sock = self.fd = None
        if self.kind == "udpin":
            self.peer = None
        self.next_try = time.monotonic() + self.backoff

    def poll(self, timeout: float) -> list[dict]:
        if not self._ensure():
            time.sleep(min(timeout, max(0.0, self.next_try - time.monotonic())))
            return []
        handle = self.sock if self.sock is not None else self.fd
        try:
            r, _, _ = select.select([handle], [], [], timeout)
            if not r:
                return []
            if self.sock is not None:
                if self.kind == "tcp":
                    data = self.sock.recv(4096)
                    if not data:
                        self.close("(peer closed)")
                        return []
                else:
                    data, addr = self.sock.recvfrom(4096)
                    if self.kind == "udpin":
                        self.peer = addr
            else:
                data = os.read(self.fd, 4096)
        except (BlockingIOError, InterruptedError):
            return []
        except OSError as e:
            self.close(f"({e})")
            return []
        return self.codec.feed(data)

    def send(self, name: str, **values) -> bool:
        if not self._ensure():
            return False
        frame = self.codec.pack(name, **values)
        try:
            if self.kind == "tcp":
                self.sock.sendall(frame)
            elif self.sock is not None:
                if self.peer is None:
                    return False  # udpin: nobody has spoken to us yet
                self.sock.sendto(frame, self.peer)
            else:
                os.write(self.fd, frame)
            return True
        except OSError as e:
            self.close(f"({e})")
            return False
