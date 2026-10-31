"""tone.py split check: routes per block, verdicts, and the raw identity reply it records."""

from __future__ import annotations

import unittest

from apex6 import SUBFRAMES, Route, _parse_device_info
from tone import (MAX_SAMPLE, SPLIT_FINGERS, SPLIT_ROUTES, mode_verdict, model_renderer, model_summary,
                  model_verdict, modes_verdict, motor_ram_lines, pulsed_tone, ram_changes, ram_record_name,
                  ram_state, ram_target_verdict, rate_summary, rate_verdict, split_renderer, split_verdict,
                  tap_renderer, tap_spacing)

# Motor RAM from the first trigger-target run (fw 9.0.5.5): Space Station's
# trigger vibration on for both triggers, before and after our default enable.
RAM_BEFORE = bytes.fromhex("02 02 00 40 06 00 00 00 00 00 00 00 02 00 40 06"
                           "00 00 00 00 00 00 00 01 40 01 40 00 00 00 00 00"
                           "02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
                           "00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00")
RAM_ENABLED = bytes.fromhex("02 02 00 40 06 00 00 00 00 00 00 00 02 00 40 06"
                            "00 00 00 00 00 00 00 01 40 01 40 00 00 00 00 00"
                            "02 03 00 40 00 00 00 00 00 00 00 00 03 00 40 00"
                            "00 00 00 00 00 00 00 02 40 02 40 00 00 00 00 00")

# A real 0x01 reply from an APEX 6 Pro (wired, features 0x9F).
INFO_REPLY = bytes.fromhex("5A A5 01 01 00 96 00 00 00 00 00 2A 45 01 00 90 "
                           "55 00 00 01 09 00 00 01 42 00 00 10 61 9F 00 4A")


def ratings(same: int, cross: int) -> dict[tuple[str, str], int]:
    table = {(f, r): 2 for f in SPLIT_FINGERS for r in SPLIT_ROUTES}
    table[("L", "LEFT")] = table[("R", "RIGHT")] = same
    table[("L", "RIGHT")] = table[("R", "LEFT")] = cross
    return table


class SplitRendererTest(unittest.TestCase):
    def test_fixed_route_keeps_the_tone(self) -> None:
        block = split_renderer("RIGHT", 110.0, 20.0, 0.1)(SUBFRAMES)
        self.assertEqual(block.route, Route.RIGHT)
        self.assertTrue(any(block.trigger))
        self.assertFalse(any(block.left_grip) or any(block.right_grip))

    def test_alt_sends_tone_left_and_silence_right(self) -> None:
        render = split_renderer("ALT", 110.0, 20.0, 0.1)
        even, odd = render(2 * SUBFRAMES), render(3 * SUBFRAMES)
        self.assertEqual((even.route, odd.route), (Route.LEFT, Route.RIGHT))
        self.assertTrue(any(even.trigger))
        self.assertFalse(any(odd.trigger))

    def test_ends_after_its_length(self) -> None:
        self.assertIsNone(split_renderer("BOTH", 110.0, 20.0, 0.1)(100))


class SplitVerdictTest(unittest.TestCase):
    def test_verdicts(self) -> None:
        self.assertTrue(split_verdict(ratings(same=2, cross=0)).startswith("분리됨"))
        self.assertTrue(split_verdict(ratings(same=2, cross=2)).startswith("섞임"))
        self.assertTrue(split_verdict(ratings(same=3, cross=1)).startswith("부분 분리"))
        self.assertTrue(split_verdict(ratings(same=0, cross=0)).startswith("판정 불가"))
        uneven = ratings(same=2, cross=2)
        uneven[("R", "LEFT")] = 0
        self.assertTrue(split_verdict(uneven).startswith("애매"))

    def test_route_rated_alike_under_both_fingers_is_mixed(self) -> None:
        # First real run (fw 9.0.5.5, 10%): LEFT felt weaker than RIGHT on
        # either trigger, i.e. the route set the strength, not the side.
        table = {("L", "LEFT"): 2, ("L", "RIGHT"): 3, ("L", "BOTH"): 3, ("L", "ALT"): 1,
                 ("R", "LEFT"): 2, ("R", "RIGHT"): 3, ("R", "BOTH"): 3, ("R", "ALT"): 1}
        self.assertTrue(split_verdict(table).startswith("섞임"))


def mode_ratings(**heard: tuple[int, int]) -> dict[tuple[str, int], int]:
    """{(finger, byte): rating} from m0=(left, right), m1=..., unlisted bytes 0..3 rated alike."""
    table = {(f, m): 2 for f in SPLIT_FINGERS for m in range(4)}
    for name, (left, right) in heard.items():
        table[("L", int(name[1:]))], table[("R", int(name[1:]))] = left, right
    return table


