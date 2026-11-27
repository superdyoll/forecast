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


class Server:
    """scout-web on a free port, for one test."""

    def __init__(self, state_dir, csv=None):
        import threading
        from http.server import ThreadingHTTPServer

        self.srv = ThreadingHTTPServer(
            ("127.0.0.1", 0), web.make_handler(web.App(state_dir, csv))
        )
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def get(self, path, etag=None):
        """(status, ETag, body) of GET path; HTTP errors are returned too."""
        import urllib.error
        import urllib.request

        req = urllib.request.Request(self.base + path)
        if etag:
            req.add_header("If-None-Match", etag)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers["ETag"], r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers["ETag"], e.read()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Hindcast(unittest.TestCase):
    """/api/config and /api/hindcast, on a synthetic telemetry CSV."""

    @classmethod
    def setUpClass(cls):
        from scout_forecast import testdata

        cls.tmp = tempfile.TemporaryDirectory()
        d = Path(cls.tmp.name)
        csv = testdata.write(d / "telemetry.csv", days=4)
        cls.web = Server(d, csv)
        cls.bare = Server(d)  # no CSV configured

    @classmethod
    def tearDownClass(cls):
        cls.web.close()
        cls.bare.close()
        cls.tmp.cleanup()

    def json(self, server, path):
        code, _, body = server.get(path)
        return code, json.loads(body)

    def test_config_says_what_can_be_hindcast(self):
        code, c = self.json(self.web, "/api/config")
        self.assertEqual(code, 200)
        self.assertTrue(c["hindcast"])
        self.assertTrue(c["data_from"] < c["data_to"])
        self.assertFalse(self.json(self.bare, "/api/config")[1]["hindcast"])

    def test_hindcast_scores_a_past_window(self):
        code, r = self.json(self.web, "/api/hindcast?from=2025-07-16T00:00Z&hours=6")
        self.assertEqual(code, 200)
        self.assertEqual(len(r["steps"]), 24)
        self.assertLess(r["summary"]["mae"], r["summary"]["mae_persistence"])

    def test_bad_requests(self):
        for path, code in (
            ("/api/hindcast?from=2025-07-16T00:00Z&plan=guess", 400),
            ("/api/hindcast?hours=6", 400),  # no start
            ("/api/hindcast?from=yesterday", 400),
            ("/api/hindcast?from=2024-01-01T00:00Z", 400),  # outside the data
            ("/api/live?hours=lots", 400),
            ("/nothing-here", 404),
        ):
            got, body = self.json(self.web, path)
            self.assertEqual(got, code, path)
            self.assertIn("error", body)
        got, body = self.json(self.bare, "/api/hindcast?from=2025-07-16T00:00Z")
        self.assertEqual(got, 404)
        self.assertIn("SCOUT_TELEMETRY_CSV", body["error"])

    def test_page_is_served(self):
        code, _, body = self.web.get("/")
        self.assertEqual(code, 200)
        self.assertIn(b"<html", body)


class LivePolling(unittest.TestCase):
    """The page polls /api/live every few seconds: unchanged data is a 304."""

    def test_etag_answers_304_until_the_data_changes(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            history(d, [{"time": T0.isoformat(), "battery_pct": 70}])
            srv = Server(d)

            def get(etag=None):
                return srv.get("/api/live", etag)

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
                srv.close()


if __name__ == "__main__":
    unittest.main()
