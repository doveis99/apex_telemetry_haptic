"""Effect tests: each effect plays alone, sided ones left then right, and in place of a running drive."""

from __future__ import annotations

import unittest
from unittest import mock

import ats_haptics
import preview
from apex6 import MAX_SAMPLE, SUBFRAMES
from effects import TOGGLEABLE, Equalizer, Gains
from preview import SCRIPTS, Player, Preview


def play(name: str, gains: Gains | None = None) -> tuple[list[float], list[float], list[float]]:
    """(left grip, right grip, trigger) of one whole preview."""
    p = Preview(name, gains or Gains(), Equalizer.for_player({}))
    left, right, trigger = [], [], []
    while (block := p.render()) is not None:
        left += block.left_grip
        right += block.right_grip
        trigger += block.trigger
    return left, right, trigger


def peak(samples: list[float]) -> float:
    return max((abs(x) for x in samples), default=0.0) / MAX_SAMPLE


class PreviewTest(unittest.TestCase):
    def test_every_effect_plays_and_fades_out(self) -> None:
        self.assertEqual(set(SCRIPTS), set(TOGGLEABLE))
        for name in TOGGLEABLE:
            with self.subTest(name):
                channels = play(name)
                self.assertGreater(max(peak(c) for c in channels), 0.1)
                self.assertLess(max(peak(c[-1:]) for c in channels), 1e-3)  # no step into silence

    def test_sided_effects_go_left_then_right(self) -> None:
        for name in ("blinker", "rumble"):
            with self.subTest(name):
                left, right, _ = play(name)
                half = len(left) // 2
                self.assertGreater(peak(left[:half]), 0.1)
                self.assertEqual(peak(right[:half]), 0.0)
                self.assertLess(peak(left[half:]), 0.02)  # at most the last of the resonance cut's tail
                self.assertGreater(peak(right[half:]), 0.1)

    def test_bumps_hit_the_left_wheels_first(self) -> None:
        left, right, _ = play("bumps")
        first_joint = slice(250, 600)  # ms, around the joint under the left wheels at 0.3 s
        self.assertGreater(peak(left[first_joint]), 0.1)
        self.assertEqual(peak(right[first_joint]), 0.0)

    def test_only_that_effect_plays_even_when_switched_off(self) -> None:
        alone = play("gear")
        self.assertEqual(play("gear", Gains(disabled=["gear"])), alone)
        self.assertEqual(peak(play("gear", Gains(gear=0.0))[0]), 0.0)  # at its own strength
        self.assertEqual(peak(alone[2]), 0.0)  # the throttle held in the script plays no trigger

    def test_follows_the_route(self) -> None:
        left, right, trigger = play("blinker", Gains(routes={"blinker": "trigger"}))
        self.assertEqual(peak(left) + peak(right), 0.0)
        self.assertGreater(peak(trigger), 0.1)


class RunnerPreviewTest(unittest.TestCase):
    def test_plays_in_place_of_the_drive_then_hands_back(self) -> None:
        runner = ats_haptics.Runner(Gains(), simulate=True, sim_fps=None)
        runner.preview = Preview("blinker", Gains(), runner.eq)
        alone = Preview("blinker", Gains(), runner.eq)
        while (expected := alone.render()) is not None:
            self.assertEqual(runner.render().left_grip, expected.left_grip)
        self.assertIsNotNone(runner.render())  # the drive again
        self.assertIsNone(runner.preview)
        runner.close()


class PlayerTest(unittest.TestCase):
    def test_holds_the_controller_while_previews_play(self) -> None:
        opened, blocks = [], []

        class Pad:
            def __enter__(self):
                opened.append(1)
                return self

            def __exit__(self, *exc) -> None:
                pass

            def stream(self, render) -> None:
                while (block := render(0)) is not None:
                    blocks.append(block)
                    if len(blocks) == 10:  # a second test pressed while the first plays takes over
                        player.play(Preview("gear", Gains(), Equalizer()))

        player = Player()
        with mock.patch.object(preview, "Apex6", Pad):
            player.play(Preview("blinker", Gains(), Equalizer()))
            player.thread.join(timeout=10.0)
        self.assertFalse(player.busy)
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(blocks), 10 + round(SCRIPTS["gear"][1] * 1000 / SUBFRAMES))
        self.assertEqual(player.name, "gear")
        self.assertGreater(max(player.take_peaks()), 0.0)

    def test_reports_a_missing_controller(self) -> None:
        class Pad:
            def __enter__(self):
                raise preview.DeviceUnavailable("no controller")

            def __exit__(self, *exc) -> None:
                pass

        player = Player()
        with mock.patch.object(preview, "Apex6", Pad):
            player.play(Preview("blinker", Gains(), Equalizer()))
            player.thread.join(timeout=10.0)
        self.assertFalse(player.busy)
        self.assertEqual(player.error, "no controller")


if __name__ == "__main__":
    unittest.main()
