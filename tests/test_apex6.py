"""Apex6.stream: every 8 ms block reaches the pad, silence included."""

from __future__ import annotations

import unittest
from unittest import mock

from apex6 import (CMD_MOTOR_ROUTE, CMD_RAM_READ, MAGIC, RAM_MOTOR, SUBFRAMES, Apex6, Block, _inline_command,
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
        with mock.patch("apex6.time", clock):
            stats = pad.stream(lambda first: Block() if first < 10 * SUBFRAMES else None, period=0.002)
        self.assertEqual((stats.frames, stats.late_frames), (10, 0))
        self.assertAlmostEqual(clock.now, 0.02)


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

    def test_an_experimental_target_is_released_too(self) -> None:
        pad = Apex6.__new__(Apex6)
        pad._dev, pad._enabled, pad._trigger_mode, pad._trigger_target = FakeDevice(), True, 0x02, 0x11
        pad.disable()
        self.assertIn(build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x11, 0x00])), pad._dev.reports)
        self.assertIn(build_command(CMD_MOTOR_ROUTE, bytes([0x01, 0x12, 0x00])), pad._dev.reports)


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
