"""Live tuning window for the ATS -> APEX 6 Pro haptics.

  pythonw gui.py            normal use (or double-click start_gui.cmd)
  pythonw gui.py --autostart --exit-with-game --minimized
                            how launch.py starts it alongside the game
  python gui.py --dry-run   no controller, for testing the window

Sliders, on/off switches and the 그립 | 트리거 | 둘 다 selectors change the
running effects immediately; 저장 writes config.json, which ats_haptics.py also
reads. Only one window runs at a time; a second launch exits quietly. At
start it looks for a newer GitHub release and offers it in the top bar.
The widgets and dark theme live in widgets.py.
"""

from __future__ import annotations

import argparse
import ctypes
import threading
import tkinter as tk
import webbrowser
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import plugin_install
import updater
from ats_haptics import Runner, run
from version import __version__
from effects import BOTH, DEFAULT_ROUTES, GRIP, TOGGLEABLE, TRIGGER, Gains, route_of
from gamewatch import GameWatcher
from settings import load_app_options, load_gains, save_app_options, save_gains
from widgets import (ACCENT, BG, BOTH_COLOR, CARD, FAINT, GO, GRIP_COLOR, MUTED, STOP, TEXT, TILE,
                     TRIGGER_COLOR, WARN, Button, Meter, Segmented, Slider, Switch, apply_theme, card,
                     dark_title_bar, font)

POLL_MS = 100
METER_DECAY = 0.8
INSTANCE_MUTEX = "Local\\ApexAtsHapticsGui"

# key, label, slider min, slider max, slider units per gain unit, unit text
OUTPUT_SLIDERS = (
    ("master", "전체", 0, 150, 100, "%"),
    ("grip_level", "그립", 0, 150, 100, "%"),
    ("trigger_level", "트리거", 0, 150, 100, "%"),
)
EFFECT_SLIDERS = (
    ("engine", "엔진", 0, 200, 100, "%"),
    ("road", "노면", 0, 200, 100, "%"),
    ("bumps", "요철", 0, 200, 100, "%"),
    ("gear", "변속", 0, 200, 100, "%"),
    ("retarder", "리타더", 0, 200, 100, "%"),
    ("impact", "충돌", 0, 200, 100, "%"),
    ("trigger", "엑셀/브레이크", 0, 100, 100, "%"),
    ("throttle_pulse", "└ 엔진 박자", 0, 100, 100, "%"),
)
EVENT_SLIDERS = (
    ("pedal", "페달 바닥", 0, 200, 100, "%"),
    ("squeal", "브레이크 스퀼", 0, 200, 100, "%"),
    ("trailer", "트레일러 연결", 0, 200, 100, "%"),
    ("park_brake", "파킹 브레이크", 0, 200, 100, "%"),
    ("blinker", "방향지시등", 0, 200, 100, "%"),
    ("engine_start", "시동 켜기/끄기", 0, 200, 100, "%"),
    ("shifter", "레인지/스플리터", 0, 200, 100, "%"),
    ("game_events", "게임 알림", 0, 200, 100, "%"),
)
THRESHOLD_SLIDERS = (
    ("bump_threshold", "요철 감지 기준", 5, 100, 100, "cm/s"),
    ("impact_threshold", "충돌 감지 기준", 4, 40, 1, "m/s²"),
)
# where an effect vibrates; each choice has its colour, which its slider takes too
ROUTE_OPTIONS = ((GRIP, "그립", GRIP_COLOR), (TRIGGER, "트리거", TRIGGER_COLOR), (BOTH, "둘 다", BOTH_COLOR))
ROUTE_COLORS = {route: color for route, _, color in ROUTE_OPTIONS}
LEVEL_COLORS = {"master": ACCENT, "grip_level": GRIP_COLOR, "trigger_level": TRIGGER_COLOR,
                "throttle_pulse": TRIGGER_COLOR}


