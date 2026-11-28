"""Recovery paths the main service test doesn't reach: the mission file
missing or broken, a corrupt state file, a refused mission upload, older
firmware, station-keeping commands, and heartbeat bookkeeping."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from test_service import wait

from scout_forecast import energy as E
from scout_lowpower import mavlink as M
from scout_lowpower import service as S
from scout_lowpower.fake_autopilot import FakeRover
from scout_lowpower.mission import Mission, save

TRANSIT = Mission(kind="transit", waypoints=[[60.30, 2.20]])


class Running(unittest.TestCase):
    """A service against a fake rover, both torn down after each test."""

    def start(self, rover=None, mission=TRANSIT, **settings):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.rover = (rover or FakeRover(volt=24.5)).start()
        self.rover.armed = True
        if mission is not None:
            save(mission, self.state / "mission.json")
        st = S.Settings(
            mavlink=f"tcp:127.0.0.1:{self.rover.port}",
            state_dir=self.state,
            interval_s=3,
            **settings,
        )
        self.svc = S.Service(st, E.fitted_params())
        self.thread = threading.Thread(target=self.svc.run, daemon=True)
        self.thread.start()

    def tearDown(self):
        if hasattr(self, "svc"):
            self.svc.stop = True
            self.thread.join(5)
            self.rover.close()
            self.tmp.cleanup()


class MissionFile(Running):
    def test_waits_for_a_mission_then_follows_it(self):
        with self.assertLogs("scout_lowpower", "INFO") as logs:
            self.start(mission=None)
            self.assertTrue(wait(lambda: self.svc.ap.online, 10))
            self.assertIsNone(self.svc.plan)
            self.assertEqual(self.rover.mission, [])  # nothing uploaded
            save(TRANSIT, self.state / "mission.json")
            self.assertTrue(wait(lambda: self.rover.mode == M.MODE_AUTO, 20))
        self.assertTrue(any("no mission at" in x for x in logs.output))

    def test_a_broken_edit_keeps_the_previous_mission(self):
        self.start()
        self.assertTrue(wait(lambda: self.svc.plan is not None, 20))
        good = self.svc.mission.id
        with self.assertLogs("scout_lowpower", "ERROR") as logs:
            (self.state / "mission.json").write_text('{"kind": "transit", "waypo')
            self.assertTrue(wait(lambda: logs.output, 10))
        self.assertIn("keeping the previous mission", logs.output[0])
        self.assertEqual(self.svc.mission.id, good)
        self.assertEqual(self.rover.mode, M.MODE_AUTO)


class StateFile(unittest.TestCase):
    def test_corrupt_state_is_ignored_not_fatal(self):
        for text in ("not json", json.dumps({"plan": {"action": "transit"}})):
            with self.subTest(text), tempfile.TemporaryDirectory() as d:
                (Path(d) / "state.json").write_text(text)
                svc = S.Service(S.Settings(state_dir=Path(d)), E.fitted_params())
                self.assertIsNone(svc.plan)
                self.assertEqual(svc.current, (0.0, 0.0))


class Upload(Running):
    def setUp(self):
        self.saved_retry = S.RESYNC_RETRY_S
        S.RESYNC_RETRY_S = 1.0

    def tearDown(self):
        S.RESYNC_RETRY_S = self.saved_retry
        super().tearDown()

    def test_a_refused_upload_is_retried(self):
        # Three attempts per resync, all refused: the next resync succeeds.
        with self.assertLogs("scout_lowpower", "WARNING") as logs:
            self.start(rover=FakeRover(volt=24.5, reject_uploads=3))
            self.assertTrue(wait(lambda: len(self.rover.mission) == 3, 30))
            self.assertTrue(wait(lambda: self.rover.mode == M.MODE_AUTO, 15))
        text = "\n".join(logs.output)
        self.assertIn("mission upload rejected", text)
        self.assertIn("mission upload failed; retrying", text)

    def test_older_firmware_asking_with_mission_request(self):
        self.start(rover=FakeRover(volt=24.5, legacy_requests=True))
        self.assertTrue(wait(lambda: len(self.rover.mission) == 3, 20))
        self.assertEqual(self.rover.mission[-1][0], M.MAV_CMD_NAV_LOITER_UNLIM)


class StationCommands(Running):
    def test_loiter_radius_and_starlink_relay(self):
        station = Mission(kind="station", station=[60.2, 2.2], radius_m=40)
        self.start(mission=station, starlink_relay=3)
        r = self.rover
        self.assertTrue(wait(lambda: r.params.get("LOIT_RADIUS") == 40, 20))
        self.assertEqual(r.mode, M.MODE_AUTO)  # LOITER_UNLIM at the station
        relay = [c for c in r.commands if c[0] == M.MAV_CMD_DO_SET_RELAY]
        self.assertTrue(relay)
        self.assertEqual(relay[0][1], 3)
        self.assertNotIn("WP_SPEED", r.params)  # no cruise speed when holding


class Heartbeats(unittest.TestCase):
    """Autopilot: what counts as the autopilot, and when it's lost or rebooted."""

    def hb(self, mode=M.MODE_AUTO, armed=True, autopilot=3, type_=11):
        return {
            "name": "HEARTBEAT",
            "autopilot": autopilot,
            "type": type_,
            "custom_mode": mode,
            "base_mode": M.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0,
            "_sysid": 1,
            "_compid": 1,
        }

    def pos(self, boot_ms):
        return {"name": "GLOBAL_POSITION_INT", "time_boot_ms": boot_ms, "_compid": 1}

    def test_ground_stations_and_companions_are_ignored(self):
        ap = S.Autopilot()
        self.assertEqual(ap.update(self.hb(), 0.0), ["online"])
        for other in (
            self.hb(mode=0, autopilot=M.MAV_AUTOPILOT_INVALID),
            self.hb(mode=0, type_=M.MAV_TYPE_GCS),
            self.hb(mode=0, type_=M.MAV_TYPE_ONBOARD_CONTROLLER),
        ):
            self.assertEqual(ap.update(other, 1.0), [])
        self.assertEqual((ap.mode, ap.armed), (M.MODE_AUTO, True))

    def test_lost_regained_and_rebooted(self):
        ap = S.Autopilot()
        ap.update(self.hb(), 0.0)
        self.assertFalse(ap.check_timeout(S.HEARTBEAT_TIMEOUT_S - 0.1))
        self.assertTrue(ap.check_timeout(S.HEARTBEAT_TIMEOUT_S + 0.1))
        self.assertFalse(ap.online)
        self.assertFalse(ap.check_timeout(100.0))  # reported once
        self.assertEqual(ap.update(self.hb(mode=16), 101.0), ["regained"])
        self.assertEqual(ap.update(self.pos(60_000), 102.0), [])
        self.assertEqual(ap.update(self.pos(60_100), 102.1), [])  # jitter: fine
        self.assertEqual(ap.update(self.pos(1_000), 103.0), ["rebooted"])
        self.assertEqual(ap.update(self.hb(mode=16), 104.0), ["initialising"])

    def test_boot_state_unknown_until_the_boot_clock_arrives(self):
        ap = S.Autopilot()
        ap.reconnected(10.0)
        self.assertTrue(ap.boot_unknown(10.0 + S.BOOT_CHECK_S - 0.1))
        self.assertFalse(ap.boot_unknown(10.0 + S.BOOT_CHECK_S + 0.1))
        ap.reconnected(50.0)
        ap.update(self.pos(5_000), 51.0)
        self.assertFalse(ap.boot_unknown(51.0))

    def test_restarted_recently(self):
        ap = S.Autopilot()
        self.assertFalse(ap.restarted_recently(0.0))
        ap.restarted_at = 100.0
        self.assertTrue(ap.restarted_recently(100.0 + S.REBOOT_GRACE_S - 1))
        self.assertFalse(ap.restarted_recently(100.0 + S.REBOOT_GRACE_S + 1))


if __name__ == "__main__":
    unittest.main()
