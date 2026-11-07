"""Calibration: neighbour order, the two-choice staircase, a session's chained gains, pad buttons, playback."""

from __future__ import annotations

import math
import random
import unittest
from unittest import mock

import calibrate
from calibrate import CANCEL, FIRST, REPLAY, SECOND, PadButtons, Session, Staircase
from effects import Equalizer


def db(gain: float) -> float:
    return 20 * math.log10(gain)


class NeighbourTest(unittest.TestCase):
    def test_outward_from_the_reference(self) -> None:
        self.assertEqual(calibrate.neighbours("grip"),
                         [(80, 65), (100, 80), (130, 100), (160, 130), (200, 160), (250, 200),
                          (50, 65), (40, 50), (30, 40)])
        pairs = calibrate.neighbours("trigger")
        self.assertEqual(pairs[0], (130, 100))
        self.assertEqual(pairs[-1], (50, 65))
        self.assertEqual(sorted(f for f, _ in pairs), sorted(calibrate.FREQS["trigger"]))


class StaircaseTest(unittest.TestCase):
    def run_observer(self, pse_db: float, low: float = -12.0, high: float = 12.0) -> Staircase:
        """An observer who feels the test stronger exactly when it is above pse_db."""
        stairs = Staircase(low, high)
        while not stairs.done:
            stairs.answer(stairs.db > pse_db)
        return stairs

    def test_closes_in_on_the_point_of_equality(self) -> None:
        for pse in (-5.0, -1.0, 0.0, 2.2, 4.0, 7.5):
            with self.subTest(pse=pse):
                stairs = self.run_observer(pse)
                self.assertAlmostEqual(stairs.result_db, pse, delta=1.0)
                self.assertLessEqual(stairs.trials, Staircase.MAX_TRIALS)

    def test_a_limit_ends_it_there(self) -> None:
        stairs = self.run_observer(30.0, high=6.0)  # needs more than the output can give
        self.assertEqual(stairs.result_db, 6.0)
        self.assertLess(stairs.trials, 8)


class SessionTest(unittest.TestCase):
    def answer_as(self, session: Session, pse: dict[float, float]) -> None:
        """Answer every trial as an observer for whom freq needs pse[freq] dB over its neighbour."""
        channel, freq, neighbour = session.current
        level = db(session.tones()[0 if session.test_first else 1][1] / session.gain(channel, neighbour))
        test_stronger = level > pse[freq]
        session.answer(FIRST if test_stronger == session.test_first else SECOND)

    def test_gains_chain_through_the_neighbours(self) -> None:
        pse = {80: 3.0, 100: 2.0, 130: 1.0, 160: 0.0, 200: -1.0, 250: 0.0, 50: 4.0, 40: 3.0, 30: 3.0}
        session = Session(["grip"], rng=random.Random(3))
        while session.current:
            self.answer_as(session, pse)
        curve = session.curves["grip"]
        self.assertEqual(session.finished, ["grip"])
        self.assertEqual(curve["65"], 1.0)
        self.assertAlmostEqual(db(curve["80"]), 3.0, delta=1.0)
        self.assertAlmostEqual(db(curve["100"]), 5.0, delta=1.5)  # 80's gain and its own 2 dB
        self.assertAlmostEqual(db(curve["30"]), 10.0, delta=2.0)  # 65 -> 50 -> 40 -> 30

    def test_order_is_shuffled_and_answers_follow_it(self) -> None:
        session = Session(["trigger"], rng=random.Random(1))
        orders = set()
        for _ in range(8):
            orders.add(session.test_first)
            before = session.staircase.db
            # the test was stronger: whichever slot it played in
            session.answer(FIRST if session.test_first else SECOND)
            if session.current[1] != 130:
                break
            self.assertLess(session.staircase.db, before)
        self.assertEqual(orders, {True, False})

    def test_other_answers_change_nothing(self) -> None:
        session = Session(["grip"])
        for answer in (REPLAY, CANCEL, "same"):
            self.assertIsNone(session.answer(answer))
        self.assertEqual(session.staircase.trials, 0)

    def test_gains_stay_within_the_equalizer_and_the_output(self) -> None:
        session = Session(["grip"], rng=random.Random(5))
        while session.current:
            channel, freq, neighbour = session.current
            stronger = False  # the test always feels weaker: every step asks for more
            session.answer(FIRST if stronger == session.test_first else SECOND)
        top = min(Equalizer.LIMITS[1], 100.0 / calibrate.AMP)
        for gain in session.curves["grip"].values():
            self.assertLessEqual(gain, top + 1e-9)

    def test_save_keeps_a_motor_not_redone(self) -> None:
        stored = {"trigger": {"100": 1.0, "200": 2.0}}
        session = Session(["grip"], rng=random.Random(2))
        while session.current:
            session.answer(FIRST)
        with mock.patch("settings.load_eq", return_value=dict(stored)), mock.patch("settings.save_eq") as save:
            calibrate.save(session)
        self.assertEqual(save.call_args[0][0], {"trigger": stored["trigger"], "grip": session.curves["grip"]})


class PadButtonsTest(unittest.TestCase):
    def buttons(self, states: list[int]) -> list[str | None]:
        pad = PadButtons.__new__(PadButtons)
        queue = iter(states)
        pad._buttons = lambda: next(queue)
        pad._held = pad._buttons()
        return [pad.poll() for _ in states[1:]]

    def test_presses_count_once_when_they_go_down(self) -> None:
        left, right, lb, rb, x, b, a = 0x0004, 0x0008, 0x0100, 0x0200, 0x4000, 0x2000, 0x1000
        answers = self.buttons([left, left, 0, right, right, 0, lb, rb, x, b, a])
        # left held at the start is not a press; holding right repeats nothing; A means nothing now
        self.assertEqual(answers, [None, None, SECOND, None, None, FIRST, SECOND, REPLAY, CANCEL, None])

    def test_without_xinput_nothing_comes(self) -> None:
        with mock.patch.object(calibrate.ctypes, "windll", None, create=True):
            pad = PadButtons()
        self.assertFalse(pad.connected)
        self.assertIsNone(pad.poll())


class PlayTest(unittest.TestCase):
    class Pad:
        def __init__(self) -> None:
            self.blocks = []

        def stream(self, render) -> None:
            first = 0
            while (block := render(first)) is not None:
                self.blocks.append(block)
                first += 8

    def test_plays_both_tones_with_a_pause(self) -> None:
        pad, session = self.Pad(), Session(["trigger"], seconds=0.4)
        self.assertIsNone(calibrate.play(pad, session))
        self.assertEqual(len(pad.blocks), 50 + math.ceil(calibrate.GAP_MS / 8) + 50)
        tone = [b for b in pad.blocks if any(b.trigger)]
        self.assertEqual(len(tone), 100)  # both tones on the trigger, the pause silent

    def test_an_answer_stops_the_playback(self) -> None:
        pad, session = self.Pad(), Session(["grip"], seconds=0.4)
        heard = iter([None] * 10 + [SECOND])
        self.assertEqual(calibrate.play(pad, session, lambda: next(heard, None)), SECOND)
        self.assertEqual(len(pad.blocks), 10)

    def test_wait_gives_up_when_cancelled(self) -> None:
        self.assertEqual(calibrate.wait(lambda: None, cancelled=lambda: True), CANCEL)
        self.assertEqual(calibrate.wait(iter([None, FIRST]).__next__), FIRST)


if __name__ == "__main__":
    unittest.main()
