"""`scout-sim score` on a small made-up SITL run directory."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path

from scout_forecast import clock
from scout_lowpower.mission import Mission, save
from scout_sim import run
from scout_sim.score import score

T0 = clock.parse_iso("2025-07-23T06:00Z")


def make_run(d: Path):
    """2 h of truth a minute apart, a decision every 10 min forecasting the
    battery 0.5 points high, transit then hold at 07:30 (deadline 08:00)."""
    (d / "state").mkdir(parents=True)
    save(
        Mission(
            kind="transit", waypoints=[[60.3, 2.2]], arrive_by=T0 + timedelta(hours=2)
        ),
        d / "state" / "mission.json",
    )
    pct = lambda k: 70 - k / 60  # noqa: E731  one point an hour
    with open(d / "sim-truth.jsonl", "w") as fh:
        for k in range(121):
            t = T0 + timedelta(minutes=k)
            fh.write(
                json.dumps(
                    {
                        "time": t.isoformat(),
                        "pct": pct(k),
                        "drift_n": 0.1,
                        "drift_e": 0.2,
                        "storm": 0.5 if 30 <= k < 60 else 0.0,
                    }
                )
                + "\n"
            )
        fh.write('{"half')  # a crash mid-line
    with open(d / "state" / "history.jsonl", "w") as fh:
        for k in range(0, 120, 10):
            t = T0 + timedelta(minutes=k)
            fc = [[(t + timedelta(hours=1)).isoformat(), pct(k + 60) + 0.5]]
            fh.write(
                json.dumps(
                    {
                        "time": t.isoformat(),
                        "battery_pct": pct(k),
                        "forecast": fc,
                        "action": "transit" if k < 90 else "hold",
                        "speed_ms": 0.5,
                        "sog": 0.75,
                        "mode": "AUTO",
                        "current_ms": [0.1, 0.5],
                    }
                )
                + "\n"
            )
    (d / "sim.log").write_text("WARNING SITL runs at x4.0 but the clock assumes x5\n")


class Score(unittest.TestCase):
    def test_scores_a_run(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            make_run(d)
            s = score(d)
            self.assertEqual(s["boat_hours"], 2.0)
            self.assertEqual(s["decisions"], 12)
            e = s["forecast_error"]["1h"]
            self.assertEqual(e["n"], 7)  # made at 0-60 min: their hour is in the truth
            self.assertAlmostEqual(e["mae"], 0.5)
            self.assertAlmostEqual(e["bias"], 0.5)
            self.assertAlmostEqual(e["mae_persistence"], 1.0)
            self.assertEqual(s["arrived"], (T0 + timedelta(minutes=90)).isoformat())
            self.assertAlmostEqual(s["drift_error_ms"], 0.3)
            self.assertEqual(s["sog_over_command"], 1.5)  # ArduPilot ran fast
            self.assertEqual((s["storm_max"], s["pace_warnings"]), (0.5, 1))
            self.assertTrue((d / "score.json").exists())

    def test_cli_table_and_an_empty_run(self):
        with tempfile.TemporaryDirectory() as d:
            good, empty = Path(d) / "good", Path(d) / "empty"
            make_run(good)
            empty.mkdir()
            buf = io.StringIO()
            with redirect_stdout(buf):
                run.main(["score", str(good), str(empty)])
            lines = buf.getvalue().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("-30m", lines[1])  # arrived 30 min before the deadline
            self.assertIn("no truth or no decisions", lines[2])


if __name__ == "__main__":
    unittest.main()