class TriggerModeVerdictTest(unittest.TestCase):
    def test_one_byte(self) -> None:
        self.assertEqual(mode_verdict(2, 0), "왼쪽만")
        self.assertEqual(mode_verdict(0, 2), "오른쪽만")
        self.assertEqual(mode_verdict(0, 0), "안 울림")
        self.assertEqual(mode_verdict(2, 2), "양쪽 같음")
        self.assertEqual(mode_verdict(3, 1), "왼쪽이 더 셈")
        self.assertEqual(mode_verdict(1, 2), "오른쪽이 더 셈")

    def test_route_like_byte_separates(self) -> None:
        self.assertTrue(modes_verdict(mode_ratings(m0=(2, 0), m1=(0, 2))).startswith("분리됨"))

    def test_weaker_cross_side_is_partial(self) -> None:
        self.assertTrue(modes_verdict(mode_ratings(m1=(2, 1), m2=(2, 2), m3=(1, 2))).startswith("부분 분리"))

    def test_only_one_side_found(self) -> None:
        self.assertTrue(modes_verdict(mode_ratings(m0=(0, 0), m1=(2, 0))).startswith("한쪽으로만"))

    def test_every_byte_alike_is_no_separation(self) -> None:
        self.assertTrue(modes_verdict(mode_ratings(m0=(0, 0))).startswith("분리 안 됨"))

    def test_control_off_reference_asks_for_a_rerun(self) -> None:
        self.assertTrue(modes_verdict(mode_ratings(m0=(2, 0), m1=(0, 2), m2=(3, 2))).startswith("다시 측정"))

    def test_target_check_uses_its_own_control(self) -> None:
        table = {(f, t): 2 for f in SPLIT_FINGERS for t in (0x10, 0x11, 0x12)}
        table[("R", 0x10)] = table[("L", 0x11)] = 0
        self.assertTrue(modes_verdict(table, control=0x12).startswith("분리됨"))
        table[("L", 0x12)] = 1
        self.assertTrue(modes_verdict(table, control=0x12).startswith("다시 측정"))


class MotorRamTest(unittest.TestCase):
    def test_both_sides_decoded(self) -> None:
        # left: Space Station's scene 1 at travel level 2; right: off
        block = bytes([0, 1, 1, 80, 255, 1, 58, 32, 64, 64, 1, 255] + [0] * 52)
        lines = motor_ram_lines(block)
        self.assertIn("장면 1, 파형 1, 행정 80~255, 반복, 간격 58, 게인 32", lines[0])
        self.assertIn("오른쪽 트리거: 끔", lines[1])
        self.assertEqual(len(lines), 2 + 4)  # and the block in 16-byte rows

    def test_real_block_decodes_as_space_station_reads_it(self) -> None:
        lines = motor_ram_lines(RAM_BEFORE)
        self.assertIn("왼쪽 트리거: 그립 진동을 따라 울림", lines[0])
        self.assertIn("오른쪽 트리거: 그립 진동을 따라 울림", lines[1])

    def test_records_by_area(self) -> None:
        self.assertEqual(ram_state(RAM_BEFORE), "0x0_ 왼쪽 끔, 0x0_ 오른쪽 끔, 0x1_ 왼쪽 끔, 0x1_ 오른쪽 끔")
        self.assertEqual(ram_state(RAM_ENABLED), "0x0_ 왼쪽 3, 0x0_ 오른쪽 3, 0x1_ 왼쪽 2, 0x1_ 오른쪽 2")
        self.assertEqual(ram_state(RAM_BEFORE, area=0), "0x0_ 왼쪽 2, 0x0_ 오른쪽 2, 0x1_ 왼쪽 1, 0x1_ 오른쪽 1")
        self.assertEqual(ram_record_name(0x37), "area 1 0x1_ 왼쪽 mode")
        self.assertEqual(ram_record_name(0x3A), "area 1 0x1_ 오른쪽 param")
        self.assertEqual(ram_record_name(0x23), "area 1 0x0_ 왼쪽 param")
        self.assertEqual(ram_record_name(0x20), "area 1 머리")
        self.assertEqual(ram_record_name(0x1F), "area 0 나머지")

    def test_changes_name_their_records(self) -> None:
        changes = ram_changes(RAM_BEFORE, RAM_ENABLED)
        self.assertEqual(len(changes), 8)
        self.assertIn("0x21 area 1 0x0_ 왼쪽 mode: 00 -> 03", changes[0])
        self.assertEqual(ram_changes(RAM_BEFORE, RAM_BEFORE), [])

    def test_target_verdicts(self) -> None:
        self.assertTrue(ram_target_verdict({0x10: (True, False), 0x11: (False, True)}).startswith("0x10은 왼쪽"))
        self.assertTrue(ram_target_verdict({0x10: (True, True), 0x11: (True, True)}).startswith("0x10·0x11도 양쪽"))
        self.assertTrue(ram_target_verdict({0x10: (False, False), 0x11: (False, False)}).startswith("0x10·0x11은"))
        self.assertTrue(ram_target_verdict({0x10: (True, False), 0x11: (True, False)}).startswith("예상과 다른"))
        self.assertTrue(ram_target_verdict({0x12: (True, True)}).startswith("0x10과 0x11을"))

    def test_unknown_mode_and_short_block(self) -> None:
        self.assertIn("알 수 없음 (mode 7)", motor_ram_lines(bytes([0, 7] + [0] * 30))[0])
        self.assertIn("해석 못 함", motor_ram_lines(bytes(8))[0])


