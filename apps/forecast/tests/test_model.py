"""The telemetry loader, the energy model, its fit and the commands, on a
synthetic Tether export (scout_forecast.testdata) with known parameters."""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from pathlib import Path

from scout_forecast import data as D
from scout_forecast import energy as E
from scout_forecast import evaluate, forecast
from scout_forecast import testdata as T

TMP = tempfile.TemporaryDirectory()
CSV = T.write(Path(TMP.name) / "telemetry.csv", days=4)


def tearDownModule():
    TMP.cleanup()


def quiet(fn, *args):
    """Run fn(*args) with stdout and stderr captured; returns (result, stdout)."""
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        r = fn(*args)
    return r, out.getvalue()


class ParseTime(unittest.TestCase):
    def test_variable_length_fractions_and_none(self):
        t = D.parse_time("2025-07-14 00:20:51.0605+00")
        self.assertEqual(
            (t.second, t.microsecond, t.utcoffset()), (51, 60500, timedelta(0))
        )
        self.assertEqual(D.parse_time("2025-07-14 00:20:51+00").microsecond, 0)


class Loader(unittest.TestCase):
    """Each cleaning rule in scout_forecast.data, on its own quirk."""

    @classmethod
    def setUpClass(cls):
        cls.rows = D.load(CSV)

    def flagged(self, flag):
        return [r for r in self.rows if flag in r["flags"]]

    def test_sorted_by_receipt_time(self):
        times = [r["time"] for r in self.rows]
        self.assertEqual(times, sorted(times))

    def test_temperature_is_centidegrees(self):
        self.assertAlmostEqual(self.rows[0]["temperature_c"], 31.2)

    def test_globalstar_battery_pct_is_missing_not_zero(self):
        gs = [r for r in self.rows if r["link"] == "globalstar"]
        self.assertTrue(gs)
        self.assertTrue(all(r["battery_pct"] is None for r in gs))
        no_gs = D.load(CSV, include_globalstar=False)
        self.assertEqual(len(no_gs), len(self.rows) - len(gs))

    def test_restart_record_is_flagged_and_its_zeros_are_missing(self):
        (r,) = self.flagged("reboot")
        self.assertEqual(r["mode_name"], "INITIALISING")
        for k in (
            "lat",
            "lon",
            "speed_ms",
            "battery_v",
            "battery_pct",
            "temperature_c",
        ):
            self.assertIsNone(r[k], k)

    def test_backlog_flush_is_flagged(self):
        backlog = self.flagged("backlog")
        self.assertEqual(len(backlog), 2)  # the first of the flush looks normal
        self.assertTrue(all(r["link"] == "imt" for r in backlog))

    def test_impossible_speed_is_dropped(self):
        (r,) = self.flagged("bad_speed")
        self.assertIsNone(r["speed_ms"])

    def test_power_channels(self):
        r = next(r for r in self.rows if r["link"] == "direct" and r["solar_w"])
        self.assertAlmostEqual(r["thruster_w"], r["thruster_v"] * r["thruster_a"])
        self.assertAlmostEqual(
            r["other_w"], r["solar_w"] - r["thruster_w"] - r["battery_w"]
        )
        imt = next(r for r in self.rows if r["link"] == "imt" and r["thruster_a"])
        # Iridium has no thruster voltage: load uses the battery bus voltage.
        self.assertIsNone(imt["thruster_v"])
        self.assertAlmostEqual(imt["thruster_w"], imt["battery_v"] * imt["thruster_a"])
        self.assertIsNone(imt["solar_w"])

    def test_resample_leaves_out_backlog_and_averages_angles_on_the_circle(self):
        buckets = D.resample(self.rows, 600)
        flushed = [
            b for b in buckets if b["time"] <= T.BACKLOG_AT + timedelta(minutes=1)
        ]
        self.assertNotIn("backlog", flushed[-1]["flags"])
        h = buckets[0]["heading_deg"]  # 350 and 10 alternate: north, not 180
        self.assertLess(min(h, 360 - h), 1.0)
        times = [b["time"] for b in buckets]
        self.assertEqual(times, sorted(set(times)))

    def test_summary_lists_links_and_restarts(self):
        s = D.summary(self.rows)
        self.assertIn("direct=", s)
        self.assertIn("autopilot restarts:", s)


