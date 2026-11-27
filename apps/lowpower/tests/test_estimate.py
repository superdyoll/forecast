"""The service's estimate of the water current and the hull's top speed, from
telemetry (Service._observe_current), without an autopilot or a clock: the
messages and their monotonic times are made up here."""

import math
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from scout_forecast import clock
from scout_forecast import energy as E
from scout_lowpower import mavlink as M
from scout_lowpower import planner as P
from scout_lowpower import service as S

PARAMS = E.fitted_params()
VOLT = 24.0
CURRENT = (0.0, 0.3)  # water going east, m/s
T0 = 10_000.0  # monotonic seconds


def plan(action, speed=None, duty=0.25, settled=True):
    now = clock.now()
    return P.Plan(
        action=action,
        speed_ms=speed,
        starlink_duty=duty,
        feasible=True,
        min_pct=50.0,
        end_pct=50.0,
        eta=None,
        decided_at=now,
        mission_id="m",
        since=now - timedelta(hours=1 if settled else 0),
    )


class Estimate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        st = S.Settings(state_dir=Path(self.tmp.name))
        self.svc = S.Service(st, PARAMS)
        self.svc.ap.mode, self.svc.ap.armed = M.MODE_AUTO, True

    def tearDown(self):
        self.tmp.cleanup()

    def feed(self, stw, heading_deg, seconds=3600, starlink_first_s=900, ground=None):
        """An hour at 1 Hz of a boat holding `stw` through the water on
        `heading_deg` in CURRENT, Starlink on for the first `starlink_first_s`
        of it (a 25 % duty), as the service sees it. Returns the last time."""
        h = math.radians(heading_deg)
        water = (stw * math.cos(h), stw * math.sin(h))
        if ground is None:
            ground = (water[0] + CURRENT[0], water[1] + CURRENT[1])
        tel = self.svc.tel
        for k in range(seconds):
            now = T0 + k
            load = E.load_component(PARAMS, "hotel_w") + E.propulsion_w(PARAMS, stw)
            if k < starlink_first_s:
                load += E.load_component(PARAMS, "starlink_w")
            for msg in (
                {
                    "name": "GLOBAL_POSITION_INT",
                    "lat": 602000000,
                    "lon": 22000000,
                    "vx": round(ground[0] * 100),
                    "vy": round(ground[1] * 100),
                },
                {
                    "name": "VFR_HUD",
                    "groundspeed": math.hypot(*ground),
                    "heading": heading_deg,
                },
                {
                    "name": "BATTERY_STATUS",
                    "id": 1,
                    "voltages": (int(VOLT * 1000),) + (65535,) * 9,
                    "current_battery": round(load / VOLT * 100),
                },
            ):
                tel.update(msg, now)
        return now

    def observe(self, now, times=30):
        for _ in range(times):
            self.svc._observe_current(now)
        return self.svc.current

    def assertCurrent(self, got, want, tol=0.03):
        self.assertLess(math.dist(got, want), tol, f"{got} != {want}")

    def test_current_from_thrust_over_a_whole_starlink_cycle(self):
        # Heading north at 0.5 m/s; the current shows only as the gap between
        # where the boat points (and the power it uses) and where it goes.
        self.svc.plan = plan("transit", 0.5)
        now = self.feed(0.5, 0.0)
        self.assertCurrent(self.observe(now), CURRENT)
        # ~24 h average: barely moved after 30 observations.
        self.assertLess(math.hypot(*self.svc.current_mean), 0.02)

    def test_the_window_keeps_the_whole_hour(self):
        # Regression: telemetry used to be cut to 15 min, so with Starlink on
        # for the first 15 min of the hour the average saw it all-off and read
        # too little propulsion: a slower boat, so a current pushing it ahead.
        self.svc.plan = plan("transit", 0.5)
        now = self.feed(0.5, 0.0)
        self.assertLess(now - self.svc.tel.load[0][0], 3601)
        self.assertGreater(now - self.svc.tel.load[0][0], 3500)
        self.assertCurrent(self.observe(now), CURRENT)

    def test_top_speed_learnt_when_the_boat_cant_make_its_command(self):
        # Asked for 1.0 m/s over ground; it makes ~0.58 at 0.5 through the water.
        self.svc.plan = plan("transit", 1.0)
        now = self.feed(0.5, 0.0)
        self.observe(now, times=1)
        self.assertAlmostEqual(self.svc.stw_max, 0.5, delta=0.02)

    def test_no_top_speed_learnt_when_the_command_is_met(self):
        self.svc.plan = plan("transit", 0.5)
        self.observe(self.feed(0.5, 0.0), times=1)
        self.assertIsNone(self.svc.stw_max)

    def test_drifting_reads_the_current_off_the_ground_velocity(self):
        self.svc.plan = plan("drift")
        self.svc.ap.mode = M.MODE_HOLD
        now = self.feed(0.0, 0.0, ground=(0.1, -0.2))
        self.assertCurrent(self.observe(now), (0.1, -0.2))

    def test_nothing_learnt_when_it_cant_be_trusted(self):
        cases = {
            "plan not settled yet": dict(plan=plan("transit", 0.5, settled=False)),
            "operator in MANUAL": dict(plan=plan("transit", 0.5), mode=0),
            "holding in LOITER but disarmed": dict(
                plan=plan("hold"), mode=M.MODE_LOITER, armed=False
            ),
            "drift plan, armed in AUTO": dict(plan=plan("drift"), mode=M.MODE_AUTO),
        }
        for why, c in cases.items():
            with self.subTest(why):
                self.tearDown()  # a fresh service for each case
                self.setUp()
                self.svc.plan = c["plan"]
                self.svc.ap.mode = c.get("mode", self.svc.ap.mode)
                self.svc.ap.armed = c.get("armed", True)
                self.observe(self.feed(0.5, 0.0, seconds=700), times=3)
                self.assertEqual(self.svc.current, (0.0, 0.0))

    def test_turning_in_circles_has_no_heading(self):
        tel = self.svc.tel
        for k in range(600):
            tel.update(
                {"name": "VFR_HUD", "groundspeed": 0.5, "heading": k * 7}, T0 + k
            )
        self.assertIsNone(tel.mean_heading(600, T0 + 600))
        for k in range(600):
            tel.update(
                {"name": "VFR_HUD", "groundspeed": 0.5, "heading": 355 + k % 2 * 10},
                T0 + 600 + k,
            )
        self.assertLess(
            min(
                tel.mean_heading(600, T0 + 1200), 360 - tel.mean_heading(600, T0 + 1200)
            ),
            0.5,
        )

    def test_estimate_survives_a_service_restart(self):
        self.svc.plan = plan("transit", 0.5)
        self.observe(self.feed(0.5, 0.0))
        self.svc.stw_max = 0.6
        self.svc._save_state()
        again = S.Service(self.svc.st, PARAMS)
        self.assertCurrent(again.current, CURRENT)
        self.assertEqual(again.stw_max, 0.6)
        self.assertEqual(again.plan.action, "transit")


if __name__ == "__main__":
    unittest.main()
