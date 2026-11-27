"""Web API: scoring past forecasts against the readings that followed."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scout_web import server as web

T0 = datetime(2025, 7, 23, 6, tzinfo=timezone.utc)


def history(path: Path, rows):
    with open(path / "history.jsonl", "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        fh.write('{"half-written')  # a crash mid-line must not break the page


class Scoring(unittest.TestCase):
    def test_scores_forecasts_against_later_readings(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rows = []
            for k in range(8):  # hourly readings: 70, 69, 68, ...
                t = T0 + timedelta(hours=k)
                fc = [
                    [(t + timedelta(hours=h)).isoformat(), 70 - k - h + 0.5]
                    for h in (1, 2, 3)
                ]
                rows.append(
                    {"time": t.isoformat(), "battery_pct": 70 - k, "forecast": fc}
                )
            history(d, rows)
            live = web.live(d, 48)
            self.assertEqual(len(live["history"]), 8)
            e1 = live["errors"]["1h"]
            self.assertEqual(e1["n"], 7)  # last reading has no later actual
            self.assertAlmostEqual(e1["mae"], 0.5)
            self.assertAlmostEqual(e1["bias"], 0.5)  # optimistic by half a point
            self.assertAlmostEqual(e1["mae_persistence"], 1.0)
            self.assertEqual(live["errors"]["3h"]["n"], 5)
            self.assertEqual(live["errors"]["24h"]["n"], 0)

    def test_empty_state_dir(self):
        with tempfile.TemporaryDirectory() as d:
            live = web.live(Path(d), 48)
            self.assertIsNone(live["decision"])
            self.assertEqual(live["history"], [])


if __name__ == "__main__":
    unittest.main()
