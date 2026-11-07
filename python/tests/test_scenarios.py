"""The scenario drives through the synth: effects that come together on the road stay apart,
nothing knocks where the road is smooth, and the steady fade settles and comes back."""

from __future__ import annotations

import unittest

from apex6 import MAX_SAMPLE
from effects import DT, TOGGLEABLE, Equalizer, Gains, Synth, effective_gains
from i18n import t
from scenarios import CITY, HIGHWAY, SCENARIOS, SPLIT_LEAD, TRAILER_AXLES, Drive, Scenario, ScenarioTelemetry


class Run:
    """One round of a scenario as the game would show it at 60 fps, with the default settings."""

    def __init__(self, scenario: Scenario, gains: Gains | None = None) -> None:
        clock = [0.0]
        source = ScenarioTelemetry(scenario, 60, clock=lambda: clock[0])
        self.drive = source.drive
        s = Synth(effective_gains(gains or Gains()), Equalizer.for_player({}))
        self.events: list[tuple[float, str]] = []
        self.bumps: list[float] = []  # when each bump pulse started
        self.fresh: list[dict[str, float]] = []  # the steady fade's state after each block
        self.channels: tuple[list[float], list[float], list[float]] = ([], [], [])  # left, right, trigger
        seen = {}  # holds the pulses, so an id is never reused by a later one
        for n in range(round(scenario.seconds / DT)):
            clock[0] = n * DT
            block = s.render(source.read())
            self.events.extend((n * DT, event) for event in s.levels.events)
            for effect, _, burst in s.bursts:
                if effect == "bumps" and id(burst) not in seen:
                    seen[id(burst)] = burst
                    self.bumps.append(n * DT)
            self.fresh.append(dict(s.fresh))
            for out, samples in zip(self.channels, (block.left_grip, block.right_grip, block.trigger)):
                out.extend(samples)

    def count(self, text: str) -> int:
        return sum(event == text for _, event in self.events)

    def peak(self, start: float, end: float) -> float:
        return max(abs(x) for x in self.channels[0][int(start * 1000):int(end * 1000)]) / MAX_SAMPLE


_runs: dict[str, Run] = {}


def run(name: str) -> Run:
    if name not in _runs:
        _runs[name] = Run(SCENARIOS[name])
    return _runs[name]


class ScenarioTest(unittest.TestCase):
    def test_every_shift_is_felt(self) -> None:
        for name in SCENARIOS:
            with self.subTest(name):
                r = run(name)
                for _, gear in r.drive.shifts[1:]:
                    self.assertGreaterEqual(r.count(t("변속 {gear}단", gear=gear)), 1, gear)
                self.assertEqual(sum(event.startswith(t("변속 {gear}단", gear="").split()[0]) for _, event in r.events),
                                 len(r.drive.shifts) - 1)
                self.assertEqual(r.count(t("스플리터")), len(r.drive.splits))

    def test_no_knocks_where_the_road_is_smooth(self) -> None:
        for name in SCENARIOS:
            with self.subTest(name):
                r = run(name)
                d = r.drive
                last_axle = max(back for back, _ in TRAILER_AXLES)
                # from the cab reaching a feature until the trailer's last axle is over it and the cab settled
                windows = [(at - 0.05, d._when(d._at(at) + last_axle + 2.0) + 0.5) for at in d.road_features()]
                stray = [at for at in r.bumps if not any(start <= at <= end for start, end in windows)]
                self.assertEqual(stray, [])

    def test_every_step_in_the_road_knocks_at_the_cab(self) -> None:
        for name in SCENARIOS:
            with self.subTest(name):
                r = run(name)
                # Steps only: a swell over a metre or more (a speed bump taken slowly, a frost
                # heave) moves the suspension too slowly for the bump detector.
                for hit in r.drive.s.hits:
                    if hit.width <= 1.0:
                        self.assertTrue(any(hit.at - 0.05 <= b <= hit.at + 0.3 for b in r.bumps), hit)

    def test_rarely_limited(self) -> None:
        for name in SCENARIOS:
            with self.subTest(name):
                for samples in run(name).channels:
                    self.assertLess(sum(abs(x) > 0.9 * MAX_SAMPLE for x in samples) / len(samples), 0.01)

    def test_a_split_shift_is_no_louder_than_a_shift(self) -> None:
        gains = Gains(steady_fade=0.0, disabled=[n for n in TOGGLEABLE if n not in ("gear", "shifter")])
        r = Run(CITY, gains)
        split = {round(at + SPLIT_LEAD, 3) for at, _ in r.drive.splits}
        plain = max(r.peak(at - 0.35, at + 0.55) for at, _ in r.drive.shifts[1:] if round(at, 3) not in split)
        stacked = max(r.peak(at - 0.35, at + 0.55) for at, _ in r.drive.shifts[1:] if round(at, 3) in split)
        self.assertLess(stacked, 1.15 * plain)

    def test_the_cruise_settles_and_the_overtake_brings_it_back(self) -> None:
        fresh = run("highway").fresh
        at = lambda s: fresh[round(s / DT)]
        for bed in ("engine", "road", "trigger"):
            self.assertLess(at(48.0)[bed], 0.1, bed)  # cruise control since 20 s
        self.assertGreater(at(112.0)["engine"], 0.9)  # floored to overtake at 111
        self.assertGreater(at(112.0)["trigger"], 0.9)


class DriveTest(unittest.TestCase):
    def test_gears_follow_the_speed(self) -> None:
        d = Drive(HIGHWAY)
        self.assertEqual(d.gear(0.0), 10)  # 60 km/h on the ramp
        self.assertEqual(d.gear(100.0), 12)
        self.assertTrue(1100 < d.frame(100.0).rpm < 1400)

    def test_axles_behind_reach_a_hole_later_at_low_speed(self) -> None:
        d = Drive(CITY)  # a manhole under the left wheels at 34 s, 50 km/h
        front = min((d.frame(34.0 + k * 0.001).wheels[0].deflection, k) for k in range(-50, 50))[1]
        drive = min((d.frame(34.0 + k * 0.001).wheels[2].deflection, k) for k in range(0, 800))[1]
        self.assertAlmostEqual((drive - front) / 1000, 5.0 / (50 / 3.6), delta=0.02)

    def test_the_round_ends_in_a_pause(self) -> None:
        clock = [HIGHWAY.seconds + 0.5]
        source = ScenarioTelemetry(HIGHWAY, None, clock=lambda: clock[0])
        source._start = 0.0
        self.assertTrue(source.read().paused)
        clock[0] = HIGHWAY.seconds + ScenarioTelemetry.PAUSE + 0.1
        self.assertTrue(source.read().live)


if __name__ == "__main__":
    unittest.main()
