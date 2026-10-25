"""ATS / ETS2 telemetry -> APEX 6 Pro voice-coil haptics (command line).

  python ats_haptics.py                 run with the game (waits for it)
  python ats_haptics.py --simulate      scripted 60 s test drive, no game needed
  python ats_haptics.py --simulate --sim-fps 30   the same drive as a 30 fps game (0: new data every block)
  python ats_haptics.py --dry-run       no controller; print levels only
  python ats_haptics.py --gain 0.8 --road 0.4 --trigger 0.3 --monitor
  python ats_haptics.py --exit-with-game

The controller is only opened while ATS/ETS2 is running and released when
the game quits. Defaults come from config.json (saved by gui.py); flags
override them. Close the X-Haptics engine first: it streams to the same
motors. Ctrl+C stops and releases the motors. For live tuning use gui.py.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import deque
from dataclasses import fields
from typing import Callable

from apex6 import FRAME_PERIOD, MAX_SAMPLE, Apex6, Block, DeviceUnavailable, StreamStats
from blocklog import BlockLog
from effects import Equalizer, Gains, Synth, effective_gains
from gamewatch import GameWatcher
from i18n import set_language, system_language, t
from settings import load_app_options, load_eq, load_gains
from telemetry import SIMULATED_FPS, Frame, SharedMemoryTelemetry, SimulatedTelemetry

STALE_AFTER_S = 0.3  # game minimized, loading or closed: render_time stops moving
RETRY_S = 2.0  # wait between attempts to reach an absent or sleeping controller


class Runner:
    """Produces one block per 8 ms tick and keeps state for a monitor or UI.

    Call apply() with edited gains from any thread; the change takes effect
    on the next block.
    """

    def __init__(self, gains: Gains, simulate: bool = False,
                 on_state: Callable[[str], None] | None = None, seconds: float | None = None,
                 log_blocks: bool = False, sim_fps: float | None = SIMULATED_FPS,
                 eq: Equalizer | None = None) -> None:
        self.gains = effective_gains(gains)
        self.eq = eq or Equalizer()
        self.synth = Synth(self.gains, self.eq)
        self.simulated = simulate
        self.source = SimulatedTelemetry(sim_fps) if simulate else SharedMemoryTelemetry()
        self.watcher = None if simulate else GameWatcher()
        self.on_state = on_state
        self.deadline = time.perf_counter() + seconds if seconds else None
        self.stop_requested = False
        self.game_ended = False
        self.last_stats: StreamStats | None = None
        self.state = ""
        self.frame: Frame | None = None
        self.peaks = [0.0, 0.0, 0.0]  # left grip, right grip, trigger; 0..1, held until read
        self.events: deque[str] = deque(maxlen=50)
        self.max_accel = 0.0
        self._last_render_time = None
        self._last_render_change = 0.0
        self.log_blocks = log_blocks
        self.block_log: BlockLog | None = None

    def _read_frame(self) -> Frame | None:
        frame = self.source.read()
        now = time.perf_counter()
        if frame is None:
            problem = getattr(self.source, "problem", None)
            self._set_state(t("텔레메트리 없음: {problem} → '텔레메트리 플러그인'에서 설치", problem=problem)
                            if problem else t("텔레메트리 대기 중 (게임 로딩 중이거나 플러그인 없음)"))
            return None
        if frame.render_time != self._last_render_time:
            self._last_render_time = frame.render_time
            self._last_render_change = now
        if not frame.live:
            self._set_state(t("일시정지/메뉴"))
            return None
        if now - self._last_render_change > STALE_AFTER_S:
            self._set_state(t("텔레메트리 멈춤 (최소화/로딩)"))
            return None
        self._set_state(t("주행 중 ({source})", source=self.source.name))
        return frame

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            if self.on_state:
                self.on_state(state)

    def apply(self, gains: Gains) -> None:
        """Copy edited gains into the running synth (disabled effects at zero)."""
        effective = effective_gains(gains)
        for f in fields(Gains):
            setattr(self.gains, f.name, getattr(effective, f.name))

    @property
    def finished(self) -> bool:
        return self.stop_requested or bool(self.deadline and time.perf_counter() > self.deadline)

    def wait_for_game(self) -> bool:
        """Block until ATS/ETS2 runs. False if stopped first."""
        while not self.finished:
            if self.watcher is None or self.watcher.game:
                return True
            self.frame = None
            self._set_state(t("게임 대기 중 (ATS/ETS2 실행 전)"))
            time.sleep(0.25)
        return False

    def start_session(self) -> None:
        self.game_ended = False
        self.synth = Synth(self.gains, self.eq)  # fresh edge detectors: no stale events
        self._last_render_time = None
        # Map the plugin afresh: the next game may write another plugin's mapping,
        # and holding the old one keeps a dead copy alive.
        self.source.close()

    def _open_log(self) -> None:
        """Start a debug log once the controller is taken: one file per connection.

        Not per attempt: retrying a sleeping controller every RETRY_S would
        rotate the log that recorded the disconnect out of the folder.
        """
        self._close_log()
        if not self.log_blocks:
            return
        stamp = time.strftime("%H:%M:%S")
        try:
            self.block_log = BlockLog()
            self.events.append(f"{stamp} {t('디버그 기록: {name}', name=self.block_log.path.name)}")
        except OSError as exc:
            self.events.append(f"{stamp} {t('디버그 기록을 열 수 없음 ({error})', error=exc)}")

    def render(self, _first_sample: int = 0) -> Block | None:
        if self.finished:
            return None
        if self.watcher is not None and not self.watcher.game:
            self.game_ended = True
            self.frame = None
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
        if self.block_log is not None:
            self.block_log.add(block, frame, levels.events)
        return block

    def take_peaks(self) -> list[float]:
        peaks, self.peaks = self.peaks, [0.0, 0.0, 0.0]
        return peaks

    def take_max_accel(self) -> float:
        value, self.max_accel = self.max_accel, 0.0
        return value

    def _close_log(self) -> None:
        if self.block_log is not None:
            self.block_log.close()
            self.block_log = None

    def close(self) -> None:
        self._close_log()
        self.source.close()
        if self.watcher is not None:
            self.watcher.close()


def run(runner: Runner, dry_run: bool = False,
        on_connected: Callable[[str | None], None] | None = None,
        exit_with_game: bool = False) -> None:
    """Hold the controller only while the game runs; repeat for each game launch.

    on_connected(name) fires when the controller is taken, on_connected(None)
    when it is released. Returns when stopped, or after the game quits if
    exit_with_game.
    """
    while runner.wait_for_game():
        runner.start_session()
        if dry_run:
            runner._open_log()
            deadline = time.perf_counter()
            while runner.render() is not None:
                deadline += FRAME_PERIOD
                remaining = deadline - time.perf_counter()
                if remaining > 0:
                    time.sleep(remaining)
            runner._close_log()
        else:
            try:
                with Apex6() as pad:
                    runner._open_log()
                    if on_connected:
                        on_connected(pad.info.name)
                    runner.last_stats = pad.stream(runner.render)
            except DeviceUnavailable as exc:
                # Asleep, unplugged or reconnecting: keep trying while the game runs.
                if on_connected:
                    on_connected(None)
                runner.frame = None
                runner._set_state(t("{error} · {seconds}초 후 재시도", error=exc, seconds=f"{RETRY_S:.0f}"))
                runner.events.append(f"{time.strftime('%H:%M:%S')} {exc}")
                deadline = time.perf_counter() + RETRY_S
                while not runner.finished and time.perf_counter() < deadline:
                    time.sleep(0.1)
                if runner.watcher is None or runner.watcher.game:
                    continue
                runner.game_ended = True  # quit without ever reaching the pad
            finally:
                runner._close_log()  # flushed now, not when the next game starts
        if on_connected:
            on_connected(None)
        if not runner.game_ended or exit_with_game:
            return
        runner._set_state(t("게임 종료됨, 컨트롤러 해제"))


def parse_args(defaults: Gains) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--simulate", action="store_true", help="use the scripted drive instead of the game")
    parser.add_argument("--sim-fps", type=float, default=SIMULATED_FPS,
                        help="game frame rate the scripted drive updates at; 0 updates every 8 ms block")
    parser.add_argument("--dry-run", action="store_true", help="do not open the controller")
    parser.add_argument("--monitor", action="store_true", help="print a status line twice a second")
    parser.add_argument("--seconds", type=float, help="stop after this many seconds")
    parser.add_argument("--exit-with-game", action="store_true", help="quit when ATS/ETS2 quits")
    parser.add_argument("--log-blocks", action="store_true",
                        help="write every output block as CSV under %%APPDATA%%/ATSHaptics/logs (see blocklog.py)")
    parser.add_argument("--gain", type=float, default=defaults.master, help="master gain 0..1+")
    parser.add_argument("--grip-level", type=float, default=defaults.grip_level, help="grip output strength")
    parser.add_argument("--trigger-level", type=float, default=defaults.trigger_level,
                        help="trigger output strength")
    for name in ("engine", "road", "bumps", "rumble", "wheelspin", "gear", "retarder", "impact",
                 "trigger"):
        parser.add_argument(f"--{name}", type=float, default=getattr(defaults, name))
    parser.add_argument("--throttle-pulse", type=float, default=defaults.throttle_pulse,
                        help="engine-rate pulse depth of the throttle trigger, 0..1")
    parser.add_argument("--cylinders", type=int, default=defaults.cylinders)
    return parser.parse_args()


def main() -> None:
    set_language(load_app_options().language or system_language())
    gains = load_gains()
    args = parse_args(gains)
    gains.master = args.gain
    for name in ("grip_level", "trigger_level", "engine", "road", "bumps", "rumble", "wheelspin", "gear", "retarder",
                 "impact", "trigger", "throttle_pulse", "cylinders"):
        setattr(gains, name, getattr(args, name))
    runner = Runner(gains, simulate=args.simulate, seconds=args.seconds, log_blocks=args.log_blocks,
                    sim_fps=args.sim_fps or None, eq=Equalizer.for_player(load_eq()),
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
    def on_connected(name: str | None) -> None:
        if name:
            print("\n" + t("{name} 연결됨. Ctrl+C로 종료.", name=name), flush=True)
        elif runner.last_stats:
            print("\n" + t("컨트롤러 해제. {stats}", stats=runner.last_stats.summary()), flush=True)

    try:
        run(runner, dry_run=args.dry_run, on_connected=on_connected, exit_with_game=args.exit_with_game)
    except KeyboardInterrupt:
        print("\n" + t("종료: 모터 해제 완료."))
    finally:
        runner.close()


if __name__ == "__main__":
    sys.exit(main())
