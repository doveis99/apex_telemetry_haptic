"""ATS / ETS2 telemetry -> APEX 6 Pro voice-coil haptics (command line).

  python ats_haptics.py                 run with the game (waits for it)
  python ats_haptics.py --simulate      scripted 40 s drive, no game needed
  python ats_haptics.py --dry-run       no controller; print levels only
  python ats_haptics.py --gain 0.8 --road 0.4 --trigger 0.3 --monitor

Defaults come from config.json (saved by gui.py); flags override them.
Close the X-Haptics engine first: it streams to the same motors.
Ctrl+C stops and releases the motors. For live tuning use gui.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from typing import Callable

from apex6 import FRAME_PERIOD, MAX_SAMPLE, Apex6, Block, StreamStats
from effects import Gains, Synth
from settings import load_gains
from telemetry import Frame, SharedMemoryTelemetry, SimulatedTelemetry

STALE_AFTER_S = 0.3  # game minimized, loading or closed: render_time stops moving


class Runner:
    """Produces one block per 8 ms tick and keeps state for a monitor or UI.

    gains is shared with the caller: changing its attributes from another
    thread takes effect on the next block.
    """

    def __init__(self, gains: Gains, simulate: bool = False,
                 on_state: Callable[[str], None] | None = None, seconds: float | None = None) -> None:
        self.gains = gains
        self.synth = Synth(gains)
        self.source = SimulatedTelemetry() if simulate else SharedMemoryTelemetry()
        self.on_state = on_state
        self.deadline = time.perf_counter() + seconds if seconds else None
        self.stop_requested = False
        self.state = ""
        self.frame: Frame | None = None
        self.peaks = [0.0, 0.0, 0.0]  # left grip, right grip, trigger; 0..1, held until read
        self.events: deque[str] = deque(maxlen=50)
        self.max_accel = 0.0
        self._last_render_time = None
        self._last_render_change = 0.0

    def _read_frame(self) -> Frame | None:
        frame = self.source.read()
        now = time.perf_counter()
        if frame is None:
            self._set_state("게임 대기 중")
            return None
        if frame.render_time != self._last_render_time:
            self._last_render_time = frame.render_time
            self._last_render_change = now
        if not frame.live:
            self._set_state("일시정지/메뉴")
            return None
        if now - self._last_render_change > STALE_AFTER_S:
            self._set_state("텔레메트리 멈춤 (최소화/로딩)")
            return None
        self._set_state(f"주행 중 ({self.source.name})")
        return frame

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            if self.on_state:
                self.on_state(state)

    def render(self, _first_sample: int = 0) -> Block | None:
        if self.stop_requested or (self.deadline and time.perf_counter() > self.deadline):
            return None
        frame = self._read_frame()
        block = self.synth.render(frame)
        self.frame = frame
        levels = self.synth.levels
        self.max_accel = max(self.max_accel, levels.max_accel)
        levels.max_accel = 0.0
        stamp = time.strftime("%H:%M:%S")
        self.events.extend(f"{stamp} {event}" for event in levels.events)
        for i, channel in enumerate((block.left_grip, block.right_grip, block.trigger)):
            self.peaks[i] = max(self.peaks[i], max(abs(x) for x in channel) / MAX_SAMPLE)
        return block

    def take_peaks(self) -> list[float]:
        peaks, self.peaks = self.peaks, [0.0, 0.0, 0.0]
        return peaks

    def take_max_accel(self) -> float:
        value, self.max_accel = self.max_accel, 0.0
        return value

    def close(self) -> None:
        self.source.close()


def run(runner: Runner, dry_run: bool = False, on_connected: Callable[[str], None] | None = None) -> StreamStats | None:
    """Drive the runner until it stops. Opens the controller unless dry_run."""
    if dry_run:
        deadline = time.perf_counter()
        while runner.render() is not None:
            deadline += FRAME_PERIOD
            remaining = deadline - time.perf_counter()
            if remaining > 0:
                time.sleep(remaining)
        return None
    with Apex6() as pad:
        if on_connected:
            on_connected(pad.info.name)
        return pad.stream(runner.render)


def parse_args(defaults: Gains) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulate", action="store_true", help="use the scripted drive instead of the game")
    parser.add_argument("--dry-run", action="store_true", help="do not open the controller")
    parser.add_argument("--monitor", action="store_true", help="print a status line twice a second")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds")
    parser.add_argument("--gain", type=float, default=defaults.master, help="master gain 0..1+")
    for name in ("engine", "road", "bumps", "gear", "retarder", "impact", "trigger"):
        parser.add_argument(f"--{name}", type=float, default=getattr(defaults, name))
    parser.add_argument("--cylinders", type=int, default=defaults.cylinders)
    return parser.parse_args()


def main() -> None:
    gains = load_gains()
    args = parse_args(gains)
    gains.master = args.gain
    for name in ("engine", "road", "bumps", "gear", "retarder", "impact", "trigger", "cylinders"):
        setattr(gains, name, getattr(args, name))
    runner = Runner(gains, simulate=args.simulate, seconds=args.seconds,
                    on_state=lambda s: print(f"\n[{time.strftime('%H:%M:%S')}] {s}", flush=True))
    monitor = args.monitor or args.dry_run
    if monitor:
        render = runner.render
        next_status = [0.0]

        def render_with_status(first: int = 0) -> Block | None:
            block = render(first)
            for _ in range(len(runner.events)):
                print(f"\n  event: {runner.events.popleft()}", flush=True)
            frame, now = runner.frame, time.perf_counter()
            if frame is not None and now >= next_status[0]:
                next_status[0] = now + 0.5
                lv = runner.synth.levels
                print(f"\r  {abs(frame.speed) * 3.6:5.1f} km/h  {frame.rpm:5.0f} rpm  gear {frame.gear:>2}  "
                      f"thr {frame.user_throttle:.2f} brk {frame.user_brake:.2f}  "
                      f"engine {lv.engine:.2f} road L{lv.road_left:.2f}/R{lv.road_right:.2f}  "
                      f"trig {lv.trigger_mode}:{lv.trigger:.2f}  surface {lv.surface or '-':10.10s} "
                      f"accel {runner.take_max_accel():4.1f}", end="", flush=True)
            return block

        runner.render = render_with_status
    try:
        stats = run(runner, dry_run=args.dry_run,
                    on_connected=lambda name: print(f"{name} 연결됨. Ctrl+C로 종료.", flush=True))
        if stats:
            print(f"\n{stats.summary()}")
    except KeyboardInterrupt:
        print("\n종료: 모터 해제 완료.")
    finally:
        runner.close()


if __name__ == "__main__":
    sys.exit(main())
