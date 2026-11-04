"""Test tones for the APEX 6 Pro voice coils.

Examples:
  python tone.py probe                       identity check only, no vibration
  python tone.py tone --target left --freq 80 --amp 50 --seconds 2
  python tone.py sweep --target grips --from 30 --to 300 --seconds 6
  python tone.py walk                        left grip, right grip, left trigger, right trigger
  python tone.py split                       blind left/right trigger separation check
  python tone.py trigger-mode                blind check whether the 0x53 trigger mode byte picks a trigger
  python tone.py trigger-target              the same for its target byte, after showing the on-board trigger setup
  python tone.py trigger-ram                 motor RAM after enabling each trigger target, no vibration
  python tone.py grip-target                 the same for the grip frame's target, then where a grip tone lands
  python tone.py grip-ram                    motor RAM after enabling each grip frame target, no vibration
  python tone.py trigger-side                blind check that one tone follows its side as it moves, from the pad
  python tone.py report-rate                 blind check whether reports sent faster than 8 ms raise the 1 kHz rate
  python tone.py report-model                blind X/Y check: are those reports played faster, or cut off at 1 kHz
  python tone.py calibrate                  match each frequency's strength by feel, for the synth's eq
  python tone.py pad-sound                  sines and taps to record with a microphone: what the pad sounds like
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from apex6 import (DEFAULT_TRIGGER_MODE, DEFAULT_TRIGGER_SIDE, DEFAULT_TRIGGER_TARGET, FRAME_PERIOD, MAX_SAMPLE,
                   RAM_MOTOR, SAMPLE_RATE, SUBFRAMES, Apex6, Block, Route)

TARGETS = {
    "left": (False, True, False, Route.MUTE),
    "right": (False, False, True, Route.MUTE),
    "grips": (False, True, True, Route.MUTE),
    "trigger-left": (True, False, False, Route.LEFT),
    "trigger-right": (True, False, False, Route.RIGHT),
    "triggers": (True, False, False, Route.BOTH),
    "all": (True, True, True, Route.BOTH),
}
FADE_SECONDS = 0.015  # avoids a click at start and end


def tone_renderer(target: str, freq_from: float, freq_to: float, amp_percent: float, seconds: float):
    use_trigger, use_left, use_right, route = TARGETS[target]
    total = int(seconds * SAMPLE_RATE)
    fade = max(1, int(FADE_SECONDS * SAMPLE_RATE))
    peak = MAX_SAMPLE * max(0.0, min(100.0, amp_percent)) / 100.0
    phase = 0.0

    def render(first: int) -> Block | None:
        nonlocal phase
        if first >= total:
            return None
        samples = []
        for n in range(first, first + SUBFRAMES):
            if n >= total:
                samples.append(0.0)
                continue
            progress = n / total
            # Exponential sweep sounds even across octaves; constant when from == to.
            freq = freq_from * (freq_to / freq_from) ** progress
            envelope = min(1.0, n / fade, (total - n) / fade)
            samples.append(math.sin(2 * math.pi * phase) * peak * envelope)
            phase = (phase + freq / SAMPLE_RATE) % 1.0
        silent = [0.0] * SUBFRAMES
        return Block(
            trigger=samples if use_trigger else silent,
            left_grip=samples if use_left else silent,
            right_grip=samples if use_right else silent,
            route=route if use_trigger else Route.MUTE,
        )

    return render


# (label, 0x53 trigger mode byte, route, forced trigger-valid bit)
DIAG_STEPS = [
    ("기본 설정 + 왼쪽 라우팅 (지금 증상 재현)", 0x02, Route.LEFT, None),
    ("기본 설정 + 뮤트 라우팅, 트리거 비트 켬", 0x02, Route.MUTE, True),
    ("기본 설정 + 왼쪽 라우팅, 트리거 비트 끔 (안 울려야 정상)", 0x02, Route.LEFT, False),
    ("트리거 설정 0 + 왼쪽 라우팅", 0x00, Route.LEFT, None),
    ("트리거 설정 1 + 왼쪽 라우팅", 0x01, Route.LEFT, None),
    ("트리거 설정 1 + 오른쪽 라우팅", 0x01, Route.RIGHT, None),
    ("트리거 설정 3 + 왼쪽 라우팅", 0x03, Route.LEFT, None),
    ("트리거 설정 3 + 오른쪽 라우팅", 0x03, Route.RIGHT, None),
]


def run_diag(pad: Apex6, freq: float, amp: float, seconds: float) -> None:
    print("양쪽 검지를 각 트리거에 가볍게 올려 두세요.")
    print("단계마다 '없음 / 왼쪽 / 오른쪽 / 양쪽' 중 어디서 울렸는지 기록해 주세요.\n")
    for number, (label, mode, route, trigger_bit) in enumerate(DIAG_STEPS, start=1):
        print(f"[{number}/{len(DIAG_STEPS)}] {label}  (0x53 mode={mode}, route={route.name})")
        input("  Enter를 누르면 울립니다...")
        pad.reconfigure(mode)
        tone = tone_renderer("trigger-left", freq, freq, amp, seconds)

        def render(first: int, tone=tone, route=route, trigger_bit=trigger_bit) -> Block | None:
            block = tone(first)
            if block is not None:
                block.route = route
                block.trigger_bit = trigger_bit
            return block

        pad.stream(render)
    print("\n끝. 단계 번호별 결과를 알려 주세요 (예: 1 양쪽, 2 없음, ...).")


# Left/right separation check. ApexSenseBridge plays different LT and RT
# effects by alternating route LEFT and RIGHT every block, and one of its
# testers felt each route on its own side (controller firmware 9.0.4.4),
# while diag above felt LEFT on both triggers. This check rates each route
# with one fingertip on one trigger at a time, at low amplitude and in a
# shuffled order, so body transmission and expectation bias show up as such.
# Every test tone follows a BOTH reference at the same amplitude and is rated
# against it, so there is no absolute scale to guess; BOTH itself is the
# control and should come out "비슷".
SPLIT_ROUTES = ("LEFT", "RIGHT", "BOTH", "ALT")
SPLIT_FINGERS = {"L": "왼쪽 트리거", "R": "오른쪽 트리거"}
RATINGS = {"0": "없음", "1": "약함", "2": "비슷", "3": "강함"}
REFERENCE_GAP = 500  # samples = ms of silence between reference and test


def split_renderer(route: str, freq: float, amp_percent: float, seconds: float):
    """Trigger tone on one route; ALT alternates LEFT (tone) and RIGHT (silent) per block."""
    tone = tone_renderer("trigger-left", freq, freq, amp_percent, seconds)

    def render(first: int) -> Block | None:
        block = tone(first)
        if block is None:
            return None
        if route == "ALT":
            if (first // SUBFRAMES) % 2:
                block.route, block.trigger = Route.RIGHT, [0.0] * SUBFRAMES
            else:
                block.route = Route.LEFT
        else:
            block.route = Route[route]
        return block

    return render


def split_verdict(ratings: dict[tuple[str, str], int]) -> str:
    """Judge one amplitude from {(finger, route): rating 0..3}.

    Separation shows as a route rated differently under the two fingers;
    the same rating under both means the route reaches both triggers alike.
    """
    same = (ratings[("L", "LEFT")], ratings[("R", "RIGHT")])
    cross = (ratings[("L", "RIGHT")], ratings[("R", "LEFT")])
    if min(same) == 0:
        return "판정 불가: 자기 쪽 라우트가 안 느껴짐"
    if max(cross) == 0:
        return "분리됨: 한쪽 라우트는 그쪽 트리거에서만 느껴짐"
    if all(s > c for s, c in zip(same, cross)):
        return "부분 분리: 반대쪽은 더 약함 (몸체로 전해진 진동일 수 있음)"
    if all(ratings[("L", r)] == ratings[("R", r)] for r in ("LEFT", "RIGHT")):
        return "섞임: 라우트마다 손가락 위치와 상관없이 같은 세기 (좌우 구분 안 됨)"
    return "애매: 손가락별 결과가 일관되지 않음, 다시 측정"


def _ask_rating(replay) -> int:
    while True:
        answer = input("  기준음과 비교해 시험음은? 0 없음 / 1 약함 / 2 비슷 / 3 강함 / r 다시: ").strip().lower()
        if answer in RATINGS:
            return int(answer)
        if answer == "r":
            replay()


def _firmware_text(info) -> str:
    names = {"controller": "컨트롤러", "dongle": "동글", "si": "SI", "trigger": "트리거",
             "screen": "Screen", "adc": "ADC", "rf": "RF"}
    return ", ".join(f"{names[k]} {v}" for k, v in info.firmware.items()) or "읽지 못함"


def _device_lines(info, firmware: str, dongle: str) -> list[str]:
    """The pad's identity for a saved report: firmware first, then the raw 0x01 reply."""
    return [
        f"컨트롤러 펌웨어: {firmware or '-'}",
        f"구성 요소 펌웨어: {_firmware_text(info)}",
        f"동글 펌웨어: {dongle or '-'}",
        f"기기: {info.name}, connection_mode={info.connection_mode}, features=0x{info.features:02X}",
        f"0x01 응답: {info.raw.hex(' ').upper()}",
    ]


