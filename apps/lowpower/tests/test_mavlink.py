"""MAVLink codec tests. The pymavlink cross-check runs only if it's installed."""

import random
import unittest

from scout_lowpower import mavlink as M

try:
    from pymavlink.dialects.v20 import ardupilotmega as pm
except ImportError:  # pragma: no cover
    pm = None


class Codec(unittest.TestCase):
    def test_round_trip_all_messages(self):
        tx, rx = M.Codec(1, 191), M.Codec(1, 1)
        for name in M.MESSAGES:
            (msg,) = rx.feed(tx.pack(name))
            self.assertEqual(msg["name"], name)

    def test_values_survive(self):
        frame = M.Codec(1, 1).pack(
            "MISSION_ITEM_INT",
            seq=3,
            command=16,
            x=602000000,
            y=22000000,
            param3=50.0,
            frame=6,
        )
        (msg,) = M.Codec(1, 1).feed(frame)
        self.assertEqual((msg["seq"], msg["x"], msg["param3"]), (3, 602000000, 50.0))

    def test_resyncs_after_garbage(self):
        hb = M.Codec(1, 1).pack("HEARTBEAT", custom_mode=10, type=11, autopilot=3)
        msgs = M.Codec(1, 1).feed(b"\xfd\x05junk\xfe\x00" + hb)
        self.assertEqual([m["custom_mode"] for m in msgs], [10])

    def test_rejects_bad_crc(self):
        frame = bytearray(M.Codec(1, 1).pack("HEARTBEAT", custom_mode=10))
        frame[-1] ^= 0xFF
        self.assertEqual(M.Codec(1, 1).feed(bytes(frame)), [])

    @unittest.skipIf(pm is None, "pymavlink not installed")
    def test_identical_to_pymavlink(self):
        class _F:
            pass

        mav = pm.MAVLink(_F(), srcSystem=1, srcComponent=191)
        ours = M.Codec(1, 191)
        rnd = random.Random(0)
        gen = {
            "f": lambda: round(rnd.uniform(-100, 100), 3),
            "I": lambda: rnd.randint(0, 2**31),
            "i": lambda: rnd.randint(-(2**30), 2**30),
            "H": lambda: rnd.randint(0, 65535),
            "h": lambda: rnd.randint(-30000, 30000),
            "B": lambda: rnd.randint(0, 255),
            "b": lambda: rnd.randint(-100, 100),
        }
        for name, m in M.MESSAGES.items():
            cls = getattr(pm, f"MAVLink_{name.lower()}_message")
            vals = {}
            for f, code, count in m.fields:
                if code.endswith("s"):
                    vals[f] = "WP_SPEED"
                elif count > 1:
                    vals[f] = [gen[code]() for _ in range(count)]
                else:
                    vals[f] = gen[code]()
            kw = {
                pf: vals[of] for pf, (of, _, _) in zip(cls.ordered_fieldnames, m.fields)
            }
            kw = {k: v.encode() if isinstance(v, str) else v for k, v in kw.items()}
            mav.seq = ours.seq
            ref = cls(**{k: kw[k] for k in cls.fieldnames}).pack(mav)
            self.assertEqual(ours.pack(name, **vals), ref, name)


def free_port(kind=None):
    import socket

    with socket.socket(socket.AF_INET, kind or socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def poll_until(link, name, timeout=3.0):
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for msg in link.poll(0.05):
            if msg["name"] == name:
                return msg
    return None


class Links(unittest.TestCase):
    """The self-reconnecting links, on localhost (serial needs a device)."""

    def test_unsupported_url(self):
        for url in (
            "http:x:1",
            "tcp",
            "tcp:localhost",
            "udpin:0.0.0.0:",
            "serial:",
            "udp:1.2.3.4:5",
        ):
            with self.subTest(url), self.assertRaises(ValueError):
                M.Connection(url)
        for url in (
            "tcp:127.0.0.1:5760",
            "udpin::14550",
            "serial:/dev/ttyACM0",
            "serial:/dev/ttyACM0:115200",
        ):
            M.Connection(url)  # no I/O until the first poll or send

    def test_udp_listen_replies_to_whoever_spoke_last(self):
        port = free_port()
        server = M.Connection(f"udpin:127.0.0.1:{port}")
        client = M.Connection(f"udpout:127.0.0.1:{port}", sysid=2)
        try:
            server.poll(0.01)  # bind
            self.assertFalse(server.send("HEARTBEAT"), "no peer yet: nowhere to send")
            self.assertTrue(client.send("HEARTBEAT", custom_mode=4))
            msg = poll_until(server, "HEARTBEAT")
            self.assertEqual((msg["custom_mode"], msg["_sysid"]), (4, 2))
            self.assertTrue(server.send("STATUSTEXT", text="hello"))
            self.assertEqual(poll_until(client, "STATUSTEXT")["text"], "hello")
        finally:
            server.close()
            client.close()

    def test_refused_tcp_backs_off_then_connects(self):
        import socket
        import time

        port = free_port(socket.SOCK_STREAM)
        link = M.Connection(f"tcp:127.0.0.1:{port}")
        with self.assertLogs("scout_lowpower.mavlink", "WARNING"):
            self.assertEqual(link.poll(0.01), [])
        self.assertFalse(link.connected)
        self.assertEqual(link.backoff, 2.0)  # doubles from 1 s, at most 30 s
        self.assertFalse(link.send("HEARTBEAT"), "sent while backing off")
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        try:
            link.next_try = time.monotonic()  # skip the wait
            self.assertTrue(link.send("HEARTBEAT"))
            self.assertEqual(link.opens, 1)
            peer, _ = srv.accept()
            peer.sendall(M.Codec(1, 1).pack("HEARTBEAT", custom_mode=10))
            self.assertEqual(poll_until(link, "HEARTBEAT")["custom_mode"], 10)
            peer.close()  # autopilot side goes away
            with self.assertLogs("scout_lowpower.mavlink", "WARNING"):
                end = time.monotonic() + 3
                while link.connected and time.monotonic() < end:
                    link.poll(0.05)
            self.assertFalse(link.connected)
            self.assertEqual(link.backoff, 1.0)  # reset by the good connection
        finally:
            link.close()
            srv.close()

    def test_backoff_is_capped(self):
        import socket
        import time

        link = M.Connection(f"tcp:127.0.0.1:{free_port(socket.SOCK_STREAM)}")
        with self.assertLogs("scout_lowpower.mavlink", "WARNING"):
            for _ in range(8):
                link.next_try = time.monotonic()
                link.poll(0)
        self.assertEqual(link.backoff, 30.0)


if __name__ == "__main__":
    unittest.main()
