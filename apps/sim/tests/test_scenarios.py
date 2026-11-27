"""Regression gate: the whole system (environment, power truth, planner and
current estimate) on the scenarios REPORT.md quotes, with their results as
the floor. About 6 s. A planner change that fails here changes what the boat
does: rerun the REPORT tables and update these numbers deliberately."""

import argparse
import json
import tempfile
import unittest

from scout_sim import fast


def run(*argv):
    """The summary of `scout-sim fast argv...`."""
    ap = argparse.ArgumentParser()
    fast.add_args(ap)
    with tempfile.TemporaryDirectory() as out:
        return fast.run(ap.parse_args([*argv, "--out", out]))


SCENARIOS = {
    "storm": (
        "--days",
        "5",
        "--mission",
        "station",
        "--storm",
        "24:72",
        "--pct0",
        "70",
    ),
    "calm station": ("--days", "2", "--mission", "station"),
    "transit seed 1": ("--days", "0.75", "--seed", "1"),
    "transit seed 2": ("--days", "0.75", "--seed", "2"),
    "transit seed 3": ("--days", "0.75", "--seed", "3"),
}


class Scenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = {name: run(*argv) for name, argv in SCENARIOS.items()}

    def test_a_three_day_storm_from_70_percent_never_breaches_the_reserve(self):
        # REPORT.md "Storms": 30.5 % low, 0 h below the 30 % reserve. Before
        # the storm fixes this ran flat: 0 % and 74 h below.
        s = self.results["storm"]
        self.assertEqual(s["hours_below_reserve"], 0)
        self.assertGreaterEqual(s["battery_min_pct"], 30.0)
        self.assertIn("drift", s["actions_share"])

    def test_calm_days_on_station_stay_well_charged(self):
        s = self.results["calm station"]
        self.assertGreaterEqual(s["battery_min_pct"], 65.0)
        self.assertGreater(s["actions_share"].get("hold", 0), 0.9)

    def test_transits_arrive_on_time_above_the_reserve(self):
        for name in ("transit seed 1", "transit seed 2", "transit seed 3"):
            with self.subTest(name):
                s = self.results[name]
                self.assertIsNotNone(s["arrived"])
                self.assertLessEqual(s["arrived"], s["arrive_by"])
                self.assertEqual(s["hours_below_reserve"], 0)

    def test_forecast_error(self):
        # At 3 h the forecast beats "stays the same" everywhere. Not always at
        # 1 h (storm 1.14 vs 1.07, seed 2 1.43 vs 1.03: the battery barely
        # moves in an hour) or at 6 h on seed 2 (5.8 vs 5.1). Hold those
        # errors where they are.
        for name, s in self.results.items():
            e = s["forecast_error"]
            with self.subTest(name):
                self.assertLess(e["3h"]["mae"], e["3h"]["mae_persistence"])
                self.assertLess(e["1h"]["mae"], 1.6)
                self.assertLess(e["6h"]["mae"], 6.0)

    def test_results_repeat(self):
        again = run(*SCENARIOS["transit seed 2"])
        self.assertEqual(json.dumps(again), json.dumps(self.results["transit seed 2"]))


if __name__ == "__main__":
    unittest.main()
