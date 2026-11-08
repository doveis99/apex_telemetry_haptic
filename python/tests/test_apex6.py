"""Apex6.stream: every 8 ms block reaches the pad, silence included."""

from __future__ import annotations

import ctypes
import unittest
from ctypes import wintypes
from unittest import mock

from apex6 import (CMD_MOTOR_ROUTE, CMD_RAM_READ, ENABLE_DELAY, FRAME_PERIOD, MAGIC, MOTOR_DISABLE, RAM_MOTOR,
                   SILENT_REPORT, SUBFRAMES, THREAD_PRIORITY_TIME_CRITICAL, Apex6, Block, Route, _inline_command,
                   _ram_chunk, build_command, build_realtime, motor_enable_reports)


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
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x12
        pad._trigger_side = 0x02
        tone = Block(left_grip=[50.0] * SUBFRAMES)
        blocks = [tone] * 5 + [Block()] * 40 + [tone] * 5
        stats = pad.stream(lambda first: blocks[first // SUBFRAMES] if first // SUBFRAMES < len(blocks) else None)
        realtime = [r for r in pad._dev.reports if r == build_realtime(Block()) or r == build_realtime(tone)]
        self.assertEqual(len(realtime), len(blocks))
        self.assertEqual(stats.frames, len(blocks))

    def test_period_sets_the_pace(self) -> None:
        clock = FakeClock()
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x12
        pad._trigger_side = 0x02
        with mock.patch("apex6.time", clock):
            stats = pad.stream(lambda first: Block() if first < 10 * SUBFRAMES else None, period=0.002)
        self.assertEqual((stats.frames, stats.late_frames), (10, 0))
        self.assertAlmostEqual(clock.now, 0.02)

    def test_the_stream_runs_time_critical_and_lets_it_go(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentThread.restype = wintypes.HANDLE
        kernel32.GetThreadPriority.argtypes = (wintypes.HANDLE,)

        def priority() -> int:
            return kernel32.GetThreadPriority(kernel32.GetCurrentThread())

        before, during = priority(), []
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x12
        pad._trigger_side = 0x02
        pad.stream(lambda first: during.append(priority()) or (Block() if first < 3 * SUBFRAMES else None))
        self.assertEqual(set(during), {THREAD_PRIORITY_TIME_CRITICAL})
        self.assertEqual(priority(), before)


class EnableTest(unittest.TestCase):
    def test_the_wait_after_the_route_is_silence_every_block(self) -> None:
        clock = FakeClock()
        pad = Apex6.__new__(Apex6)
        pad._dev = FakeDevice()
        with mock.patch("apex6.time", clock):
            pad.enable()
        self.assertEqual(pad._dev.reports[:2], list(motor_enable_reports()))
        silence = pad._dev.reports[2:]
        self.assertEqual(silence, [SILENT_REPORT] * len(silence))
        self.assertEqual(len(silence), round(ENABLE_DELAY / FRAME_PERIOD))
        self.assertAlmostEqual(clock.now, len(silence) * FRAME_PERIOD)


class EncodeTest(unittest.TestCase):
    def test_samples_use_the_whole_int8_range(self) -> None:
        report = build_realtime(Block(left_grip=[127.0] * SUBFRAMES, right_grip=[-300.0] * SUBFRAMES))
        self.assertEqual((report[7], report[8]), (0x80 + 127, 0x80 - 127))


class UpsampleBitTest(unittest.TestCase):
    def test_bit_7_is_set_unless_a_block_clears_it(self) -> None:
        tone = [10.0] * SUBFRAMES
        joined = build_realtime(Block(left_grip=tone, right_grip=tone))
        raw = build_realtime(Block(left_grip=tone, right_grip=tone, upsample=False))
        self.assertEqual(joined[5], 0x9B)
        self.assertEqual(raw[5], 0x1B)
        self.assertEqual(raw[6:32], joined[6:32])
        self.assertEqual(SILENT_REPORT[5] & 0x80, 0x80)


class PrimerTest(unittest.TestCase):
    def test_primers_go_out_ahead_then_one_per_period(self) -> None:
        clock = FakeClock()
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x12
        pad._trigger_side = 0x02
        sent: list[float] = []
        with mock.patch("apex6.time", clock):
            stats = pad.stream(lambda first: Block() if first < 5 * SUBFRAMES else None, period=0.004,
                               primers=2, sent=sent)
        self.assertEqual(stats.frames, 5)
        self.assertEqual([round(t * 1000, 6) for t in sent], [0.0, 0.0, 0.0, 4.0, 8.0])
        self.assertEqual(stats.started, 0.0)

    def test_slow_writes_during_the_primers_keep_the_spare(self) -> None:
        # The 2026-11-08 dropout run: writes take ~2 ms, so after a few primers the 4 ms pace
        # had passed; counting that as late reset the pace and threw away 10 of 24 ms.
        clock = FakeClock()

        class SlowDevice(FakeDevice):
            def write(self, report: bytes) -> int:
                clock.now += 0.002
                return super().write(report)

        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = SlowDevice(), True, 0x02, 0x12
        pad._trigger_side = 0x02
        sent: list[float] = []
        with mock.patch("apex6.time", clock):
            stats = pad.stream(lambda first: Block() if first < 20 * SUBFRAMES else None, period=0.004,
                               primers=6, sent=sent)
        spare = [round((k * 0.004 - t) * 1000, 6) for k, t in enumerate(sent)]
        self.assertEqual(spare[-8:], [24.0] * 8)
        self.assertEqual(stats.late_frames, 0)

    def test_no_primers_keeps_the_pace(self) -> None:
        clock = FakeClock()
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x12
        pad._trigger_side = 0x02
        sent: list[float] = []
        with mock.patch("apex6.time", clock):
            pad.stream(lambda first: Block() if first < 3 * SUBFRAMES else None, sent=sent)
        self.assertEqual([round(t * 1000, 6) for t in sent], [0.0, 8.0, 16.0])


class FakeClock:
    """Stands in for the time module: sleeping moves the clock."""

    def __init__(self) -> None:
        self.now = 0.0

    def perf_counter(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def ram_reply(index: int, data: bytes, total: int, status: int = 0, ram_id: int = RAM_MOTOR,
              checksum_at_end: bool = False, report_id: bool = True) -> list[int]:
    """A 0xA3 READ_CHUNK reply as the pad would send it: no length byte, checksum after the data."""
    body = [CMD_RAM_READ, 0x00, status, ram_id, 1, total & 0xFF, total >> 8, index, len(data), *data]
    frame = [*MAGIC, *body]
    if checksum_at_end:  # padded, with the checksum in the last byte as 0x01 replies have it
        frame += [0] * (31 - len(frame))
        frame.append(sum(frame[2:31]) & 0xFF)
    else:
        frame.append(sum(body) & 0xFF)
        frame += [0] * (32 - len(frame))
    return [0, *frame] if report_id else frame


class FakeRamDevice(FakeDevice):
    """Answers 0xA3 reads of a block; other writes are only recorded."""

    def __init__(self, block: bytes, status: int = 0) -> None:
        super().__init__()
        self.block, self.status, self.pending = block, status, []

    def write(self, report: bytes) -> int:
        if report[3] == CMD_RAM_READ:
            index, size = report[7], report[8]
            part = self.block[index * size:(index + 1) * size]
            self.pending.append(ram_reply(index, part, len(self.block), self.status, ram_id=report[6]))
        return super().write(report)

    def read(self, size: int, timeout_ms: int) -> list[int]:
        return self.pending.pop(0) if self.pending else []


class MotorRouteTest(unittest.TestCase):
    def test_default_frames_are_the_captured_ones(self) -> None:
        self.assertEqual(motor_enable_reports(), (
            build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x02, 0x03, 0x00, 0x40])),
            build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x12, 0x02, 0x40, 0x00]))))

    def test_trigger_target_replaces_only_its_byte(self) -> None:
        _, trigger = motor_enable_reports(trigger_target=0x10)
        self.assertEqual(trigger, build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x10, 0x02, 0x40, 0x00])))

    def test_trigger_side_replaces_only_its_byte(self) -> None:
        side, other = motor_enable_reports(trigger_side=Route.LEFT)
        self.assertEqual(side, build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x00, 0x03, 0x00, 0x40])))
        self.assertEqual(other, motor_enable_reports()[1])

    def test_an_experimental_target_is_released_too(self) -> None:
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x11
        pad._trigger_side = 0x02
        pad.disable()
        self.assertIn(build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x11, 0x00])), pad._dev.reports)
        self.assertIn(build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x12, 0x00])), pad._dev.reports)
        self.assertNotIn(build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x00, 0x00])), pad._dev.reports)

    def test_one_side_is_released_too(self) -> None:
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x12
        pad._trigger_side = Route.RIGHT
        pad.disable()
        released = [r for r in pad._dev.reports if r[3] == CMD_MOTOR_ROUTE]
        self.assertEqual(released, [build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x01, 0x00])),
                                    *MOTOR_DISABLE])

    def test_reasserts_keep_the_side(self) -> None:
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled = FakeDevice(), False
        with mock.patch("apex6.time", FakeClock()):
            pad.reconfigure(trigger_side=Route.LEFT)
        pad.set_trigger_side(Route.RIGHT)
        pad._dev.reports.clear()
        pad.reassert()
        self.assertEqual(pad._dev.reports, list(motor_enable_reports(trigger_side=Route.RIGHT)))


