"""Service integration test against a fake ArduPilot (no SITL needed).

Covers the failure modes from the brief: autopilot restart, the autopilot
link going away and coming back, plus operator override and service restart.
Takes about 40 s.
"""

import tempfile
import threading
import time
import unittest
from pathlib import Path

from fake_autopilot import FakeRover

from scout_forecast import energy as E
from scout_lowpower import mavlink as M
from scout_lowpower import service as S
from scout_lowpower.mission import Mission, save


def wait(cond, timeout=20.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.2)
    return False


class ServiceRecovers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.rover = FakeRover().start()
        self.rover.armed = True
        save(
            Mission(kind="transit", waypoints=[[60.30, 2.20], [60.50, 2.20]]),
            self.state / "mission.json",
        )
        st = S.Settings(
            mavlink=f"tcp:127.0.0.1:{self.rover.port}",
            state_dir=self.state,
            interval_s=3,
        )
        self.svc = S.Service(st, E.Params())
        self.thread = threading.Thread(target=self.svc.run, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.svc.stop = True
        self.thread.join(5)
        self.rover.close()
        self.tmp.cleanup()

    def engaged(self):
        r = self.rover
        return len(r.mission) == 4 and r.mode == M.MODE_AUTO and "WP_SPEED" in r.params

    def test_restart_link_loss_override_and_service_restart(self):
        r = self.rover
        self.assertTrue(wait(self.engaged), "mission, AUTO and speed not set")
        self.assertEqual(r.mission[-1][0], M.MAV_CMD_NAV_LOITER_UNLIM)

        r.restart(down_s=2)  # reboots disarmed in MANUAL
        self.assertTrue(
            wait(lambda: r.mode == M.MODE_AUTO and r.armed, 30),
            "did not re-assert AUTO and re-arm after autopilot restart",
        )

        r.stop()  # autopilot gone
        time.sleep(8)
        r.mode = 0
        r.start()
        self.assertTrue(
            wait(lambda: r.mode == M.MODE_AUTO, 30), "did not recover after link loss"
        )

        time.sleep(1)
        r.mode = 0  # operator takes manual control
        time.sleep(6)
        self.assertEqual(r.mode, 0, "service fought an operator override")
        self.assertTrue(self.svc.override)

        r.mode = M.MODE_HOLD  # operator hands back
        self.assertTrue(
            wait(lambda: r.mode == M.MODE_AUTO and not self.svc.override, 15)
        )

        self.svc.stop = True
        self.thread.join(5)
        again = S.Service(self.svc.st, E.Params())
        self.assertIsNotNone(again.plan, "plan not restored after service restart")
        self.assertIsNotNone(again.uploaded)
        self.assertTrue((self.state / "decision.json").exists())


if __name__ == "__main__":
    unittest.main()
