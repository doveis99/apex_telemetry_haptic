"""Synth edge detectors: a new reference fires nothing, bump pulses stay bounded,
bumps do not depend on the game's frame rate."""

from __future__ import annotations

import cmath
import itertools
import math
import random
import unittest
from unittest import mock

from apex6 import MAX_SAMPLE, SAMPLE_RATE, Route
from effects import (AMT, BUMP_HZ, BUMP_OVER_RIDE, BUMP_RATTLE_MAX, BUMP_RATTLE_S, BUMP_RIDE_MAX, CAB_FAR_SIDE,
                     CEILING, CONVERTER, DT, ENGINE_STANDING, RUMBLE_SPACING, TAU, TOGGLEABLE, Burst, Equalizer, Gains,
                     LowPass, NoiseBurst, ResonanceCut, Synth, Texture, Tick, effective_gains, side_surface,
                     surface_of)
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
    for key, value in (("rpm", 1300.0), ("speed", 22.0), ("game_throttle", 0.4), ("user_throttle", 0.4),
                       ("engine_on", True)):
        values.setdefault(key, value)
    return Frame(active=True, paused=False, gear=gear, accel=accel, wheels=wheels(truck_wheels, deflection),
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

    def test_selector_moved_on_the_hill_hold_clicks(self) -> None:
        s = synth()  # stopped: the gearbox sits in neutral while the dashboard follows the selector
        s.render(frame(gear=1, gear_engine=0, speed=0.0, user_throttle=0.0, game_throttle=0.0))
        s.render(frame(gear=-1, gear_engine=0, speed=0.0, user_throttle=0.0, game_throttle=0.0))
        self.assertIn("gear", effects_of(s))

    def test_lever_comes_out_softly_then_the_gear_clacks_in(self) -> None:
        s, samples = only("gear"), []  # one sample per ms from the change
        s.render(frame(gear=7))
        for _ in range(50):
            samples.extend(s.render(frame(gear=8)).left_grip)
        clack = max(range(len(samples)), key=lambda n: abs(samples[n]))
        self.assertAlmostEqual(clack, 140, delta=5)  # where the recordings have it
        lever = max(abs(x) for x in samples[20:clack - 10])
        self.assertGreater(lever, 0.1 * abs(samples[clack]))
        self.assertLess(lever, 0.4 * abs(samples[clack]))
        edge = rms(samples[clack:clack + 20])
        self.assertGreater(rms(samples[clack + 50:clack + 70]), 0.25 * edge)  # it rings on, not just a click
        self.assertLess(rms(samples[clack + 200:clack + 220]), 0.1 * edge)  # and is 20 dB down by 0.2 s

    def converter_shift(self, throttle: float) -> list[float]:
        """The left grip from a shift up through a torque converter, alone, one sample per ms."""
        s, samples = Synth(effective_gains(Gains(gearbox=CONVERTER, disabled=[
            name for name in TOGGLEABLE if name != "gear"]))), []
        s.render(frame(gear=7, game_throttle=throttle, user_throttle=throttle))
        for _ in range(60):
            samples.extend(s.render(frame(gear=8, game_throttle=throttle, user_throttle=throttle)).left_grip)
        return samples

    def test_a_torque_converter_pushes_softly_without_the_clack(self) -> None:
        samples = self.converter_shift(0.7)
        height = max(range(len(samples)), key=lambda n: abs(samples[n]))
        self.assertAlmostEqual(height, 60, delta=8)  # a swell, not an edge
        self.assertLess(max(abs(x) for x in samples[:15]), 0.4 * abs(samples[height]))
        self.assertLess(tone_level(samples, 140.0), 0.1 * tone_level(samples, 45.0))  # no clack in it
        self.assertLess(rms(samples[420:440]), 0.1 * rms(samples[50:70]))  # 20 dB down by 0.42 s

    def test_the_converter_push_follows_the_throttle(self) -> None:
        self.assertGreater(rms(self.converter_shift(1.0)), 3.0 * rms(self.converter_shift(0.0)))

    def test_the_converter_push_leaves_the_driving_vibration_alone(self) -> None:
        for gearbox in (AMT, CONVERTER):
            with self.subTest(gearbox):
                s, deepest = Synth(effective_gains(Gains(gearbox=gearbox))), 0.0
                s.render(frame(gear=7))
                for _ in range(50):
                    s.render(frame(gear=8))
                    deepest = max(deepest, s.duck[0], s.duck[1])
                if gearbox == AMT:
                    self.assertGreater(deepest, 0.3)  # the drive breaks off for the lever
                else:
                    self.assertEqual(deepest, 0.0)  # and never does through a converter


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
        s.render(frame(deflection=0.0348))  # that stays, settling: the frame after shows it was no spike
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
            self.assertLessEqual(effects_of(s).count("bumps"), 34 + BUMP_RATTLE_MAX)  # a pulse each, and the rattle

    def test_pulse_rises_to_the_stronger_hit(self) -> None:
        s = synth()
        for depth in (0.02, 0.0225, 0.0325, 0.0324):  # a pothole: a slight dip first, then the hard edge
            s.render(frame(deflection=depth, truck_wheels=1))
        pulses = [entry[1] for entry in s.bump_bursts if entry]
        self.assertEqual(len(pulses), 1)
        self.assertAlmostEqual(pulses[0].amplitude, 1.0)
        self.assertAlmostEqual(pulses[0].freq, BUMP_HZ[1])  # and to its depth

    @staticmethod
    def hit(step: float, trailer: bool = False) -> Synth:
        """One wheel dropping by step metres within a block, and staying there as it settles."""
        s = synth()
        for depth in (0.02, 0.02 + step, 0.02 + 0.97 * step):
            f = frame(deflection=depth, truck_wheels=0 if trailer else 1, trailer_wheels=1 if trailer else 0)
            if trailer:
                f.trailer_wheels[0].deflection = depth
            s.render(f)
        return s

    def test_big_hits_are_lower_and_longer(self) -> None:
        small, big = (self.hit(step).bump_bursts[0][1] for step in (0.0035, 0.012))  # a joint, a pothole
        self.assertLess(big.freq, small.freq - 15.0)
        self.assertGreater(big.decay_s, small.decay_s)

    def test_trailer_hits_land_lighter_and_higher(self) -> None:
        truck, trailer = self.hit(0.012).bump_bursts[0][1], self.hit(0.012, trailer=True).bump_bursts[0][1]
        self.assertGreater(trailer.freq, truck.freq)
        self.assertLess(trailer.amplitude, truck.amplitude)

    def test_a_one_frame_leap_is_no_bump(self) -> None:
        # as the game reports it now and then: one wheel 50 mm up for a frame and straight back
        s, depths = synth(), [0.02] * 6
        for n in range(60):
            depths[1] = 0.07 if n == 30 else 0.02
            s.render(frame(deflection=list(depths)))
            self.assertNotIn("bumps", effects_of(s))
        self.assertLess(max(s.roughness), 0.01)

    def test_a_leap_held_over_a_repeated_frame_is_no_bump(self) -> None:
        # the game sent the leap twice, every wheel the same, then came back
        s, depths = synth(), [0.02] * 6
        for n in range(60):
            depths[1] = 0.07 if n in (30, 31) else 0.02
            s.render(frame(deflection=list(depths)))
            self.assertNotIn("bumps", effects_of(s))

    def test_a_leap_of_a_few_frames_at_walking_pace_is_no_bump(self) -> None:
        # in a yard at 5 km/h: one wheel 50 mm up for three frames, rising, then back
        s, depths = synth(), [0.02] * 6
        for n in range(60):
            depths[1] = {30: 0.07, 31: 0.075, 32: 0.08}.get(n, 0.02 + 0.0001 * (n % 2))
            s.render(frame(deflection=list(depths), speed=1.4))
            self.assertNotIn("bumps", effects_of(s))

    def test_a_kerb_at_walking_pace_still_knocks(self) -> None:
        # the front wheel climbing 40 mm over five frames at 9 km/h, as a wheel rolls up a kerb
        s, depths = synth(), [0.02] * 6
        for n in range(30):
            depths[0] = 0.02 + 0.008 * min(5, max(0, n - 10)) + 0.0001 * (n % 2)
            s.render(frame(deflection=list(depths), speed=2.5))
        self.assertIn("bumps", effects_of(s))

    def test_a_leap_at_speed_is_left_to_the_spike_rules(self) -> None:
        s, depths = synth(), [0.02] * 6
        for n in range(60):
            depths[1] = {30: 0.07, 31: 0.075, 32: 0.08}.get(n, 0.02 + 0.0001 * (n % 2))
            s.render(frame(deflection=list(depths)))  # 22 m/s: a wheel could get there
        self.assertIn("bumps", effects_of(s))

    def test_a_step_knocks_one_frame_late(self) -> None:
        s = synth()
        s.render(frame(deflection=0.02, truck_wheels=1))
        s.render(frame(deflection=0.032, truck_wheels=1))
        self.assertNotIn("bumps", effects_of(s))  # it could still be a spike
        s.render(frame(deflection=0.031, truck_wheels=1))
        self.assertIn("bumps", effects_of(s))

    @staticmethod
    def ride(s: Synth, swing: float, seconds: float = 60.0, **values) -> None:
        """Drive on with every truck wheel moving at about swing m/s, steady enough to stay under the threshold."""
        rng = random.Random(3)
        depths = [0.02] * 6
        for _ in range(round(seconds / DT)):
            depths = [depth + rng.choice((-1.0, 1.0)) * swing * DT for depth in depths]
            s.render(frame(deflection=list(depths), **values))

    def step(self, s: Synth, size: float) -> bool:
        """One truck wheel stepping size m/s for a frame and staying there as it settles; did it knock?"""
        depths = [w.deflection for w in s.held_frame[0].wheels]
        s.bursts.clear()
        for n in range(3):
            depths[0] += size * DT if n == 0 else -0.0001  # a frame that moves nothing is the game repeating one
            s.render(frame(deflection=list(depths)))
        return "bumps" in effects_of(s)

    def test_a_rough_riding_truck_learns_a_higher_threshold(self) -> None:
        calm, rough = synth(), synth()  # each driven in turn: the frames' clock is shared
        self.ride(calm, 0.01)
        self.assertEqual(max(calm.g.bump_threshold, BUMP_OVER_RIDE * calm.ride[0]), calm.g.bump_threshold)
        self.assertTrue(self.step(calm, 0.4))  # a joint the calm truck knocks on
        self.ride(rough, 0.08)
        self.assertFalse(self.step(rough, 0.4))  # is part of the rough one's ride
        self.assertTrue(self.step(rough, 1.5))  # a pothole still knocks

    def test_a_dirt_road_does_not_lift_the_threshold_past_its_cap(self) -> None:
        s = synth()
        self.ride(s, 0.3)  # as much as a dirt road moves the wheels, all the time
        self.assertGreater(BUMP_OVER_RIDE * s.ride[0], 2.0 * BUMP_RIDE_MAX)
        self.assertFalse(self.step(s, 0.4))  # the dirt's own bumps
        self.assertTrue(self.step(s, 0.7))  # a rut in it

    def test_a_pothole_lands_as_hard_on_a_rough_riding_truck(self) -> None:
        calm, rough = synth(), synth()  # each driven in turn: the frames' clock is shared
        self.ride(calm, 0.01)
        self.assertTrue(self.step(calm, 0.9))
        self.ride(rough, 0.08)
        self.assertTrue(self.step(rough, 0.9))
        self.assertAlmostEqual(rough.bump_bursts[0][1].amplitude, calm.bump_bursts[0][1].amplitude, places=2)

    @staticmethod
    def sway(accels: list[float | tuple[float, float, float]], s: Synth | None = None) -> Synth:
        """The synth after the cab's tilt acceleration ran through accels, a frame a block:
        about the roll axis, or as pitch, yaw and roll."""
        s = s or synth()
        for n, accel in enumerate(accels):
            s.render(frame(deflection=0.02 + 0.0001 * (n % 2),
                           cabin_accel=accel if isinstance(accel, tuple) else (0.0, 0.0, accel)))
        return s

    def test_the_cab_sways_past_pavements_own_shaking(self) -> None:
        self.assertEqual(self.sway([0.05, -0.05] * 20).sway, [0.0, 0.0])
        lurch, top = self.sway([0.14] * 30).sway[0], self.sway([0.6] * 30).sway[0]
        self.assertAlmostEqual(lurch, 0.25, places=2)  # as strong as the smallest knock was
        self.assertAlmostEqual(top, 1.0, places=2)
        self.assertEqual(self.sway([0.0] * 10 + [0.6, 0.6] + [0.0] * 40).sway, [0.0, 0.0])  # and settles with it

    def test_the_cab_sways_to_the_side_it_rolls(self) -> None:
        s = self.sway([0.4] * 30)  # rolled positive: the left wheels sprang
        self.assertAlmostEqual(s.sway[1], CAB_FAR_SIDE * s.sway[0], places=3)
        self.sway([-0.4] * 30, s)  # and back as the cab rocks
        self.assertAlmostEqual(s.sway[0], CAB_FAR_SIDE * s.sway[1], places=3)
        pitch = self.sway([(0.4, 0.0, 0.0)] * 30).sway
        self.assertAlmostEqual(pitch[0], pitch[1])

    def test_the_sway_follows_a_rocking_cab_smoothly(self) -> None:
        # a 2 Hz rock, read at the game's ~55 frames a second: each swing one swell, no steps
        s, left, right = synth(), [], []
        for n in range(125):
            accel = 0.4 * math.sin(TAU * 2.0 * round(n * DT / 0.018) * 0.018)
            s.render(frame(deflection=0.02 + 0.0001 * (n % 2), cabin_accel=(0.0, 0.0, accel)))
            left.append(s.sway[0])
            right.append(s.sway[1])
        self.assertGreater(max(left[:31]), 1.5 * max(right[:31]))  # the first quarter second leans left
        self.assertGreater(max(right[31:63]), 1.5 * max(left[31:63]))  # the next one right
        self.assertLess(max(abs(b - a) for a, b in zip(left, left[1:])), 0.15)  # a block moves it a little at a time

    def test_the_sway_stops_with_the_telemetry(self) -> None:
        s = self.sway([0.6] * 30)
        for _ in range(40):
            s.render(None)
        self.assertEqual(s.sway, [0.0, 0.0])

    def test_the_swaying_cab_vibrates_the_grips(self) -> None:
        def grips(accel: float) -> tuple[float, float]:
            s = synth()
            with mock.patch("effects.Texture.next", return_value=0.0):  # the road would shake the grips too
                for n in range(30):
                    block = s.render(frame(deflection=0.02 + 0.0001 * (n % 2), cabin_accel=(0.0, 0.0, accel)))
            return max(map(abs, block.left_grip)), max(map(abs, block.right_grip))
        still, (left, right) = grips(0.0), grips(0.6)
        self.assertGreater(left, still[0] + 20)
        self.assertGreater(left, right)

    def test_a_rocking_cab_leaves_the_road_and_its_rattle_alone(self) -> None:
        def road(accel: float) -> tuple[float, float]:
            s = synth()
            for n in range(100):
                s.render(frame(deflection=0.02 + 0.0001 * (n % 2), cabin_accel=(0.0, 0.0, accel * (-1) ** n)))
            return s.levels.road_left, s.texture[0].rattle.rate
        self.assertEqual(road(0.38), road(0.0))  # a cab rocking after a kerb only sways

    @staticmethod
    def patch() -> float:
        """How much stronger the road is over a rougher patch (8-9 s of 12 s on pavement) than before it."""
        s, levels, depths = synth(), [], [0.02] * 6
        rng = random.Random(5)
        for n in range(1500):
            swing = 0.04 if 1000 <= n < 1125 else 0.02
            depths = [d + rng.choice((-1.0, 1.0)) * swing * DT for d in depths]
            s.render(frame(deflection=list(depths)))
            levels.append(s.levels.road_left)
        before, patch = levels[900:1000], levels[1060:1125]
        return (sum(patch) / len(patch)) / (sum(before) / len(before))

    def test_the_road_follows_a_rough_patch_beyond_its_strength(self) -> None:
        with mock.patch("effects.DETAIL_RANGE", (1.0, 1.0)):
            level_only = self.patch()
        self.assertGreater(self.patch(), 1.1 * level_only)  # the patch felt against the road around it

    def test_the_ride_is_kept_while_parked_and_starts_over_in_another_truck(self) -> None:
        s = synth()
        self.ride(s, 0.08)
        learnt = s.ride[0]
        self.ride(s, 0.0, seconds=30.0, speed=0.0)
        self.assertAlmostEqual(s.ride[0], learnt, places=4)  # but the frame read one late, still moving
        learnt = s.ride[0]
        s.render(frame(trailer_wheels=6))
        self.assertEqual(s.ride[0], learnt)  # hooking a trailer leaves the truck's own ride
        for n in range(2):  # read one frame late
            s.render(frame(deflection=0.02 + 0.0001 * n, truck_wheels=4, trailer_wheels=6))
        self.assertEqual(s.ride[0], 0.0)

    def test_a_pothole_leaves_the_cab_rattling(self) -> None:
        pothole, joint = self.hit(0.012), self.hit(0.0035)
        knocks = [burst for burst in pothole.bump_rattles]
        self.assertGreaterEqual(len(knocks), 2)
        for knock in knocks:  # after the hit, within its tail, softer than it
            self.assertTrue(-BUMP_RATTLE_S <= knock.age < 0)
            self.assertLess(knock.amplitude, 0.5 * pothole.bump_bursts[0][1].amplitude)
        self.assertEqual(joint.bump_rattles, [])


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
        # on while the carrier swings within the last 110 Hz cycle, past a quarter of the peak
        # (the first pulse ramps up from the braking level, so a fixed level would track the gain)
        level = 0.25 * max(abs(x) for x in samples)
        on = [max(abs(x) for x in samples[max(0, n - 9):n + 1]) > level for n in range(len(samples))]
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


class PedalTriggerTest(unittest.TestCase):
    @staticmethod
    def trigger(locked: bool = False, **pedals) -> list[float]:
        """The trigger for 0.4 s once it has faded in, the pedal effect alone and held."""
        s = Synth(effective_gains(Gains(steady_fade=0.0, disabled=[n for n in TOGGLEABLE if n != "trigger"])))
        samples = []
        for _ in range(60):
            f = frame(**pedals)
            if locked:
                for wheel in f.wheels:
                    wheel.velocity = 0.0
            samples.extend(s.render(f).trigger)
        return samples[80:480]

    def test_braking_is_felt_like_the_throttle(self) -> None:
        brake, throttle = rms(self.trigger(user_brake=0.3, user_throttle=0.0)), rms(self.trigger(user_throttle=0.3))
        self.assertTrue(0.5 < brake / throttle < 1.0, brake / throttle)  # 0.25 x pedal made it a sixth

    def test_the_lock_warning_stands_over_a_full_stop(self) -> None:
        full = self.trigger(user_brake=1.0, user_throttle=0.0)
        locked = self.trigger(locked=True, user_brake=1.0, user_throttle=0.0)
        self.assertGreaterEqual(max(map(abs, locked)), max(map(abs, full)))


class TriggerSideTest(unittest.TestCase):
    """The brake's effects play on the left trigger, the throttle's on the right, anything else on both."""

    @staticmethod
    def sides(frames: list[Frame], **gains) -> list[Route | None]:
        s = Synth(effective_gains(Gains(**gains)))
        return [s.render(f).side for f in frames]

    def test_each_pedal_plays_on_its_trigger(self) -> None:
        self.assertEqual(set(self.sides([frame(user_throttle=0.4) for _ in range(10)])), {Route.RIGHT})
        self.assertEqual(set(self.sides([frame(user_throttle=0.0, user_brake=0.4) for _ in range(10)])), {Route.LEFT})

    def test_the_tail_stays_on_its_pedals_trigger(self) -> None:
        s = Synth(effective_gains(Gains()))
        for _ in range(10):
            s.render(frame(user_throttle=0.0, user_brake=0.4))
        blocks = [s.render(frame(user_throttle=0.0, user_brake=0.0)) for _ in range(20)]
        self.assertEqual(blocks[0].side, Route.LEFT)
        self.assertEqual({b.side for b in blocks}, {Route.LEFT, None})
        self.assertEqual(blocks[-1].route, Route.MUTE)  # and once it is quiet the trigger mutes, leaving the side

    def test_a_floored_pedal_knocks_its_own_trigger(self) -> None:
        others = [n for n in TOGGLEABLE if n != "pedal"]
        for pedals, side in (({"user_throttle": 1.0}, Route.RIGHT),
                             ({"user_throttle": 0.0, "user_brake": 1.0}, Route.LEFT)):
            idle = frame(**dict.fromkeys(pedals, 0.0))
            sides = self.sides([idle] + [frame(**pedals) for _ in range(5)], disabled=others)
            self.assertEqual(sides[0], None)
            self.assertEqual(set(sides[1:]), {side}, pedals)

    def test_both_pedals_at_once_play_on_both(self) -> None:
        idle = frame(user_throttle=0.0, user_brake=0.0)
        self.assertEqual(self.sides([idle, frame(user_throttle=1.0, user_brake=1.0)])[1], Route.BOTH)

    def test_another_effect_on_the_trigger_takes_both(self) -> None:
        sides = self.sides([frame(user_throttle=0.4) for _ in range(10)], routes={"engine": "both"})
        self.assertEqual(set(sides), {Route.BOTH})


class SteadyFadeTest(unittest.TestCase):
    @staticmethod
    def drive(s: Synth, seconds: float, substance: str = "road", **values) -> None:
        """Values may be functions of the time in s."""
        for n in range(round(seconds / DT)):
            f = frame(**{key: value(n * DT) if callable(value) else value for key, value in values.items()})
            for wheel in f.wheels:
                wheel.substance = substance
            s.render(f)

    @staticmethod
    def road_gain(s: Synth, substance: str = "road", speed: float = 22.0) -> float:
        """The road's level against its unfaded level."""
        texture = min(1.0, surface_of(substance).level * min(1.0, speed / 25.0) ** 0.7)
        return s.levels.road_left / (s.g.road * texture)

    def test_a_steady_road_settles_to_the_floor(self) -> None:
        s = only("road")
        self.drive(s, 1.0)
        self.assertGreater(self.road_gain(s), 0.9)
        self.drive(s, 30.0)
        self.assertAlmostEqual(self.road_gain(s), 1.0 - s.g.steady_fade, delta=0.03)

    def test_a_new_surface_or_pace_brings_it_back(self) -> None:
        s = only("road")
        self.drive(s, 30.0)
        self.drive(s, 0.2, substance="gravel")
        self.assertGreater(self.road_gain(s, "gravel"), 0.95)
        self.drive(s, 30.0, "gravel")
        self.drive(s, 3.0, "gravel", speed=lambda t: 22.0 - 2.0 * t)  # braking from 79 to 58 km/h
        self.assertGreater(self.road_gain(s, "gravel", speed=16.0), 0.95)

    def test_cruise_drift_and_a_resting_finger_do_not_count(self) -> None:
        s = Synth(effective_gains(Gains(disabled=[n for n in TOGGLEABLE if n not in ("road", "trigger")])))
        self.drive(s, 30.0, speed=lambda t: 22.0 + 0.5 * math.sin(t),  # +-1.8 km/h
                   user_throttle=lambda t: 0.4 + 0.02 * math.sin(7 * t))
        self.assertLess(s.fresh["road"], 0.05)
        self.assertLess(s.fresh["trigger"], 0.05)

    def test_engine_returns_on_a_shift(self) -> None:
        s = only("engine")
        self.drive(s, 30.0)
        settled = s.levels.engine
        self.drive(s, 0.2, gear=11, rpm=1050.0)
        self.assertGreater(s.levels.engine, 1.8 * settled)

    def test_coming_back_from_a_pause_starts_in_full(self) -> None:
        s = only("road")
        self.drive(s, 30.0)
        s.render(None)
        self.drive(s, 0.1)
        self.assertGreater(self.road_gain(s), 0.95)


def only(effect: str) -> Synth:
    return Synth(effective_gains(Gains(disabled=[name for name in TOGGLEABLE if name != effect])))


def left_grip(s: Synth, blocks: int = 60, **values) -> list[float]:
    """400 samples of the left grip once the effect has faded in."""
    samples = []
    for _ in range(blocks):
        samples.extend(s.render(frame(**values)).left_grip)
    return samples[80:480]


def tone_level(samples: list[float], freq: float) -> float:
    """Amplitude of one frequency in the samples (a single DFT bin)."""
    w = math.tau * freq / SAMPLE_RATE
    re = sum(x * math.cos(w * n) for n, x in enumerate(samples))
    im = sum(x * math.sin(w * n) for n, x in enumerate(samples))
    return 2.0 * math.hypot(re, im) / len(samples)


class EngineTest(unittest.TestCase):
    @staticmethod
    def engine(**values) -> list[float]:
        gains = Gains(steady_fade=0.0, disabled=[name for name in TOGGLEABLE if name != "engine"])
        return left_grip(Synth(effective_gains(gains)), blocks=200, **values)[:1500]

    @staticmethod
    def firing_peaks(samples: list[float]) -> list[float]:
        """The crest of each firing: the highest sample between rising zero crossings."""
        peaks, current = [], None
        for a, b in zip(samples, samples[1:]):
            if a <= 0 < b:
                if current is not None:
                    peaks.append(current)
                current = b
            elif current is not None:
                current = max(current, b)
        return peaks

    @staticmethod
    def level(speed: float, throttle: float = 0.0) -> float:
        s = Synth(effective_gains(Gains(steady_fade=0.0)))
        for _ in range(20):
            s.render(frame(speed=speed, rpm=700.0, game_throttle=throttle, user_throttle=throttle))
        return s.levels.engine

    def test_the_idle_shakes_the_cab_once_the_truck_stops(self) -> None:
        rolling, crawling, standing = self.level(5.0), self.level(1.5), self.level(0.0)
        self.assertAlmostEqual(standing, ENGINE_STANDING * rolling)
        self.assertTrue(rolling < crawling < standing)
        revving, driving = self.level(0.0, throttle=1.0), self.level(5.0, throttle=1.0)
        self.assertAlmostEqual(revving, driving)  # the revs on top of the idle are not made stronger

    def firings(self, cylinders: int = 6, **values) -> tuple[float, float]:
        """Spread of the firings' crests over 16 s: the pattern each cycle of the cylinders
        repeats, and what is left from firing to firing."""
        gains = Gains(steady_fade=0.0, cylinders=cylinders,
                      disabled=[name for name in TOGGLEABLE if name != "engine"])
        s, samples = Synth(effective_gains(gains)), []
        for _ in range(2000):
            samples.extend(s.render(frame(**values)).left_grip)
        peaks = self.firing_peaks(samples[400:])
        cylinders = gains.cylinders
        peaks = peaks[:len(peaks) // cylinders * cylinders]
        mean = sum(peaks) / len(peaks)
        pattern = [sum(peaks[i::cylinders]) / len(peaks[i::cylinders]) / mean for i in range(cylinders)]
        imbalance = math.sqrt(sum((p - 1.0) ** 2 for p in pattern) / cylinders)
        jitter = math.sqrt(sum((p / mean - pattern[i % cylinders]) ** 2 for i, p in enumerate(peaks)) / len(peaks))
        return imbalance, jitter

    def test_cylinders_fire_unevenly_most_at_idle(self) -> None:
        # 1300 rpm, not 1500: at 75 Hz the crest's sample repeats every third
        # firing and passes for a cylinder pattern of 0.02
        idle = self.firings(rpm=650.0, game_throttle=0.0, user_throttle=0.0)
        pulling = self.firings(rpm=1300.0, game_throttle=0.8, user_throttle=0.8)
        self.assertGreater(idle[0], 1.3 * pulling[0])  # the idle's lope
        self.assertGreater(pulling[0], 0.03)  # the recorded engines never fire quite alike
        for imbalance, jitter in (idle, pulling):
            self.assertGreater(jitter, 0.07)  # and every firing differs from the last

    def test_a_v8_stays_lumpy_under_load(self) -> None:
        # 1000 rpm: eight cylinders fire at 66.7 Hz, the crest on the same sample every firing
        six = self.firings(rpm=1300.0, game_throttle=0.8, user_throttle=0.8)
        idle = self.firings(8, rpm=650.0, game_throttle=0.0, user_throttle=0.0)
        pulling = self.firings(8, rpm=1000.0, game_throttle=0.8, user_throttle=0.8)
        self.assertGreater(pulling[0], 3.0 * six[0])  # the recorded V8 against the sixes
        self.assertLess(idle[0], 1.3 * pulling[0])  # and no lope of its own at idle
        self.assertGreater(pulling[1], 0.07)

    def test_load_turns_it_harsh(self) -> None:
        def harshness(load: float) -> float:
            samples = self.engine(game_throttle=load, user_throttle=load)  # 1300 rpm, 6 cylinders: 65 Hz
            return tone_level(samples, 195.0) / tone_level(samples, 65.0)
        self.assertGreater(harshness(0.9), harshness(0.0) + 0.15)


class ThrottlePulseTest(unittest.TestCase):
    IDLE = {"rpm": 650.0, "game_throttle": 0.0, "user_throttle": 0.3}  # 32.5 Hz firings: slow enough to see

    @staticmethod
    def drive(pulse: float, brake_first: int = 0) -> tuple[list[float], list[float]]:
        """Trigger envelope (over one 100 Hz carrier cycle) and engine vibration on the grip, throttle held."""
        gains = Gains(throttle_pulse=pulse, steady_fade=0.0,
                      disabled=[name for name in TOGGLEABLE if name not in ("engine", "trigger")])
        s, trigger, grip = Synth(effective_gains(gains)), [], []
        for _ in range(brake_first):  # the trigger follows the brake meanwhile; the engine runs on
            s.render(frame(**dict(ThrottlePulseTest.IDLE, user_throttle=0.0, user_brake=0.5)))
        for _ in range(100):
            block = s.render(frame(**ThrottlePulseTest.IDLE))
            trigger.extend(block.trigger)
            grip.extend(block.left_grip)
        envelope = [max(abs(x) for x in trigger[n - 5:n + 5]) for n in range(205, 795)]
        return envelope, grip[205:795]

    @staticmethod
    def correlation(a: list[float], b: list[float]) -> float:
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
        return cov / math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))

    def test_steady_at_zero(self) -> None:
        envelope, _ = self.drive(0.0)
        self.assertLess(max(envelope) - min(envelope), 0.02 * max(envelope))

    def test_beats_with_the_engine_even_after_braking(self) -> None:
        # The pulse ran on an oscillator of its own that stood still while braking.
        for blocks in (0, 3, 7):
            with self.subTest(brake_first=blocks):
                envelope, grip = self.drive(0.8, brake_first=blocks)
                self.assertGreater(self.correlation(envelope, grip), 0.6)

    def test_each_firing_pulses_as_strong_as_it_fires(self) -> None:
        envelope, grip = self.drive(0.8)
        crests, current = [], None  # the pulse's crest in each firing, cut where the engine rises through zero
        for x, (a, b) in zip(envelope[1:], zip(grip, grip[1:])):
            if a <= 0 < b:
                if current is not None:
                    crests.append(current)
                current = x
            elif current is not None:
                current = max(current, x)
        mean = sum(crests) / len(crests)
        spread = math.sqrt(sum((c - mean) ** 2 for c in crests) / len(crests)) / mean
        self.assertGreater(spread, 0.04)  # a steady sine pulsed every firing alike


