"""Calibration: gain steps, a session's progress and results, pad button presses, answers during playback."""

from __future__ import annotations

import unittest
from unittest import mock

import calibrate
from calibrate import CANCEL, REPLAY, SAME, STRONGER, WEAKER, PadButtons, Session
from effects import Equalizer


class StepTest(unittest.TestCase):
    def test_steps_and_limits(self) -> None:
        factor = 10 ** (calibrate.STEP_DB / 20)
        self.assertAlmostEqual(calibrate.step(1.0, STRONGER), factor)  # the test felt weaker: more drive
        self.assertAlmostEqual(calibrate.step(1.0, WEAKER), 1.0 / factor)
        low, high = Equalizer.LIMITS
        self.assertEqual(calibrate.step(high, STRONGER), high)
        self.assertEqual(calibrate.step(low, WEAKER), low)


class SessionTest(unittest.TestCase):
    def test_answers_walk_both_motors(self) -> None:
        session = Session(["grip", "trigger"])
        self.assertEqual(session.current, ("grip", calibrate.FREQS["grip"][0]))
        session.answer(STRONGER)
        session.answer(STRONGER)
        self.assertIsNone(session.answer(SAME))
        self.assertAlmostEqual(session.curves["grip"]["30"], round(10 ** (2 * calibrate.STEP_DB / 20), 3))
        self.assertEqual(session.gain, 1.0)  # each frequency starts even with the reference
        finished = [session.answer(SAME) for _ in range(len(calibrate.FREQS["grip"]) - 1)]
        self.assertEqual(finished[-1], "grip")
        self.assertEqual(session.finished, ["grip"])
        self.assertEqual(session.curves["grip"]["65"], 1.0)  # the reference itself
        for _ in calibrate.FREQS["trigger"]:
            session.answer(SAME)
        self.assertIsNone(session.current)
        self.assertEqual(session.finished, ["grip", "trigger"])

    def test_a_limit_waits_for_same(self) -> None:
        session = Session(["grip"])
        for _ in range(40):
            session.answer(STRONGER)
        self.assertTrue(session.at_limit)
        self.assertEqual(session.current[1], 30.0)  # still on it until the player takes it
        for answer in (REPLAY, CANCEL):
            self.assertIsNone(session.answer(answer))
        self.assertEqual(session.current[1], 30.0)

    def test_save_keeps_a_motor_not_redone(self) -> None:
        stored = {"trigger": {"100": 1.0, "200": 2.0}}
        session = Session(["grip"])
        for _ in calibrate.FREQS["grip"]:
            session.answer(SAME)
        with mock.patch("settings.load_eq", return_value=dict(stored)), mock.patch("settings.save_eq") as save:
            calibrate.save(session)
        self.assertEqual(save.call_args[0][0], {"trigger": stored["trigger"], "grip": session.curves["grip"]})


class PadButtonsTest(unittest.TestCase):
    def buttons(self, states: list[int]) -> tuple[PadButtons, list[str | None]]:
        pad = PadButtons.__new__(PadButtons)
        queue = iter(states)
        pad._buttons = lambda: next(queue)
        pad._held = pad._buttons()
        return pad, [pad.poll() for _ in states[1:]]

    def test_presses_count_once_when_they_go_down(self) -> None:
        up, down, a, x, b = 0x0001, 0x0002, 0x1000, 0x4000, 0x2000
        _, answers = self.buttons([a, a, 0, up, up, up | down, 0, x, b, 0x0300])
        # A held at the start is not a press; holding up repeats nothing; LB/RB mean nothing
        self.assertEqual(answers, [None, None, STRONGER, None, WEAKER, None, REPLAY, CANCEL, None])

    def test_without_xinput_nothing_comes(self) -> None:
        with mock.patch.object(calibrate.ctypes, "windll", None, create=True):
            pad = PadButtons()
        self.assertFalse(pad.connected)
        self.assertIsNone(pad.poll())


class PlayTest(unittest.TestCase):
    class Pad:
        def __init__(self) -> None:
            self.blocks = 0

        def stream(self, render) -> None:
            first = 0
            while render(first) is not None:
                self.blocks += 1
                first += 8

    def test_plays_reference_gap_and_test(self) -> None:
        pad, session = self.Pad(), Session(["trigger"], seconds=0.4)
        self.assertIsNone(calibrate.play(pad, session))
        self.assertEqual(pad.blocks, 50 + 50 + 50)  # 0.4 s, 0.4 s, 0.4 s of 8 ms blocks

    def test_an_answer_stops_the_playback(self) -> None:
        pad, session = self.Pad(), Session(["grip"], seconds=0.4)
        heard = iter([None] * 10 + [WEAKER])
        self.assertEqual(calibrate.play(pad, session, lambda: next(heard, None)), WEAKER)
        self.assertEqual(pad.blocks, 10)

    def test_wait_gives_up_when_cancelled(self) -> None:
        self.assertEqual(calibrate.wait(lambda: None, cancelled=lambda: True), CANCEL)
        self.assertEqual(calibrate.wait(iter([None, SAME]).__next__), SAME)


if __name__ == "__main__":
    unittest.main()