def run_split(pad: Apex6, freq: float, amps: list[float], seconds: float,
              firmware: str | None, dongle: str | None) -> None:
    import random
    from datetime import datetime

    from version import __version__

    info = pad.info
    firmware = firmware or info.firmware.get("controller", "")
    print(f"펌웨어: {_firmware_text(info)}")
    dongle = dongle or info.firmware.get("dongle", "")
    print("\n컨트롤러를 책상에 내려놓고 그립은 잡지 마세요.")
    print("단계마다 안내하는 트리거 하나에만 손가락 끝을 가볍게 올려 두세요.")
    print("단계마다 기준음(양쪽 라우트) 다음에 0.5초 쉬고 시험음이 울립니다.")
    print("시험음이 기준음보다 어땠는지 답하세요. 라우트는 섞어서 나오고, 끝나면 표에서 공개됩니다.\n")

    steps = []
    for amp in amps:
        for finger in SPLIT_FINGERS:
            routes = list(SPLIT_ROUTES)
            random.shuffle(routes)
            steps += [(amp, finger, route) for route in routes]
    ratings: dict[tuple[float, str, str], int] = {}
    finger_now = None
    for number, (amp, finger, route) in enumerate(steps, start=1):
        if finger != finger_now:
            finger_now = finger
            print(f"--- 이제 {SPLIT_FINGERS[finger]}에만 손가락을 올려 두세요 (세기 {amp:g}%) ---")
        input(f"[{number}/{len(steps)}] Enter를 누르면 울립니다...")

        def play(amp=amp, route=route) -> None:
            pad.stream(split_renderer("BOTH", freq, amp, seconds))
            pad.stream(lambda first: Block() if first < REFERENCE_GAP else None)
            pad.stream(split_renderer(route, freq, amp, seconds))

        play()
        ratings[(amp, finger, route)] = _ask_rating(play)

    lines = [
        f"트리거 좌우 분리 진단 {datetime.now():%Y-%m-%d %H:%M:%S}, ATS 햅틱 {__version__}",
        *_device_lines(info, firmware, dongle),
        f"톤: {freq:g} Hz, {seconds:g}초, 0x53 mode=0x02",
        "평가: 같은 세기의 기준음(BOTH)과 비교한 시험음 (BOTH 칸은 대조군이라 '비슷'이 정상)",
        "",
        "세기   손가락         " + "  ".join(f"{r:>5}" for r in SPLIT_ROUTES),
    ]
    for amp in amps:
        for finger, name in SPLIT_FINGERS.items():
            cells = "  ".join(f"{RATINGS[str(ratings[(amp, finger, r)])]:>4}" for r in SPLIT_ROUTES)
            lines.append(f"{amp:>4g}%  {name:<10} {cells}")
        verdict = split_verdict({(f, r): ratings[(amp, f, r)] for f in SPLIT_FINGERS for r in SPLIT_ROUTES})
        lines.append(f"       -> {verdict}")
    lines.append("ALT: 블록마다 LEFT(톤)와 RIGHT(무음)를 번갈아 보냄 (ApexSenseBridge 방식)")
    _save_report("trigger-split", lines)


# 0x53 frame checks. The trigger frame is 01 tt mm 40 00: area 1, target tt,
# mode mm (Space Station 5.0's [area, target, mode, params] layout).
# - mode: the xHaptics Engine (5.0.2.6) configures the motors from separate
#   left and right trigger switches (motor53Config), and mm is captured as 0x02
#   and sent as 0x00 when disabling. It might be a route like 0x57's or a mask.
# - target: Space Station's own trigger haptics (area 0) use target 0 left,
#   1 right and 2 both, so 0x12 may be "triggers, both" and 0x10 and 0x11 one
#   trigger each.
# - grip-target: the grip frame 01 gg 03 00 40 writes the motor RAM records
#   where area 0 keeps those trigger records, so its gg (0x02 captured) may be
#   the trigger side instead, 0x00 left and 0x01 right. It may pick a grip as
#   well, which a grip pass after the triggers asks.
# Each value plays under one fingertip at a time, shuffled, after a reference
# at the default value, with the same 0x57 route throughout, so only that byte
# changes.
TRIGGER_MODES = (0x00, 0x01, 0x02, 0x03)
TRIGGER_TARGETS = (0x10, 0x11, 0x12)
GRIP_TARGETS = (0x00, 0x01, 0x02)
# check -> (Apex6.enable keyword, the captured value played as the reference,
# name in prompts and reports, the RAM_RECORDS part its frame writes, and the
# values that would write only the left and only the right record)
BYTE_FIELDS = {
    "trigger-mode": ("trigger_mode", DEFAULT_TRIGGER_MODE, "트리거 mode", 1, None),
    "trigger-target": ("trigger_target", DEFAULT_TRIGGER_TARGET, "트리거 target", 1, (0x10, 0x11)),
    "grip-target": ("trigger_side", DEFAULT_TRIGGER_SIDE, "그립 프레임 target", 0, (0x00, 0x01)),
}
# motor RAM check -> the byte it varies
RAM_CHECKS = {"trigger-ram": "trigger-target", "grip-ram": "grip-target"}


def mode_verdict(left: int, right: int) -> str:
    """Where one byte played, from its ratings (0..3) under the left and the right fingertip."""
    if not left and not right:
        return "안 울림"
    if not right:
        return "왼쪽만"
    if not left:
        return "오른쪽만"
    if left != right:
        return "왼쪽이 더 셈" if left > right else "오른쪽이 더 셈"
    return "양쪽 같음"


def modes_verdict(ratings: dict[tuple[str, int], int], control: int = DEFAULT_TRIGGER_MODE) -> str:
    """Judge one amplitude from {(finger, byte): rating 0..3}; control is the reference's own value."""
    modes = sorted({mode for _, mode in ratings})
    if control in modes and (ratings[("L", control)], ratings[("R", control)]) != (2, 2):
        return f"다시 측정: 대조군 0x{control:02X}가 양쪽 다 '비슷'이 아님"
    sides = [mode_verdict(ratings[("L", m)], ratings[("R", m)]) for m in modes]
    left = [s for s in sides if s.startswith("왼쪽")]
    right = [s for s in sides if s.startswith("오른쪽")]
    if left and right:
        if "왼쪽만" in left and "오른쪽만" in right:
            return "분리됨: 이 바이트로 트리거를 고를 수 있음"
        return "부분 분리: 바이트에 따라 한쪽이 더 셈 (반대쪽은 몸체로 전해진 진동일 수 있음)"
    if left or right:
        return "한쪽으로만 치우침: 다른 쪽을 고르는 값은 이 중에 없음"
    return "분리 안 됨: 이 바이트는 좌우를 고르지 않음"


def run_byte_check(pad: Apex6, field: str, values: list[int], route: str, freq: float,
                   amps: list[float], seconds: float) -> list[str]:
    """Blind check of one 0x53 byte (a BYTE_FIELDS key) on the triggers; returns the report lines."""
    import random
    from datetime import datetime

    from version import __version__

    keyword, control, name, _, _ = BYTE_FIELDS[field]

    def configure(value: int) -> None:
        pad.reconfigure(**{keyword: value})

    info = pad.info
    print(f"펌웨어: {_firmware_text(info)}")
    print("\n컨트롤러를 책상에 내려놓고 그립은 잡지 마세요.")
    print("단계마다 안내하는 트리거 하나에만 손가락 끝을 가볍게 올려 두세요.")
    print(f"단계마다 기준음(0x53 {name} 바이트 0x{control:02X}) 다음에 잠깐 쉬고 시험음이 울립니다.")
    print("시험음이 기준음보다 어땠는지 답하세요. 바이트는 섞어서 나오고, 끝나면 표에서 공개됩니다.\n")

    steps = []
    for amp in amps:
        for finger in SPLIT_FINGERS:
            order = list(values)
            random.shuffle(order)
            steps += [(amp, finger, value) for value in order]
    ratings: dict[tuple[float, str, int], int] = {}
    finger_now = None
    for number, (amp, finger, value) in enumerate(steps, start=1):
        if finger != finger_now:
            finger_now = finger
            print(f"--- 이제 {SPLIT_FINGERS[finger]}에만 손가락을 올려 두세요 (세기 {amp:g}%) ---")
        input(f"[{number}/{len(steps)}] Enter를 누르면 울립니다...")

        def play(amp=amp, value=value) -> None:
            configure(control)
            pad.stream(split_renderer(route, freq, amp, seconds))
            pad.stream(lambda first: Block() if first < REFERENCE_GAP else None)
            configure(value)
            pad.stream(split_renderer(route, freq, amp, seconds))

        play()
        ratings[(amp, finger, value)] = _ask_rating(play)
    configure(control)

    lines = [
        f"0x53 {name} 바이트 진단 {datetime.now():%Y-%m-%d %H:%M:%S}, ATS 햅틱 {__version__}",
        *_device_lines(info, info.firmware.get("controller", ""), info.firmware.get("dongle", "")),
        f"톤: {freq:g} Hz, {seconds:g}초, 0x57 route={route}, 기준 {name} 0x{control:02X}",
        f"평가: 기본값으로 울린 기준음과 비교한 시험음 (0x{control:02X} 칸은 대조군이라 '비슷'이 정상)",
        "",
        "세기   손가락         " + "  ".join(f" 0x{v:02X}" for v in values),
    ]
    for amp in amps:
        for finger, name in SPLIT_FINGERS.items():
            cells = "  ".join(f"{RATINGS[str(ratings[(amp, finger, v)])]:>4}" for v in values)
            lines.append(f"{amp:>4g}%  {name:<10} {cells}")
        table = {(f, v): ratings[(amp, f, v)] for f in SPLIT_FINGERS for v in values}
        lines.append("       " + ", ".join(f"0x{v:02X} {mode_verdict(table[('L', v)], table[('R', v)])}"
                                         for v in values))
        lines.append(f"       -> {modes_verdict(table, control)}")
    return lines


# Space Station 5.0 reads motor RAM as [1] left trigger mode, [2:12] its
# params, [12] right trigger mode, [13:23] its params; a scene's params are
# id, travel start and end, loop, frequency interval, gain, 64, 64, wave, 255.
TRIGGER_RAM_MODES = {0: "끔", 1: "내장 트리거 햅틱 장면", 2: "그립 진동을 따라 울림"}