class WheelSpinTest(unittest.TestCase):
    # Logged 2026-11-02 13:59:43 on a truck with a driven front axle, 12.5 km/h on light throttle:
    # rim speeds in m/s, front axle first, left then right. The yaw rate is the one the rear axle's
    # spread gives (0.95 m/s over 2 m), about an 8 m radius turning left.
    RIMS = [3.73, 4.61, 3.01, 3.94, 3.08, 4.00]
    POS = [(-1.0, -4.0), (1.0, -4.0), (-1.0, 0.0), (1.0, 0.0), (-1.0, 1.3), (1.0, 1.3)]  # (x, z) in m

    def frame(self, yaw: float, rims: list[float] | None = None, powered: bool = True) -> Frame:
        wheels = [Wheel(velocity=rim / (TAU * 0.5), radius=0.5, pos_x=x, pos_z=z, powered=powered)
                  for rim, (x, z) in zip(rims or self.RIMS, self.POS)]
        return Frame(active=True, paused=False, speed=3.47, yaw_rate=yaw, user_throttle=0.08, engine_on=True,
                     wheels=wheels)

    def test_the_outer_front_wheel_of_a_tight_turn_is_no_spin(self) -> None:
        s = synth()
        self.assertGreater(s._wheel_spin(self.frame(0.0)), 0.0)  # what the straight-line check saw
        self.assertEqual(s._wheel_spin(self.frame(0.47)), 0.0)

    def test_a_turn_the_other_way_does_not_excuse_it(self) -> None:  # with the sign as the SDK says
        self.assertGreater(synth()._wheel_spin(self.frame(-0.47)), 0.0)

    def test_a_driven_wheel_outrunning_its_spot_in_the_turn_is(self) -> None:
        rims = list(self.RIMS)
        rims[3] = 6.5  # the rear right well past its 3.94
        self.assertAlmostEqual(synth()._wheel_spin(self.frame(0.47, rims)), 1 / 6)

    def test_a_game_whose_yaw_runs_the_other_way_is_caught_by_its_wheels(self) -> None:
        s = synth()
        backwards = self.frame(-0.47)  # the same left turn, the yaw rate reported negative
        backwards.user_throttle = 0.0
        for _ in range(4):
            s.render(backwards)
        self.assertEqual(s.yaw_sign, 1.0)  # not enough yet to overrule the SDK's axes
        for _ in range(10):
            s.render(backwards)
        self.assertEqual(s.yaw_sign, -1.0)
        self.assertEqual(s._wheel_spin(self.frame(-0.47)), 0.0)

    def test_the_sdk_sign_holds_when_the_wheels_agree(self) -> None:
        s = synth()
        turn = self.frame(0.47)
        turn.user_throttle = 0.0
        for _ in range(20):
            s.render(turn)
        self.assertEqual(s.yaw_sign, 1.0)
        self.assertGreater(s.yaw_evidence, 0.0)

    def test_wheels_the_engine_does_not_drive_never_spin(self) -> None:
        self.assertEqual(synth()._wheel_spin(self.frame(0.0, [9.0] * 6, powered=False)), 0.0)


