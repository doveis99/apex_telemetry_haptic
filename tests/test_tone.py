"""tone.py split check: routes per block, verdicts, and the raw identity reply it records."""

from __future__ import annotations

import unittest

from apex6 import SUBFRAMES, Route, _parse_device_info
from tone import SPLIT_FINGERS, SPLIT_ROUTES, mode_verdict, modes_verdict, split_renderer, split_verdict

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