def side_on(side: int) -> bytes:
    return build_command(CMD_MOTOR_ROUTE, bytes([0x01, side, 0x03, 0x00, 0x40]))


def side_off(side: int) -> bytes:
    return build_command(CMD_MOTOR_ROUTE, bytes([0x01, side, 0x00]))


class TriggerSideTest(unittest.TestCase):
    def pad(self, side: Route = Route.BOTH) -> Apex6:
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x12
        pad._trigger_side = side
        return pad

    def test_the_new_side_goes_on_before_the_old_one_goes_off(self) -> None:
        for old, new, sent in ((Route.BOTH, Route.LEFT, [side_on(0), side_off(1)]),
                               (Route.LEFT, Route.RIGHT, [side_on(1), side_off(0)]),
                               (Route.RIGHT, Route.BOTH, [side_on(2)]),
                               (Route.LEFT, Route.LEFT, [])):
            pad = self.pad(old)
            pad.set_trigger_side(new)
            self.assertEqual(pad._dev.reports, sent, f"{old.name} -> {new.name}")

    def test_the_stream_follows_each_blocks_side(self) -> None:
        pad = self.pad()
        tone = [30.0] * SUBFRAMES
        blocks = [Block(trigger=tone, route=Route.BOTH, side=Route.LEFT)] * 3 + [Block()] * 2 \
            + [Block(trigger=tone, route=Route.BOTH, side=Route.RIGHT)] * 2
        pad.stream(lambda first: blocks[first // SUBFRAMES] if first // SUBFRAMES < len(blocks) else None)
        names = {side_on(0): "L on", side_off(0): "L off", side_on(1): "R on", side_off(1): "R off",
                 build_realtime(blocks[0]): "tone", SILENT_REPORT: "-"}
        # each switch goes out just before the first block that needs it
        self.assertEqual([names[r] for r in pad._dev.reports],
                         ["L on", "R off", "tone", "tone", "tone", "-", "-", "R on", "L off", "tone", "tone"])

    def test_blocks_without_a_side_leave_it(self) -> None:
        pad = self.pad(Route.LEFT)
        pad.stream(lambda first: Block(trigger=[30.0] * SUBFRAMES, route=Route.RIGHT) if first < 3 * SUBFRAMES
                   else None)
        self.assertFalse([r for r in pad._dev.reports if r[3] == CMD_MOTOR_ROUTE])


class RamReadTest(unittest.TestCase):
    def test_chunk_reply(self) -> None:
        data = bytes(range(16))
        self.assertEqual(_ram_chunk(ram_reply(2, data, 64), RAM_MOTOR, 2), (0, 1, 64, data))
        self.assertEqual(_ram_chunk(ram_reply(2, data, 64, report_id=False), RAM_MOTOR, 2), (0, 1, 64, data))
        self.assertEqual(_ram_chunk(ram_reply(2, data, 64, checksum_at_end=True), RAM_MOTOR, 2), (0, 1, 64, data))

    def test_other_replies_are_skipped(self) -> None:
        data = bytes(16)
        self.assertIsNone(_ram_chunk(ram_reply(1, data, 64), RAM_MOTOR, 2))  # another chunk
        self.assertIsNone(_ram_chunk(ram_reply(2, data, 64, ram_id=4), RAM_MOTOR, 2))  # another block
        broken = ram_reply(2, data, 64)
        broken[12] ^= 0xFF
        self.assertIsNone(_ram_chunk(broken, RAM_MOTOR, 2))
        self.assertIsNone(_ram_chunk([0] * 33, RAM_MOTOR, 2))

    def test_reads_the_whole_block(self) -> None:
        block = bytes((i * 7) & 0xFF for i in range(64))
        pad = Apex6.__new__(Apex6)
        pad._dev = FakeRamDevice(block)
        self.assertEqual(pad.read_ram(RAM_MOTOR, timeout=0.05), (1, block))

    def test_inline_checksum_follows_the_payload(self) -> None:
        # Space Station's builder: 5A A5 cmd len payload sum(cmd..payload), then padding
        report = _inline_command(CMD_RAM_READ, bytes([0x00, RAM_MOTOR, 0x00, 0x10]))
        self.assertEqual(list(report[:10]), [0, 0x5A, 0xA5, 0xA3, 0x06, 0x00, 0x06, 0x00, 0x10, 0xBF])
        self.assertFalse(any(report[10:]))
        self.assertEqual(build_command(CMD_RAM_READ, bytes([0x00, RAM_MOTOR, 0x00, 0x10]))[32], 0xBF)

    def test_second_attempt_uses_the_other_framing(self) -> None:
        pad = Apex6.__new__(Apex6)
        pad._dev = FakeDevice()
        pad._dev.read = lambda size, timeout_ms: []
        with self.assertRaises(RuntimeError):
            pad.read_ram(RAM_MOTOR, timeout=0.01, attempts=2)
        request = bytes([0x00, RAM_MOTOR, 0x00, 0x10])
        self.assertEqual(pad._dev.reports, [build_command(CMD_RAM_READ, request), _inline_command(CMD_RAM_READ, request)])

    def test_status_and_silence_raise(self) -> None:
        pad = Apex6.__new__(Apex6)
        pad._dev = FakeRamDevice(bytes(64), status=3)
        with self.assertRaisesRegex(RuntimeError, "status 3"):
            pad.read_ram(RAM_MOTOR, timeout=0.05)
        pad._dev = FakeDevice()
        pad._dev.read = lambda size, timeout_ms: []
        with self.assertRaisesRegex(RuntimeError, "no reply"):
            pad.read_ram(RAM_MOTOR, timeout=0.01, attempts=2)


if __name__ == "__main__":
    unittest.main()