class OutputLimitTest(unittest.TestCase):
    def test_quiet_output_keeps_its_level_and_peaks_stop_at_each_ceiling(self) -> None:
        s = Synth(effective_gains(Gains()))
        for channel in ("grip", "trigger"):
            with self.subTest(channel):
                self.assertAlmostEqual(s._out(0.01, 1.0, CEILING[channel]), 0.01 * MAX_SAMPLE, places=2)
                self.assertLess(s._out(100.0, 1.0, CEILING[channel]), CEILING[channel] + 1e-9)
                self.assertGreater(s._out(100.0, 1.0, CEILING[channel]), CEILING[channel] - 0.5)
        self.assertEqual(CEILING, {"grip": 127, "trigger": 102})


class EqualizerTest(unittest.TestCase):
    CURVES = {"grip": {"30": 2.0, "65": 1.0, "250": 2.0}, "trigger": {"100": 1.0, "200": 1.5}}
    G = Equalizer.LEVEL["grip"]

    def test_flat_without_curves(self) -> None:
        self.assertEqual(Equalizer().pair(40.0), (1.0, 1.0))
        self.assertEqual(Equalizer({"grip": {"low": "x", "-5": 2.0}, "trigger": [1, 2]}).pair(40.0), (1.0, 1.0))

    def test_default_for_a_motor_the_player_has_not_measured(self) -> None:
        untouched = Equalizer.for_player({})
        default = Equalizer.DEFAULT
        for got, want in zip((untouched.gain("grip", 100.0), untouched.gain("trigger", 100.0),
                              untouched.gain("grip", 65.0), untouched.gain("trigger", 65.0)),
                             (default["grip"]["100"] * self.G, 1.0, self.G, default["trigger"]["65"])):
            self.assertAlmostEqual(got, want)
        own = Equalizer.for_player({"grip": self.CURVES["grip"]})
        self.assertAlmostEqual(own.gain("grip", 30.0), 2.0 * self.G)
        self.assertAlmostEqual(own.gain("trigger", 200.0), Equalizer.DEFAULT["trigger"]["200"])
        for curve in Equalizer.DEFAULT.values():  # inside the limits, so stored as measured
            self.assertTrue(all(Equalizer.LIMITS[0] <= gain <= Equalizer.LIMITS[1] for gain in curve.values()))

    def test_follows_the_measured_points_in_db(self) -> None:
        eq = Equalizer(self.CURVES)
        self.assertAlmostEqual(eq.gain("grip", 30.0), 2.0 * self.G)  # at the grips' level
        self.assertAlmostEqual(eq.gain("grip", math.sqrt(30.0 * 65.0)), math.sqrt(2.0) * self.G)  # halfway in both
        self.assertAlmostEqual(eq.gain("grip", 20.0), 2.0 * self.G)  # held past the ends
        self.assertAlmostEqual(eq.gain("trigger", 300.0), 1.5)
        self.assertAlmostEqual(Equalizer({"grip": {"30": 50.0}}).gain("grip", 30.0), Equalizer.LIMITS[1] * self.G)

    def test_resonance_cut_fitted_to_the_curve(self) -> None:
        center, depth, q = Equalizer.for_player({}).resonances["grip"]
        self.assertAlmostEqual(center, 65.0)
        self.assertAlmostEqual(depth, 20 * math.log10(3.27 / 1.0), places=1)  # the floor: median from 100 Hz up
        self.assertTrue(3.0 < q < 6.0)
        self.assertIsNone(Equalizer().resonances["grip"])  # flat
        self.assertIsNone(Equalizer({"grip": {"50": 1.2, "65": 1.0, "100": 1.2, "200": 1.1}}).resonances["grip"])

    def test_pair_makes_up_the_cut(self) -> None:
        eq = Equalizer.for_player({})
        for freq in (30.0, 65.0, 80.0, 160.0):
            grip, trigger = eq.pair(freq)
            self.assertAlmostEqual(grip * eq.shapes["grip"].magnitude(freq), eq.gain("grip", freq))
            self.assertAlmostEqual(trigger * eq.shapes["trigger"].magnitude(freq), eq.gain("trigger", freq))

    def test_steady_tone_as_strong_as_the_curve_says(self) -> None:
        values = {"retarder": 2, "retarder_steps": 4, "speed": 25.0}  # a pure 65 Hz hum
        gains = Gains(disabled=[name for name in TOGGLEABLE if name != "retarder"])
        flat, eq = Synth(effective_gains(gains)), Synth(effective_gains(gains), Equalizer.for_player({}))
        flat_rms, eq_rms = (rms(left_grip(s, **values)) for s in (flat, eq))
        self.assertAlmostEqual(eq_rms / flat_rms, Equalizer.for_player({}).gain("grip", 65.0), delta=0.02)

    def test_knock_near_the_resonance_stops_with_the_cut(self) -> None:
        """Through a motor that resonates as the curve says, a 65 Hz knock rings on without the cut."""
        eq = Equalizer.for_player({})
        center, depth, q = eq.resonances["grip"]

        def felt(cut: bool) -> list[float]:
            knock, motor, shaper = Burst(65.0, 0.5, 0.025, phase=0.25), ResonanceCut(center, -depth, q), eq.cut("grip")
            gain = eq.pair(65.0)[0] if cut else eq.gain("grip", 65.0)
            return [motor.next(shaper.next(knock.next() * gain) if cut else knock.next() * gain) for _ in range(300)]

        def length(x: list[float]) -> int:
            above = [n for n, v in enumerate(x) if abs(v) > 0.1 * max(map(abs, x))]
            return above[-1] - above[0]

        self.assertLess(length(felt(True)), 60)  # ms: the knock's own 55
        self.assertGreater(length(felt(False)), 2 * length(felt(True)))

    def test_default_grips_as_loud_as_flat_on_the_scripted_drive(self) -> None:
        def left(eq: Equalizer | None) -> list[float]:
            clock = [0.0]
            source = SimulatedTelemetry(None, clock=lambda: clock[0])
            s = Synth(effective_gains(Gains()), eq)
            samples = []
            for n in range(round(25.0 / DT)):  # yard, launch, highway with potholes and a rumble strip
                clock[0] = n * DT
                samples.extend(s.render(source.read()).left_grip)
            return samples

        flat, default = left(None), left(Equalizer.for_player({}))
        self.assertLess(abs(math.log(rms(default) / rms(flat))), math.log(1.25))  # within 2 dB
        # rarely limited: vibration held at the limit, not the one-sample ticks (TICK_SHARE) on a clack's crest
        loud = [abs(x) > 0.9 * MAX_SAMPLE for x in default]
        held = sum(here and (before or after) for before, here, after in zip([False] + loud, loud, loud[1:] + [False]))
        self.assertLess(held / len(default), 0.01)

    def synths(self, effect: str) -> tuple[Synth, Synth]:
        gains = Gains(steady_fade=0.0, disabled=[name for name in TOGGLEABLE if name != effect])
        with mock.patch.dict(Equalizer.LEVEL, grip=1.0):  # the curve's shape alone
            eq = Equalizer(self.CURVES)
        return Synth(effective_gains(gains)), Synth(effective_gains(gains), eq)

    def test_idle_engine_gets_the_grips_low_end_boost(self) -> None:
        values = {"rpm": 650.0, "game_throttle": 0.0, "user_throttle": 0.0}  # 32.5 Hz, x1.86 on this curve
        flat, boosted = (rms(left_grip(s, **values)) for s in self.synths("engine"))
        self.assertGreater(boosted, 1.6 * flat)

    def test_trigger_follows_its_own_curve(self) -> None:
        flat, eq = self.synths("trigger")  # throttle trigger at 100 Hz, where the trigger curve is 1
        for s in (flat, eq):
            for _ in range(20):
                s.render(frame())
        self.assertEqual(flat.render(frame()).trigger, eq.render(frame()).trigger)


