"""`scout-sim fast`: days of the real planner and current estimate without
SITL. Short runs, checked for what a regression would break first."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scout_forecast import clock
from scout_sim import environment as env
from scout_sim import run


def fast(*argv):
    """Run `scout-sim fast argv...` in a temporary directory: (summary, out dir)."""
    tmp = tempfile.TemporaryDirectory()
    out = Path(tmp.name)
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = run.main(["fast", *argv, "--out", str(out)])
    assert code == 0
    summary = json.loads(buf.getvalue())
    return summary, out, tmp


class Transit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.summary, cls.out, cls.tmp = fast("--days", "0.25", "--arrive-in", "4")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_arrives_on_time_above_the_reserve(self):
        s = self.summary
        self.assertIsNotNone(s["arrived"], "never reached the last waypoint")
        self.assertLessEqual(s["arrived"], s["arrive_by"])
        self.assertEqual(s["hours_below_reserve"], 0)
        self.assertIn("transit", s["actions_share"])

    def test_forecasts_are_scored_and_beat_persistence(self):
        e = self.summary["forecast_error"]["1h"]
        self.assertGreater(e["n"], 30)
        self.assertLess(e["mae"], e["mae_persistence"])

    def test_writes_truth_and_history_as_the_service_does(self):
        truth = (self.out / "truth.jsonl").read_text().splitlines()
        self.assertEqual(len(truth), 6 * 60)  # one line a boat minute
        hist = [
            json.loads(x) for x in (self.out / "history.jsonl").read_text().splitlines()
        ]
        self.assertEqual(len(hist), 6 * 12)  # a decision every 5 boat minutes
        for k in ("time", "battery_pct", "action", "forecast", "current_ms"):
            self.assertIn(k, hist[0])
        self.assertEqual(len(hist[0]["forecast"][0]), 2)  # [iso time, pct]

    def test_the_current_is_estimated(self):
        hist = [
            json.loads(x) for x in (self.out / "history.jsonl").read_text().splitlines()
        ]
        self.assertGreater(max(abs(c) for h in hist for c in h["current_ms"]), 0.05)


class Storm(unittest.TestCase):
    def test_storm_spec(self):
        cfg = env.env_config(3, "24:72", 0.1)
        self.assertEqual(
            (cfg.seed, cfg.storm_start_h, cfg.storm_hours, cfg.storm_cloud),
            (3, 24.0, 72.0, 0.1),
        )
        self.assertIsNone(env.env_config(3, None, 0.1).storm_start_h)
        self.assertEqual(env.env_config(3, "24:72", 0.1).storm_ramp_h, 6.0)
        short = env.env_config(3, "0.5:2", 0.1)
        self.assertEqual(short.storm_ramp_h, 0.5)  # a quarter: still reaches full
        e = env.Environment(short, clock.parse_iso("2025-07-23T06:00Z"))
        self.assertEqual(e.storm(clock.parse_iso("2025-07-23T07:30Z")), 1.0)
        self.assertEqual(env.env_config(3, "1:4:2", 0.1).storm_ramp_h, 2.0)
        for bad in ("24", "1:0", "a:b", "1:2:3:4"):
            with self.assertRaises(ValueError):
                env.env_config(3, bad, 0.1)

    def test_a_storm_on_station_is_survived_with_motors_off(self):
        s, out, tmp = fast(
            "--days", "1", "--mission", "station", "--storm", "2:20", "--pct0", "45"
        )
        try:
            truth = [
                json.loads(x) for x in (out / "truth.jsonl").read_text().splitlines()
            ]
            self.assertGreater(max(t["storm"] for t in truth), 0.99)
            self.assertLess(min(t["cloud"] for t in truth), 0.1)
            self.assertIn("drift", s["actions_share"])  # holding stopped to save energy
            self.assertGreater(s["battery_min_pct"], 15)
        finally:
            tmp.cleanup()

    def test_without_the_current_estimate(self):
        _, out, tmp = fast("--days", "0.1", "--no-current-estimate")
        try:
            hist = [
                json.loads(x) for x in (out / "history.jsonl").read_text().splitlines()
            ]
            self.assertTrue(hist)
        finally:
            tmp.cleanup()


class Clock(unittest.TestCase):
    def test_clock_prints_a_spec_the_services_read(self):
        from scout_forecast import clock

        buf = io.StringIO()
        with redirect_stdout(buf):
            run.main(["clock", "--start", "2025-07-23T06:00Z", "--rate", "20"])
        start, _, rate = clock.parse(buf.getvalue().strip())
        self.assertEqual((start.isoformat(), rate), ("2025-07-23T06:00:00+00:00", 20.0))


if __name__ == "__main__":
    unittest.main()
