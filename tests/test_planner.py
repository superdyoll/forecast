"""Decision-logic tests for the low-power planner (no autopilot needed)."""

import unittest
from datetime import datetime, timedelta, timezone

from scout_forecast import energy as E
from scout_forecast.lowpower.mission import Mission
from scout_forecast.lowpower.planner import BoatState, Config, decide

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

    def test_heavier_load_factor_means_slower(self):
        normal = decide(state(60), transit(), PARAMS)
        headwind = decide(state(60, load_factor=2.5), transit(), PARAMS)
        self.assertLessEqual(headwind.speed_ms, normal.speed_ms)


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

    def test_drifts_and_returns_when_far(self):
        tight = state(34, t=T0.replace(hour=21), cloud=0.1, load_factor=3.0)
        p = decide(tight, self.station(), PARAMS)
        self.assertEqual(p.action, "drift")
        far = BoatState(
            time=tight.time,
            lat=60.21,
            lon=2.20,
            battery_pct=34,
            volt=21.7,
            cloud=0.1,
            load_factor=3.0,
        )  # ~1.1 km away
        self.assertTrue(decide(far, self.station(), PARAMS).returning)

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
