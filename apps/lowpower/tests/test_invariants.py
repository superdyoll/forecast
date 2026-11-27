"""Properties the planner must keep for any boat state, checked over a few
hundred random ones (seeded, so a failure repeats), and a handful of saved
decisions that catch behaviour nobody meant to change."""

import copy
import math
import random
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from scout_forecast import energy as E
from scout_lowpower import planner as P
from scout_lowpower.mission import Mission

PARAMS = E.fitted_params()
CFG = P.Config()
N = 150


def random_case(rnd: random.Random):
    """A boat state and mission anywhere in the space the service can see."""
    t = datetime(2025, rnd.choice((3, 7, 11)), rnd.randint(1, 28), tzinfo=timezone.utc)
    t += timedelta(minutes=rnd.randrange(24 * 60))
    reserve = rnd.choice((10, 30, 50))
    if rnd.random() < 0.6:
        m = Mission(
            kind="transit",
            waypoints=[
                [60.2 + rnd.uniform(-0.2, 0.2), 2.2 + rnd.uniform(-0.3, 0.3)]
                for _ in range(rnd.randint(1, 3))
            ],
            arrive_by=(
                t + timedelta(hours=rnd.uniform(-2, 30)) if rnd.random() < 0.5 else None
            ),
            min_reserve_pct=reserve,
        )
    else:
        m = Mission(
            kind="station",
            station=[60.2, 2.2],
            min_reserve_pct=reserve,
            hold_until=(
                t + timedelta(hours=rnd.uniform(-5, 80)) if rnd.random() < 0.3 else None
            ),
        )

    def vec(scale):
        a, r = rnd.uniform(0, 2 * math.pi), rnd.uniform(0, scale)
        return (r * math.cos(a), r * math.sin(a))

    pct = rnd.uniform(0, 100)
    s = P.BoatState(
        time=t,
        lat=60.2 + rnd.uniform(-0.05, 0.05),
        lon=2.2 + rnd.uniform(-0.05, 0.05),
        battery_pct=pct,
        volt=20 + pct / 20,
        next_wp=rnd.randint(0, 2),
        battery_w=rnd.choice((None, rnd.uniform(-80, 150))),
        cloud=rnd.choice((None, rnd.uniform(0.05, 1.0))),
        current_ms=vec(1.0),
        current_mean_ms=vec(0.3),
        stw_max_ms=rnd.choice((None, rnd.uniform(0.2, 1.0))),
        at_final=rnd.random() < 0.1,
    )
    return s, m


class Invariants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rnd = random.Random(20251127)
        cls.cases = []
        for _ in range(N):
            s, m = random_case(rnd)
            before = copy.deepcopy((s, m))
            cls.cases.append((s, m, P.decide(s, m, PARAMS, CFG), before))

    def check(self, test):
        """test(state, mission, plan, inputs before) for every case."""
        for i, (s, m, plan, before) in enumerate(self.cases):
            with self.subTest(case=i, pct=round(s.battery_pct, 1), kind=m.kind):
                test(s, m, plan, before)

    def test_below_the_floor_the_motors_stop(self):
        def test(s, m, plan, _):
            if s.battery_pct < m.min_reserve_pct - CFG.floor_pct:
                self.assertIn(plan.action, ("pause", "drift"))
                self.assertEqual(plan.starlink_duty, CFG.starlink_duty_min)

        self.check(test)

    def test_actions_and_speeds_agree(self):
        def test(s, m, plan, _):
            self.assertIn(plan.action, ("transit", "pause", "hold", "drift"))
            if plan.action == "transit":
                self.assertIn(plan.speed_ms, CFG.speeds_ms)
                self.assertEqual(m.kind, "transit")
            else:
                self.assertIsNone(plan.speed_ms)
            if m.kind == "station":
                self.assertIn(plan.action, ("hold", "drift"))
            duties = (CFG.starlink_duty_normal, CFG.starlink_duty_min)
            self.assertIn(plan.starlink_duty, duties)

        self.check(test)

    def test_feasible_means_above_reserve_plus_margin(self):
        def test(s, m, plan, _):
            line = m.min_reserve_pct + CFG.margin_pct
            if plan.feasible:
                self.assertGreaterEqual(plan.min_pct, line - 0.01)
            else:
                self.assertLess(plan.min_pct, line + 0.01)
            self.assertLessEqual(plan.min_pct, plan.end_pct + 1e-9)
            self.assertTrue(0 <= plan.min_pct <= 100)

        self.check(test)

    def test_forecast_and_eta_are_well_formed(self):
        def test(s, m, plan, _):
            times = [datetime.fromisoformat(t) for t, _ in plan.forecast]
            self.assertTrue(times)
            steps = {b - a for a, b in zip(times, times[1:])}
            self.assertLessEqual(steps, {timedelta(hours=1)})
            self.assertEqual(times[0] - s.time, timedelta(hours=1))
            if plan.eta is not None:
                self.assertEqual(plan.action, "transit")
                self.assertGreaterEqual(plan.eta, s.time)
            self.assertTrue(plan.reasons)

        self.check(test)

    def test_decide_is_pure(self):
        def test(s, m, plan, before):
            self.assertEqual((s, m), before, "decide() changed its inputs")

        self.check(test)
        s, m, plan, _ = self.cases[0]
        self.assertEqual(P.decide(s, m, PARAMS, CFG).to_dict(), plan.to_dict())

    def etas(self, s, m):
        from scout_lowpower.mission import legs

        route = legs(m, (s.lat, s.lon), s.next_wp)
        return route, [
            P._transit_option(s, m, PARAMS, CFG, v, 0.25, route).hours_to_go
            for v in CFG.speeds_ms
        ]

    def test_eta_never_later_at_a_higher_speed_in_a_steady_current(self):
        def test(s, m, plan, _):
            if m.kind != "transit":
                return
            steady = replace(s, current_mean_ms=s.current_ms)
            _, hours = self.etas(steady, m)
            for a, b in zip(hours, hours[1:]):
                self.assertLessEqual(b, a + 1e-9, hours)

        self.check(test)

    def test_beyond_reach_a_faster_command_changes_nothing(self):
        # With the current changing, one leg: once the hull is at its top
        # speed, asking for more gives the same ETA, not a later one. (Over
        # several legs a faster boat can meet a stronger current on a later
        # leg and really arrive later: one command holds for the whole route.)
        def test(s, m, plan, _):
            if m.kind != "transit":
                return
            route, hours = self.etas(s, m)
            if len(route) != 1:
                return
            # Within 36 s: where a command just within reach meets the first
            # one beyond it, their mid-leg times (and currents) differ a little.
            for a, b in zip(hours, hours[1:]):
                self.assertLessEqual(b, a + 0.01, hours)

        self.check(test)