def motor_ram_lines(data: bytes) -> list[str]:
    """The on-board trigger settings in a motor RAM block, then the block itself."""
    lines = []
    if len(data) >= 23:
        for name, at in (("왼쪽", 1), ("오른쪽", 12)):
            mode, params = data[at], data[at + 1:at + 11]
            text = TRIGGER_RAM_MODES.get(mode, f"알 수 없음 (mode {mode})")
            if mode == 1:
                text += (f": 장면 {params[0]}, 파형 {params[8]}, 행정 {params[1]}~{params[2]}, "
                         f"{'반복' if params[3] else '한 번'}, 간격 {params[4]}, 게인 {params[5]}")
            lines.append(f"  온보드 {name} 트리거: {text}  [{params.hex(' ').upper()}]")
    else:
        lines.append(f"  온보드 트리거 설정: {len(data)}바이트만 와서 해석 못 함")
    lines += [f"  {i:02X}: {data[i:i + 16].hex(' ').upper()}" for i in range(0, len(data), 16)]
    return lines


def _motor_ram(pad: Apex6) -> tuple[bytes | None, list[str]]:
    try:
        slot, data = pad.read_ram(RAM_MOTOR)
    except RuntimeError as exc:
        return None, [f"모터 RAM 읽기 실패: {exc}"]
    return data, [f"모터 RAM (0xA3, ramId 0x{RAM_MOTOR:02X}, 슬롯 {slot}, {len(data)}바이트)", *motor_ram_lines(data)]


# Motor RAM as two 32-byte areas (0x00 area 0, 0x20 area 1), each a header
# byte, then the left and right records of part 0 (target 0x0_: mode and 10
# params) and of part 1 (target 0x1_: mode and one param). Read off the first
# trigger-target run (2026-10-27): our grip frame (target 0x02) filled area 1's
# two part-0 records and our trigger frame (0x12) its two part-1 records.
RAM_AREA_SIZE = 0x20
RAM_RECORDS = ((0, "왼쪽", 1, 11), (0, "오른쪽", 12, 11), (1, "왼쪽", 23, 2), (1, "오른쪽", 25, 2))


def ram_record_name(offset: int) -> str:
    """Which record a motor RAM byte belongs to, as "area 1 0x1_ 왼쪽 mode"."""
    area, at = divmod(offset, RAM_AREA_SIZE)
    if at == 0:
        return f"area {area} 머리"
    for part, side, start, length in RAM_RECORDS:
        if start <= at < start + length:
            return f"area {area} 0x{part}_ {side} {'mode' if at == start else 'param'}"
    return f"area {area} 나머지"


def ram_state(data: bytes, area: int = 1) -> str:
    """The modes of one area's four records, e.g. "0x0_ 왼쪽 3, 0x0_ 오른쪽 3, 0x1_ 왼쪽 2, 0x1_ 오른쪽 끔"."""
    base = area * RAM_AREA_SIZE
    cells = []
    for part, side, start, _ in RAM_RECORDS:
        mode = data[base + start] if base + start < len(data) else None
        cells.append(f"0x{part}_ {side} {'끔' if not mode else mode}")
    return ", ".join(cells)


def ram_changes(before: bytes, after: bytes) -> list[str]:
    return [f"    0x{i:02X} {ram_record_name(i)}: {a:02X} -> {b:02X}"
            for i, (a, b) in enumerate(zip(before, after)) if a != b]


def ram_target_verdict(written: dict[int, tuple[bool, bool]], field: str = "trigger-target") -> str:
    """Judge from {target: (left, right) record of the field's part on in area 1 while enabled}."""
    left_value, right_value = BYTE_FIELDS[field][4]
    left_name, right_name = f"0x{left_value:02X}", f"0x{right_value:02X}"
    if left_value not in written or right_value not in written:
        return f"{left_name}과 {right_name}을 둘 다 봐야 판정할 수 있음"
    left, right = written[left_value], written[right_value]
    if left == (True, False) and right == (False, True):
        return (f"{left_name}은 왼쪽, {right_name}은 오른쪽 기록만 씀. {field}에서 양쪽이 같게 느껴졌다면 "
                "실시간 출력이 이 기록과 상관없이 양쪽으로 나감 (펌웨어 쪽 한계로 보임)")
    if left == right == (True, True):
        return f"{left_name}·{right_name}도 양쪽 기록을 다 씀: 펌웨어가 이 target을 '양쪽'으로 받음"
    if left == right == (False, False):
        return f"{left_name}·{right_name}은 이 프레임의 기록을 쓰지 않음: 펌웨어가 받지 않는 값"
    return "예상과 다른 조합: 위 표를 보고 판단"


def run_ram_check(pad: Apex6, check: str, targets: list[int]) -> None:
    """Enable the motors with each target of a RAM_CHECKS check and read motor RAM, on and off again; no vibration."""
    from datetime import datetime

    from version import __version__

    field = RAM_CHECKS[check]
    keyword, _, name, part, _ = BYTE_FIELDS[field]
    info = pad.info
    lines = [
        f"0x53 {name}별 모터 RAM {datetime.now():%Y-%m-%d %H:%M:%S}, ATS 햅틱 {__version__}",
        *_device_lines(info, info.firmware.get("controller", ""), info.firmware.get("dongle", "")),
        "진동 없이 모터를 켜고 끄기만 하며, 바뀐 바이트는 시작할 때와 비교",
        "",
    ]
    base, base_lines = _motor_ram(pad)
    if base is None:
        print("\n".join(base_lines))
        return
    lines += ["시작 " + base_lines[0], *base_lines[1:], f"  area 1: {ram_state(base)}", ""]
    written: dict[int, tuple[bool, bool]] = {}
    for target in targets:
        print(f"target 0x{target:02X} 켜고 끄는 중...")
        pad.enable(**{keyword: target})
        on, on_lines = _motor_ram(pad)
        pad.disable()
        off, off_lines = _motor_ram(pad)
        lines.append(f"target 0x{target:02X} 켬: " + (f"area 1: {ram_state(on)}" if on is not None else on_lines[0]))
        if on is not None:
            lines += ram_changes(base, on) or ["    바뀐 바이트 없음"]
            if len(on) >= 2 * RAM_AREA_SIZE:
                written[target] = tuple(bool(on[RAM_AREA_SIZE + start])
                                        for record_part, _, start, _ in RAM_RECORDS if record_part == part)
        lines.append("  끈 뒤: " + (f"area 1: {ram_state(off)}" if off is not None else off_lines[0]))
        if off is not None:
            lines += ram_changes(base, off) or ["    시작할 때와 같음"]
    lines += ["", f"-> {ram_target_verdict(written, field)}"]
    _save_report(check, lines)


# The grip frame may set up a grip rather than a trigger, so grip-target then
# asks where a tone on both grips lands with each target, the controller held
# as usual and no finger on the triggers.
GRIP_SIDES = {0: "안 울림", 1: "왼손만", 2: "오른손만", 3: "양손"}
GRIP_SIDE_FREQ = 80.0  # near the grip resonance, as tone and walk default to


def grip_sides_verdict(heard: dict[int, int]) -> str:
    """Judge the grip pass from {grip frame target: GRIP_SIDES key}."""
    _, control, _, _, (left, right) = BYTE_FIELDS["grip-target"]
    if control in heard and heard[control] != 3:
        return f"다시 측정: 기준값 0x{control:02X}에서 양손에 울리지 않음"
    if left not in heard or right not in heard:
        return f"0x{left:02X}과 0x{right:02X}을 둘 다 봐야 판정할 수 있음"
    if (heard[left], heard[right]) == (1, 2):
        return f"그립 좌우를 고름: 이 프레임은 그립 설정 (0x{left:02X} 왼쪽, 0x{right:02X} 오른쪽)"
    if heard[left] == heard[right] == 3:
        return "그립과 상관없음: 이 target으로 그립은 바뀌지 않음"
    return "예상과 다른 조합: 위 결과를 보고 판단"


def _ask_side(replay) -> int:
    while True:
        answer = input("  어느 손에서 울렸나요? 0 안 울림 / 1 왼손만 / 2 오른손만 / 3 양손 / r 다시: ").strip().lower()
        if answer.isdigit() and int(answer) in GRIP_SIDES:
            return int(answer)
        if answer == "r":
            replay()


def run_grip_sides(pad: Apex6, values: list[int], amp: float, seconds: float) -> list[str]:
    """Where a tone on both grips lands with each grip frame target; returns the report lines."""
    import random

    print("\n--- 이제 컨트롤러를 평소처럼 양손으로 잡고, 트리거에서는 손가락을 떼세요 ---")
    print("단계마다 양쪽 그립에 같은 톤이 울립니다. 어느 손에서 느껴졌는지 답하세요.\n")
    order = list(values)
    random.shuffle(order)
    heard: dict[int, int] = {}
    for number, value in enumerate(order, start=1):
        input(f"[{number}/{len(order)}] Enter를 누르면 울립니다...")

        def play(value=value) -> None:
            pad.reconfigure(trigger_side=value)
            pad.stream(tone_renderer("grips", GRIP_SIDE_FREQ, GRIP_SIDE_FREQ, amp, seconds))

        play()
        heard[value] = _ask_side(play)
    pad.reconfigure()
    return [f"그립 톤: {GRIP_SIDE_FREQ:g} Hz, {amp:g}%, 양쪽 그립에 같은 톤",
            "       " + ", ".join(f"0x{v:02X} {GRIP_SIDES[heard[v]]}" for v in values),
            f"       -> {grip_sides_verdict(heard)}"]


