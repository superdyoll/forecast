"""Water current: estimating it, and what it does to a commanded ground speed."""

import math
import unittest

from scout_forecast import energy as E
from scout_lowpower import current as C
from scout_lowpower.mission import Mission, legs

P = E.fitted_params()
NORTH = (1.0, 0.0)


class AlongLeg(unittest.TestCase):
    def test_still_water(self):
        self.assertEqual(C.along_leg(0.5, NORTH, (0.0, 0.0), 0.85), (0.5, 0.5))

    def test_following_current_needs_less_water_speed(self):
        sog, stw = C.along_leg(0.7, NORTH, (0.45, 0.0), 0.85)
        self.assertAlmostEqual(sog, 0.7)
        self.assertAlmostEqual(stw, 0.25)

    def test_cannot_go_slower_than_the_water(self):
        self.assertEqual(C.along_leg(0.3, NORTH, (0.5, 0.0), 0.85), (0.5, 0.0))

    def test_top_speed_caps_ground_speed(self):
        sog, stw = C.along_leg(1.2, NORTH, (-0.3, 0.0), 0.85)
        self.assertAlmostEqual(sog, 0.55)
        self.assertEqual(stw, 0.85)

    def test_cross_current_costs_water_speed(self):
        sog, stw = C.along_leg(0.6, NORTH, (0.0, 0.3), 0.85)
        self.assertAlmostEqual(sog, 0.6)
        self.assertAlmostEqual(stw, math.hypot(0.6, 0.3))

    def test_no_headway(self):
        sog, _ = C.along_leg(0.5, NORTH, (-1.0, 0.0), 0.85)
        self.assertLessEqual(sog, 0.0)


class Estimate(unittest.TestCase):
    def test_from_thrust_recovers_the_current(self):
        # Heading north at 0.4 m/s through the water, 0.3 m/s current to the east.
        truth = (0.0, 0.3)
        ground = (0.4 + truth[0], truth[1])
        est = C.from_thrust(P, ground, 0.0, E.propulsion_w(P, 0.4))
        self.assertAlmostEqual(est[0], truth[0], places=6)
        self.assertAlmostEqual(est[1], truth[1], places=6)

    def test_blend(self):
        self.assertEqual(C.blend((0.0, 0.0), (1.0, 2.0), 0.5), (0.5, 1.0))


class TopSpeed(unittest.TestCase):
    def test_short_of_the_command_means_at_the_limit(self):
        self.assertAlmostEqual(C.learn_top_speed(None, 1.2, 0.7, 0.3, True), 0.3)
        self.assertAlmostEqual(C.learn_top_speed(0.5, 1.2, 0.7, 0.3, True), 0.44)

    def test_making_the_command_faster_raises_the_cap(self):
        self.assertEqual(C.learn_top_speed(0.3, 0.6, 0.6, 0.5, False), 0.5)

    def test_short_of_the_command_at_low_power_is_not_a_limit(self):
        # Overnight SITL run transit-s1: a window spanning the stationary start
        # read 0.2 m/s over ground for a 0.7 command at a few watts and learnt
        # a 0.40 m/s top speed; the hull then made 0.9.
        self.assertIsNone(C.learn_top_speed(None, 0.7, 0.2, 0.25, False))
        self.assertEqual(C.learn_top_speed(0.8, 0.7, 0.2, 0.25, False), 0.8)

    def test_otherwise_nothing_learnt(self):
        self.assertIsNone(C.learn_top_speed(None, 0.6, 0.6, 0.4, False))
        self.assertEqual(C.learn_top_speed(0.5, 0.6, 0.6, 0.4, False), 0.5)


class Window(unittest.TestCase):
    def test_a_whole_cycle_when_nothing_changed(self):
        self.assertEqual(C.window_s(10_000, 3600), 3600)

    def test_never_back_past_a_change_less_the_settling(self):
        self.assertEqual(C.window_s(1200, 3600), 1200 - C.SETTLE_S)

    def test_none_until_there_is_enough(self):
        self.assertIsNone(C.window_s(C.SETTLE_S + C.MIN_SPAN_S - 1, 3600))
        self.assertEqual(C.window_s(C.SETTLE_S + C.MIN_SPAN_S, 3600), C.MIN_SPAN_S)


class Model(unittest.TestCase):
    def test_fitted_params_ship_with_the_package(self):
        self.assertEqual(P.capacity_wh, 2300)  # run 20261127T233027Z
        self.assertAlmostEqual(E.max_stw_ms(P), 0.85, delta=0.02)
        for v in (0.2, 0.5, 0.8):
            self.assertAlmostEqual(E.stw_for_power(P, E.propulsion_w(P, v)), v)

    def test_legs_follow_the_route(self):
        m = Mission(kind="transit", waypoints=[[60.30, 2.20], [60.30, 2.40]])
        route = legs(m, (60.20, 2.20), 0)
        self.assertEqual(len(route), 2)
        self.assertAlmostEqual(route[0][1][0], 1.0, places=3)  # north
        self.assertAlmostEqual(route[1][1][1], 1.0, places=3)  # then east


if __name__ == "__main__":
    unittest.main()
