"""Apex6.stream: every 8 ms block reaches the pad, silence included."""

from __future__ import annotations

import unittest

from apex6 import SUBFRAMES, Apex6, Block, build_realtime


class FakeDevice:
    def __init__(self) -> None:
        self.reports: list[bytes] = []

    def write(self, report: bytes) -> int:
        self.reports.append(bytes(report))
        return len(report)


class StreamTest(unittest.TestCase):
    def test_silence_is_sent_every_block(self) -> None:
        # Thinned to a keepalive every 48 ms, silence left the pad playing the
        # next tone low and pulsing; sent every block it plays it cleanly.
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode = FakeDevice(), True, 0x02
        tone = Block(left_grip=[50.0] * SUBFRAMES)
        blocks = [tone] * 5 + [Block()] * 40 + [tone] * 5
        stats = pad.stream(lambda first: blocks[first // SUBFRAMES] if first // SUBFRAMES < len(blocks) else None)
        realtime = [r for r in pad._dev.reports if r == build_realtime(Block()) or r == build_realtime(tone)]
        self.assertEqual(len(realtime), len(blocks))
        self.assertEqual(stats.frames, len(blocks))


if __name__ == "__main__":
    unittest.main()