def run_target_check(pad: Apex6, field: str, targets: list[int], route: str, freq: float, amps: list[float],
                     seconds: float) -> None:
    before, before_lines = _motor_ram(pad)
    print("\n".join(before_lines))
    if before and len(before) >= 23 and (before[1] or before[12]):
        print("  온보드 트리거 설정이 켜져 있어요. 손가락만 얹어 두면 대개 울리지 않지만, "
              "결과가 이상하면 Space Station에서 끄고 다시 해 주세요.")
    lines = run_byte_check(pad, field, targets, route, freq, amps, seconds)
    if field == "grip-target":
        lines += ["", *run_grip_sides(pad, targets, max(amps), seconds)]
    after, after_lines = _motor_ram(pad)
    lines += ["", "시작 전 " + before_lines[0], *before_lines[1:]]
    if before is not None and before == after:
        lines.append("끝난 뒤 모터 RAM: 시작 전과 같음")
    else:
        lines += ["끝난 뒤 " + after_lines[0], *after_lines[1:]]
        if before is not None and after is not None:
            changed = [f"0x{i:02X} {a:02X}->{b:02X}" for i, (a, b) in enumerate(zip(before, after)) if a != b]
            if changed:
                lines.append("  바뀐 바이트: " + ", ".join(changed))
    _save_report(field, lines)


# Trigger side check. grip-target found the trigger's side, each time after a
# fresh enable and its 94 ms of silence; the app moves it while it streams
# (Block.side), the 0x53 frames going out just before the first block on the
# new side. Here one tone plays on while it moves every SIDE_SEGMENT_S to a
# side picked at random, never the same twice in a row; the player names the
# sides in the order felt, as they come or after, then says whether the moves
# clicked or dropped out.
# All of it from the pad: with both index fingers on the triggers the thumbs
# are free, while a hand going to the keyboard leaves the side it judges
# (felt on the first run). The D-pad names the sides, ← left, → right, ↑ both,
# ↓ takes the last back; A starts and says "no click", Y "a click", X plays it
# again from the start, B stops. The keyboard does the same with arrows or
# l r b, Backspace, Enter, 0 / 1, x and q.
SIDE_NAMES = {"L": Route.LEFT, "R": Route.RIGHT, "B": Route.BOTH}
SIDE_LABELS = {"L": "왼쪽", "R": "오른쪽", "B": "양쪽"}
SIDE_SEGMENT_S = 0.8
SIDE_SEGMENTS = 5
CLICKS = {0: "없음", 1: "있음"}
UNDO, START, CLICK, REPLAY, STOP = "undo", "start", "click", "replay", "stop"
SIDE_PAD = ((0x0004, "L"), (0x0008, "R"), (0x0001, "B"), (0x0002, UNDO),
            (0x1000, START), (0x8000, CLICK), (0x4000, REPLAY), (0x2000, STOP))  # XInput wButtons bits
SIDE_KEYS = {"l": "L", "r": "R", "b": "B", "\x08": UNDO, "\r": START, "0": START, "1": CLICK, "x": REPLAY,
             "q": STOP, "\x1b": STOP}
SIDE_ARROWS = {"K": "L", "M": "R", "H": "B", "P": UNDO}


def side_plan(segments: int, rng) -> list[str]:
    """Sides for each segment (SIDE_NAMES keys), each a move from the one before."""
    plan: list[str] = []
    for _ in range(segments):
        plan.append(rng.choice([side for side in SIDE_NAMES if not plan or side != plan[-1]]))
    return plan


def side_renderer(plan: list[str], freq: float, amp_percent: float, segment_s: float = SIDE_SEGMENT_S):
    """One unbroken trigger tone, each segment on its side of the plan."""
    tone = tone_renderer("triggers", freq, freq, amp_percent, segment_s * len(plan))
    per = segment_s * SAMPLE_RATE

    def render(first: int) -> Block | None:
        block = tone(first)
        if block is not None:
            block.side = SIDE_NAMES[plan[min(len(plan) - 1, int(first / per))]]
        return block

    return render


def side_verdict(trials: list[tuple[list[str], str]], clicks: list[int]) -> str:
    """Judge from (plan, sides named) per trial and the click ratings."""
    wrong = sum(sent != felt for plan, named in trials for sent, felt in zip(plan, named))
    total = sum(len(plan) for plan, _ in trials)
    if wrong == 0:
        follows = "중간에 바꿔도 그대로 따라감"
    elif all(named[1:] == "".join(plan[1:]) for plan, named in trials):
        follows = "첫 구간만 틀림: 시작할 때 늦게 바뀜"
    else:
        follows = f"{total}구간 중 {wrong}구간이 다르게 느껴짐: 중간에 바꾸면 늦거나 안 바뀌는 듯"
    clicked = ("바뀔 때 딸깍·끊김 있음: 조용할 때만 바꾸게 해야 할 수 있음" if any(clicks)
               else "바뀔 때 딸깍·끊김 없음")
    return f"{follows}. {clicked}"


def name_side(named: list[str], event: str | None, count: int) -> None:
    """Take one press into the sides named so far: a side adds itself until there are count, UNDO drops the last."""
    if event in SIDE_NAMES and len(named) < count:
        named.append(event)
    elif event == UNDO and named:
        named.pop()


def _side_event(buttons) -> str | None:
    """The next press on the pad or a key in the console, as a SIDE_PAD name; never blocks."""
    pressed = buttons.pressed()
    return next((name for mask, name in SIDE_PAD if pressed & mask), None) or _console_event(SIDE_KEYS, SIDE_ARROWS)


def _wait_event(buttons, *wanted: str) -> str:
    import time

    while (event := _side_event(buttons)) not in wanted:
        time.sleep(0.01)
    return event


def _hear_sides(pad: Apex6, buttons, plan: list[str], freq: float, amp: float) -> tuple[str, int] | None:
    """Play the plan and take the sides as they are named, while it plays or after.

    Returns the sides and how often it was played again, or None when stopped.
    """
    replays = 0
    while True:
        named: list[str] = []
        heard: list[str] = []  # REPLAY or STOP, pressed while it played
        tone = side_renderer(plan, freq, amp)

        def render(first: int, tone=tone, named=named, heard=heard) -> Block | None:
            if not heard:
                event = _side_event(buttons)
                if event in (REPLAY, STOP):
                    heard.append(event)
                name_side(named, event, len(plan))
            return None if heard else tone(first)

        pad.stream(render)
        while not heard and len(named) < len(plan):
            event = _wait_event(buttons, *SIDE_NAMES, UNDO, REPLAY, STOP)
            if event in (REPLAY, STOP):
                heard.append(event)
            name_side(named, event, len(plan))
        if not heard:
            return "".join(named), replays
        if heard[0] == STOP:
            return None
        replays += 1
        print("  처음부터 다시")


def run_side_check(pad: Apex6, freq: float, amp: float, trials: int) -> None:
    import random
    from datetime import datetime

    import calibrate
    from version import __version__

    info = pad.info
    buttons = calibrate.PadButtons()
    print(f"펌웨어: {_firmware_text(info)}")
    print("\n패드를 양손으로 평소처럼 잡고 검지는 양쪽 트리거에 가볍게 올려 두세요. 진행은 모두 패드로 합니다.")
    print(f"톤 하나가 끊기지 않고 {SIDE_SEGMENT_S:g}초마다 왼쪽·오른쪽·양쪽 중 한 곳으로 옮겨 가며 "
          f"{SIDE_SEGMENTS}구간 울립니다.")
    print("  느낀 곳을 차례로, 울리는 동안 바로 눌러도 됩니다: 십자키 ← 왼쪽 / → 오른쪽 / ↑ 양쪽 / ↓ 하나 지우기")
    print("  A 시작 / X 처음부터 다시 / B 그만. 다 누르면 바뀔 때 딸깍·끊김이 A 없음 / Y 있음")
    print("  키보드: Enter 시작, ← → ↑ 또는 L R B, Backspace, x 다시, 0 없음 / 1 있음, q 그만")
    if not buttons.connected:
        print("  (XInput 패드가 안 보여 키보드로만 받습니다)")
    rng = random.Random()
    results: list[tuple[list[str], str]] = []
    clicks: list[int] = []
    replays: list[int] = []
    for number in range(1, trials + 1):
        plan = side_plan(SIDE_SEGMENTS, rng)
        print(f"\n[{number}/{trials}] A를 누르면 울립니다")
        if _wait_event(buttons, START, STOP) == STOP:
            break
        heard = _hear_sides(pad, buttons, plan, freq, amp)
        if heard is None:
            break
        named, again = heard
        print("  " + " → ".join(SIDE_LABELS[side] for side in named))
        print("  바뀔 때 딸깍·끊김이 있었나요? A 없음 / Y 있음 / X 다시 듣기")
        while (event := _wait_event(buttons, START, CLICK, REPLAY, STOP)) == REPLAY:
            pad.stream(side_renderer(plan, freq, amp))
            again += 1
        if event == STOP:
            break
        results.append((plan, named))
        clicks.append(int(event == CLICK))
        replays.append(again)
    if not results:
        print("그만둡니다.")
        return

    lines = [
        f"트리거 좌우 전환 진단 {datetime.now():%Y-%m-%d %H:%M:%S}, ATS 햅틱 {__version__}",
        *_device_lines(info, info.firmware.get("controller", ""), info.firmware.get("dongle", "")),
        f"톤: {freq:g} Hz, {amp:g}%, {SIDE_SEGMENT_S:g}초씩 {SIDE_SEGMENTS}구간, 스트리밍 중에 0x53으로 쪽을 바꿈",
        f"답: {'패드 버튼' if buttons.connected else '키보드'}",
        "",
    ]
    for number, ((plan, named), click, again) in enumerate(zip(results, clicks, replays), start=1):
        marks = " ".join(f"{SIDE_LABELS[sent]}{'' if sent == felt else f'(→{SIDE_LABELS[felt]})'}"
                         for sent, felt in zip(plan, named))
        lines.append(f"{number}회: {marks}  / 딸깍·끊김 {CLICKS[click]} / 다시 듣기 {again}번")
    lines += ["", f"-> {side_verdict(results, clicks)}"]
    _save_report("trigger-side", lines)


