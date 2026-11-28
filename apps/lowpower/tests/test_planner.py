"""Decision-logic tests for the low-power planner (no autopilot needed)."""

import unittest
from datetime import datetime, timedelta, timezone

from scout_forecast import energy as E
from scout_lowpower.mission import Mission
from scout_lowpower.planner import BoatState, Config, decide

T0 = datetime(2025, 7, 23, 6, tzinfo=timezone.utc)
# Parameters close to the 17-day fit (REPORT.md).
PARAMS = E.Params(
    solar_k=236,
    solar_gamma=1.5,
    solar_offset_h=-0.5,
    cloud_default=0.63,
    capacity_wh=2200,
    sag_pct_per_a=0.9,
    loss_w=2.0,
    load={
        "hotel_w": 9.9,
        "starlink_w": 22.0,
        "auto_propulsion_w": 60.2,
        "auto_sog_median": 0.8,
        "mode17_propulsion_w": 4.5,
    },
)
WAYPOINTS = [[60.30, 2.20], [60.50, 2.20]]  # ~33 km from START
START = (60.20, 2.20)


def state(pct, t=T0, **kw):
    return BoatState(
        time=t, lat=START[0], lon=START[1], battery_pct=pct, volt=20 + pct / 20, **kw
    )


def transit(**kw):
    return Mission(kind="transit", waypoints=WAYPOINTS, min_reserve_pct=30, **kw)


class TransitDecisions(unittest.TestCase):
    def test_deadline_picks_economical_speed_that_arrives_in_time(self):
        m = transit(arrive_by=T0 + timedelta(hours=20))
        p = decide(state(70), m, PARAMS)
        self.assertEqual(p.action, "transit")
        self.assertTrue(p.feasible)
        self.assertLessEqual(p.eta, m.arrive_by)
        # The next slower speed would miss the deadline.
        speeds = Config().speeds_ms
        slower = speeds[speeds.index(p.speed_ms) - 1]
        self.assertGreater(T0 + (p.eta - T0) * p.speed_ms / slower, m.arrive_by)

    def test_reserve_outranks_deadline(self):
        m = transit(arrive_by=T0 + timedelta(hours=6))  # impossible within reserve
        p = decide(state(70), m, PARAMS)
        self.assertEqual(p.action, "transit")
        self.assertGreaterEqual(p.min_pct, m.min_reserve_pct + Config().margin_pct)
        self.assertGreater(p.eta, m.arrive_by)
        self.assertTrue(any("late" in r for r in p.reasons))

    def test_no_deadline_goes_faster_when_energy_is_plentiful(self):
        rich = decide(state(90), transit(), PARAMS)
        tight = decide(state(42), transit(), PARAMS)
        self.assertGreater(rich.speed_ms, tight.speed_ms)

    def test_pauses_when_no_speed_is_safe(self):
        dark = state(36, t=T0.replace(hour=20), cloud=0.05)
        p = decide(dark, transit(), PARAMS)
        self.assertEqual(p.action, "pause")
        self.assertEqual(p.starlink_duty, Config().starlink_duty_min)

    def test_below_floor_stops_propulsion_immediately(self):
        p = decide(state(25), transit(arrive_by=T0 + timedelta(hours=10)), PARAMS)
        self.assertEqual(p.action, "pause")
        self.assertIsNone(p.speed_ms)

    def test_hysteresis_keeps_recent_speed(self):
        m = transit()
        first = decide(state(90), m, PARAMS)
        first.speed_ms = 0.6  # pretend we chose a slower, still-feasible speed
        later = decide(state(90, t=T0 + timedelta(minutes=5)), m, PARAMS, prev=first)
        self.assertEqual(later.speed_ms, 0.6)
        after_dwell = decide(
            state(90, t=T0 + timedelta(minutes=20)), m, PARAMS, prev=first
        )
        self.assertNotEqual(after_dwell.speed_ms, 0.6)

    def test_hold_expires_when_decisions_are_chained(self):
        # Regression: the hold was timed from the previous *decision*, so a
        # speed decided every 5 min was held forever.
        m = transit()
        p = decide(state(90), m, PARAMS)
        p.speed_ms, p.since = 0.6, T0  # set 0.6 m/s at T0 (feasible, not preferred)
        for minutes in range(5, 35, 5):
            p = decide(state(90, t=T0 + timedelta(minutes=minutes)), m, PARAMS, prev=p)
            if minutes < 15:
                self.assertEqual(p.speed_ms, 0.6, minutes)
        self.assertNotEqual(p.speed_ms, 0.6)
        self.assertGreater(p.since, T0)


