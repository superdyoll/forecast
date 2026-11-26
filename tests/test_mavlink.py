"""MAVLink codec tests. The pymavlink cross-check runs only if it's installed."""

import random
import unittest

from scout_forecast.lowpower import mavlink as M

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


if __name__ == "__main__":
    unittest.main()
