"""run(): retrying an absent controller neither rotates debug logs nor keeps a stale mapping."""

from __future__ import annotations

import unittest
from unittest import mock

import ats_haptics
from apex6 import DeviceUnavailable, StreamStats
from effects import Gains


class FakeLog:
    opened = 0

    def __init__(self) -> None:
        FakeLog.opened += 1
        self.path = mock.Mock()
        self.path.name = f"blocks-{FakeLog.opened}.csv"
        self.rows = 0
        self.closed = False

    def add(self, *_args) -> None:
        self.rows += 1

    def close(self) -> None:
        self.closed = True


class RunTest(unittest.TestCase):
    def test_log_per_connection_not_per_attempt(self) -> None:
        FakeLog.opened = 0
        runner = ats_haptics.Runner(Gains(), simulate=True, log_blocks=True)
        source_closes = []
        runner.source.close = lambda: source_closes.append(1)
        attempts = []

        class Pad:
            info = mock.Mock()

            def __enter__(self):
                attempts.append(1)
                if len(attempts) <= 3:  # asleep for three tries, then it answers
                    raise DeviceUnavailable("asleep")
                return self

            def __exit__(self, *exc) -> None:
                pass

            def stream(self, render) -> StreamStats:
                for _ in range(5):
                    render(0)
                runner.stop_requested = True
                return StreamStats()

        with mock.patch.object(ats_haptics, "Apex6", Pad), mock.patch.object(ats_haptics, "BlockLog", FakeLog), \
                mock.patch.object(ats_haptics, "RETRY_S", 0.01):
            log_seen = []
            real_open = runner._open_log
            runner._open_log = lambda: (real_open(), log_seen.append(runner.block_log))
            ats_haptics.run(runner)

        self.assertEqual(len(attempts), 4)
        self.assertEqual(FakeLog.opened, 1)
        self.assertEqual(log_seen[0].rows, 5)
        self.assertTrue(log_seen[0].closed)
        self.assertEqual(len(source_closes), 4)  # the mapping is opened afresh for each attempt

    def test_switch_mid_drive_starts_and_stops_the_log(self) -> None:
        runner = ats_haptics.Runner(Gains(), simulate=True)
        with mock.patch.object(ats_haptics, "BlockLog", FakeLog):
            runner._open_log()  # the controller is taken with the switch off
            runner.render()
            self.assertIsNone(runner.block_log)
            runner.log_blocks = True  # switched on from the GUI thread
            runner.render()
            log = runner.block_log
            self.assertEqual(log.rows, 1)
            runner.log_blocks = False
            runner.render()
            self.assertIsNone(runner.block_log)
            self.assertTrue(log.closed)
            self.assertEqual(log.rows, 1)
            runner._close_log()  # controller let go: switching on now waits for the next connection
            runner.log_blocks = True
            runner.render()
            self.assertIsNone(runner.block_log)


if __name__ == "__main__":
    unittest.main()