class Current(unittest.TestCase):
    """The track runs due north; ArduPilot holds speed over ground."""

    AGAINST = (-0.3, 0.0)  # water going south
    WITH = (0.3, 0.0)

    def test_opposing_current_costs_more_for_the_same_arrival(self):
        m = transit(arrive_by=T0 + timedelta(hours=40))
        calm = decide(state(70), m, PARAMS)
        against = decide(state(70, current_ms=self.AGAINST), m, PARAMS)
        self.assertLessEqual(against.eta, m.arrive_by)
        self.assertLess(against.min_pct, calm.min_pct)
        self.assertTrue(any("against" in r for r in against.reasons))

    def test_following_current_is_cheaper(self):
        m = transit(arrive_by=T0 + timedelta(hours=40))
        calm = decide(state(70), m, PARAMS)
        helped = decide(state(70, current_ms=self.WITH), m, PARAMS)
        self.assertGreater(helped.min_pct, calm.min_pct)
        self.assertTrue(any("with the boat" in r for r in helped.reasons))

    def test_speeds_beyond_reach_collapse_to_the_slowest_command(self):
        # Top speed through the water ~0.85 m/s: against 0.5 m/s the boat makes
        # ~0.35 m/s over ground whatever it asks for above that.
        m = transit()  # no deadline: it would pick the fastest otherwise
        steady = (-0.5, 0.0)
        p = decide(state(95, current_ms=steady, current_mean_ms=steady), m, PARAMS)
        self.assertEqual(p.action, "transit")
        self.assertLessEqual(p.speed_ms, 0.4)
        # The plan never claims more over ground than the hull can make.
        hours = (p.eta - T0).total_seconds() / 3600
        reach = E.max_stw_ms(PARAMS) - 0.5
        self.assertGreaterEqual(hours, 33_000 / reach / 3600)

    def test_a_peak_current_is_not_assumed_to_last(self):
        # Station-keeping against today's peak tidal flow vs. its daily mean.
        m = Mission(kind="station", station=[60.2, 2.2], min_reserve_pct=30)
        peak = (0.6, 0.0)
        lasting = decide(state(60, current_ms=peak, current_mean_ms=peak), m, PARAMS)
        tidal = decide(state(60, current_ms=peak), m, PARAMS)  # mean ~0
        self.assertGreater(tidal.min_pct, lasting.min_pct)

    def test_learnt_top_speed_slows_the_eta(self):
        m = transit()
        fast = decide(state(95), m, PARAMS)
        slow_hull = decide(state(95, stw_max_ms=0.3), m, PARAMS)
        self.assertGreater(slow_hull.eta, fast.eta)
        self.assertLessEqual(slow_hull.speed_ms, 0.3)

    def test_strong_tidal_current_keeps_going_because_it_eases(self):
        # Against 1 m/s now (faster than the boat), but a tide with mean ~0:
        # the planner crawls on and expects to arrive once it slackens.
        p = decide(state(90, current_ms=(-1.0, 0.0)), transit(), PARAMS)
        self.assertEqual(p.action, "transit")
        self.assertIsNotNone(p.eta)

    def test_current_faster_than_the_boat_pauses_when_not_affordable(self):
        night = T0.replace(hour=21)
        steady = (-1.0, 0.0)
        s = state(36, t=night, cloud=0.1, current_ms=steady, current_mean_ms=steady)
        p = decide(s, transit(), PARAMS)
        self.assertEqual(p.action, "pause")
        self.assertTrue(any("no headway" in r for r in p.reasons))