class RetarderTest(unittest.TestCase):
    ENGINE_BRAKE = {"motor_brake": True, "game_throttle": 0.0, "user_throttle": 0.0}

    def test_engine_brake_knocks_at_the_firing_rate(self) -> None:
        samples = left_grip(only("retarder"), **self.ENGINE_BRAKE)  # 1300 rpm, 6 cylinders: 65 Hz
        firing = tone_level(samples, 65.0)
        self.assertGreater(firing, 3 * tone_level(samples, 50.0))
        self.assertGreater(tone_level(samples, 130.0), 0.3 * firing)  # a knock, not a sine

    def test_engine_brake_adds_to_the_engine(self) -> None:
        # The knock has the engine's frequency; on a phase of its own it could cancel the engine.
        # Driving on the throttle first leaves the engine anywhere in its cycle when the brake comes in.
        for blocks_on_throttle in range(8):
            with self.subTest(blocks_on_throttle=blocks_on_throttle):
                levels = []
                for effects in (("engine",), ("engine", "retarder")):
                    s = Synth(effective_gains(Gains(disabled=[n for n in TOGGLEABLE if n not in effects])))
                    for _ in range(blocks_on_throttle):
                        s.render(frame())
                    levels.append(rms(left_grip(s, **self.ENGINE_BRAKE)))
                self.assertGreater(levels[1], 1.3 * levels[0])

    def test_no_engine_brake_under_throttle(self) -> None:
        samples = left_grip(only("retarder"), motor_brake=True)
        self.assertLess(max(map(abs, samples)), 1.0)

    def test_retarder_hum_rises_with_speed(self) -> None:
        slow = left_grip(only("retarder"), retarder=2, retarder_steps=4, speed=3.0)  # 43 Hz
        fast = left_grip(only("retarder"), retarder=2, retarder_steps=4, speed=25.0)  # 65 Hz
        self.assertGreater(tone_level(slow, 43.0), 3 * tone_level(slow, 65.0))
        self.assertGreater(tone_level(fast, 65.0), 3 * tone_level(fast, 43.0))


