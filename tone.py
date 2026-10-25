"""Test tones for the APEX 6 Pro voice coils.

Examples:
  python tone.py probe                       identity check only, no vibration
  python tone.py tone --target left --freq 80 --amp 50 --seconds 2
  python tone.py sweep --target grips --from 30 --to 300 --seconds 6
  python tone.py walk                        left grip, right grip, left trigger, right trigger
  python tone.py split                       blind left/right trigger separation check
  python tone.py calibrate                   match each frequency's strength by feel, for the synth's eq
"""

from __future__ import annotations

import argparse
import math

from apex6 import MAX_SAMPLE, SAMPLE_RATE, SUBFRAMES, Apex6, Block, Route

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


def run_split(pad: Apex6, freq: float, amps: list[float], seconds: float,
              firmware: str | None, dongle: str | None) -> None:
    import random
    from datetime import datetime

    from blocklog import LOG_DIR
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
        f"컨트롤러 펌웨어: {firmware or '-'}",
        f"구성 요소 펌웨어: {_firmware_text(info)}",
        f"동글 펌웨어: {dongle or '-'}",
        f"기기: {info.name}, connection_mode={info.connection_mode}, features=0x{info.features:02X}",
        f"0x01 응답: {info.raw.hex(' ').upper()}",
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
    report = "\n".join(lines)
    print("\n" + report)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"trigger-split-{datetime.now():%Y%m%d-%H%M%S}.txt"
    path.write_text(report + "\n", encoding="utf-8")
    print(f"\n저장: {path}")


CAL_NAMES = {"grip": "그립", "trigger": "트리거"}
# console keys besides the pad's buttons; arrows come as a 0xE0 prefix and a code
CAL_KEYS = {"1": "first", "2": "second", "r": "replay", "q": "cancel", "\x1b": "cancel"}
CAL_ARROWS = {"K": "first", "M": "second"}


def _console_key() -> str | None:
    """A key waiting in the console, as a calibration answer; never blocks."""
    import msvcrt
    if not msvcrt.kbhit():
        return None
    key = msvcrt.getwch()
    if key in ("\x00", "\xe0"):
        return CAL_ARROWS.get(msvcrt.getwch())
    return CAL_KEYS.get(key.lower())


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
