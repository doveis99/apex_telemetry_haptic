"""Test tones for the APEX 6 Pro voice coils.

Examples:
  python tone.py probe                       identity check only, no vibration
  python tone.py tone --target left --freq 80 --amp 50 --seconds 2
  python tone.py sweep --target grips --from 30 --to 300 --seconds 6
  python tone.py walk                        left grip, right grip, left trigger, right trigger
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

    args = parser.parse_args()

    with Apex6() as pad:
        info = pad.info
        print(f"{info.name}: connection_mode={info.connection_mode} features=0x{info.features:02X} "
              f"grip_haptics={info.grip_haptics} trigger_haptics={info.trigger_haptics}")
        if args.command == "probe":
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