class SurfaceTest(unittest.TestCase):
    def road(self, substance: str, speed: float = 22.0) -> list[float]:
        s, samples = only("road"), []
        for _ in range(400):
            f = frame(speed=speed)
            for wheel in f.wheels:
                wheel.substance = substance
            samples.extend(s.render(f).left_grip)
        return samples[200:]

    @staticmethod
    def crossings(samples: list[float]) -> float:
        """Zero crossings per second: follows where the texture's energy sits."""
        return sum(1 for a, b in zip(samples, samples[1:]) if a * b < 0) * SAMPLE_RATE / len(samples)

    @staticmethod
    def kurtosis(samples: list[float]) -> float:
        mean = sum(samples) / len(samples)
        var = sum((x - mean) ** 2 for x in samples) / len(samples)
        return sum((x - mean) ** 4 for x in samples) / len(samples) / var ** 2

    def test_hard_surfaces_feel_finer_than_soft_ones(self) -> None:
        # asphalt and snow cross about as often (286 and 285 a second before the bed's low-pass,
        # 269 and 270 after): what tells them apart is snow's stones, not the pitch
        ice, asphalt, snow, gravel, dirt = (self.crossings(self.road(name))
                                            for name in ("ice", "road", "snow", "gravel", "dirt"))
        self.assertGreater(ice, max(asphalt, snow))
        self.assertGreater(min(asphalt, snow), max(gravel, dirt))

    def test_texture_rises_with_speed(self) -> None:
        self.assertGreater(self.crossings(self.road("road", 25.0)), 1.2 * self.crossings(self.road("road", 3.0)))

    def test_gravel_crackles_and_asphalt_does_not(self) -> None:
        # slow enough that single stones stand apart; noise alone has a kurtosis near 3
        self.assertGreater(self.kurtosis(self.road("gravel", 4.0)), 1.5 * self.kurtosis(self.road("road", 4.0)))

    def test_grass_swishes_high(self) -> None:
        self.assertGreater(self.crossings(self.road("grass")), self.crossings(self.road("road")))

    @staticmethod
    def texture(roughness: float = 0.0, steady: bool = False, seconds: float = 4.0) -> list[float]:
        texture = Texture(seed=5)
        texture.tune(surface_of("road"), 20.0, roughness)
        if steady:
            texture.swell.next = lambda: 1.0
        return [texture.next() for _ in range(int(seconds * SAMPLE_RATE))]

    def test_texture_swells_a_few_times_a_second(self) -> None:
        def unevenness(samples: list[float]) -> float:
            windows = [rms(samples[i:i + 200]) for i in range(0, len(samples) - 200, 200)]  # 0.2 s each
            mean = sum(windows) / len(windows)
            return math.sqrt(sum((w - mean) ** 2 for w in windows) / len(windows)) / mean
        self.assertGreater(unevenness(self.texture()), 1.4 * unevenness(self.texture(steady=True)))

    def test_swell_keeps_the_strength(self) -> None:
        self.assertAlmostEqual(rms(self.texture(seconds=30.0)) / rms(self.texture(steady=True, seconds=30.0)),
                               1.0, delta=0.1)

    def test_rough_ground_rattles_the_cab(self) -> None:
        smooth, rough = self.texture(), self.texture(roughness=0.3)
        self.assertGreater(rms(rough), 1.15 * rms(smooth))
        self.assertGreater(self.kurtosis(rough), 1.3 * self.kurtosis(smooth))  # in knocks, not more hiss
        self.assertEqual(self.texture(roughness=0.03), smooth)  # under the start of the rattle

    def test_static_ground_is_still_paved(self) -> None:
        # yards, bridges and ramps; it used to drop the road out for seconds at speed
        self.assertGreater(rms(self.road("static")), 0.8 * rms(self.road("road")))

    @staticmethod
    def envelope(samples: list[float], width: int) -> list[float]:
        return [rms(samples[i:i + width]) for i in range(0, len(samples) - width, width)]

    def test_concrete_knocks_at_each_slab_joint(self) -> None:
        envelope = self.envelope(self.road("concrete"), 10)  # 10 ms steps

        def correlation(lag: int) -> float:
            mean = sum(envelope) / len(envelope)
            d = [e - mean for e in envelope]
            return sum(a * b for a, b in zip(d, d[lag:])) / sum(a * a for a in d)
        slab = round(4.6 / 22.0 * 100)  # a joint every 4.6 m at 22 m/s
        self.assertGreater(correlation(slab), 0.3)
        self.assertGreater(correlation(slab), 3 * correlation(slab // 2))

    def test_planks_drum_at_the_speed(self) -> None:
        def strongest(samples: list[float]) -> int:
            def power(hz: int) -> float:
                w = TAU * hz / SAMPLE_RATE
                return abs(sum(x * cmath.exp(-1j * w * n) for n, x in enumerate(samples))) ** 2
            return max(range(30, 251), key=power)
        # the hiss moves with the speed only by PITCH_STANDING; the planks, 20 cm apart, all the way
        for speed in (11.0, 22.0):
            with self.subTest(speed):
                self.assertAlmostEqual(strongest(self.road("wood", speed)) / (speed / 0.2), 1.0, delta=0.1)

    def test_asphalt_is_even_and_dirt_heaves(self) -> None:
        def unevenness(samples: list[float]) -> float:
            windows = self.envelope(samples, 100)
            mean = sum(windows) / len(windows)
            return math.sqrt(sum((w - mean) ** 2 for w in windows) / len(windows)) / mean
        self.assertLess(unevenness(self.road("road")), 0.6 * unevenness(self.road("road_dirt")))

    def test_seams_survive_a_mixed_side(self) -> None:
        wheels = [Wheel(substance="wood"), Wheel(substance="road")]
        mixed = side_surface(wheels)
        self.assertEqual(mixed.seams, surface_of("wood").seams)
        self.assertLess(mixed.seam_share, surface_of("wood").seam_share)

    def test_strength_does_not_follow_the_pitch(self) -> None:
        # neither has stones: ice hisses at 140 Hz, wood at 65 Hz; a plain band-pass made ice 1.5x stronger
        ice, wood = rms(self.road("ice")), rms(self.road("wood"))
        self.assertAlmostEqual(ice / wood, 0.15 / 0.55, delta=0.05)


class EventShapeTest(unittest.TestCase):
    def park_brake(self, setting: bool) -> list[float]:
        """The left grip for a second after the park brake is set or released."""
        s, samples = only("park_brake"), []
        s.render(frame(park_brake=not setting))
        for _ in range(125):
            samples.extend(s.render(frame(park_brake=setting)).left_grip)
        return samples

    def test_venting_air_swells_in(self) -> None:
        burst = NoiseBurst(250.0, 1.0, 0.35, attack_s=0.15)
        levels = []
        for n in range(200):
            levels.append(burst.level)
            burst.next()
        self.assertLess(levels[10], 0.2 * levels[150])

    def test_setting_the_park_brake_hisses_for_most_of_a_second(self) -> None:
        samples = self.park_brake(True)
        self.assertGreater(rms(samples[600:800]), 0.18 * rms(samples[50:250]))

    def test_releasing_the_park_brake_pops_after_the_thunk(self) -> None:
        samples = self.park_brake(False)
        self.assertGreater(rms(samples[280:340]), 2 * rms(samples[180:260]))

    def test_releasing_the_park_brake_thunks_where_the_knob_clips_do(self) -> None:
        thunk = self.park_brake(False)[:100]
        self.assertGreater(tone_level(thunk, 130.0), 2 * tone_level(thunk, 70.0))

    @staticmethod
    def release(blocks: int, pedal: float, s: Synth | None = None) -> list:
        """Hold the brake pedal for some 8 ms blocks, let go, and return the air brake's bursts."""
        s = s or only("air_brake")
        for _ in range(blocks):
            s.render(frame(user_brake=pedal, user_throttle=0.0))
        s.render(frame(user_brake=0.0, user_throttle=0.0))
        return [burst for effect, _, burst in s.bursts if effect == "air_brake"]

    def test_letting_go_of_the_brake_vents_the_chambers(self) -> None:
        hard, soft = self.release(50, 0.9), self.release(50, 0.2)
        self.assertEqual(len(hard), 2)  # the valve's thump and the hiss
        self.assertGreater(hard[0].amplitude, soft[0].amplitude)
        self.assertEqual(self.release(20, 0.9), [])  # a dab of 0.16 s

    def test_venting_does_not_duck_the_driving_effects(self) -> None:
        s = Synth(effective_gains(Gains(disabled=[n for n in TOGGLEABLE if n not in ("engine", "air_brake")])))
        self.release(50, 0.9, s)
        for _ in range(10):
            s.render(frame(user_brake=0.0, user_throttle=0.0))
            self.assertEqual(max(s.duck), 0.0)

    def test_squeal_comes_in_separate_squeals(self) -> None:
        s, samples = only("squeal"), []
        for _ in range(250):  # braking gently at walking pace: 2 s
            samples.extend(s.render(frame(speed=1.5, user_brake=0.3, user_throttle=0.0)).trigger)
        windows = [rms(samples[i:i + 20]) for i in range(0, len(samples), 20)]
        quiet = sum(1 for w in windows if w < 0.05 * max(windows)) / len(windows)
        self.assertGreater(quiet, 0.08)  # the old one wobbled at 5 Hz and never went quiet

    @staticmethod
    def event(effect: str, blocks: int, before: dict, after: dict) -> list[float]:
        """The left grip from the moment a state changes, the effect alone."""
        s, samples = only(effect), []
        s.render(frame(**before))
        for _ in range(blocks):
            samples.extend(s.render(frame(**after)).left_grip)
        return samples

    @staticmethod
    def peak(samples: list[float]) -> float:
        return max(abs(x) for x in samples)

    def test_the_air_shift_follows_the_knob(self) -> None:
        def flip(selector: int) -> list[float]:
            return self.event("shifter", 50, {"shifter_toggle": (False, False)},
                              {"shifter_toggle": (selector == 0, selector == 1)})
        range_, split = flip(0), flip(1)
        knob = self.peak(range_[:30])
        self.assertLess(self.peak(range_[40:110]), 0.2 * knob)
        clack = self.peak(range_[120:260])
        self.assertGreater(clack, 0.7 * knob)  # the gearbox shifting, as hard as a gear shift
        self.assertLess(self.peak(split[100:260]), 0.5 * clack)  # the splitter's 12 dB softer

    def test_the_flasher_relay_clicks_short(self) -> None:
        samples = self.event("blinker", 10, {"blinker_left": False}, {"blinker_left": True})
        self.assertLess(self.peak(samples[10:40]), 0.25 * self.peak(samples))

    def test_coupling_clatters(self) -> None:
        samples = self.event("trailer", 100, {}, {"trailer_attached": True})
        windows = [self.peak(samples[i:i + 20]) for i in range(0, 600, 20)]
        self.assertGreaterEqual(sum(w > 0.15 * self.peak(samples) for w in windows), 15)  # knocks all through 0.6 s

    def test_the_engine_cranks_for_a_second_then_catches(self) -> None:
        samples = self.event("engine_start", 250, {"engine_on": False}, {"engine_on": True})
        loudest = max(range(len(samples)), key=lambda n: abs(samples[n]))
        self.assertTrue(1100 < loudest < 1500)
        self.assertGreater(self.peak(samples[300:1000]), 0.1 * abs(samples[loudest]))  # compression knocks

    def test_stopping_fires_on_then_runs_down(self) -> None:
        samples = self.event("engine_start", 125, {"engine_on": True}, {"engine_on": False})
        idle = self.peak(samples[200:320])
        self.assertLess(self.peak(samples[360:460]), 0.4 * idle)  # 8 dB down once it stops firing
        self.assertGreater(self.peak(samples[600:700]), 0.05 * idle)  # and still running down


class RumbleTest(unittest.TestCase):
    def test_strip_hums_with_every_other_groove_harder(self) -> None:
        s, samples = only("rumble"), []
        for _ in range(60):
            f = frame(speed=22.0)
            for wheel in f.wheels:
                wheel.substance = "rumble_stripe"
            samples.extend(s.render(f).left_grip)
        groove = 22.0 / RUMBLE_SPACING
        samples = samples[80:480]
        fundamental = tone_level(samples, groove)
        self.assertGreater(tone_level(samples, groove / 2), 0.2 * fundamental)
        self.assertLess(tone_level(samples, 3 * groove), 0.15 * fundamental)  # not a clipped buzz


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
            # three quarters of the threshold; per block it looked like three times the threshold
            speed = 0.75 * Gains().bump_threshold
            s.render(frame(deflection=0.02 + speed * game_time, render_time=int(game_time * 1e6)))
        self.assertNotIn("bumps", effects_of(s))

    def test_hitch_is_not_a_bump(self) -> None:
        for hitch in (0.15, 1.0):  # within MAX_FRAME_GAP, then past it
            with self.subTest(hitch=hitch):
                s, game_time = synth(), 0.0
                for n in range(60):
                    game_time += hitch if n == 30 else 1 / 60
                    # settling at 6 Hz, its fastest at 60% of the threshold
                    swing = 0.6 * Gains().bump_threshold / (math.tau * 6)
                    deflection = 0.02 + swing * math.sin(math.tau * 6 * game_time)
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
        s.render(frame(deflection=0.0398, accel=(3.0, 0.0, -20.0), render_time=int(2e6 / 60)))
        self.assertIn("bumps", effects_of(s))


def band_share(samples: list[float], low: float, high: float) -> float:
    """Share of the samples' energy between low and high Hz (a DFT over those bins, Hann window
    so the strong low band does not leak into them)."""
    n = len(samples)
    samples = [x * (0.5 - 0.5 * math.cos(2 * math.pi * i / n)) for i, x in enumerate(samples)]
    total = sum(x * x for x in samples) * n / 2
    power = 0.0
    for k in range(math.ceil(low * n / SAMPLE_RATE), math.floor(high * n / SAMPLE_RATE) + 1):
        z = sum(x * cmath.exp(-2j * math.pi * k * i / n) for i, x in enumerate(samples))
        power += abs(z) ** 2
    return power / total


def bed_filter(on: bool):
    """As it is, or with the bed's low-pass (and its make-up) taken out."""
    import contextlib
    if on:
        return contextlib.nullcontext()
    return mock.patch.multiple(LowPass, next=lambda self, x: x, magnitude=lambda self, f: 1.0)


class BedLowPassTest(unittest.TestCase):
    def test_passes_the_effects_and_shuts_the_band_that_beats(self) -> None:
        lp = LowPass()
        loss = {f: -20.0 * math.log10(lp.magnitude(f)) for f in (45.0, 100.0, 190.0, 250.0, 450.0, 490.0)}
        self.assertLess(loss[190.0], 0.5)
        self.assertLess(loss[250.0], 1.5)
        self.assertGreater(loss[450.0], 25.0)
        self.assertGreater(loss[490.0], 45.0)

    def test_silence_stays_exact(self) -> None:
        lp = LowPass()
        lp.next(1.0)
        tail = [lp.next(0.0) for _ in range(200)]
        self.assertEqual(tail[-1], 0.0)

    def test_steady_effects_keep_their_strength(self) -> None:
        s = synth()
        for freq in (45.0, 100.0, 250.0):
            gains = s._bed_pair(freq)
            self.assertAlmostEqual(gains[0] * s._bed_shape.magnitude(freq), s.eq.pair(freq)[0])

    def test_road_bed_has_next_to_nothing_where_the_images_beat(self) -> None:
        def road(filtered: bool) -> list[float]:
            with bed_filter(filtered):
                return SurfaceTest().road("road", 25.0)[:1000]
        before, after = band_share(road(False), 450.0, 500.0), band_share(road(True), 450.0, 500.0)
        self.assertGreater(before, 100 * after)  # 5e-4 of the road's energy came to 8e-7
        self.assertLess(after, 1e-5)

    def test_events_keep_their_edges(self) -> None:
        def shift(filtered: bool) -> list[float]:
            with bed_filter(filtered):
                s, samples = only("gear"), []
                s.render(frame(gear=7))
                for _ in range(40):
                    samples.extend(s.render(frame(gear=8)).left_grip)
                return samples
        self.assertEqual(shift(True), shift(False))


class TickTest(unittest.TestCase):
    @staticmethod
    def ticks(effect: str, before: dict, after: dict) -> list[float]:
        """The delay of every tick the change makes, as each is made (one at once has played by the block's end)."""
        made: list[float] = []

        class Recorded(Tick):
            def __init__(self, amplitude: float, delay_s: float = 0.0) -> None:
                super().__init__(amplitude, delay_s)
                made.append(delay_s)

        with mock.patch("effects.Tick", Recorded):
            s = only(effect)
            s.render(frame(**before))
            s.render(frame(**after))
        return made

    def test_a_tick_is_one_sample(self) -> None:
        tick = Tick(0.5, delay_s=0.003)
        samples = [tick.next() for _ in range(10)]
        self.assertEqual(samples, [0.0, 0.0, 0.0, 0.5] + [0.0] * 6)
        self.assertTrue(tick.done)
        self.assertFalse(tick.ducks)

    def test_the_motor_eq_leaves_it_alone(self) -> None:
        grip, trigger = Equalizer.for_player({}).pair(0.0)
        self.assertAlmostEqual(grip, 1.0)
        self.assertAlmostEqual(trigger, 1.0)

    def test_the_clicks_open_with_one(self) -> None:
        self.assertEqual(len(self.ticks("blinker", {"blinker_left": False}, {"blinker_left": True})), 1)
        self.assertEqual(self.ticks("gear", {"gear": 7}, {"gear": 8}), [0.14])  # the clack
        self.assertEqual(self.ticks("shifter", {"shifter_toggle": (False, False)},
                                    {"shifter_toggle": (True, False)}), [0.0, 0.13, 0.17])  # knob, two clacks
        self.assertEqual(self.ticks("shifter", {"shifter_toggle": (False, False)},
                                    {"shifter_toggle": (False, True)}), [0.016, 0.12, 0.18])
        coupled = self.ticks("trailer", {}, {"trailer_attached": True})
        self.assertGreaterEqual(len(coupled), 15)  # one with every knock of the clatter

    def test_felt_thumps_get_none(self) -> None:
        self.assertEqual(self.ticks("park_brake", {"park_brake": False}, {"park_brake": True}), [])
        self.assertEqual(self.ticks("trailer", {"trailer_attached": True}, {"trailer_attached": False}), [])

    def test_a_tick_stands_out_above_the_click_it_opens(self) -> None:
        def flash(share: float) -> list[float]:
            with mock.patch("effects.TICK_SHARE", share):
                return EventShapeTest.event("blinker", 4, {"blinker_left": False}, {"blinker_left": True})
        with_tick, without = flash(1.0), flash(0.0)
        self.assertGreater(with_tick[0] - without[0], 0.25 * MAX_SAMPLE)  # one sample, on the click's crest
        self.assertEqual(with_tick[1:], without[1:])  # the body after it is as it was


class AirBrakeHissTest(unittest.TestCase):
    def test_it_hisses_where_the_shell_speaks(self) -> None:
        s, samples = only("air_brake"), []
        for _ in range(50):
            s.render(frame(user_brake=0.9, user_throttle=0.0))
        for _ in range(40):
            samples.extend(s.render(frame(user_brake=0.0, user_throttle=0.0)).left_grip)
        hiss = samples[60:316]  # past the valve's 50 Hz thump
        self.assertGreater(band_share(hiss, 280.0, 450.0), 0.6)
        self.assertLess(band_share(hiss, 0.0, 200.0), 0.1)
        self.assertLess(band_share(hiss, 470.0, 500.0), 0.01)


if __name__ == "__main__":
    unittest.main()