class ReportRateTest(unittest.TestCase):
    def blocks(self, period_ms: float, taps: int = 8) -> list:
        render, out = tap_renderer(period_ms, taps, 70.0), []
        while (block := render(len(out) * SUBFRAMES)) is not None:
            out.append(block)
        return out

    def test_spacing_is_about_half_a_second_and_odd(self) -> None:
        self.assertEqual([tap_spacing(p) for p in (8.0, 4.0, 2.0)], [63, 125, 251])

    def test_each_tap_fills_one_report(self) -> None:
        for period in (8.0, 4.0, 2.0):
            blocks = self.blocks(period)
            tapped = [i for i, b in enumerate(blocks) if any(b.left_grip)]
            self.assertEqual(len(tapped), 8)
            self.assertEqual({b - a for a, b in zip(tapped, tapped[1:])}, {tap_spacing(period)})
            click = blocks[tapped[0]]
            self.assertEqual(click.left_grip, click.right_grip)
            self.assertFalse(any(click.trigger))
            self.assertTrue(all(s > 0 for s in click.left_grip[:4]) and all(s < 0 for s in click.left_grip[4:]))
            self.assertGreater(tapped[0], 0)
            self.assertGreater(len(blocks) - 1, tapped[-1])

    def test_a_pad_keeping_one_report_in_k_keeps_one_tap_in_k(self) -> None:
        for period, k in ((4.0, 2), (2.0, 4)):
            blocks = self.blocks(period)
            for start in range(k):
                kept = sum(1 for b in blocks[start::k] if any(b.left_grip))
                self.assertEqual(kept, 8 // k)

    def test_verdicts(self) -> None:
        self.assertEqual(rate_verdict(8.0, 8, 8), "대조군 정상")
        self.assertEqual(rate_verdict(8.0, 8, 7), "대조군 정상")
        self.assertEqual(rate_verdict(8.0, 8, 4), "대조군 어긋남")
        self.assertEqual(rate_verdict(2.0, 8, 8), "다 들림: 리포트를 다 재생 (4 kHz)")
        self.assertEqual(rate_verdict(4.0, 8, 4), "1 kHz 고정: 리포트 2개 중 1개만 재생")
        self.assertEqual(rate_verdict(2.0, 8, 2), "1 kHz 고정: 리포트 4개 중 1개만 재생")
        self.assertTrue(rate_verdict(4.0, 8, 6).startswith("1 kHz 고정으로 보임"))
        self.assertTrue(rate_verdict(2.0, 8, 0).startswith("판정 불가"))
        self.assertTrue(rate_verdict(2.0, 8, 11).startswith("예상 밖"))

    def test_summary(self) -> None:
        control = {8.0: (8, 8)}
        self.assertTrue(rate_summary({**control, 4.0: (8, 8), 2.0: (8, 7)}).startswith("패드가 빠른 리포트를 다 재생함"))
        self.assertTrue(rate_summary({**control, 4.0: (8, 4), 2.0: (8, 2)}).startswith("패드는 1 kHz로 재생함"))
        self.assertTrue(rate_summary({**control, 4.0: (8, 8), 2.0: (8, 2)}).startswith("섞인 결과"))
        self.assertTrue(rate_summary({**control, 4.0: (8, 4), 2.0: (8, 0)}).startswith("다시 측정: 탭이"))
        self.assertTrue(rate_summary({8.0: (8, 5), 4.0: (8, 4)}).startswith("다시 측정: 대조군"))
        self.assertTrue(rate_summary(control).startswith("8 ms보다 짧은"))


SECONDS = 0.48  # a whole number of 8 ms reports


def model_blocks(shape: str, period_ms: float) -> list:
    render, out = model_renderer(shape, period_ms, 160.0, 40.0, SECONDS), []
    while (block := render(len(out) * SUBFRAMES)) is not None:
        out.append(block)
    return out


def preempt(blocks: list, period_ms: int) -> list[float]:
    """What a pad plays at 1 kHz if each report sounds until the next cuts it off."""
    return [s for b in blocks for s in b.left_grip[:period_ms]]


def play_all(blocks: list) -> list[float]:
    """What a pad plays if every sample of every report sounds, at 8/P kHz."""
    return [s for b in blocks for s in b.left_grip]


class ReportModelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.reference = play_all(model_blocks("X", 8.0))

    def test_reference_is_the_pulsed_tone_at_1_khz(self) -> None:
        self.assertEqual(model_blocks("Y", 8.0)[3].left_grip, model_blocks("X", 8.0)[3].left_grip)
        self.assertEqual(len(self.reference), 480)
        peak = MAX_SAMPLE * 0.4
        expected = [pulsed_tone(n / 1000.0, 160.0, peak, SECONDS) for n in range(480)]
        self.assertLess(max(abs(a - b) for a, b in zip(self.reference, expected)), 1e-9)
        self.assertEqual(self.reference[200:250], [0.0] * 50)  # the gap after 175 ms on

    def test_x_matches_the_reference_only_under_preempt(self) -> None:
        for period in (4, 2):
            blocks = model_blocks("X", float(period))
            self.assertEqual(preempt(blocks, period), self.reference)
            self.assertEqual(blocks[5].left_grip[period:], [0.0] * (SUBFRAMES - period))
            self.assertNotEqual(play_all(blocks)[:480], self.reference)

    def test_y_matches_the_reference_only_when_all_play(self) -> None:
        peak = MAX_SAMPLE * 0.4
        for period in (4, 2):
            blocks = model_blocks("Y", float(period))
            rate = SUBFRAMES / period * 1000.0
            played = play_all(blocks)
            expected = [pulsed_tone(n / rate, 160.0, peak, SECONDS) for n in range(len(played))]
            self.assertEqual(len(played), len(expected))
            self.assertLess(max(abs(a - b) for a, b in zip(played, expected)), 1e-9)
            self.assertNotEqual(preempt(blocks, period), self.reference)

    def test_both_cover_the_same_time(self) -> None:
        self.assertEqual(len(model_blocks("X", 4.0)), 120)
        self.assertEqual(len(model_blocks("Y", 4.0)), 120)
        self.assertEqual(len(model_blocks("Y", 2.0)), 240)

    def test_verdicts(self) -> None:
        self.assertTrue(model_verdict(4.0, ["X", "X"]).startswith("선점"))
        self.assertEqual(model_verdict(2.0, ["Y", "Y"]), "다 재생: 4 kHz")
        self.assertEqual(model_verdict(4.0, ["Y", "Y", ""]), "다 재생: 2 kHz (구분 안 된 답 있음)")
        # second run (wired, 2026-10-31): one X and one shrug at 4 ms is no verdict
        self.assertTrue(model_verdict(4.0, ["X", ""]).startswith("판정 불가"))
        self.assertTrue(model_verdict(4.0, ["X", "Y"]).startswith("애매"))
        self.assertTrue(model_verdict(4.0, ["", ""]).startswith("판정 불가"))

    def test_summary(self) -> None:
        preempted = model_verdict(4.0, ["X", "X"])
        all_played = model_verdict(2.0, ["Y", "Y"])
        self.assertTrue(model_summary({4.0: preempted, 2.0: preempted}).startswith("패드는 1 kHz로"))
        self.assertTrue(model_summary({4.0: all_played, 2.0: all_played}).startswith("패드가 리포트 간격만큼"))
        self.assertTrue(model_summary({4.0: preempted, 2.0: all_played}).startswith("섞인 결과"))


class DeviceInfoTest(unittest.TestCase):
    def test_raw_reply_is_kept(self) -> None:
        info = _parse_device_info(list(INFO_REPLY))
        self.assertIsNotNone(info)
        self.assertEqual((info.device_type, info.features), (0x96, 0x9F))
        self.assertEqual(info.raw, INFO_REPLY)

    def test_firmware_matches_space_station(self) -> None:
        info = _parse_device_info(list(INFO_REPLY))
        self.assertEqual(info.firmware, {"controller": "9.0.5.5", "si": "0.1.0.9",
                                         "screen": "0.1.4.2", "rf": "1.0.6.1"})
        self.assertEqual(_parse_device_info([0, *INFO_REPLY]).firmware["controller"], "9.0.5.5")

    def test_dongle_version_when_present(self) -> None:
        reply = bytearray(INFO_REPLY)
        reply[17:19] = b"\x04\x91"
        reply[31] = sum(reply[2:31]) & 0xFF
        self.assertEqual(_parse_device_info(list(reply)).firmware["dongle"], "0.4.9.1")


if __name__ == "__main__":
    unittest.main()