class Model(unittest.TestCase):
    """The battery projection the planner relies on."""

    def project(self, pct, load_w, cloud=0.5, net0=None):
        t = datetime(2025, 7, 23, 6, tzinfo=timezone.utc)
        return E.project(
            PARAMS,
            t,
            60.2,
            2.2,
            pct,
            20 + pct / 20,
            lambda k, t: load_w,
            48,
            cloud=cloud,
            net0=net0,
        )["pct"]

    def test_more_battery_never_forecasts_less(self):
        rnd = random.Random(1)
        for _ in range(50):
            pct, load = rnd.uniform(0, 90), rnd.uniform(5, 150)
            lo, hi = self.project(pct, load), self.project(
                pct + rnd.uniform(0.1, 10), load
            )
            # Within 0.05 points: sag scales with current (power / the starting
            # voltage), so once both runs are empty the one that started lower
            # shows a little more rise per watt when the sun returns (~0.03).
            self.assertTrue(all(h >= l_ - 0.05 for l_, h in zip(lo, hi)))

    def test_more_load_never_forecasts_more(self):
        rnd = random.Random(2)
        for _ in range(50):
            pct, load = rnd.uniform(5, 95), rnd.uniform(5, 100)
            light, heavy = self.project(pct, load), self.project(
                pct, load + rnd.uniform(1, 50)
            )
            self.assertTrue(all(h <= l_ + 1e-9 for l_, h in zip(light, heavy)))

    def test_stays_within_0_and_100(self):
        for pct, load in ((99.9, 0.0), (0.5, 300.0)):
            self.assertTrue(
                all(0 <= v <= 100 for v in self.project(pct, load, cloud=1.0))
            )


# Saved decisions: (description, BoatState kwargs, mission, expected).
# min_pct is checked to within half a point; update deliberately, with the
# reason in the commit, when the planner is meant to change.
T0 = datetime(2025, 7, 23, 6, tzinfo=timezone.utc)
ROUTE = [[60.30, 2.20], [60.50, 2.20]]
SAVED = [
    (
        "calm transit, no deadline",
        dict(battery_pct=80),
        dict(kind="transit", waypoints=ROUTE),
        ("transit", 0.9, 0.25, 64.9),
    ),
    (
        "transit with a 30 h deadline",
        dict(battery_pct=60),
        dict(kind="transit", waypoints=ROUTE, arrive_by=T0 + timedelta(hours=30)),
        ("transit", 0.4, 0.25, 60.0),
    ),
    (
        "33 km into a 0.5 m/s current: unaffordable",
        dict(battery_pct=60, current_ms=(-0.5, 0.0), current_mean_ms=(-0.5, 0.0)),
        dict(kind="transit", waypoints=ROUTE),
        ("pause", None, 0.05, 60.1),
    ),
    (
        "low battery at night",
        dict(battery_pct=38, time=T0.replace(hour=21), cloud=0.2),
        dict(kind="transit", waypoints=ROUTE),
        ("pause", None, 0.05, 33.1),
    ),
    (
        "below the floor",
        dict(battery_pct=25),
        dict(kind="transit", waypoints=ROUTE),
        ("pause", None, 0.05, 25.2),
    ),
    (
        "station, calm",
        dict(battery_pct=60),
        dict(kind="station", station=[60.2, 2.2]),
        ("hold", None, 0.25, 60.0),
    ),
    (
        "station in a gale at night",
        dict(
            battery_pct=40, time=T0.replace(hour=21), cloud=0.1, current_ms=(0.0, 0.9)
        ),
        dict(kind="station", station=[60.2, 2.2]),
        ("drift", None, 0.05, 32.3),
    ),
]


class SavedDecisions(unittest.TestCase):
    def test_saved_decisions(self):
        for why, kw, mission, (action, speed, duty, low) in SAVED:
            kw = {"time": T0, "lat": 60.2, "lon": 2.2, **kw}
            kw.setdefault("volt", 20 + kw["battery_pct"] / 20)
            plan = P.decide(
                P.BoatState(**kw), Mission(min_reserve_pct=30, **mission), PARAMS
            )
            with self.subTest(why):
                self.assertEqual(
                    (plan.action, plan.speed_ms, plan.starlink_duty),
                    (action, speed, duty),
                    plan.reasons,
                )
                self.assertAlmostEqual(plan.min_pct, low, delta=0.5)


if __name__ == "__main__":
    unittest.main()
