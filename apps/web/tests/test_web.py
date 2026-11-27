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


class LivePolling(unittest.TestCase):
    """The page polls /api/live every few seconds: unchanged data is a 304."""

    def test_etag_answers_304_until_the_data_changes(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            history(d, [{"time": T0.isoformat(), "battery_pct": 70}])
            srv = ThreadingHTTPServer(
                ("127.0.0.1", 0), web.make_handler(web.App(d, None))
            )
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{srv.server_address[1]}/api/live"

            def get(etag=None):
                req = urllib.request.Request(url)
                if etag:
                    req.add_header("If-None-Match", etag)
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, r.headers["ETag"], r.read()
                except urllib.error.HTTPError as e:
                    return e.code, e.headers["ETag"], e.read()

            try:
                code, tag, body = get()
                self.assertEqual(code, 200)
                self.assertTrue(json.loads(body)["history"])
                self.assertEqual(get(tag)[:1], (304,))
                self.assertEqual(get(tag)[2], b"")
                history(d, [{"time": T0.isoformat(), "battery_pct": 69}])
                code, new_tag, _ = get(tag)
                self.assertEqual(code, 200)
                self.assertNotEqual(new_tag, tag)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
