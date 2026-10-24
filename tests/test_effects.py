"""Synth edge detectors: a new reference fires nothing, bump pulses stay bounded,
bumps do not depend on the game's frame rate."""

from __future__ import annotations

import itertools
import math
import random
import unittest

from effects import DT, TOGGLEABLE, Gains, Synth, effective_gains
from telemetry import Frame, SimulatedTelemetry, Wheel

_render_time = itertools.count(0, 8000)  # us: one new game frame per 8 ms block unless a test says otherwise


def wheels(count: int, deflection: float | list[float]) -> list[Wheel]:
    depths = deflection if isinstance(deflection, list) else [deflection] * count
    return [Wheel(deflection=depth, substance="road", pos_x=-1.0 if k % 2 == 0 else 1.0, velocity=7.0, radius=0.5)
            for k, depth in enumerate(depths)]


def frame(gear: int = 10, deflection: float | list[float] = 0.02, truck_wheels: int = 6, trailer_wheels: int = 0,
          accel: tuple[float, float, float] = (0.2, 0.0, 0.5), **values) -> Frame:
    values.setdefault("trailer_attached", bool(trailer_wheels))
    values.setdefault("render_time", next(_render_time))
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


class LockChopTest(unittest.TestCase):
    def pulses(self) -> list[tuple[int, int]]:
        """(start, length) in ms of the brake trigger's pulses once every wheel locks."""
        s = synth()
        for _ in range(7):  # braking without a lock first, so the chop clock is mid-cycle
            s.render(frame(user_brake=0.9))
        samples = []
        for _ in range(60):
            f = frame(user_brake=0.9)
            for wheel in f.wheels:
                wheel.velocity = 0.0
            samples.extend(s.render(f).trigger)
        # on while the carrier swings within the last 110 Hz cycle
        on = [max(abs(x) for x in samples[max(0, n - 9):n + 1]) > 10.0 for n in range(len(samples))]
        runs, start = [], None
        for n, value in enumerate(on + [False]):
            if value and start is None:
                start = n
            elif not value and start is not None:
                runs.append((start, n - start))
                start = None
        return runs

    def test_pulses_are_even(self) -> None:
        runs = self.pulses()[:-1]  # the last may be cut off
        self.assertGreaterEqual(len(runs), 4)
        lengths = [length for _, length in runs]
        self.assertLessEqual(max(lengths) - min(lengths), 2)
        gaps = [b - a for (a, _), (b, _) in zip(runs, runs[1:])]
        for gap in gaps:  # +-2.5: where the 110 Hz carrier first crosses the detector's level varies
            self.assertAlmostEqual(gap, 1000 / 12, delta=2.5)

    def test_first_pulse_starts_with_the_lock(self) -> None:
        self.assertLessEqual(self.pulses()[0][0], 10)


def drive(fps: float | None, start: float, end: float) -> tuple[list[float], int]:
    """Bump-only left grip over [start, end) s of the scripted drive, and the bump pulses started there."""
    clock = [0.0]
    source = SimulatedTelemetry(fps, clock=lambda: clock[0])
    s = Synth(effective_gains(Gains(disabled=[name for name in TOGGLEABLE if name != "bumps"])))
    left, pulses = [], set()
    for n in range(round(end / DT)):
        clock[0] = n * DT
        block = s.render(source.read())
        if clock[0] >= start:
            left.extend(block.left_grip)
            pulses.update(burst for effect, _, burst in s.bursts if effect == "bumps")
    return left, len(pulses)


def rms(samples: list[float]) -> float:
    return math.sqrt(sum(x * x for x in samples) / len(samples))


class FrameRateTest(unittest.TestCase):
    """The plugin updates once per game frame, so most blocks below 125 fps read a frame again."""

    def test_simulated_drive_holds_frames(self) -> None:
        clock = [0.0]
        source = SimulatedTelemetry(60, clock=lambda: clock[0])
        times = []
        for t in (0.0, 0.008, 0.016, 0.017):
            clock[0] = t
            times.append(source.read().render_time)
        self.assertEqual(times[0], times[2])  # three blocks inside the first 16.7 ms frame
        self.assertGreater(times[3], times[2])

    def test_no_bumps_while_parked_at_any_frame_rate(self) -> None:
        for fps in (None, 30, 60, 144):
            with self.subTest(fps=fps):
                _, pulses = drive(fps, 0.5, 5.0)  # the yard: only the suspension settling
                self.assertEqual(pulses, 0)  # 30 fps used to ring every wheel without end

    def test_gravel_is_not_rougher_at_60_fps(self) -> None:
        continuous, _ = drive(None, 25.0, 28.0)
        at_60, _ = drive(60, 25.0, 28.0)
        self.assertLess(rms(at_60), 1.5 * rms(continuous))  # used to be 8x

    def test_potholes_still_come_through_at_60_fps(self) -> None:
        continuous, _ = drive(None, 16.9, 20.0)
        at_60, _ = drive(60, 16.9, 20.0)
        self.assertGreater(rms(at_60), 0.7 * rms(continuous))
        self.assertLess(rms(at_60), 1.3 * rms(continuous))

    def test_steady_travel_read_again_is_not_a_bump(self) -> None:
        s = synth()
        for n in range(60):
            game_time = math.floor(n * DT * 30) / 30  # a 30 fps game: each frame read by four blocks
            # 0.2 m/s, under the 0.25 m/s threshold; per block it looked like 0.8 m/s
            s.render(frame(deflection=0.02 + 0.2 * game_time, render_time=int(game_time * 1e6)))
        self.assertNotIn("bumps", effects_of(s))

    def test_hitch_is_not_a_bump(self) -> None:
        for hitch in (0.15, 1.0):  # within MAX_FRAME_GAP, then past it
            with self.subTest(hitch=hitch):
                s, game_time = synth(), 0.0
                for n in range(60):
                    game_time += hitch if n == 30 else 1 / 60
                    deflection = 0.02 + 0.006 * math.sin(math.tau * 6 * game_time)  # gravel-like settling
                    s.render(frame(deflection=deflection, render_time=int(game_time * 1e6)))
                self.assertNotIn("bumps", effects_of(s))

    def test_timer_restart_takes_a_new_reference(self) -> None:
        s = synth()
        for n in range(30):
            s.render(frame(render_time=int((10.0 + n / 60) * 1e6)))
        # after a load the game's timer starts over; the truck sits lower and pulls differently
        s.render(frame(deflection=0.03, accel=(3.0, 0.0, -20.0), render_time=0))
        self.assertEqual(effects_of(s), [])
        s.render(frame(deflection=0.04, accel=(3.0, 0.0, -20.0), render_time=int(1e6 / 60)))  # and they work again
        self.assertIn("bumps", effects_of(s))


if __name__ == "__main__":
    unittest.main()
