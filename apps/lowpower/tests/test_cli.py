"""The `scout-lowpower` command, as an operator uses it (no autopilot needed)."""

import io
import json
import os
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path

from scout_forecast import clock
from scout_lowpower import cli
from scout_lowpower.mission import load

ENV = (
    "SCOUT_STATE_DIR",
    "STATE_DIRECTORY",
    "SCOUT_MAVLINK",
    "SCOUT_POWER_LINK",
    "SCOUT_MISSION",
    "SCOUT_PARAMS",
    "SCOUT_CLOCK",
)


class Command(unittest.TestCase):
    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in ENV if k in os.environ}
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)

    def tearDown(self):
        for k in ENV:
            os.environ.pop(k, None)
        os.environ.update(self.saved)
        self.tmp.cleanup()

    def run_cli(self, *argv):
        """(exit code, stdout) of scout-lowpower --state-dir <tmp> argv..."""
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = cli.main(["--state-dir", str(self.state), *argv])
        return code, out.getvalue()

    def test_set_mission_transit_with_a_relative_deadline(self):
        before = clock.now()
        code, out = self.run_cli(
            "set-mission",
            "--transit",
            "60.30,2.20",
            "60.50,2.20",
            "--arrive-in",
            "4",
            "--reserve",
            "35",
        )
        self.assertEqual(code, 0)
        m = load(self.state / "mission.json")
        self.assertIn(m.id, out)
        self.assertEqual(m.waypoints, [[60.30, 2.20], [60.50, 2.20]])
        self.assertEqual(m.min_reserve_pct, 35)
        due = m.arrive_by - before
        self.assertTrue(timedelta(hours=4) <= due < timedelta(hours=4, seconds=5))

    def test_arrive_in_is_on_the_simulated_clock(self):
        start = clock.parse_iso("2025-07-23T06:00Z")
        os.environ["SCOUT_CLOCK"] = clock.make(start, rate=1)
        self.run_cli("set-mission", "--transit", "60.3,2.2", "--arrive-in", "2")
        due = load(self.state / "mission.json").arrive_by - start
        self.assertTrue(timedelta(hours=2) <= due < timedelta(hours=2, seconds=5))

    def test_set_mission_station_and_absolute_times(self):
        self.run_cli(
            "set-mission",
            "--station",
            "60.5,2.2",
            "--radius",
            "40",
            "--drift-radius",
            "300",
            "--hold-until",
            "2025-07-28T00:00Z",
        )
        m = load(self.state / "mission.json")
        self.assertEqual(
            (m.kind, m.station, m.radius_m, m.drift_radius_m),
            ("station", [60.5, 2.2], 40, 300),
        )
        self.assertEqual(m.hold_until.isoformat(), "2025-07-28T00:00:00+00:00")

    def test_bad_missions_are_refused(self):
        for argv in (
            ["--transit", "60.3"],  # no longitude
            ["--transit", "91,2"],  # off the globe
            ["--station", "60,2", "--radius", "600"],  # loiter > drift radius
            [
                "--transit",
                "60,2",
                "--arrive-in",
                "1",
                "--arrive-by",
                "2025-07-25T12:00Z",
            ],
        ):
            with self.subTest(argv), self.assertRaises((ValueError, SystemExit)):
                self.run_cli("set-mission", *argv)
        self.assertFalse((self.state / "mission.json").exists())

    def test_status_before_and_after_a_decision(self):
        code, out = self.run_cli("status")
        self.assertEqual(code, 1)
        self.assertIn("no decision yet", out)
        (self.state / "decision.json").write_text(
            json.dumps({"plan": {"action": "hold"}})
        )
        code, out = self.run_cli("status")
        self.assertEqual((code, json.loads(out)["plan"]["action"]), (0, "hold"))

    def test_plan_is_a_what_if(self):
        self.run_cli("set-mission", "--station", "60.2,2.2")
        code, out = self.run_cli(
            "plan",
            "--lat",
            "60.21",
            "--lon",
            "2.2",
            "--pct",
            "60",
            "--time",
            "2025-07-23T06:00Z",
        )
        self.assertEqual(code, 0)
        text, distance = out.rsplit("\n", 2)[0], out.strip().splitlines()[-1]
        plan = json.loads(text)
        self.assertEqual(plan["action"], "hold")
        self.assertNotIn("forecast", plan)
        self.assertRegex(distance, r"distance to station: 11\d\d m")
        self.assertFalse((self.state / "decision.json").exists())  # only a what-if

    def test_a_bad_clock_is_a_clear_error_not_a_crash(self):
        os.environ["SCOUT_CLOCK"] = "2025-07-23T06:00Z@0x0"
        with self.assertRaises(SystemExit) as e:
            self.run_cli("status")
        self.assertIn("bad $SCOUT_CLOCK", str(e.exception))

    def test_plan_without_a_mission(self):
        with self.assertRaises(SystemExit) as e:
            self.run_cli("plan", "--lat", "60", "--lon", "2", "--pct", "50")
        self.assertIn("invalid mission", str(e.exception))


