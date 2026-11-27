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
    POSITION_DELAY_S = 0.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        # The service plans on the real clock (e.g. November at 60 N: little sun
        # for its 48 h look-ahead), so start it well charged (24.5 V = 90 %).
        self.rover = FakeRover(
            position_delay_s=self.POSITION_DELAY_S, volt=24.5
        ).start()
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

        # A real autopilot has been up a while before it reboots; give the fake
        # some uptime so its boot clock visibly goes back (the only sign of a
        # reboot shorter than the heartbeat timeout).
        time.sleep(3)
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


class QuickRebootSeenBeforeItsBootClock(unittest.TestCase):
    """A reboot shorter than the heartbeat timeout is only recognised from the
    boot clock in GLOBAL_POSITION_INT. If heartbeats of the disarmed, rebooted
    autopilot arrive first, the service must not take "disarmed" as what the
    operator wanted (it failed this way on a slow CI runner)."""

    def test_rearms_when_position_lags_the_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            rover = FakeRover(position_delay_s=2.0, volt=24.5).start()
            rover.armed = True
            save(
                Mission(kind="transit", waypoints=[[60.30, 2.20]]),
                state / "mission.json",
            )
            st = S.Settings(
                mavlink=f"tcp:127.0.0.1:{rover.port}", state_dir=state, interval_s=3
            )
            svc = S.Service(st, E.Params())
            t = threading.Thread(target=svc.run, daemon=True)
            t.start()
            try:
                self.assertTrue(wait(lambda: rover.mode == M.MODE_AUTO and rover.armed))
                time.sleep(3)  # uptime, so the boot clock visibly goes back
                rover.restart(down_s=1)
                self.assertTrue(
                    wait(lambda: rover.mode == M.MODE_AUTO and rover.armed, 30),
                    "did not re-arm after a quick reboot",
                )
            finally:
                svc.stop = True
                t.join(5)
                rover.close()


if __name__ == "__main__":
    unittest.main()
