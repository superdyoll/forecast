"""`scout-sim run` against the fake autopilot from scout-lowpower's tests,
standing in for SITL: drift pushed as SIM_TIDE, arming, the power link and
the truth file, the pace warning and a SITL restart."""

import math
import os
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from pathlib import Path

from scout_forecast import clock
from scout_forecast import energy as E
from scout_lowpower import mavlink as M
from scout_lowpower.fake_autopilot import FakeRover
from scout_sim import run


def wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def args(sitl_port, power_port, truth, **kw):
    a = dict(
        sitl=f"tcp:127.0.0.1:{sitl_port}",
        power_out=f"udpout:127.0.0.1:{power_port}",
        truth=str(truth),
        params=None,
        seed=1,
        pct0=70.0,
        lat=60.2,
        lon=2.2,
        arm=True,
        arm_delay=0.5,
        tide_every=0.5,
        truth_every=0.5,
        storm=None,
        storm_cloud=0.08,
        verbose=False,
    )
    return Namespace(**{**a, **kw})


def free_udp_port():
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AgainstAFakeSitl(unittest.TestCase):
    def setUp(self):
        self.saved_clock = os.environ.pop(clock.ENV, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.rover = FakeRover(volt=24.0).start()  # disarmed until the sim arms it
        port = free_udp_port()
        self.power = M.Connection(f"udpin:127.0.0.1:{port}")
        self.power.poll(0.01)  # bind before the sim sends
        self.truth = Path(self.tmp.name) / "truth.jsonl"
        self.sim = run.Sim(args(self.rover.port, port, self.truth), E.fitted_params())
        self.thread = threading.Thread(target=self.sim.run, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.sim.stop = True
        self.thread.join(5)
        self.power.close()
        self.rover.close()
        self.tmp.cleanup()
        if self.saved_clock is not None:
            os.environ[clock.ENV] = self.saved_clock

    def test_drift_arming_power_and_truth(self):
        r = self.rover
        self.assertTrue(wait(lambda: "SIM_TIDE_DIR" in r.params), "no drift pushed")
        self.assertEqual(r.params["SIM_SPEEDUP"], 1.0)
        dn, de = self.sim.env.drift(clock.now())
        self.assertAlmostEqual(r.params["SIM_TIDE_SPEED"], math.hypot(dn, de), places=2)
        self.assertTrue(wait(lambda: r.armed), "operator arming not done")

        seen = {}
        end = time.monotonic() + 5
        while len(seen) < 3 and time.monotonic() < end:
            for m in self.power.poll(0.1):
                if m["name"] == "BATTERY_STATUS":
                    seen[m["id"]] = m
        self.assertEqual(set(seen), {0, 1, 2})  # battery, whole-boat load, solar
        self.assertTrue(23.0 < seen[0]["voltages"][0] / 1000 < 25.0)
        self.assertGreater(seen[1]["current_battery"], 0)  # the load draws current

        self.assertTrue(
            wait(
                lambda: self.truth.exists() and self.truth.read_text().count("\n") >= 2
            )
        )
        line = self.truth.read_text().splitlines()[-1]
        for k in ('"pct"', '"u_water"', '"drift_n"', '"armed"', '"cloud"'):
            self.assertIn(k, line)

    def test_tide_is_given_as_where_it_comes_from(self):
        # SITL's SIM_TIDE_DIR is the direction the water comes FROM.
        self.sim.env.drift = lambda t: (0.0, 0.3)  # water going east
        self.sim.push_drift()
        self.assertTrue(
            wait(lambda: abs(self.rover.params.get("SIM_TIDE_DIR", -1) - 270) < 0.01)
        )
        self.assertAlmostEqual(self.rover.params["SIM_TIDE_SPEED"], 0.3, places=5)

    def test_a_sitl_restart_re_requests_telemetry(self):
        self.assertTrue(
            wait(lambda: self.sim.boot_ms is not None and self.sim.boot_ms > 1000)
        )
        before = len(self.rover.commands)
        with self.assertLogs("scout_sim", "WARNING") as logs:
            self.rover.boot = time.monotonic()  # its boot clock goes back
            self.assertTrue(
                wait(lambda: any("SITL restarted" in x for x in logs.output))
            )
        self.assertTrue(wait(lambda: len(self.rover.commands) > before))


class Pace(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop(clock.ENV, None)

    def tearDown(self):
        os.environ.pop(clock.ENV, None)
        if self.saved is not None:
            os.environ[clock.ENV] = self.saved

    def sim(self):
        return run.Sim(args(1, 1, "unused.jsonl", arm=False), E.fitted_params())

    def test_warns_when_sitl_lags_the_clock(self):
        os.environ[clock.ENV] = clock.make(clock.now(), rate=20)
        s = self.sim()
        s.check_pace(0, 100.0)
        with self.assertLogs("scout_sim", "WARNING") as logs:
            s.check_pace(330_000, 160.0)  # 330 s of SITL in 60 s: x5.5
        self.assertIn("SITL runs at x5.5 but the clock assumes x20", logs.output[0])

    def test_quiet_when_it_keeps_up(self):
        os.environ[clock.ENV] = clock.make(clock.now(), rate=5)
        s = self.sim()
        s.check_pace(0, 100.0)
        with self.assertRaises(AssertionError):  # no warning logged
            with self.assertLogs("scout_sim", "WARNING"):
                s.check_pace(300_000, 160.0)


if __name__ == "__main__":
    unittest.main()