class Settings(unittest.TestCase):
    """Flags win over the environment; the environment over the defaults."""

    def args(self, **kw):
        base = dict(
            state_dir=None,
            mavlink=None,
            power_link=None,
            mission=None,
            params=None,
            interval_s=None,
            dry_run=False,
            no_rearm=False,
            starlink_relay=None,
        )
        return Namespace(**{**base, **kw})

    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in ENV if k in os.environ}

    def tearDown(self):
        for k in ENV:
            os.environ.pop(k, None)
        os.environ.update(self.saved)

    def test_defaults(self):
        st = cli._settings(self.args())
        self.assertEqual(st.state_dir, Path("lowpower-state"))
        self.assertEqual(st.mavlink, "tcp:127.0.0.1:5760")
        self.assertEqual((st.interval_s, st.dry_run, st.rearm), (300.0, False, True))
        self.assertIsNone(st.power_link)
        self.assertEqual(st.mission_path, Path("lowpower-state/mission.json"))

    def test_environment_and_systemd(self):
        os.environ.update(
            STATE_DIRECTORY="/var/lib/a:/var/lib/b",
            SCOUT_MAVLINK="serial:/dev/ttyACM0:115200",
            SCOUT_MISSION="/etc/scout/m.json",
        )
        st = cli._settings(self.args())
        self.assertEqual(st.state_dir, Path("/var/lib/a"))  # first of systemd's list
        self.assertEqual(st.mavlink, "serial:/dev/ttyACM0:115200")
        self.assertEqual(st.mission_path, Path("/etc/scout/m.json"))
        os.environ["SCOUT_STATE_DIR"] = "/srv/scout"
        self.assertEqual(cli._settings(self.args()).state_dir, Path("/srv/scout"))

    def test_flags_win(self):
        os.environ["SCOUT_MAVLINK"] = "tcp:x:1"
        st = cli._settings(
            self.args(
                mavlink="udpin:0.0.0.0:14550",
                interval_s=7.0,
                dry_run=True,
                no_rearm=True,
                starlink_relay=2,
            )
        )
        self.assertEqual(st.mavlink, "udpin:0.0.0.0:14550")
        self.assertEqual(
            (st.interval_s, st.dry_run, st.rearm, st.starlink_relay),
            (7.0, True, False, 2),
        )

    def test_decisions_scale_with_a_fast_simulated_clock(self):
        os.environ["SCOUT_CLOCK"] = clock.make(clock.now(), rate=10)
        self.assertEqual(cli._settings(self.args()).interval_s, 30.0)
        os.environ["SCOUT_CLOCK"] = clock.make(clock.now(), rate=100)
        self.assertEqual(cli._settings(self.args()).interval_s, 15.0)  # floor


if __name__ == "__main__":
    unittest.main()