class StationDecisions(unittest.TestCase):
    def station(self, **kw):
        return Mission(
            kind="station",
            station=[60.20, 2.20],
            radius_m=50,
            drift_radius_m=500,
            min_reserve_pct=30,
            **kw,
        )

    def test_holds_when_affordable(self):
        p = decide(state(60), self.station(), PARAMS)
        self.assertEqual(p.action, "hold")

    def test_holding_is_priced_at_the_learnt_top_speed(self):
        # A 0.8 m/s current, but the hull is known to manage only 0.3 m/s
        # through the water: holding can't draw more than that speed costs.
        strong = dict(current_ms=(0.8, 0.0), current_mean_ms=(0.8, 0.0))
        model = decide(state(80, **strong), self.station(), PARAMS)
        learnt = decide(state(80, stw_max_ms=0.3, **strong), self.station(), PARAMS)
        self.assertEqual(model.action, "drift")  # stemming 0.8 m/s: unaffordable
        self.assertEqual(learnt.action, "hold")  # 0.3 m/s worth: affordable

    def test_drifts_with_motors_off_when_holding_is_unaffordable(self):
        # Stemming a 0.8 m/s current takes ~50 W: too much at night with 34 %.
        tight = state(34, t=T0.replace(hour=21), cloud=0.1, current_ms=(0.8, 0.0))
        p = decide(tight, self.station(), PARAMS)
        self.assertEqual(p.action, "drift")
        far = BoatState(
            time=tight.time,
            lat=60.21,
            lon=2.20,
            battery_pct=34,
            volt=21.7,
            cloud=0.1,
            current_ms=(0.8, 0.0),
        )  # ~1.1 km away
        far_plan = decide(far, self.station(), PARAMS)
        self.assertEqual(far_plan.action, "drift")
        self.assertIsNone(far_plan.speed_ms)  # motors off; holding again brings it back

    def test_below_the_floor_never_motors_back(self):
        far = BoatState(
            time=T0.replace(hour=21), lat=60.21, lon=2.20, battery_pct=20, volt=21
        )  # ~1.1 km away, below reserve 30 - floor 3
        p = decide(far, self.station(), PARAMS)
        self.assertEqual(p.action, "drift")
        self.assertIsNone(p.speed_ms)  # motors off

    def test_nothing_affordable_means_motors_off(self):
        far = BoatState(
            time=T0.replace(hour=21),
            lat=60.21,
            lon=2.20,
            battery_pct=36,
            volt=21.8,
            cloud=0.05,
            current_ms=(0.0, 0.6),  # slower than the boat, but costly to stem
        )
        p = decide(far, self.station(), PARAMS)
        self.assertEqual(p.action, "drift")
        self.assertFalse(p.feasible)
        self.assertIsNone(p.speed_ms)  # motors off

    def test_no_futile_return_against_a_gale(self):
        far = BoatState(
            time=T0.replace(hour=21),
            lat=60.21,
            lon=2.20,
            battery_pct=50,
            volt=22.5,
            cloud=0.1,
            current_ms=(0.0, 0.9),  # drift faster than the boat's ~0.85 m/s
        )
        p = decide(far, self.station(), PARAMS)
        self.assertEqual(p.action, "drift")
        self.assertIsNone(p.speed_ms)  # motors off

    def test_bad_weather_is_assumed_to_persist(self):
        m = self.station()
        gloomy = decide(state(60, cloud=0.1), m, PARAMS)
        fair = decide(state(60, cloud=0.63), m, PARAMS)
        shortsighted = decide(
            state(60, cloud=0.1), m, PARAMS, cfg=Config(bad_weather_tau_h=0)
        )
        self.assertLess(gloomy.end_pct, shortsighted.end_pct)
        self.assertLess(gloomy.end_pct, fair.end_pct)

    def test_arrival_switches_to_station_keeping(self):
        at_end = BoatState(
            time=T0, lat=60.50, lon=2.20, battery_pct=60, volt=23, next_wp=1
        )
        p = decide(at_end, transit(), PARAMS)
        self.assertIn(p.action, ("hold", "drift"))


class Arrival(unittest.TestCase):
    def test_arrival_latches_while_loitering(self):
        # Regression: a boat loitering 60 m off the last waypoint flipped
        # back to "transit" because arrival was only "within 30 m".
        m = transit()
        at_end = BoatState(
            time=T0, lat=60.50, lon=2.20, battery_pct=60, volt=23, next_wp=1
        )
        first = decide(at_end, m, PARAMS)
        self.assertIn(first.action, ("hold", "drift"))
        off = BoatState(
            time=T0 + timedelta(minutes=5),
            lat=60.5006,
            lon=2.20,
            battery_pct=60,
            volt=23,
            next_wp=1,
        )  # ~65 m away
        self.assertIn(decide(off, m, PARAMS, prev=first).action, ("hold", "drift"))

    def test_autopilot_on_loiter_item_counts_as_arrived(self):
        s = BoatState(
            time=T0,
            lat=60.5006,
            lon=2.20,
            battery_pct=60,
            volt=23,
            next_wp=1,
            at_final=True,
        )
        self.assertIn(decide(s, transit(), PARAMS).action, ("hold", "drift"))


