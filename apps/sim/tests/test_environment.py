"""Simulator environment and power model sanity checks."""

import math
import unittest
from datetime import datetime, timedelta, timezone

from scout_forecast import energy as E
from scout_sim.environment import EnvConfig, Environment
from scout_sim.power import PowerModel, PowerTruth

T0 = datetime(2025, 7, 23, 6, tzinfo=timezone.utc)


class EnvironmentTests(unittest.TestCase):
    def test_seeded_runs_repeat(self):
        a, b = Environment(EnvConfig(seed=3), T0), Environment(EnvConfig(seed=3), T0)
        t = T0 + timedelta(hours=7.3)
        self.assertEqual(a.drift(t), b.drift(t))
        self.assertEqual(a.cloud(t), b.cloud(t))

    def test_tide_reverses_over_half_a_cycle(self):
        cfg = EnvConfig(current_mean_ms=0.0, tide_minor_ratio=0.0)
        env = Environment(cfg, T0)
        n1, e1 = env.current(T0)
        n2, e2 = env.current(T0 + timedelta(hours=cfg.tide_period_h / 2))
        self.assertAlmostEqual(n1, -n2, places=6)
        self.assertAlmostEqual(e1, -e2, places=6)

    def test_leeway_pushes_downwind(self):
        cfg = EnvConfig(
            current_mean_ms=0.0,
            tide_amp_ms=0.0,
            wind_sd_ms=0.0,
            wind_dir_sd_deg=0.0,
            wind_from_deg=270.0,
        )  # westerly
        n, e = Environment(cfg, T0).drift(T0)
        self.assertGreater(e, 0.1)  # blown east
        self.assertAlmostEqual(n, 0.0, places=6)

    def test_cloud_stays_in_range(self):
        env = Environment(EnvConfig(seed=9), T0)
        vals = [env.cloud(T0 + timedelta(hours=h)) for h in range(0, 24 * 30, 3)]
        self.assertTrue(all(0.05 <= v <= 1.0 for v in vals))


class PowerTests(unittest.TestCase):
    def test_pushing_through_water_drains_more_than_drifting(self):
        night = datetime(2025, 7, 23, 0, tzinfo=timezone.utc)
        idle, moving = (
            PowerModel(PowerTruth(starlink_duty=0), E.Params(), pct0=60)
            for _ in range(2)
        )
        for k in range(36):  # 3 h in 5-min steps
            t = night + timedelta(minutes=5 * k)
            idle.step(t, 300, 60.2, 2.2, 0.0, False, 0.5)
            moving.step(t, 300, 60.2, 2.2, 1.0, True, 0.5)
        self.assertLess(moving.pct, idle.pct - 5)
        self.assertTrue(math.isfinite(moving.volt))


class Params(unittest.TestCase):
    def test_missing_params_file_falls_back_to_defaults(self):
        from scout_sim.run import load_params

        self.assertEqual(load_params("/nonexistent/params.json"), E.Params())
        self.assertEqual(load_params(None), E.Params())


if __name__ == "__main__":
    unittest.main()
