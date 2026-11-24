"""The plugin's event flags flip at each event and stay: an event is a flag changing."""

from __future__ import annotations

import unittest

from telemetry import FLIPPED_EVENTS, GAME_EVENT_NAMES, EventPulses


def sounding(pulses: EventPulses, now: float, render_time: int, active: bool = True, **flags: bool) -> str:
    state = tuple(flags.get(name, False) for name in GAME_EVENT_NAMES[:FLIPPED_EVENTS])
    return " ".join(name for name, on in zip(GAME_EVENT_NAMES, pulses.read(active, render_time, state, now)) if on)


class EventPulsesTest(unittest.TestCase):
    def test_a_flipped_flag_is_one_event_whichever_way_it_flips(self) -> None:
        pulses = EventPulses(pulse_s=0.3)
        # left on by a ferry earlier in this game session
        self.assertEqual(sounding(pulses, 0.0, 1000, ferry=True), "")
        self.assertEqual(sounding(pulses, 1.0, 2000, ferry=True, tollgate=True), "tollgate")
        self.assertEqual(sounding(pulses, 1.2, 3000, ferry=True, tollgate=True), "tollgate")
        self.assertEqual(sounding(pulses, 1.4, 4000, ferry=True, tollgate=True), "")
        # the second tollgate turns the flag off again
        self.assertEqual(sounding(pulses, 5.0, 5000, ferry=True), "tollgate")
        self.assertEqual(sounding(pulses, 5.4, 6000, ferry=True), "")

    def test_a_game_restart_is_no_event(self) -> None:
        pulses = EventPulses(pulse_s=0.3)
        self.assertEqual(sounding(pulses, 0.0, 1000, ferry=True), "")
        # the plugin goes inactive on quitting, and starts the next game with its memory cleared
        self.assertEqual(sounding(pulses, 1.0, 1000, active=False, ferry=True), "")
        self.assertEqual(sounding(pulses, 9.0, 100), "")
        # nothing was read while it was down: the game clock starting over tells
        self.assertEqual(sounding(pulses, 10.0, 8000, ferry=True), "ferry")
        self.assertEqual(sounding(pulses, 20.0, 50), "")


if __name__ == "__main__":
    unittest.main()