class PastDeadline(unittest.TestCase):
    def test_says_the_deadline_passed_and_goes_as_fast_as_is_safe(self):
        m = transit(arrive_by=T0 - timedelta(hours=1))
        p = decide(state(60), m, PARAMS)
        self.assertEqual(p.action, "transit")
        self.assertIn("the deadline passed 1.0 h ago", " ".join(p.reasons))
        self.assertNotIn("without going below", " ".join(p.reasons))
        now = decide(state(60), transit(arrive_by=T0), PARAMS)  # due right now
        self.assertIn("the deadline passed 0.0 h ago", " ".join(now.reasons))
        fastest_safe = decide(state(60), transit(), PARAMS, Config(comfort_pct=0))
        self.assertGreaterEqual(p.speed_ms, fastest_safe.speed_ms)


class DeadlineReasons(unittest.TestCase):
    """Overnight SITL run transit-s1 said "deadline can't be met without going
    below 35%" with a forecast low of 70 %: the hull, not energy, was the limit."""

    def test_hull_limited(self):
        m = transit(arrive_by=T0 + timedelta(hours=2))  # 33 km: needs ~4.6 m/s
        p = decide(state(90), m, PARAMS)
        text = " ".join(p.reasons)
        self.assertIn("no speed arrives in time, at the hull's top speed", text)
        self.assertNotIn("without going below", text)

    def test_energy_limited(self):
        # 33 km in 14 h needs ~0.7 m/s; at night with 55 % that would breach
        # the reserve, so it goes at 0.4 m/s and arrives a day late.
        night = T0.replace(hour=20)
        m = transit(arrive_by=night + timedelta(hours=14))
        p = decide(state(55, t=night, cloud=0.1), m, PARAMS)
        self.assertEqual((p.action, p.speed_ms), ("transit", 0.4))
        self.assertGreater(p.eta, m.arrive_by)
        self.assertIn(
            "deadline can't be met without going below 35%", " ".join(p.reasons)
        )


class Geometry(unittest.TestCase):
    def test_legs_cross_the_180th_meridian_the_short_way(self):
        from scout_lowpower.mission import distance_m, legs, unit

        m = Mission(kind="transit", waypoints=[[0.0, -179.99]])
        ((length, u),) = legs(m, (0.0, 179.99), 0)
        self.assertAlmostEqual(u[0], 0.0, places=6)
        self.assertAlmostEqual(u[1], 1.0, places=6)  # east, not round the world west
        self.assertAlmostEqual(length, distance_m((0.0, 179.99), (0.0, -179.99)))
        self.assertAlmostEqual(unit((0.0, -179.99), (0.0, 179.99))[0][1], -1.0)
        self.assertLess(unit((0.0, 179.99), (0.0, -179.99))[1], 2300)  # ~2.2 km

    def test_legs_skip_waypoints_the_boat_is_on(self):
        from scout_lowpower.mission import legs

        m = Mission(kind="transit", waypoints=[[60.2, 2.2], [60.3, 2.2]])
        route = legs(m, (60.2, 2.2), 0)
        self.assertEqual(len(route), 1)
        self.assertAlmostEqual(route[0][1][0], 1.0)  # due north


class StaleIndex(unittest.TestCase):
    def test_stale_loiter_index_far_from_the_end_is_not_arrival(self):
        # Regression: the autopilot's index from the previous mission said
        # "on the loiter item" and the new mission was declared done at once.
        s = state(70, at_final=True)  # 33 km from the last waypoint
        self.assertEqual(decide(s, transit(), PARAMS).action, "transit")


class MissionValidation(unittest.TestCase):
    def test_rejects_bad_missions(self):
        with self.assertRaises(ValueError):
            Mission(kind="transit")
        with self.assertRaises(ValueError):
            Mission(kind="station", station=[95, 0])
        with self.assertRaises(ValueError):
            Mission.from_dict({"kind": "transit", "waypoints": [[1, 1]], "speed": 3})

    def test_round_trip_and_stable_id(self):
        m = transit(arrive_by=T0)
        self.assertEqual(Mission.from_dict(m.to_dict()).id, m.id)


if __name__ == "__main__":
    unittest.main()