class BadFiles(unittest.TestCase):
    def test_empty_export_summarises_without_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "empty.csv"
            path.write_text(",".join(T.COLUMNS) + "\n")
            self.assertEqual(D.summary(D.load(path)), "rows: 0")
            self.assertIn("rows: 0", quiet(D.main, ["--csv", str(path)])[1])
            with self.assertRaises(SystemExit):
                E.build_grid(str(path))  # nothing to model: a clear exit

    def test_renamed_columns_are_refused_not_loaded_as_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "other.csv"
            path.write_text(
                "created_at,communication_mode,volts\n2025-07-14 00:00:00+00,imt,24\n"
            )
            with self.assertRaises(ValueError) as e:
                D.load(path)
            self.assertIn("battery_pct", str(e.exception))
            for main in (D.main, evaluate.main):
                with self.assertRaises(SystemExit):
                    quiet(main, ["--csv", str(path)])
            with self.assertRaises(SystemExit):
                quiet(
                    forecast.main, ["--csv", str(path), "--from", "2025-07-14T01:00Z"]
                )


class Gaps(unittest.TestCase):
    def test_short_gaps_filled_long_gaps_kept(self):
        x = [1.0, None, 3.0, None, None, None, 7.0]
        self.assertEqual(
            E.fill_gaps(x, 2, hold=False), [1.0, 2.0, 3.0, None, None, None, 7.0]
        )
        self.assertEqual(E.fill_gaps([0, None, 1], 1, hold=True), [0, 0, 1])
        self.assertEqual(
            E.fill_gaps([None, 1.0], 4, hold=False), [None, 1.0]
        )  # no left edge


class Fit(unittest.TestCase):
    """The fit finds the parameters the synthetic battery was built with."""

    @classmethod
    def setUpClass(cls):
        cls.g = E.build_grid(CSV)
        H = 12 * E.STEPS_PER_H
        cls.p = E.fit(cls.g, E.usable_origins(cls.g, 24 * E.STEPS_PER_H, H), H)

    def test_load_components(self):
        load = self.p.load
        self.assertAlmostEqual(load["hotel_w"], T.HOTEL_W, places=1)
        self.assertAlmostEqual(load["starlink_w"], T.STARLINK_W, places=1)
        self.assertAlmostEqual(load["auto_propulsion_w"], 40.0, places=1)
        self.assertAlmostEqual(load["mode17_propulsion_w"], 4.5, places=1)
        self.assertAlmostEqual(self.p.loss_w, T.TRUTH.loss_w, places=1)

    def test_battery_capacity(self):
        self.assertAlmostEqual(self.p.capacity_wh, T.TRUTH.capacity_wh, delta=200)

    def test_sag_absorbs_the_half_step_timing(self):
        # The model gives battery_pct at the end of each 15-min step; the grid
        # holds the mean over the step, half a step earlier. In that half step
        # the rested level moves by net * STEP_H/2 * 100 / capacity, which is
        # proportional to net power like sag, so the fit takes it as less sag:
        # truth - STEP_H/2 * 100 * V / capacity (V ~ 24.3 V here: ~0.75).
        volts = sum(v for v in self.g["volt"] if v) / sum(
            1 for v in self.g["volt"] if v
        )
        shift = E.STEP_H / 2 * 100 * volts / T.TRUTH.capacity_wh
        effective = T.TRUTH.sag_pct_per_a - shift
        self.assertAlmostEqual(self.p.sag_pct_per_a, effective, delta=0.1)

    def test_solar(self):
        # Clear-sky k and the average cloud factor trade off; their product
        # (what reaches the panels on an average day) is what's identifiable.
        truth = T.TRUTH.solar_k * T.TRUTH.cloud_default
        self.assertAlmostEqual(self.p.solar_k * self.p.cloud_default, truth, delta=10)
        self.assertEqual(self.p.solar_gamma, T.TRUTH.solar_gamma)
        self.assertEqual(self.p.solar_offset_h, 0.0)

    def test_grid_is_regular_and_marks_starlink(self):
        g = self.g
        steps = {b - a for a, b in zip(g["times"], g["times"][1:])}
        self.assertEqual(steps, {timedelta(seconds=E.STEP_S)})
        self.assertIn(True, g["starlink"])
        self.assertIn(False, g["starlink"])


