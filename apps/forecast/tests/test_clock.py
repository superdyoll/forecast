"""The shared clock: $SCOUT_CLOCK, real time when unset, and bad values."""

import os
import unittest
from datetime import datetime, timezone

from scout_forecast import clock


class Clock(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop(clock.ENV, None)

    def tearDown(self):
        os.environ.pop(clock.ENV, None)
        if self.saved is not None:
            os.environ[clock.ENV] = self.saved

    def test_real_time_when_unset(self):
        self.assertEqual(clock.rate(), 1.0)
        delta = clock.now() - datetime.now(timezone.utc)
        self.assertLess(abs(delta.total_seconds()), 1)

    def test_simulated_time_runs_at_its_rate(self):
        start = clock.parse_iso("2025-07-23T06:00Z")
        os.environ[clock.ENV] = clock.make(start, rate=20, real=0.0)
        self.assertEqual(clock.rate(), 20.0)
        self.assertGreater(clock.now().year, 2026)  # 20x since 1970
        os.environ[clock.ENV] = clock.make(start, rate=1)
        self.assertLess(abs((clock.now() - start).total_seconds()), 1)

    def test_parse_iso(self):
        self.assertEqual(clock.parse_iso(" 2025-07-23T06:00Z ").tzinfo, timezone.utc)
        naive = clock.parse_iso("2025-07-23T06:00")
        self.assertEqual(naive.isoformat(), "2025-07-23T06:00:00+00:00")
        local = clock.parse_iso("2025-07-23T08:00+02:00")
        self.assertEqual(local.isoformat(), "2025-07-23T06:00:00+00:00")

    def test_bad_values_are_refused_with_a_clear_message(self):
        for spec in (
            "2025-07-23T06:00Z@0x0",  # stopped: decisions would divide by zero
            "2025-07-23T06:00Z@0x-5",  # backwards
            "2025-07-23T06:00Z",  # no anchor
            "garbage",
            "2025-07-23T06:00Z@soonx2",
        ):
            os.environ[clock.ENV] = spec
            with self.subTest(spec), self.assertRaises(ValueError) as e:
                clock.now()
            self.assertIn("SCOUT_CLOCK", str(e.exception))


if __name__ == "__main__":
    unittest.main()
