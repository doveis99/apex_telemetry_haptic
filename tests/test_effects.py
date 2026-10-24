"""Synth edge detectors: a new reference fires nothing, bump pulses stay bounded,
bumps do not depend on the game's frame rate."""

from __future__ import annotations

import itertools
import math
import random
import unittest

from apex6 import SAMPLE_RATE
from effects import (BUMP_HZ, BUMP_RATTLE_MAX, BUMP_RATTLE_S, DT, TOGGLEABLE, Equalizer, Gains, NoiseBurst, Synth,
                     Texture, effective_gains, surface_of)
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
            self.assertLessEqual(effects_of(s).count("bumps"), 34 + BUMP_RATTLE_MAX)  # a pulse each, and the rattle

    def test_pulse_rises_to_the_stronger_hit(self) -> None:
        s = synth()
        for depth in (0.02, 0.0225, 0.0325):  # a pothole: a slight dip first, then the hard edge
            s.render(frame(deflection=depth, truck_wheels=1))
        pulses = [entry[1] for entry in s.bump_bursts if entry]
        self.assertEqual(len(pulses), 1)
        self.assertAlmostEqual(pulses[0].amplitude, 1.0)
        self.assertAlmostEqual(pulses[0].freq, BUMP_HZ[1])  # and to its depth

    @staticmethod
    def hit(step: float, trailer: bool = False) -> Synth:
        """One wheel dropping by step metres within a block."""
        s = synth()
        for depth in (0.02, 0.02 + step):
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

    def spread(self, **values) -> float:
        peaks = self.firing_peaks(self.engine(**values))
        mean = sum(peaks) / len(peaks)
        return math.sqrt(sum((p - mean) ** 2 for p in peaks) / len(peaks)) / mean

    def test_idle_fires_unevenly_and_evens_out_under_load(self) -> None:
        idle = self.spread(rpm=650.0, game_throttle=0.0, user_throttle=0.0)
        pulling = self.spread(rpm=1500.0, game_throttle=0.8, user_throttle=0.8)
        self.assertGreater(idle, 0.08)  # a sine fires every cycle alike
        self.assertGreater(idle, 1.5 * pulling)
        self.assertGreater(pulling, 0.01)  # still not a machine

    def test_load_turns_it_harsh(self) -> None:
        def harshness(load: float) -> float:
            samples = self.engine(game_throttle=load, user_throttle=load)  # 1300 rpm, 6 cylinders: 65 Hz
            return tone_level(samples, 195.0) / tone_level(samples, 65.0)
        self.assertGreater(harshness(0.9), harshness(0.0) + 0.15)


class EqualizerTest(unittest.TestCase):
    CURVES = {"grip": {"30": 2.0, "65": 1.0, "250": 2.0}, "trigger": {"100": 1.0, "200": 1.5}}

    def test_flat_until_calibrated(self) -> None:
        self.assertEqual(Equalizer().pair(40.0), (1.0, 1.0))
        self.assertEqual(Equalizer({"grip": {"low": "x", "-5": 2.0}, "trigger": [1, 2]}).pair(40.0), (1.0, 1.0))

    def test_follows_the_measured_points_in_db(self) -> None:
        eq = Equalizer(self.CURVES)
        self.assertAlmostEqual(eq.gain("grip", 30.0), 2.0)
        self.assertAlmostEqual(eq.gain("grip", math.sqrt(30.0 * 65.0)), math.sqrt(2.0))  # halfway in both
        self.assertAlmostEqual(eq.gain("grip", 20.0), 2.0)  # held past the ends
        self.assertAlmostEqual(eq.gain("trigger", 300.0), 1.5)
        self.assertEqual(Equalizer({"grip": {"30": 50.0}}).gain("grip", 30.0), Equalizer.LIMITS[1])

    def synths(self, effect: str) -> tuple[Synth, Synth]:
        gains = Gains(steady_fade=0.0, disabled=[name for name in TOGGLEABLE if name != effect])
        return Synth(effective_gains(gains)), Synth(effective_gains(gains), Equalizer(self.CURVES))

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
        ice, asphalt, snow = (self.crossings(self.road(name)) for name in ("ice", "road", "snow"))
        self.assertGreater(ice, asphalt)
        self.assertGreater(asphalt, snow)

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
        self.assertEqual(self.texture(roughness=0.05), smooth)  # under the start of the rattle

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
        self.assertIn("bumps", effects_of(s))


if __name__ == "__main__":
    unittest.main()
