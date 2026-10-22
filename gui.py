"""Live tuning window for the ATS -> APEX 6 Pro haptics.

  pythonw gui.py            normal use (or double-click start_gui.cmd)
  python gui.py --dry-run   no controller, for testing the window

Sliders change the running effects immediately; 저장 writes config.json,
which ats_haptics.py also reads.
"""

from __future__ import annotations

import argparse
import threading
import tkinter as tk
from dataclasses import asdict
from tkinter import messagebox, ttk

from ats_haptics import Runner, run
from effects import Gains
from settings import load_gains, save_gains

POLL_MS = 100
METER_DECAY = 0.8

# key, label, slider min, slider max, slider units per gain unit, unit text
EFFECT_SLIDERS = (
    ("master", "전체", 0, 150, 100, "%"),
    ("engine", "엔진", 0, 200, 100, "%"),
    ("road", "노면", 0, 200, 100, "%"),
    ("bumps", "요철", 0, 200, 100, "%"),
    ("gear", "변속", 0, 200, 100, "%"),
    ("retarder", "리타더", 0, 200, 100, "%"),
    ("impact", "충돌", 0, 200, 100, "%"),
    ("trigger", "트리거", 0, 100, 100, "%"),
)
EVENT_SLIDERS = (
    ("pedal", "페달 바닥", 0, 200, 100, "%"),
    ("squeal", "브레이크 스퀼", 0, 200, 100, "%"),
    ("trailer", "트레일러 연결", 0, 200, 100, "%"),
    ("park_brake", "파킹 브레이크", 0, 200, 100, "%"),
    ("blinker", "방향지시등", 0, 200, 100, "%"),
    ("wipers", "와이퍼", 0, 200, 100, "%"),
    ("engine_start", "시동 켜기/끄기", 0, 200, 100, "%"),
    ("shifter", "레인지/스플리터", 0, 200, 100, "%"),
    ("game_events", "게임 알림", 0, 200, 100, "%"),
)
THRESHOLD_SLIDERS = (
    ("bump_threshold", "요철 감지 기준", 5, 100, 100, "cm/s"),
    ("impact_threshold", "충돌 감지 기준", 4, 40, 1, "m/s²"),
)