class App:
    def __init__(self, root: tk.Tk, dry_run: bool = False, exit_with_game: bool = False) -> None:
        self.root = root
        self.dry_run = dry_run
        self.exit_with_game = exit_with_game
        self.gains = load_gains()
        self.saved = asdict(self.gains)
        self.runner: Runner | None = None
        self.thread: threading.Thread | None = None
        self.error: str | None = None
        self.connected: str | None = None
        self.meter_values = [0.0, 0.0, 0.0]
        self.slider_vars: dict[str, tuple[tk.DoubleVar, tk.Label, int, str]] = {}
        self.sliders: dict[str, Slider] = {}
        self.enabled_vars: dict[str, tk.BooleanVar] = {}
        self.effect_labels: dict[str, tk.Label] = {}
        self.route_pickers: dict[str, Segmented] = {}
        self.save_state = tk.StringVar()  # sliders update it while being built
        self.closed = False
        self.options = load_app_options()
        self.watcher = GameWatcher()  # only while this window is open
        self.last_game: str | None = None
        # set by background threads, picked up by _poll
        self.update_release: updater.Release | None = None
        self.update_progress: float | None = None
        self.update_ready: Path | None = None
        self.update_error: str | None = None

        title = f"ATS 햅틱 {__version__} - APEX 6 Pro" + (" (dry-run)" if dry_run else "")
        root.title(title + (" · 게임 종료 시 닫힘" if exit_with_game else ""))
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        threading.Thread(target=self._check_update, daemon=True).start()
        self._poll()

    # -- layout -----------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)

        # header: name on the left, plugin/update on the right
        header = tk.Frame(root, bg=BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 8))
        tk.Label(header, text="ATS 햅틱", bg=BG, fg=TEXT, font=font(16, "bold")).pack(side="left")
        subtitle = f"APEX 6 Pro · v{__version__}" + (" · dry-run" if self.dry_run else "")
        tk.Label(header, text=subtitle, bg=BG, fg=MUTED, font=font(9)).pack(side="left", padx=(10, 0), pady=(6, 0))
        Button(header, "텔레메트리 플러그인", self.open_plugin_dialog, size=9, height=30).pack(side="right")
        self.update_button = Button(header, "", self.update, kind="primary", size=9, height=30)  # packed once found

        # control strip: what drives the vibration, start/stop, auto start, status
        control = card(root, "출력 제어")
        control.outer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18)
        control.head.pack_forget()
        control.pack_configure(pady=12)
        tk.Label(control, text="입력", bg=CARD, fg=MUTED, font=font(9)).pack(side="left", padx=(0, 8))
        self.source = tk.StringVar(value="game")
        self.source_picker = Segmented(control, (("game", "ATS/ETS2 게임", ACCENT), ("sim", "테스트 주행", ACCENT)),
                                       self._on_source, segment_width=104, height=32, size=10)
        self.source_picker.pack(side="left")
        self.source.trace_add("write", lambda *_: self._show_run_state())
        self.run_button = Button(control, "▶  시작", self.toggle_run, kind="go", size=11, height=36,
                                 min_width=120, bg=CARD)
        self.run_button.pack(side="left", padx=(14, 18))
        self.auto_start = tk.BooleanVar(value=self.options.auto_start_on_game)
        Switch(control, self.auto_start, self._on_auto_start, color=ACCENT).pack(side="left")
        tk.Label(control, text="게임 감지 시 자동 시작", bg=CARD, fg=TEXT).pack(side="left", padx=(8, 0))
        # Status sits on the right of the strip and wraps, so long messages are not cut off.
        self.status = tk.StringVar(value="정지됨")
        tk.Label(control, textvariable=self.status, bg=CARD, fg=TEXT, anchor="e", justify="right",
                 wraplength=380).pack(side="right")
        self.status_dot = tk.Canvas(control, width=12, height=12, bg=CARD, highlightthickness=0)
        self.status_dot.pack(side="right", padx=(0, 8))
        self._show_dot(FAINT)

        # footer first: _changed (run while the sliders are built) styles the save button
        footer = tk.Frame(root, bg=BG)
        footer.grid(row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(10, 14))
        self.save_button = Button(footer, "저장", self.save, size=10, min_width=84)
        self.save_button.pack(side="left")
        Button(footer, "기본값", self.reset, size=10, min_width=84).pack(side="left", padx=(8, 0))
        self.save_label = tk.Label(footer, textvariable=self.save_state, bg=BG, fg=WARN, font=font(9))
        self.save_label.pack(side="left", padx=12)
        self.timing = tk.StringVar()
        tk.Label(footer, textvariable=self.timing, bg=BG, fg=FAINT, font=font(9)).pack(side="right")

        left = tk.Frame(root, bg=BG)
        left.grid(row=2, column=0, sticky="nsew", padx=(18, 6), pady=(12, 0))
        effects = card(left, "주행 효과", "달리는 동안 계속 나는 진동")
        effects.outer.pack(fill="x")
        self._route_header(effects)
        for row, spec in enumerate(EFFECT_SLIDERS, start=1):
            self._slider(effects, row, *spec)
        events = card(left, "이벤트 효과", "순간적으로 한 번 나는 진동")
        events.outer.pack(fill="x", pady=(12, 0))
        self._route_header(events)
        for row, spec in enumerate(EVENT_SLIDERS, start=1):
            self._slider(events, row, *spec)

        right = tk.Frame(root, bg=BG)
        right.grid(row=2, column=1, sticky="nsew", padx=(6, 18), pady=(12, 0))

        output = card(right, "출력 세기")
        output.outer.pack(fill="x")
        for row, spec in enumerate(OUTPUT_SLIDERS):
            self._slider(output, row, *spec)
        tk.Frame(output, bg="#252a34", height=1).grid(row=len(OUTPUT_SLIDERS), column=0, columnspan=4,
                                                     sticky="ew", pady=8)
        self.meters = []
        for row, (label, color) in enumerate((("왼쪽 그립", GRIP_COLOR), ("오른쪽 그립", GRIP_COLOR),
                                              ("트리거", TRIGGER_COLOR)), start=len(OUTPUT_SLIDERS) + 1):
            tk.Label(output, text=label, bg=CARD, fg=MUTED, width=14, anchor="w").grid(row=row, column=1, sticky="w")
            meter = Meter(output, color, length=200)
            meter.grid(row=row, column=2, padx=6, pady=4)
            self.meters.append(meter)

        info = card(right, "텔레메트리")
        info.outer.pack(fill="x", pady=(12, 0))
        self.info = {}
        tiles = tk.Frame(info, bg=CARD)
        tiles.pack(fill="x")
        for column, (key, label) in enumerate((("speed", "속도"), ("rpm", "RPM"), ("gear", "기어"))):
            tiles.columnconfigure(column, weight=1, uniform="tile")
            tile = tk.Frame(tiles, bg=TILE)
            tile.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
            tk.Label(tile, text=label, bg=TILE, fg=MUTED, font=font(8)).pack(anchor="w", padx=10, pady=(6, 0))
            var = tk.StringVar(value="-")
            tk.Label(tile, textvariable=var, bg=TILE, fg=TEXT, font=font(15, "bold")).pack(anchor="w", padx=10,
                                                                                          pady=(0, 6))
            self.info[key] = var
        rows = tk.Frame(info, bg=CARD)
        rows.pack(fill="x", pady=(8, 0))
        for row, (key, label) in enumerate((("pedal", "페달"), ("surface", "노면"), ("trigger", "트리거"),
                                            ("accel", "가속도 최대"))):
            tk.Label(rows, text=label, bg=CARD, fg=MUTED, width=11, anchor="w").grid(row=row, column=0, sticky="w")
            var = tk.StringVar(value="-")
            tk.Label(rows, textvariable=var, bg=CARD, fg=TEXT, anchor="w").grid(row=row, column=1, sticky="w")
            self.info[key] = var

        thresholds = card(right, "감지 기준")
        thresholds.outer.pack(fill="x", pady=(12, 0))
        for row, spec in enumerate(THRESHOLD_SLIDERS):
            self._slider(thresholds, row, *spec)
        tk.Label(thresholds, text="엔진 기통 수", bg=CARD, fg=TEXT, width=14, anchor="w").grid(
            row=len(THRESHOLD_SLIDERS), column=1, sticky="w")
        self.cylinders = tk.IntVar(value=self.gains.cylinders)
        ttk.Spinbox(thresholds, from_=4, to=12, increment=2, width=5, textvariable=self.cylinders,
                    command=self._set_cylinders).grid(row=len(THRESHOLD_SLIDERS), column=2, sticky="w",
                                                      padx=6, pady=(4, 0))

        log = card(right, "이벤트")
        log.outer.pack(fill="both", expand=True, pady=(12, 0))
        self.events = tk.Listbox(log, height=6, width=40, bg=TILE, fg=TEXT, bd=0, highlightthickness=0,
                                 selectbackground="#2e3441", selectforeground=TEXT, activestyle="none")
        self.events.pack(fill="both", expand=True)

    def _slider(self, parent: tk.Frame, row: int, key: str, label: str,
                low: int, high: int, scale: int, unit: str) -> None:
        if key in TOGGLEABLE:
            enabled = tk.BooleanVar(value=key not in self.gains.disabled)
            Switch(parent, enabled, lambda k=key: self._on_toggle(k)).grid(row=row, column=0, padx=(0, 10))
            self.enabled_vars[key] = enabled
        name = tk.Label(parent, text=label, bg=CARD, fg=TEXT, width=14, anchor="w")
        name.grid(row=row, column=1, sticky="w")
        var = tk.DoubleVar(value=getattr(self.gains, key) * scale)
        value_label = tk.Label(parent, bg=CARD, fg=TEXT, width=7, anchor="e", font=font(9, "bold"))
        slider = Slider(parent, var, low, high, lambda k=key: self._on_slider(k),
                        color=LEVEL_COLORS.get(key, ACCENT), length=200)
        slider.grid(row=row, column=2, padx=6, pady=4)
        value_label.grid(row=row, column=3, sticky="e", padx=(0, 4))
        self.slider_vars[key] = (var, value_label, scale, unit)
        self.sliders[key] = slider
        if key in TOGGLEABLE:
            self.effect_labels[key] = name
            picker = Segmented(parent, ROUTE_OPTIONS, lambda route, k=key: self._on_route(k, route))
            picker.grid(row=row, column=4, padx=(10, 0), pady=3)
            self.route_pickers[key] = picker
            self._show_effect(key)
        self._on_slider(key)

    def _route_header(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="진동 위치", bg=CARD, fg=FAINT, font=font(8)).grid(row=0, column=4, padx=(10, 0))

    def _show_dot(self, color: str) -> None:
        self.status_dot.delete("all")
        self.status_dot.create_oval(1, 1, 11, 11, fill=color, outline="")

    # -- controls -------------------------------------------------------------

    def _changed(self) -> None:
        if self.runner:
            self.runner.apply(self.gains)
        dirty = asdict(self.gains) != self.saved
        self.save_state.set("변경됨 · 저장 안 됨" if dirty else "")
        self.save_label.config(fg=WARN)
        self.save_button.set(kind="primary" if dirty else "ghost")

    def _on_slider(self, key: str) -> None:
        var, label, scale, unit = self.slider_vars[key]
        value = round(var.get())
        setattr(self.gains, key, value / scale)
        label.config(text=f"{value}{unit}" if unit == "%" else f"{value} {unit}")
        self._changed()

    def _on_toggle(self, key: str) -> None:
        disabled = [name for name in self.gains.disabled if name != key]
        if not self.enabled_vars[key].get():
            disabled.append(key)
        self.gains.disabled = sorted(disabled)
        self._show_effect(key)
        self._changed()

    def _show_effect(self, key: str) -> None:
        """Pick the effect's route and colour its slider; greyed while it is off."""
        route = route_of(self.gains, key)
        off = key in self.gains.disabled
        self.route_pickers[key].set(route, dim=off)
        self.sliders[key].set_color(FAINT if off else ROUTE_COLORS[route])
        self.effect_labels[key].config(fg=MUTED if off else TEXT)

    def _on_route(self, key: str, route: str) -> None:
        # Picking a route on an effect that is off turns it back on there.
        self.gains.routes = {**self.gains.routes, key: route}
        self.enabled_vars[key].set(True)
        self._on_toggle(key)

    def _on_source(self, source: str) -> None:
        self.source.set(source)

    def open_plugin_dialog(self) -> None:
        PluginDialog(self.root)

    def check_plugins(self) -> None:
        """At startup: open the plugin dialog if no installed game can feed telemetry."""
        games = plugin_install.find_games()
        if games and not any(game.compatible for game in games):
            self.status.set("호환되는 텔레메트리 플러그인이 없습니다 → '텔레메트리 플러그인'")
            self.open_plugin_dialog()

    def _on_auto_start(self) -> None:
        # An app option, not a tuning value: saved right away.
        self.options.auto_start_on_game = self.auto_start.get()
        save_app_options(self.options)
        self.last_game = None  # a game that is already running counts as newly detected

    def _set_cylinders(self) -> None:
        self.gains.cylinders = int(self.cylinders.get())
        self._changed()

    def save(self) -> None:
        save_gains(self.gains)
        self.saved = asdict(self.gains)
        self._changed()
        self.save_state.set("✓ 저장됨")
        self.save_label.config(fg=GO)

    def reset(self) -> None:
        defaults = Gains()
        for key, (var, _, scale, _) in self.slider_vars.items():
            var.set(getattr(defaults, key) * scale)
            self._on_slider(key)
        for var in self.enabled_vars.values():
            var.set(True)
        self.gains.disabled = []
        self.gains.routes = dict(DEFAULT_ROUTES)
        for key in self.route_pickers:
            self._show_effect(key)
        self.cylinders.set(defaults.cylinders)
        self._set_cylinders()

    def toggle_run(self) -> None:
        if self.thread and self.thread.is_alive():
            self.stop()
        else:
            self.start()

    def _show_run_state(self) -> None:
        running = bool(self.thread and self.thread.is_alive())
        if not running:
            self.run_button.set("▶  시작", kind="go", state="normal")
        elif self.runner and self.runner.stop_requested:
            self.run_button.set("정지 중…", kind="ghost", state="disabled")
        else:
            self.run_button.set("■  정지", kind="danger", state="normal")
        self.source_picker.set(self.source.get(), locked=running)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.error = None
        self.connected = None
        self.events.delete(0, "end")
        self.runner = Runner(self.gains, simulate=self.source.get() == "sim")
        self.thread = threading.Thread(target=self._worker, args=(self.runner,), daemon=True)
        self.thread.start()
        self._show_run_state()

    def _worker(self, runner: Runner) -> None:
        try:
            run(runner, dry_run=self.dry_run, on_connected=self._on_connected,
                exit_with_game=self.exit_with_game)
        except Exception as exc:  # shown in the status line
            self.error = str(exc)
        finally:
            runner.close()

    def _on_connected(self, name: str | None) -> None:
        self.connected = name
        stats = self.runner.last_stats if self.runner else None
        if name is None and stats:
            self.timing.set(f"지난 세션: 지연 프레임 {stats.late_frames} / 최대 {stats.max_lateness_ms:.1f} ms")

    def stop(self) -> None:
        if self.runner:
            self.runner.stop_requested = True
            self._show_run_state()

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
        self.watcher.close()
        self.closed = True
        self.root.destroy()

    # -- updates --------------------------------------------------------------

    def _check_update(self) -> None:
        try:
            self.update_release = updater.newer_release()
        except Exception:
            pass  # offline or rate limited: try again next start

    def update(self) -> None:
        release = self.update_release
        notes = release.notes[:600] + ("…" if len(release.notes) > 600 else "")
        text = f"새 버전 {release.version}이 있습니다 (현재 {__version__}).\n\n{notes}".rstrip()
        if not updater.can_self_update(release):
            if messagebox.askyesno("업데이트", text + "\n\n릴리스 페이지를 열까요?"):
                webbrowser.open(release.page)
            return
        if self.watcher.game:
            messagebox.showinfo("업데이트", "게임이 실행 중입니다. 게임을 끝낸 뒤 업데이트하세요.")
            return
        if not messagebox.askyesno("업데이트", text + "\n\n내려받아 설치하고 프로그램을 다시 시작할까요?"):
            return
        self.update_button.set(state="disabled")
        self.update_progress = 0.0
        threading.Thread(target=self._download_update, args=(release,), daemon=True).start()

    def _download_update(self, release: updater.Release) -> None:
        try:
            self.update_ready = updater.download(release, lambda done: setattr(self, "update_progress", done))
        except Exception as exc:
            self.update_error = str(exc)

    def _poll_update(self) -> None:
        release = self.update_release
        if release and not self.update_button.winfo_ismapped():
            self.update_button.set(text=f"업데이트 {release.version}")
            self.update_button.pack(side="right", padx=8)
        if self.update_error:
            messagebox.showerror("업데이트", f"업데이트하지 못했습니다: {self.update_error}")
            self.update_error = self.update_progress = None
            self.update_button.set(state="normal", text=f"업데이트 {release.version}")
        elif self.update_ready:
            source, self.update_ready = self.update_ready, None
            self.update_button.set(text="설치 중…")
            self.on_close()
            if self.closed:
                updater.apply_on_exit(source)  # copies once this process has exited
            else:  # save prompt cancelled
                self.update_progress = None
                self.update_button.set(state="normal", text=f"업데이트 {release.version}")
        elif self.update_progress is not None:
            self.update_button.set(text=f"내려받는 중 {self.update_progress:.0%}")

    # -- periodic UI refresh ------------------------------------------------------

    def _poll(self) -> None:
        self._poll_update()
        if self.closed:
            return
        running = bool(self.thread and self.thread.is_alive())
        # Auto start on the game appearing, not on every poll, so a manual
        # 정지 holds until the next launch.
        game = self.watcher.game
        if game and not self.last_game and self.auto_start.get() and not running:
            self.source.set("game")
            self.start()
            running = True
        self.last_game = game
        runner = self.runner
        self._show_run_state()
        if running and runner:
            device = self.connected or ("컨트롤러 없음" if self.dry_run else "컨트롤러 미사용")
            self.status.set(f"{device} · {runner.state or '시작 중'}")
            self._show_dot(GO if self.connected or self.dry_run else WARN)
            self._refresh(runner)
        else:
            if self.error:
                self.status.set(f"오류: {self.error}")
                self._show_dot(STOP)
            else:
                if runner:
                    self.status.set("정지됨")
                self._show_dot(FAINT)
            self.meter_values = [0.0, 0.0, 0.0]
            for meter in self.meters:
                meter.set(0.0)
            if runner and runner.game_ended and self.exit_with_game:
                self.exit_with_game = False  # ask at most once if the save prompt is cancelled
                self.on_close()
                if self.closed:
                    return
        self.root.after(POLL_MS, self._poll)

    def _refresh(self, runner: Runner) -> None:
        for i, peak in enumerate(runner.take_peaks()):
            self.meter_values[i] = max(peak, self.meter_values[i] * METER_DECAY)
            self.meters[i].set(self.meter_values[i])
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