# Report-rate check. Every 0x57 report carries eight samples and has no rate
# field; Flydigi's engine sends one every 8 ms (its pacing counters say
# queueDepth1k), so the pad plays 1 kHz. Wired, the link takes a report every
# 2 ms at most (writes block for 2 ms when sent faster, firmware 9.0.6.8, with
# no reply to 0x57 either way), which would allow 4 kHz. A first run with a
# pulsed tone synthesized for each period's rate came out rough at 4 and 2 ms
# with the pulse in time, as if the pad kept one report per 8 ms and dropped
# the rest, but pitch and roughness are hard to judge. So each tap here fills
# one report and the reports between taps, an odd number, are silent: a pad
# that plays one report in k plays one tap in k, each as the 8 ms control
# does, and a pad that plays every report plays every tap, only shorter.
BASE_PERIOD_MS = FRAME_PERIOD * 1000.0
RATE_PERIODS_MS = (8.0, 4.0, 2.0)
TAP_SPACING_S = 0.5
RATE_TAPS = 8


def tap_spacing(period_ms: float) -> int:
    """Reports from one tap to the next: about TAP_SPACING_S, and odd so the taps alternate parity."""
    reports = round(TAP_SPACING_S * 1000.0 / period_ms)
    return reports if reports % 2 else reports + 1