class App:
    def __init__(self, root: tk.Tk, dry_run: bool) -> None:
        self.root = root
        self.dry_run = dry_run
        self.gains = load_gains()
        self.saved = asdict(self.gains)
        self.runner: Runner | None = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.connected: str | None = None
        self.meter_values = [0.0, 0.0, 0.0]
        self.slider_vars: dict[str, tuple[tk.DoubleVar, ttk.Label, int, str]] = {}
        self.save_state = tk.StringVar()  # sliders update it while being built
        self.timing_text: str | None = None

        root.title("ATS 햅틱 - APEX 6 Pro" + (" (dry-run)" if dry_run else ""))
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        self._poll()

    # -- layout -----------------------------------------------------------------

    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        self.source = tk.StringVar(value="game")
        ttk.Radiobutton(top, text="ATS 게임", variable=self.source, value="game").pack(side="left")
        ttk.Radiobutton(top, text="시뮬레이션", variable=self.source, value="sim").pack(side="left", padx=(6, 12))
        self.start_button = ttk.Button(top, text="시작", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(top, text="정지", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        self.status = tk.StringVar(value="정지됨")
        ttk.Label(top, textvariable=self.status, width=42).pack(side="left", padx=8)

        left = ttk.Frame(self.root)
        left.grid(row=1, column=0, sticky="nsew", **pad)
        effects = ttk.LabelFrame(left, text="주행 효과")
        effects.pack(fill="x")
        for row, spec in enumerate(EFFECT_SLIDERS):
            self._slider(effects, row, *spec)
        events = ttk.LabelFrame(left, text="이벤트 효과")
        events.pack(fill="x", pady=(8, 0))
        for row, spec in enumerate(EVENT_SLIDERS):
            self._slider(events, row, *spec)

        right = ttk.Frame(self.root)
        right.grid(row=1, column=1, sticky="nsew", **pad)

        meters = ttk.LabelFrame(right, text="출력")
        meters.pack(fill="x")
        self.meters = []
        for row, label in enumerate(("왼쪽 그립", "오른쪽 그립", "트리거")):
            ttk.Label(meters, text=label, width=10).grid(row=row, column=0, sticky="w", padx=6, pady=2)
            bar = ttk.Progressbar(meters, length=200, maximum=100)
            bar.grid(row=row, column=1, padx=6, pady=2)
            self.meters.append(bar)

        info = ttk.LabelFrame(right, text="텔레메트리")
        info.pack(fill="x", pady=(8, 0))
        self.info = {}
        for row, (key, label) in enumerate((("speed", "속도"), ("rpm", "RPM"), ("gear", "기어"),
                                            ("pedal", "페달"), ("surface", "노면"), ("trigger", "트리거"),
                                            ("accel", "가속도 최대"))):
            ttk.Label(info, text=label, width=10).grid(row=row, column=0, sticky="w", padx=6)
            var = tk.StringVar(value="-")
            ttk.Label(info, textvariable=var, width=28).grid(row=row, column=1, sticky="w", padx=6)
            self.info[key] = var

        thresholds = ttk.LabelFrame(right, text="감지 기준")
        thresholds.pack(fill="x", pady=(8, 0))
        for row, spec in enumerate(THRESHOLD_SLIDERS):
            self._slider(thresholds, row, *spec)
        ttk.Label(thresholds, text="엔진 기통 수", width=12).grid(row=len(THRESHOLD_SLIDERS), column=0,
                                                              sticky="w", padx=6)
        self.cylinders = tk.IntVar(value=self.gains.cylinders)
        ttk.Spinbox(thresholds, from_=4, to=12, increment=2, width=5, textvariable=self.cylinders,
                    command=self._set_cylinders).grid(row=len(THRESHOLD_SLIDERS), column=1, sticky="w", padx=6)

        log = ttk.LabelFrame(right, text="이벤트")
        log.pack(fill="both", expand=True, pady=(8, 0))
        self.events = tk.Listbox(log, height=8, width=40)
        self.events.pack(fill="both", expand=True, padx=4, pady=4)

        bottom = ttk.Frame(self.root)
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", **pad)
        ttk.Button(bottom, text="저장", command=self.save).pack(side="left")
        ttk.Button(bottom, text="기본값", command=self.reset).pack(side="left", padx=4)
        ttk.Label(bottom, textvariable=self.save_state).pack(side="left", padx=8)
        self.timing = tk.StringVar()
        ttk.Label(bottom, textvariable=self.timing).pack(side="right")

    def _slider(self, parent: ttk.Frame, row: int, key: str, label: str,
                low: int, high: int, scale: int, unit: str) -> None:
        ttk.Label(parent, text=label, width=12).grid(row=row, column=0, sticky="w", padx=6)
        var = tk.DoubleVar(value=getattr(self.gains, key) * scale)
        value_label = ttk.Label(parent, width=9)
        ttk.Scale(parent, from_=low, to=high, length=220, variable=var,
                  command=lambda _=None, k=key: self._on_slider(k)).grid(row=row, column=1, padx=6, pady=2)
        value_label.grid(row=row, column=2, sticky="w")
        self.slider_vars[key] = (var, value_label, scale, unit)
        self._on_slider(key)

    # -- controls -------------------------------------------------------------

    def _on_slider(self, key: str) -> None:
        var, label, scale, unit = self.slider_vars[key]
        value = round(var.get())
        setattr(self.gains, key, value / scale)
        label.config(text=f"{value} {unit}")
        self._update_save_state()

    def _set_cylinders(self) -> None:
        self.gains.cylinders = int(self.cylinders.get())
        self._update_save_state()

    def _update_save_state(self) -> None:
        self.save_state.set("" if asdict(self.gains) == self.saved else "변경됨 (저장 안 됨)")

    def save(self) -> None:
        save_gains(self.gains)
        self.saved = asdict(self.gains)
        self._update_save_state()
        self.save_state.set("저장됨")

    def reset(self) -> None:
        defaults = Gains()
        for key, (var, _, scale, _) in self.slider_vars.items():
            var.set(getattr(defaults, key) * scale)
            self._on_slider(key)
        self.cylinders.set(defaults.cylinders)
        self._set_cylinders()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.error = None
        self.connected = None
        self.events.delete(0, "end")
        self.runner = Runner(self.gains, simulate=self.source.get() == "sim")
        self.thread = threading.Thread(target=self._worker, args=(self.runner,), daemon=True)
        self.thread.start()
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")

    def _worker(self, runner: Runner) -> None:
        try:
            stats = run(runner, dry_run=self.dry_run, on_connected=self._on_connected)
            if stats:
                self.timing_text = f"지연 프레임 {stats.late_frames} / 최대 {stats.max_lateness_ms:.1f} ms"
        except Exception as exc:  # shown in the status line
            self.error = str(exc)
        finally:
            runner.close()

    def _on_connected(self, name: str) -> None:
        self.connected = name

    def stop(self) -> None:
        if self.runner:
            self.runner.stop_requested = True

    def on_close(self) -> None:
        if asdict(self.gains) != self.saved:
            answer = messagebox.askyesnocancel("저장", "바뀐 설정을 저장할까요?")
            if answer is None:
                return
            if answer:
                self.save()
        self.stop()
        if self.thread:
            self.thread.join(timeout=2.0)  # lets the controller release its motors
        self.root.destroy()

    # -- periodic UI refresh ------------------------------------------------------

    def _poll(self) -> None:
        running = bool(self.thread and self.thread.is_alive())
        runner = self.runner
        if running and runner:
            device = self.connected or ("컨트롤러 없음" if self.dry_run else "연결 중…")
            self.status.set(f"{device} · {runner.state or '시작 중'}")
            self._refresh(runner)
        else:
            self.start_button.config(state="normal")
            self.stop_button.config(state="disabled")
            if self.error:
                self.status.set(f"오류: {self.error}")
            elif runner:
                self.status.set("정지됨")
            self.meter_values = [0.0, 0.0, 0.0]
            for bar in self.meters:
                bar["value"] = 0
        if self.timing_text:
            self.timing.set(self.timing_text)
        self.root.after(POLL_MS, self._poll)

    def _refresh(self, runner: Runner) -> None:
        for i, peak in enumerate(runner.take_peaks()):
            self.meter_values[i] = max(peak, self.meter_values[i] * METER_DECAY)
            self.meters[i]["value"] = self.meter_values[i] * 100
        while runner.events:
            self.events.insert(0, runner.events.popleft())
            self.events.delete(50, "end")
        frame, levels = runner.frame, runner.synth.levels
        if frame is None:
            for var in self.info.values():
                var.set("-")
            return
        self.info["speed"].set(f"{abs(frame.speed) * 3.6:.0f} km/h")
        self.info["rpm"].set(f"{frame.rpm:.0f}")
        self.info["gear"].set(str(frame.gear))
        self.info["pedal"].set(f"엑셀 {frame.user_throttle:.2f} / 브레이크 {frame.user_brake:.2f}")
        self.info["surface"].set(levels.surface or "-")
        self.info["trigger"].set({"throttle": "엑셀", "brake": "브레이크"}.get(levels.trigger_mode, "-"))
        self.info["accel"].set(f"{runner.take_max_accel():.1f} m/s²")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="run without the controller")
    args = parser.parse_args()
    root = tk.Tk()
    root.option_add("*Font", ("Malgun Gothic", 10))
    App(root, dry_run=args.dry_run)
    root.mainloop()


if __name__ == "__main__":
    main()
