"""Synth edge detectors: a new reference fires nothing, bump pulses stay bounded."""

from __future__ import annotations

import random
import unittest

from effects import Gains, Synth, effective_gains
from telemetry import Frame, Wheel


def wheels(count: int, deflection: float | list[float]) -> list[Wheel]:
    depths = deflection if isinstance(deflection, list) else [deflection] * count
    return [Wheel(deflection=depth, substance="road", pos_x=-1.0 if k % 2 == 0 else 1.0, velocity=7.0, radius=0.5)
            for k, depth in enumerate(depths)]


def frame(gear: int = 10, deflection: float | list[float] = 0.02, truck_wheels: int = 6, trailer_wheels: int = 0,
          accel: tuple[float, float, float] = (0.2, 0.0, 0.5), **values) -> Frame:
    values.setdefault("trailer_attached", bool(trailer_wheels))
    return Frame(active=True, paused=False, rpm=1300.0, speed=22.0, game_throttle=0.4, user_throttle=0.4,
                 gear=gear, engine_on=True, accel=accel, wheels=wheels(truck_wheels, deflection),
                 trailer_wheels=wheels(trailer_wheels, 0.02), **values)


def synth() -> Synth:
    return Synth(effective_gains(Gains()))


def effects_of(s: Synth) -> list[str]:
    return [effect for effect, _, _ in s.bursts]


class GearTest(unittest.TestCase):
    def test_first_frame_in_gear_is_not_a_shift(self) -> None:
        s = synth()
        s.render(frame(gear=7))  # a new session, or a reconnect, mid-drive
        self.assertEqual(s.levels.events, [])
        self.assertNotIn("gear", effects_of(s))

    def test_shift_out_of_neutral_clicks(self) -> None:
        s = synth()
        s.render(frame(gear=0))
        s.render(frame(gear=1))
        self.assertIn("gear", effects_of(s))

    def test_shift_while_driving_clicks(self) -> None:
        s = synth()
        s.render(frame(gear=7))
        s.render(frame(gear=8))
        self.assertIn("gear", effects_of(s))


class ResumeTest(unittest.TestCase):
    def gap(self, s: Synth) -> None:
        for _ in range(50):
            s.render(frame(gear=10))
        for _ in range(50):
            s.render(None)  # paused, menu, loading or stalled
        s.bursts.clear()

    def test_resume_takes_a_new_reference(self) -> None:
        s = synth()
        self.gap(s)
        # after a load or a ferry: suspension 5 mm lower, another gear, a trailer, a different pull
        s.render(frame(gear=3, deflection=0.025, accel=(3.0, 0.0, -14.0), trailer_wheels=4, park_brake=True))
        self.assertEqual(s.levels.events, [])
        self.assertEqual(effects_of(s), [])

    def test_detectors_work_again_after_resume(self) -> None:
        s = synth()
        self.gap(s)
        s.render(frame(deflection=0.025))
        s.render(frame(deflection=0.035))  # a real 10 mm step on the next block
        self.assertIn("bumps", effects_of(s))

    def test_game_event_on_resume_still_fires(self) -> None:
        s = synth()
        self.gap(s)
        s.render(frame(game_events=frozenset({"ferry"})))
        self.assertIn("game_events", effects_of(s))


class BumpTest(unittest.TestCase):
    def test_one_pulse_per_wheel_on_rough_ground(self) -> None:
        s, rng = synth(), random.Random(1)
        for _ in range(300):  # 10 truck and 24 trailer wheels, every one tripping most blocks
            f = frame(truck_wheels=10, trailer_wheels=24)
            for wheel in f.wheels + f.trailer_wheels:
                wheel.deflection = 0.02 + rng.gauss(0.0, 0.004)
            s.render(f)
            self.assertLessEqual(effects_of(s).count("bumps"), 34)

    def test_pulse_rises_to_the_stronger_hit(self) -> None:
        s = synth()
        for depth in (0.02, 0.0225, 0.0325):  # a pothole: a slight dip first, then the hard edge
            s.render(frame(deflection=depth, truck_wheels=1))
        bumps = [burst for effect, _, burst in s.bursts if effect == "bumps"]
        self.assertEqual(len(bumps), 1)
        self.assertAlmostEqual(bumps[0].amplitude, 1.0)


if __name__ == "__main__":
    unittest.main()