class Hindcast(unittest.TestCase):
    def test_known_plan_beats_persistence(self):
        g = E.build_grid(CSV)
        r = forecast.hindcast(g, T.START + timedelta(days=2), 12)
        self.assertEqual(len(r["steps"]), 12 * E.STEPS_PER_H)
        self.assertLess(r["summary"]["mae"], 1.0)
        self.assertLess(r["summary"]["mae"], r["summary"]["mae_persistence"])
        self.assertIsNone(r["note"])
        self.assertIn("fitted on data outside", r["fitted"])

    def test_past_the_end_uses_the_persisted_load(self):
        g = E.build_grid(CSV)
        r = forecast.hindcast(g, g["times"][-4], 6, params=T.TRUTH)
        self.assertIsNotNone(r["note"])
        self.assertEqual(r["fitted"], "saved parameters")

    def test_start_outside_the_data_is_an_error(self):
        g = E.build_grid(CSV)
        with self.assertRaises(ValueError):
            forecast.hindcast(g, T.START - timedelta(days=1), 6)


class Commands(unittest.TestCase):
    def test_forecast_prints_and_writes_json(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "f.json"
            argv = ["--csv", CSV, "--from", "2025-07-16T00:00Z", "--hours", "6"]
            code, text = quiet(forecast.main, argv + ["--json", str(out)])
            self.assertEqual(code, 0)
            self.assertIn("MAE vs actual", text)
            self.assertEqual(len(json.loads(out.read_text())["steps"]), 24)

    def test_evaluate_writes_params_the_forecast_can_use(self):
        with tempfile.TemporaryDirectory() as d:
            argv = [
                "--csv",
                CSV,
                "--folds",
                "3",
                "--lookback-h",
                "6",
                "--horizon-h",
                "3",
            ]
            code, _ = quiet(evaluate.main, argv + ["--out", d])
            self.assertEqual(code, 0)
            (run,) = Path(d).iterdir()
            res = json.loads((run / "results.json").read_text())
            self.assertGreaterEqual(len(res["folds"]), 2)
            phys = res["summary"]["physical_actual"]["all"]
            self.assertLess(phys, res["summary"]["persistence"]["all"])
            params = E.load_params(run / "params.json")
            self.assertAlmostEqual(params.capacity_wh, T.TRUTH.capacity_wh, delta=300)
            argv = ["--csv", CSV, "--from", "2025-07-16T00:00Z", "--hours", "3"]
            code, text = quiet(
                forecast.main, argv + ["--params", str(run / "params.json")]
            )
            self.assertIn("params from", text)

    def test_scout_data_writes_clean_and_resampled_files(self):
        with tempfile.TemporaryDirectory() as d:
            clean, five = Path(d) / "clean.csv", Path(d) / "5min.csv"
            self.assertEqual(quiet(D.main, ["--csv", CSV, "-o", str(clean)])[0], 0)
            self.assertEqual(clean.read_text().splitlines()[0].split(","), D.OUT_COLS)
            quiet(D.main, ["--csv", CSV, "-o", str(five), "--resample", "300"])
            self.assertEqual(
                five.read_text().splitlines()[0].split(","), D.RESAMPLE_COLS
            )


class Params(unittest.TestCase):
    def test_load_params_falls_back_to_the_shipped_fit(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "params.json"
            bad.write_text("{not json")
            for path in (None, ""):
                self.assertEqual(E.load_params(path), E.fitted_params())
            for path in (Path(d) / "missing.json", bad):
                with self.assertLogs("scout_forecast", "ERROR"):
                    self.assertEqual(E.load_params(path), E.fitted_params())

    def test_round_trip_ignores_unknown_keys(self):
        d = {**T.TRUTH.to_dict(), "_source": "a note"}
        self.assertEqual(E.Params.from_dict(d), T.TRUTH)


if __name__ == "__main__":
    unittest.main()