def tap_renderer(period_ms: float, taps: int, amp_percent: float):
    """Both grips: taps, each a click filling one report (half up, half down), silence in between."""
    spacing = tap_spacing(period_ms)
    lead = spacing // 2  # silence before the first tap and after the last
    total = 2 * lead + (taps - 1) * spacing + 1
    peak = MAX_SAMPLE * max(0.0, min(100.0, amp_percent)) / 100.0
    click = [peak] * (SUBFRAMES // 2) + [-peak] * (SUBFRAMES // 2)
    silent = [0.0] * SUBFRAMES

    def render(first: int) -> Block | None:
        index = first // SUBFRAMES
        if index >= total:
            return None
        tap, offset = divmod(index - lead, spacing)
        samples = click if index >= lead and offset == 0 and tap < taps else silent
        return Block(left_grip=samples, right_grip=samples)

    return render


def rate_verdict(period_ms: float, sent: int, heard: int) -> str:
    """What one period's tap count says about the pad's playback rate."""
    if period_ms >= BASE_PERIOD_MS:
        return "대조군 정상" if abs(heard - sent) <= 1 else "대조군 어긋남"
    k = BASE_PERIOD_MS / period_ms
    if heard == 0:
        return "판정 불가: 탭이 하나도 안 느껴짐"
    if heard > sent + 1:
        return "예상 밖: 보낸 것보다 많이 들림"
    if heard >= sent - 1:
        return f"다 들림: 리포트를 다 재생 ({SUBFRAMES / period_ms:g} kHz)"
    if abs(heard - sent / k) <= 1:
        return f"1 kHz 고정: 리포트 {k:.3g}개 중 1개만 재생"
    return "1 kHz 고정으로 보임: 리포트를 일부만 재생"


def rate_summary(counts: dict[float, tuple[int, int]]) -> str:
    """Judge the whole check from {period ms: (taps sent, taps heard)}."""
    verdicts = {period: rate_verdict(period, *count) for period, count in counts.items()}
    if "대조군 어긋남" in verdicts.values():
        return "다시 측정: 대조군(8 ms)에서 보낸 만큼 들리지 않음"
    faster = [verdict for period, verdict in verdicts.items() if period < BASE_PERIOD_MS]
    if not faster:
        return "8 ms보다 짧은 간격이 없어 판정할 수 없음"
    if any(v.startswith("판정 불가") for v in faster):
        return "다시 측정: 탭이 안 느껴진 간격이 있음 (--amp를 올려서)"
    if all(v.startswith("다 들림") for v in faster):
        return "패드가 빠른 리포트를 다 재생함: 1 kHz보다 높은 샘플레이트를 쓸 수 있음"
    if all(v.startswith("1 kHz 고정") for v in faster):
        return "패드는 1 kHz로 재생함: 리포트를 빨리 보내면 넘친 만큼 버려짐"
    return "섞인 결과: 간격별 판정을 보고 판단"


def _ask_count(replay) -> int:
    while True:
        answer = input("  탭이 몇 번 울렸나요? 숫자 / r 다시: ").strip().lower()
        if answer.isdigit():
            return int(answer)
        if answer == "r":
            replay()


def run_rate_check(pad: Apex6, periods_ms: list[float], taps: int, amp: float) -> None:
    import random
    import time
    from datetime import datetime

    from version import __version__

    info = pad.info
    print(f"펌웨어: {_firmware_text(info)}")
    print("\n컨트롤러를 평소처럼 잡으세요. 단계마다 짧은 탭이 몇 번 울립니다.")
    print("손에 느껴지거나 들린 탭을 세어 주세요. 단계마다 횟수가 다를 수 있어요. "
          "간격은 섞어서 나오고, 끝나면 표에서 공개됩니다.\n")
    order = list(periods_ms)
    random.shuffle(order)
    counts: dict[float, tuple[int, int]] = {}
    measured = {}
    for number, period_ms in enumerate(order, start=1):
        input(f"[{number}/{len(order)}] Enter를 누르면 울립니다...")

        def play(period_ms=period_ms) -> None:
            start = time.perf_counter()
            stats = pad.stream(tap_renderer(period_ms, taps, amp), period=period_ms / 1000.0)
            measured[period_ms] = ((time.perf_counter() - start) * 1000.0 / max(1, stats.frames), stats)

        play()
        counts[period_ms] = (taps, _ask_count(play))

    lines = [
        f"리포트 간격 진단 (탭 세기) {datetime.now():%Y-%m-%d %H:%M:%S}, ATS 햅틱 {__version__}",
        *_device_lines(info, info.firmware.get("controller", ""), info.firmware.get("dongle", "")),
        f"탭: 양쪽 그립 {amp:g}%, {taps}번, 약 {TAP_SPACING_S:g}초 간격. 탭마다 리포트 하나(8샘플)를 채우고 "
        "나머지 리포트는 무음",
        "탭 사이 리포트 수가 홀수라, 패드가 리포트를 k개 중 1개만 재생하면 탭도 k개 중 1개만 들림",
        "평가: 들린 탭 수 (8 ms 칸은 대조군이라 보낸 수와 같아야 정상)",
        "",
        "간격    합성   탭 사이   실제 간격   쓰기 평균/최대    늦음  들림/보냄  판정",
    ]
    for period_ms in sorted(counts, reverse=True):
        interval, stats = measured[period_ms]
        sent, heard = counts[period_ms]
        write_avg = stats.total_write_ms / max(1, stats.frames)
        lines.append(f"{period_ms:>4g} ms  {SUBFRAMES / period_ms:>3g} kHz  {tap_spacing(period_ms):>4}개   "
                     f"{interval:>5.2f} ms   {write_avg:>4.2f}/{stats.max_write_ms:>4.2f} ms  "
                     f"{stats.late_frames:>4}  {heard:>4}/{sent:<4}  {rate_verdict(period_ms, sent, heard)}")
    lines += ["", f"-> {rate_summary(counts)}"]
    _save_report("report-rate", lines)


# Report-model check. The tap count above found every report played (8/8 at
# 4 and 2 ms, wired and through the dongle, firmware 9.0.6.8), yet the first
# run's tone, synthesized at 8/P kHz for a period of P ms, came out rough. Two
# models fit both: the pad plays each report's eight samples at 8/P kHz
# ("all"), or it plays at 1 kHz from the moment a report arrives until the
# next one cuts it off, so only the first P samples sound ("preempt"). Each
# trial plays the 8 ms reference, then the same pulsed tone at a shorter
# period in two shapes, in random order: X puts the 1 kHz signal in each
# report's first P samples and silence after, smooth only under preempt; Y
# puts P ms of signal in all eight samples, smooth only under all. Whichever
# matches the reference names the model.
MODEL_PERIODS_MS = (4.0, 2.0)
MODEL_FREQ = 160.0
MODEL_AMP = 40.0
MODEL_SECONDS = 2.0
MODEL_PULSE_HZ = 4.0  # gaps a second, so a pad that buffers would drift
MODEL_TRIALS = 2  # per period, each in its own random order
MODEL_SHAPES = {"X": "선점", "Y": "다 재생"}


def pulsed_tone(t: float, freq: float, peak: float, seconds: float) -> float:
    """The test tone at t seconds: a sine on for 70% of each pulse, with soft edges."""
    if not 0.0 <= t < seconds:
        return 0.0
    on = 0.7 / MODEL_PULSE_HZ
    at = t % (1.0 / MODEL_PULSE_HZ)
    envelope = max(0.0, min(1.0, at / FADE_SECONDS, (on - at) / FADE_SECONDS))
    return math.sin(2 * math.pi * freq * t) * peak * envelope


def model_renderer(shape: str, period_ms: float, freq: float, amp_percent: float, seconds: float):
    """Both grips: the pulsed tone shaped for one playback model, one report every period_ms.

    X: each report's first P samples carry its P ms at 1 kHz, the rest silent.
    Y: its eight samples carry those P ms at 8/P kHz.
    At 8 ms both are the plain 1 kHz tone, the reference.
    """
    peak = MAX_SAMPLE * max(0.0, min(100.0, amp_percent)) / 100.0
    reports = math.ceil(seconds * 1000.0 / period_ms)
    step_ms = period_ms / SUBFRAMES if shape == "Y" else 1.0

    def render(first: int) -> Block | None:
        index = first // SUBFRAMES
        if index >= reports:
            return None
        start_ms = index * period_ms
        samples = [0.0 if shape == "X" and n >= period_ms
                   else pulsed_tone((start_ms + n * step_ms) / 1000.0, freq, peak, seconds)
                   for n in range(SUBFRAMES)]
        return Block(left_grip=samples, right_grip=samples)

    return render


def model_verdict(period_ms: float, picks: list[str]) -> str:
    """Judge one period from the shapes picked as the reference's match ("X", "Y", or "" for neither)."""
    x, y = picks.count("X"), picks.count("Y")
    if x and y:
        return "애매: X와 Y를 번갈아 고름, 다시 측정"
    if 2 * (x + y) <= len(picks):  # one pick against as many shrugs names nothing
        return "판정 불가: 대부분 구분이 안 됨 (둘 다 기준음과 다르면 어느 모델도 아님)"
    weak = "" if x + y == len(picks) else " (구분 안 된 답 있음)"
    if x:
        return f"선점: 1 kHz로 재생, 새 리포트가 오면 끊음{weak}"
    return f"다 재생: {SUBFRAMES / period_ms:g} kHz{weak}"


def model_summary(verdicts: dict[float, str]) -> str:
    """Judge the whole check from {period ms: model_verdict}."""
    found = {verdict.split(":")[0] for verdict in verdicts.values()}
    if found == {"선점"}:
        return ("패드는 1 kHz로 재생함: 리포트를 빨리 보내도 샘플레이트는 그대로이고, "
                "도착하는 대로 재생하니 보내는 타이밍 흔들림이 출력에 실림")
    if found == {"다 재생"}:
        return "패드가 리포트 간격만큼 빠르게 재생함: 2 ms 간격이면 4 kHz를 쓸 수 있음"
    return "섞인 결과: 간격별 판정을 보고 판단"


def _ask_pick(replay) -> str:
    while True:
        answer = input("  기준음과 더 같은 쪽은? 1 첫째 / 2 둘째 / 0 구분 안 됨 / r 다시: ").strip().lower()
        if answer in ("0", "1", "2"):
            return answer
        if answer == "r":
            replay()


def run_model_check(pad: Apex6, periods_ms: list[float], trials: int, freq: float, amp: float,
                    seconds: float) -> None:
    import random
    from datetime import datetime

    from version import __version__

    info = pad.info
    print(f"펌웨어: {_firmware_text(info)}")
    print("\n컨트롤러를 평소처럼 잡으세요. 단계마다 기준음, 첫째, 둘째가 0.5초씩 쉬고 차례로 울립니다.")
    print("첫째와 둘째 중 하나는 기준음과 같고 다른 하나는 거칠게 느껴질 거예요. 기준음과 같은 쪽을 고르세요. "
          "간격과 순서는 섞어서 나오고, 끝나면 표에서 공개됩니다.\n")
    steps = [(period, random.sample(list(MODEL_SHAPES), 2)) for period in periods_ms for _ in range(trials)]
    random.shuffle(steps)
    rows = []
    picks: dict[float, list[str]] = {period: [] for period in periods_ms}
    late: dict[float, int] = {period: 0 for period in periods_ms}
    for number, (period_ms, order) in enumerate(steps, start=1):
        input(f"[{number}/{len(steps)}] Enter를 누르면 울립니다...")

        def play(period_ms=period_ms, order=order) -> None:
            pad.stream(model_renderer("X", BASE_PERIOD_MS, freq, amp, seconds))
            for shape in order:
                pad.stream(lambda first: Block() if first < REFERENCE_GAP else None)
                stats = pad.stream(model_renderer(shape, period_ms, freq, amp, seconds),
                                   period=period_ms / 1000.0)
                late[period_ms] += stats.late_frames

        play()
        answer = _ask_pick(play)
        picked = order[int(answer) - 1] if answer != "0" else ""
        picks[period_ms].append(picked)
        rows.append(f"{number:>3}   {period_ms:>4g} ms   첫째 {order[0]}, 둘째 {order[1]}   "
                    f"{picked or '구분 안 됨':<6} {MODEL_SHAPES.get(picked, '')}")

    verdicts = {period: model_verdict(period, picks[period]) for period in periods_ms}
    lines = [
        f"리포트 재생 모델 진단 (X/Y) {datetime.now():%Y-%m-%d %H:%M:%S}, ATS 햅틱 {__version__}",
        *_device_lines(info, info.firmware.get("controller", ""), info.firmware.get("dongle", "")),
        f"톤: 양쪽 그립 {freq:g} Hz, {amp:g}%, {seconds:g}초, 1초에 {MODEL_PULSE_HZ:g}번 끊김. "
        "기준음은 8 ms 간격 1 kHz",
        "X: 리포트마다 앞 P샘플에 1 kHz 신호, 나머지 무음 (선점 모델이면 기준음과 같음)",
        "Y: 리포트 8샘플에 P ms 신호를 8/P kHz로 (다 재생 모델이면 기준음과 같음)",
        "",
        "단계  간격     순서                 고른 쪽",
        *rows,
        "",
        *(f"{period:>4g} ms: {verdicts[period]} (늦은 리포트 {late[period]})" for period in periods_ms),
        "",
        f"-> {model_summary(verdicts)}",
    ]
    _save_report("report-model", lines)


# What the pad sounds like, for a microphone rather than the hands. A voice coil is a small
# speaker and the shell radiates it; DualSense haptics use that sound on purpose, sampled at
# 3 kHz. The APEX plays 1 kHz samples, so all we can place is below 500 Hz. The recording shows
# how loud each frequency is in the air, what harmonics the shell adds (rattle), and the images
# of each tone around 1, 2, 3 kHz: their size says whether the pad holds each sample (images
# about f/(1000-f) of the tone), joins them in straight lines (about the square of that) or
# filters them (none), which is how a sharp tap will sound above 500 Hz. Nothing is rated by
# feel; a recorder sits in front of the pad and analysis is offline (tools/pad_sound_analyze.py).
#
# The sines play at every strength of a ladder in one recording, so one microphone position
# gives levels that compare: images of a held sample keep their share of the tone at any
# strength, distortion grows with it, and where it starts to grow, per frequency, is how hard
# an effect at that pitch can play before it buzzes. The frequencies are the effects' own
# (cab sway 45, rattle 55, bumps 40-65, firing 80, shifts 140-170, blinker 190) up to the
# eq's 250 Hz, then the band above it to 490.
SOUND_FREQS = (40.0, 45.0, 55.0, 65.0, 80.0, 100.0, 140.0, 170.0, 190.0, 200.0, 250.0,
               300.0, 350.0, 400.0, 450.0, 490.0)
SOUND_LEAD_MS = 2000      # silence first: the room's noise floor
SOUND_SYNC_FREQ = 250.0   # a short, loud burst the analysis aligns the timeline on
SOUND_SYNC_MS = 300
SOUND_TONE_MS = 1000
SOUND_GAP_MS = 500
SOUND_TAPS = 6
SOUND_TAP_SPACING_MS = 400
SOUND_AMPS = (10.0, 20.0, 35.0, 50.0, 75.0)  # percent, quietest first
SOUND_TAP_AMP = 100.0


def sound_plan(freqs=SOUND_FREQS, amps=SOUND_AMPS, taps: int = SOUND_TAPS) -> list[dict]:
    """The recording's timeline: segments with kind, start and length in samples (= ms).

    Every sine carries its strength (amp, percent): all frequencies at the first, then all at the next.
    """
    plan = [{"kind": "silence", "start": 0, "length": SOUND_LEAD_MS}]
    at = SOUND_LEAD_MS

    def add(kind: str, length: int, gap: int, **extra) -> None:
        nonlocal at
        plan.append({"kind": kind, "start": at, "length": length, **extra})
        at += length + gap

    add("sync", SOUND_SYNC_MS, 1000, freq=SOUND_SYNC_FREQ)
    for amp in amps:
        for freq in freqs:
            add("sine", SOUND_TONE_MS, SOUND_GAP_MS, freq=freq, amp=amp)
    # one sample at full strength: the sharpest thing the protocol can send
    add("impulse", taps * SOUND_TAP_SPACING_MS, SOUND_GAP_MS, taps=taps, spacing=SOUND_TAP_SPACING_MS)
    # report-rate's click, half a report up and half down
    add("click", taps * SOUND_TAP_SPACING_MS, 1000, taps=taps, spacing=SOUND_TAP_SPACING_MS)
    plan.append({"kind": "end", "start": at, "length": 0})
    return plan


def sound_sample(segment: dict, n: int, tap_amp_percent: float) -> float:
    """Sample n (counted from the segment's start) of one segment."""
    kind = segment["kind"]
    if kind in ("sine", "sync"):
        amp = 90.0 if kind == "sync" else max(0.0, min(100.0, segment["amp"]))
        peak = MAX_SAMPLE * amp / 100.0
        fade = FADE_SECONDS * SAMPLE_RATE
        envelope = min(1.0, n / fade, (segment["length"] - n) / fade)
        return math.sin(2 * math.pi * segment["freq"] * n / SAMPLE_RATE) * peak * envelope
    if kind in ("impulse", "click"):
        tap, offset = divmod(n, segment["spacing"])
        peak = MAX_SAMPLE * tap_amp_percent / 100.0
        start = segment["spacing"] // 2  # each tap in the middle of its slot
        if kind == "impulse":
            return peak if offset == start else 0.0
        if start <= offset < start + SUBFRAMES:
            return peak if offset < start + SUBFRAMES // 2 else -peak
    return 0.0


def sound_renderer(plan: list[dict], target: str, tap_amp_percent: float):
    use_trigger, use_left, use_right, route = TARGETS[target]
    total = plan[-1]["start"]
    tap_amp_percent = max(0.0, min(100.0, tap_amp_percent))

    def sample(t: int) -> float:
        for segment in plan:
            if segment["start"] <= t < segment["start"] + segment["length"]:
                return sound_sample(segment, t - segment["start"], tap_amp_percent)
        return 0.0

    def render(first: int) -> Block | None:
        if first >= total:
            return None
        samples = [sample(t) for t in range(first, first + SUBFRAMES)]
        silent = [0.0] * SUBFRAMES
        return Block(trigger=samples if use_trigger else silent,
                     left_grip=samples if use_left else silent,
                     right_grip=samples if use_right else silent,
                     route=route if use_trigger else Route.MUTE)

    return render


# Recordings go next to the code, in the git-ignored sample folder, not to the log folder.
SOUND_OUT = Path(__file__).with_name("sound sample") / "pad-sound"
RECORD_SETTLE_S = 1.0  # ffmpeg takes a moment to open the input
RECORD_TAIL_S = 1.0


def _percent_list(text: str) -> tuple[float, ...]:
    """"10,20,35" -> (10.0, 20.0, 35.0), for argparse."""
    try:
        values = tuple(float(part) for part in text.split(",") if part.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a list of percentages: {text!r}") from None
    if not values or not all(0.0 < value <= 100.0 for value in values):
        raise argparse.ArgumentTypeError(f"percentages must be above 0 and at most 100: {text!r}")
    return values


def ffmpeg_record_command(device: str, path: Path) -> list[str]:
    """ffmpeg recording a DirectShow input at 48 kHz, 24-bit, every channel kept."""
    return ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "dshow", "-sample_rate", "48000",
            "-i", f"audio={device}", "-c:a", "pcm_s24le", str(path)]


def run_sound(pad: Apex6, target: str, amps: tuple[float, ...], tap_amp: float, device: str | None,
              out: Path = SOUND_OUT) -> None:
    import calibrate
    import json
    import subprocess
    import time
    from datetime import datetime

    plan = sound_plan(amps=amps)
    seconds = plan[-1]["start"] / SAMPLE_RATE
    buttons = calibrate.PadButtons()
    print("패드 소리 녹음 (진동이 아니라 귀로 듣는 소리를 마이크로 잽니다)")
    print(f"  대상 {target}, 사인 {'/'.join(f'{amp:g}' for amp in amps)}%, 탭 {tap_amp:g}%, 약 {seconds:.0f}초\n")
    print("1. 게임과 음악을 끄고 조용한 곳에서, 플레이할 때처럼 패드를 쥐세요.")
    if device:
        print(f"2. 마이크({device})를 패드 앞 30 cm쯤에 두세요. 녹음은 이 프로그램이 합니다.")
    else:
        print("2. 녹음기를 패드 앞 30 cm쯤에 고정하고 (잡음 제거/자동 음량은 가능하면 끄기) 녹음을 시작하세요.")
    print("3. 패드 A (또는 Enter)를 누르면 시작합니다. 재생 중에는 손을 움직이거나 말하지 마세요. B / q 취소.")
    if _wait_event(buttons, START, STOP) == STOP:
        return
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True, exist_ok=True)
    wav = out / f"pad-sound-{stamp}.wav"
    recorder = None
    if device:
        recorder = subprocess.Popen(ffmpeg_record_command(device, wav), stdin=subprocess.PIPE,
                                    stderr=subprocess.PIPE)
        time.sleep(RECORD_SETTLE_S)
        if recorder.poll() is not None:
            print(f"녹음을 시작하지 못했습니다: {recorder.stderr.read().decode(errors='replace').strip()}")
            return
    try:
        stats = pad.stream(sound_renderer(plan, target, tap_amp))
        print(f"  {stats.summary()}")
        if recorder:
            time.sleep(RECORD_TAIL_S)
    finally:
        if recorder:
            recorder.communicate(b"q", timeout=10)  # ffmpeg's own stop key closes the file cleanly
    info = pad.info
    path = out / f"pad-sound-{stamp}.json"
    path.write_text(json.dumps({
        "target": target, "amps": list(amps), "tap_amp": tap_amp, "sample_rate": SAMPLE_RATE,
        "max_sample": MAX_SAMPLE, "plan": plan, "late_blocks": stats.summary(),
        "connection_mode": info.connection_mode, "firmware": info.firmware,
        "device": device, "recording": wav.name if device else None,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n끝. 타임라인: {path}")
    if device:
        print(f"녹음: {wav}")


def _save_report(name: str, lines: list[str]) -> None:
    from datetime import datetime

    from blocklog import LOG_DIR

    report = "\n".join(lines)
    print("\n" + report)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{name}-{datetime.now():%Y%m%d-%H%M%S}.txt"
    path.write_text(report + "\n", encoding="utf-8")
    print(f"\n저장: {path}")


CAL_NAMES = {"grip": "그립", "trigger": "트리거"}
# console keys besides the pad's buttons; arrows come as a 0xE0 prefix and a code
CAL_KEYS = {"1": "first", "2": "second", "r": "replay", "q": "cancel", "\x1b": "cancel"}
CAL_ARROWS = {"K": "first", "M": "second"}


def _console_event(keys: dict[str, str], arrows: dict[str, str]) -> str | None:
    """A key waiting in the console, named by keys (or arrows, by the code after the prefix); never blocks."""
    import msvcrt
    if not msvcrt.kbhit():
        return None
    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):
        return arrows.get(msvcrt.getwch())
    return keys.get(key.lower())


def _console_key() -> str | None:
    """A key waiting in the console, as a calibration answer; never blocks."""
    return _console_event(CAL_KEYS, CAL_ARROWS)


def run_calibrate(pad: Apex6, channels: list[str], amp: float, seconds: float) -> None:
    import calibrate
    from settings import CONFIG_PATH

    session = calibrate.Session(channels, amp, seconds)
    buttons = calibrate.PadButtons()

    def listen() -> str | None:
        return buttons.poll() or _console_key()

    print("컨트롤러를 평소처럼 잡으세요. 이웃한 두 주파수가 차례로 울리면 어느 쪽이 더 셌는지만 고르세요.")
    print("조금이라도 센 쪽이 있으면 그쪽을, 구분이 안 되면 아무 쪽이나 고르세요. 거의 같아졌다는 뜻이라 "
          "결과가 틀어지지 않습니다. 답에 따라 같게 느껴지는 세기를 좁혀 갑니다. 재생 중에 눌러도 됩니다.")
    print("  패드: 십자키 ← 또는 LB 첫 번째 / → 또는 RB 두 번째 / X 다시 듣기 / B 그만")
    print("  키보드: ← 또는 1 / → 또는 2 / r / q")
    if not buttons.connected:
        print("  (XInput 패드가 안 보여 키보드로만 받습니다)")
    shown = None
    while session.current:
        channel, freq, neighbour = session.current
        if (channel, freq) != shown:
            first = next(i for i, step in enumerate(session.steps) if step[0] == channel)
            print(f"--- {CAL_NAMES[channel]} {session.index - first + 1}/{len(calibrate.FREQS[channel])}: "
                  f"{freq:g} Hz와 {neighbour:g} Hz 비교 ---")
            shown = (channel, freq)
        answer = calibrate.play(pad, session, listen)
        if answer is None:
            print(f"  비교 {session.staircase.trials + 1}: 첫 번째 / 두 번째?")
            answer = calibrate.wait(listen)
        if answer == calibrate.CANCEL:
            print("그만둡니다.")
            break
        finished = session.answer(answer)
        if (channel, freq) != (session.current or (None, None, None))[:2]:
            print(f"  {freq:g} Hz: x{session.gain(channel, freq):.2f}")
        if finished:
            calibrate.save(session)
            curve = session.curves[finished]
            print("  저장: " + "  ".join(f"{f} Hz x{g:.2f}" for f, g in sorted(curve.items(), key=lambda kv: float(kv[0]))))
    if session.finished:
        print(f"\n{CONFIG_PATH} (\"eq\")에 저장했습니다. 다음 시작부터 적용됩니다. 지우려면 tone.py calibrate --reset")
    else:
        print("저장한 보정이 없습니다. 모터 하나를 끝까지 맞추면 저장됩니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="verify the controller, no vibration")

    tone = sub.add_parser("tone", help="fixed-frequency sine")
    tone.add_argument("--target", choices=TARGETS, default="grips")
    tone.add_argument("--freq", type=float, default=80.0)
    tone.add_argument("--amp", type=float, default=50.0, help="percent of maximum")
    tone.add_argument("--seconds", type=float, default=2.0)

    sweep = sub.add_parser("sweep", help="frequency sweep")
    sweep.add_argument("--target", choices=TARGETS, default="grips")
    sweep.add_argument("--from", dest="freq_from", type=float, default=30.0)
    sweep.add_argument("--to", dest="freq_to", type=float, default=300.0)
    sweep.add_argument("--amp", type=float, default=50.0)
    sweep.add_argument("--seconds", type=float, default=6.0)

    walk = sub.add_parser("walk", help="each actuator in turn, to check channel mapping")
    walk.add_argument("--freq", type=float, default=80.0)
    walk.add_argument("--amp", type=float, default=50.0)
    walk.add_argument("--seconds", type=float, default=1.5)

    # Trigger route check: same tone routed left, right, then both. If a
    # single-side route feels as strong on the other trigger as "both" does,
    # routing is not applied; if it is clearly weaker there, it is body
    # transmission. 110 Hz is near the trigger resonance in the xHaptics notes.
    route = sub.add_parser("route-test", help="trigger routing: left, right, both")
    route.add_argument("--freq", type=float, default=110.0)
    route.add_argument("--amp", type=float, default=30.0)
    route.add_argument("--seconds", type=float, default=2.0)
    route.add_argument("--repeat", type=int, default=2)

    diag = sub.add_parser("diag", help="interactive trigger routing diagnosis")
    diag.add_argument("--freq", type=float, default=110.0)
    diag.add_argument("--amp", type=float, default=30.0)
    diag.add_argument("--seconds", type=float, default=3.0)

    split = sub.add_parser("split", help="blind left/right trigger separation check at low amplitude")
    split.add_argument("--freq", type=float, default=110.0)
    split.add_argument("--amps", type=float, nargs="+", default=[10.0, 20.0], help="percent of maximum")
    split.add_argument("--seconds", type=float, default=2.0)
    split.add_argument("--firmware", help="override the controller firmware read from the pad")
    split.add_argument("--dongle", help="override the dongle firmware read from the pad")

    byte_checks = (("trigger-mode", "--modes", TRIGGER_MODES, "the 0x53 trigger mode byte picks a trigger"),
                   ("trigger-target", "--targets", TRIGGER_TARGETS,
                    "the 0x53 trigger target byte picks a trigger, after showing the on-board trigger setup"),
                   ("grip-target", "--targets", GRIP_TARGETS,
                    "the 0x53 grip frame's target byte picks a trigger, then where a grip tone lands"))
    for command, values, default, what in byte_checks:
        check = sub.add_parser(command, help=f"blind check whether {what}")
        check.add_argument(values, type=lambda s: int(s, 0), nargs="+", default=list(default),
                           help=f"byte values to try (default: {' '.join(f'0x{v:02X}' for v in default)})")
        check.add_argument("--route", choices=("LEFT", "RIGHT", "BOTH"), default="BOTH",
                           help="0x57 route for every tone, reference included")
        check.add_argument("--freq", type=float, default=110.0)
        check.add_argument("--amps", type=float, nargs="+", default=[20.0], help="percent of maximum")
        check.add_argument("--seconds", type=float, default=2.0)

    for command, default, what in (("trigger-ram", TRIGGER_TARGETS, "trigger target"),
                                   ("grip-ram", GRIP_TARGETS, "grip frame target")):
        ram = sub.add_parser(command, help=f"motor RAM after enabling each {what}, no vibration")
        ram.add_argument("--targets", type=lambda s: int(s, 0), nargs="+", default=list(default),
                         help=f"byte values to try (default: {' '.join(f'0x{v:02X}' for v in default)})")

    side = sub.add_parser("trigger-side", help="blind check that one trigger tone follows its side as it moves")
    side.add_argument("--freq", type=float, default=100.0, help="the throttle/brake trigger's carrier")
    side.add_argument("--amp", type=float, default=20.0)
    side.add_argument("--trials", type=int, default=2)

    report_rate = sub.add_parser("report-rate",
                                 help="blind check whether reports sent faster than 8 ms raise the 1 kHz rate")
    report_rate.add_argument("--periods", type=float, nargs="+", default=list(RATE_PERIODS_MS),
                             help="report periods in ms, 8 being the control (default: 8 4 2)")
    report_rate.add_argument("--taps", type=int, default=RATE_TAPS, help="taps per period, at least 4")
    report_rate.add_argument("--amp", type=float, default=70.0)

    report_model = sub.add_parser("report-model",
                                  help="blind X/Y check of how the pad plays reports sent faster than 8 ms")
    report_model.add_argument("--periods", type=float, nargs="+", default=list(MODEL_PERIODS_MS),
                              help="report periods in ms below 8 (default: 4 2)")
    report_model.add_argument("--trials", type=int, default=MODEL_TRIALS, help="trials per period")
    report_model.add_argument("--freq", type=float, default=MODEL_FREQ)
    report_model.add_argument("--amp", type=float, default=MODEL_AMP)
    report_model.add_argument("--seconds", type=float, default=MODEL_SECONDS)

    sound = sub.add_parser("pad-sound", help="sines and taps to record with a microphone: what the pad sounds like")
    sound.add_argument("--target", choices=TARGETS, default="grips")
    sound.add_argument("--amp", type=_percent_list, default=SOUND_AMPS, metavar="P[,P...]",
                       help="sine strengths in percent, each playing every frequency (default 10,20,35,50,75)")
    sound.add_argument("--tap-amp", type=float, default=SOUND_TAP_AMP, help="tap strength in percent")
    sound.add_argument("--record", metavar="DEVICE",
                       help="record it through ffmpeg from this DirectShow input (ffmpeg -list_devices true -f dshow -i x)")

    from calibrate import AMP, SECONDS
    cal = sub.add_parser("calibrate", help="match each frequency's strength by feel, for the synth's eq")
    cal.add_argument("--channel", choices=("grip", "trigger", "both"), default="both")
    cal.add_argument("--amp", type=float, default=AMP,
                     help="reference strength in percent; up to 4x of it must fit in 100")
    cal.add_argument("--seconds", type=float, default=SECONDS)
    cal.add_argument("--reset", action="store_true", help="forget your calibration (back to the default), no controller needed")

    args = parser.parse_args()
    if args.command == "calibrate" and args.reset:
        from settings import save_eq
        save_eq({})
        print("보정을 지웠습니다. 기본값을 씁니다.")
        return

    with Apex6() as pad:
        info = pad.info
        print(f"{info.name}: connection_mode={info.connection_mode} features=0x{info.features:02X} "
              f"grip_haptics={info.grip_haptics} trigger_haptics={info.trigger_haptics}")
        if args.command == "probe":
            print(f"firmware: {', '.join(f'{k} {v}' for k, v in info.firmware.items()) or '-'}")
            print(f"0x01 reply: {info.raw.hex(' ').upper()}")
            return
        if args.command == "split":
            run_split(pad, args.freq, args.amps, args.seconds, args.firmware, args.dongle)
            return
        if args.command in BYTE_FIELDS:
            values = list(dict.fromkeys(args.modes if args.command == "trigger-mode" else args.targets))
            if any(not 0 <= v <= 0xFF for v in values):
                parser.error("byte values go from 0 to 0xFF")
            if args.command == "trigger-mode":
                _save_report("trigger-mode", run_byte_check(pad, "trigger-mode", values, args.route, args.freq,
                                                            args.amps, args.seconds))
            else:
                run_target_check(pad, args.command, values, args.route, args.freq, args.amps, args.seconds)
            return
        if args.command in RAM_CHECKS:
            values = list(dict.fromkeys(args.targets))
            if any(not 0 <= v <= 0xFF for v in values):
                parser.error("byte values go from 0 to 0xFF")
            run_ram_check(pad, args.command, values)
            return
        if args.command == "trigger-side":
            if args.trials < 1:
                parser.error("--trials needs at least 1")
            run_side_check(pad, args.freq, args.amp, args.trials)
            return
        if args.command == "report-rate":
            periods = list(dict.fromkeys(args.periods))
            if any(not 0 < p <= BASE_PERIOD_MS for p in periods):
                parser.error(f"periods go from above 0 to {BASE_PERIOD_MS:g} ms")
            if args.taps < 4:
                parser.error("--taps needs at least 4 to tell one tap in four from none")
            run_rate_check(pad, periods, args.taps, args.amp)
            return
        if args.command == "report-model":
            periods = list(dict.fromkeys(args.periods))
            if any(not 0 < p < BASE_PERIOD_MS for p in periods):
                parser.error(f"periods go from above 0 to below {BASE_PERIOD_MS:g} ms")
            if args.trials < 1:
                parser.error("--trials needs at least 1")
            run_model_check(pad, periods, args.trials, args.freq, args.amp, args.seconds)
            return
        if args.command == "pad-sound":
            run_sound(pad, args.target, args.amp, args.tap_amp, args.record)
            return
        if args.command == "calibrate":
            channels = ["grip", "trigger"] if args.channel == "both" else [args.channel]
            run_calibrate(pad, channels, args.amp, args.seconds)
            return
        if args.command == "diag":
            run_diag(pad, args.freq, args.amp, args.seconds)
            return
        if args.command == "tone":
            plan = [(args.target, args.freq, args.freq, args.seconds)]
        elif args.command == "sweep":
            plan = [(args.target, args.freq_from, args.freq_to, args.seconds)]
        elif args.command == "route-test":
            plan = [(t, args.freq, args.freq, args.seconds)
                    for _ in range(args.repeat)
                    for t in ("trigger-left", "trigger-right", "triggers")]
        else:
            plan = [(t, args.freq, args.freq, args.seconds)
                    for t in ("left", "right", "trigger-left", "trigger-right")]
        for target, f0, f1, seconds in plan:
            label = f"{f0:g} Hz" if f0 == f1 else f"{f0:g} -> {f1:g} Hz"
            print(f"> {target}: {label}, {args.amp:g}% for {seconds:g}s")
            stats = pad.stream(tone_renderer(target, f0, f1, args.amp, seconds))
            print(f"  {stats.summary()}")
            gap = 1000 if args.command == "route-test" else 400  # samples = ms
            pad.stream(lambda first: Block() if first < gap else None)


if __name__ == "__main__":
    main()