class PluginDialog:
    """Lists ATS/ETS2 installs with their telemetry plugin and installs the bundled one."""

    def __init__(self, parent: tk.Tk) -> None:
        self.top = tk.Toplevel(parent, bg=BG)
        self.top.title("텔레메트리 플러그인")
        self.top.resizable(False, False)
        self.top.transient(parent)
        dark_title_bar(self.top)
        self.games = plugin_install.find_games()
        self.body = ttk.Frame(self.top, padding=14)
        self.body.pack(fill="both")
        self._render()

    def _render(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()
        ttk.Label(self.body, text="진동은 게임의 텔레메트리 플러그인(RenCloud 계열 rev 12) 데이터를 사용합니다.",
                  ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 8))
        if not self.games:
            ttk.Label(self.body, text="Steam에서 ATS/ETS2 설치를 찾지 못했습니다. 게임 폴더를 직접 선택하세요.",
                      ).grid(row=1, column=0, columnspan=3, sticky="w")
        for row, game in enumerate(self.games, start=1):
            ttk.Label(self.body, text=game.name, width=24).grid(row=row, column=0, sticky="w")
            ttk.Label(self.body, text=game.describe(), width=34).grid(row=row, column=1, sticky="w", padx=8)
            text = "재설치" if game.compatible else "설치"
            ttk.Button(self.body, text=text, command=lambda g=game: self._install(g)).grid(row=row, column=2, pady=2)
        bottom = ttk.Frame(self.body)
        bottom.grid(row=len(self.games) + 2, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(bottom, text="게임 폴더 직접 선택…", command=self._pick_folder).pack(side="left")
        ttk.Button(bottom, text="닫기", command=self.top.destroy).pack(side="right")

    def _pick_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self.top, title="ATS 또는 ETS2 설치 폴더")
        if not folder:
            return
        game = plugin_install.game_from_folder(Path(folder))
        if game is None:
            messagebox.showerror("플러그인", "bin\\win_x64\\amtrucks.exe 또는 eurotrucks2.exe가 있는 "
                                 "게임 폴더를 선택하세요.", parent=self.top)
            return
        self.games = [g for g in self.games if g.key != game.key] + [game]
        self._render()

    def _install(self, game: plugin_install.GameInstall) -> None:
        if not messagebox.askokcancel(
                "플러그인 설치",
                f"{game.name}의 plugins 폴더에 RenCloud scs-telemetry.dll(v1.12.1, MIT)을 복사합니다.\n\n"
                f"{game.plugins_dir}\n\n게임이 실행 중이면 다음 실행부터 적용됩니다.", parent=self.top):
            return
        try:
            target = plugin_install.install(game)
        except PermissionError:
            messagebox.showerror("플러그인 설치", "폴더에 쓸 권한이 없습니다. 프로그램을 관리자 권한으로 실행하거나 "
                                 f"아래 파일을 직접 복사하세요.\n\n{plugin_install.bundled_plugin()}\n→ {game.plugins_dir}",
                                 parent=self.top)
            return
        except OSError as exc:
            messagebox.showerror("플러그인 설치", f"복사하지 못했습니다: {exc}", parent=self.top)
            return
        messagebox.showinfo("플러그인 설치", f"설치했습니다.\n{target}", parent=self.top)
        self._render()


def _claim_single_instance() -> object | None:
    """Hold a named mutex; None if another window already holds it."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="run without the controller")
    parser.add_argument("--autostart", action="store_true", help="start the game session immediately")
    parser.add_argument("--exit-with-game", action="store_true", help="close the window when the game quits")
    parser.add_argument("--minimized", action="store_true", help="open minimized")
    args = parser.parse_args(argv)
    if _claim_single_instance() is None:
        return
    root = tk.Tk()
    apply_theme(root)
    app = App(root, dry_run=args.dry_run, exit_with_game=args.exit_with_game)
    dark_title_bar(root)
    if args.autostart:
        app.start()
    # Started via WMI (app.launch), a fixed-size Tk window shrinks to its
    # title bar when restored from minimized; a minimum size keeps it whole.
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
    if args.minimized:
        root.iconify()
    else:
        root.after(300, app.check_plugins)
    root.mainloop()


if __name__ == "__main__":
    main()
