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


class History(unittest.TestCase):
    """What /api/live builds from the service's history files."""

    def rows(self, minutes, start=T0):
        """A reading every `minutes` for 6 h, each with an hourly forecast."""
        out = []
        for k in range(6 * 60 // minutes):
            t = start + timedelta(minutes=k * minutes)
            fc = [[(t + timedelta(hours=h)).isoformat(), 60.0 - h] for h in (1, 2)]
            out.append(
                {"time": t.isoformat(), "battery_pct": 60.0 - k / 10, "forecast": fc}
            )
        return out

    def test_rotated_file_is_read_first_and_merged_in_order(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rows = self.rows(10)
            with open(d / "history.jsonl.1", "w") as fh:  # the older half
                fh.writelines(json.dumps(r) + "\n" for r in rows[:18])
            history(d, rows[18:])
            got = web.read_history(d)
            self.assertEqual([r["time"] for r in got], [r["time"] for r in rows])

    def test_forecasts_thinned_to_one_per_30_min_plus_the_latest(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rows = self.rows(5)[:-1]  # every 5 min from 06:00, the last at 11:50
            history(d, rows)
            made = [f["made"] for f in web.live(d, 48)["forecasts"]]
            self.assertEqual(
                made[:3], [rows[0]["time"], rows[6]["time"], rows[12]["time"]]
            )
            self.assertEqual(made[-1], rows[-1]["time"])  # always the latest
            self.assertEqual(len(made), 12 + 1)

    def test_each_forecast_point_carries_the_reading_that_followed(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            rows = self.rows(7)  # readings never land exactly on the hour
            history(d, rows)
            f = web.live(d, 48)["forecasts"][0]
            (t1, fc1, actual1), (t2, _, _) = f["points"]
            self.assertEqual(fc1, 59.0)
            # 07:00 has readings at 06:56 and 07:03: the nearer one, 07:03.
            want = next(r for r in rows if r["time"].startswith("2025-07-23T07:03"))
            self.assertEqual(actual1, want["battery_pct"])
            late = web.live(d, 48)["forecasts"][-1]
            self.assertIsNone(late["points"][-1][2])  # after the last reading

    def test_hours_limits_the_history_not_the_scoring(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            history(d, self.rows(10))
            short, full = web.live(d, 1), web.live(d, 48)
            self.assertLess(len(short["history"]), len(full["history"]))
            self.assertEqual(short["errors"], full["errors"])

    def test_matcher_ignores_readings_more_than_10_min_away(self):
        rows = [{"_t": T0 + timedelta(minutes=m), "battery_pct": m} for m in (0, 30)]
        at = web.matcher(rows)
        self.assertEqual(at(T0 + timedelta(minutes=9)), 0)
        self.assertEqual(at(T0 + timedelta(minutes=21)), 30)
        self.assertIsNone(at(T0 + timedelta(minutes=15)))
        self.assertIsNone(web.matcher([])(T0))


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
