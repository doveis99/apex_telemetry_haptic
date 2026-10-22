"""Live tuning window for the ATS -> APEX 6 Pro haptics.

  pythonw gui.py            normal use (or double-click start_gui.cmd)
  pythonw gui.py --autostart --exit-with-game --minimized
                            how launch.py starts it alongside the game
  python gui.py --dry-run   no controller, for testing the window

Sliders, on/off boxes and the grip/trigger route boxes change the running
effects immediately; 저장 writes config.json, which ats_haptics.py also
reads. Only one window runs at a time; a second launch exits quietly. At
start it looks for a newer GitHub release and offers it in the top bar.
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
ROUTE_LABELS = {GRIP: "그립", TRIGGER: "트리거", BOTH: "둘 다"}
THRESHOLD_SLIDERS = (
    ("bump_threshold", "요철 감지 기준", 5, 100, 100, "cm/s"),
    ("impact_threshold", "충돌 감지 기준", 4, 40, 1, "m/s²"),
)


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
        self.slider_vars: dict[str, tuple[tk.DoubleVar, ttk.Label, int, str]] = {}
        self.enabled_vars: dict[str, tk.BooleanVar] = {}
        self.route_vars: dict[str, tk.StringVar] = {}
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
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self.root)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", **pad)
        self.source = tk.StringVar(value="game")
        ttk.Radiobutton(top, text="ATS/ETS2 게임", variable=self.source, value="game").pack(side="left")
        ttk.Radiobutton(top, text="테스트 주행", variable=self.source, value="sim").pack(side="left", padx=(6, 12))
        self.start_button = ttk.Button(top, text="시작", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(top, text="정지", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=4)
        self.auto_start = tk.BooleanVar(value=self.options.auto_start_on_game)
        ttk.Checkbutton(top, text="게임 감지 시 자동 시작", variable=self.auto_start,
                        command=self._on_auto_start).pack(side="left", padx=(16, 0))
        ttk.Button(top, text="텔레메트리 플러그인…", command=self.open_plugin_dialog).pack(side="right")
        self.update_button = ttk.Button(top, command=self.update)  # packed once a release is found
        # Status lives in its own bar at the bottom so long messages are not cut off.
        self.status = tk.StringVar(value="정지됨")

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

        output = ttk.LabelFrame(right, text="출력 세기")
        output.pack(fill="x")
        for row, spec in enumerate(OUTPUT_SLIDERS):
            self._slider(output, row, *spec)
        ttk.Separator(output).grid(row=len(OUTPUT_SLIDERS), column=0, columnspan=4, sticky="ew", pady=4)
        self.meters = []
        for row, label in enumerate(("왼쪽 그립", "오른쪽 그립", "트리거"), start=len(OUTPUT_SLIDERS) + 1):
            ttk.Label(output, text=label, width=15).grid(row=row, column=1, sticky="w", padx=6, pady=2)
            bar = ttk.Progressbar(output, length=220, maximum=100)
            bar.grid(row=row, column=2, padx=6, pady=2)
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
        ttk.Label(thresholds, text="엔진 기통 수", width=15).grid(row=len(THRESHOLD_SLIDERS), column=1,
                                                              sticky="w", padx=6)
        self.cylinders = tk.IntVar(value=self.gains.cylinders)
        ttk.Spinbox(thresholds, from_=4, to=12, increment=2, width=5, textvariable=self.cylinders,
                    command=self._set_cylinders).grid(row=len(THRESHOLD_SLIDERS), column=2, sticky="w", padx=6)

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

        status_bar = ttk.Frame(self.root, relief="sunken", padding=(6, 3))
        status_bar.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Label(status_bar, textvariable=self.status, anchor="w", wraplength=1100).pack(fill="x")

    def _slider(self, parent: ttk.Frame, row: int, key: str, label: str,
                low: int, high: int, scale: int, unit: str) -> None:
        if key in TOGGLEABLE:
            enabled = tk.BooleanVar(value=key not in self.gains.disabled)
            ttk.Checkbutton(parent, variable=enabled,
                            command=lambda k=key: self._on_toggle(k)).grid(row=row, column=0, padx=(6, 0))
            self.enabled_vars[key] = enabled
        ttk.Label(parent, text=label, width=15).grid(row=row, column=1, sticky="w", padx=6)
        var = tk.DoubleVar(value=getattr(self.gains, key) * scale)
        value_label = ttk.Label(parent, width=9)
        ttk.Scale(parent, from_=low, to=high, length=220, variable=var,
                  command=lambda _=None, k=key: self._on_slider(k)).grid(row=row, column=2, padx=6, pady=2)
        value_label.grid(row=row, column=3, sticky="w")
        if key in TOGGLEABLE:
            route = tk.StringVar(value=ROUTE_LABELS[route_of(self.gains, key)])
            box = ttk.Combobox(parent, textvariable=route, values=list(ROUTE_LABELS.values()),
                               state="readonly", width=6)
            box.grid(row=row, column=4, padx=(0, 6))
            box.bind("<<ComboboxSelected>>", lambda _=None, k=key: self._on_route(k))
            self.route_vars[key] = route
        self.slider_vars[key] = (var, value_label, scale, unit)
        self._on_slider(key)

    # -- controls -------------------------------------------------------------

    def _changed(self) -> None:
        if self.runner:
            self.runner.apply(self.gains)
        self.save_state.set("" if asdict(self.gains) == self.saved else "변경됨 (저장 안 됨)")

    def _on_slider(self, key: str) -> None:
        var, label, scale, unit = self.slider_vars[key]
        value = round(var.get())
        setattr(self.gains, key, value / scale)
        label.config(text=f"{value} {unit}")
        self._changed()

    def _on_toggle(self, key: str) -> None:
        disabled = [name for name in self.gains.disabled if name != key]
        if not self.enabled_vars[key].get():
            disabled.append(key)
        self.gains.disabled = sorted(disabled)
        self._changed()

    def _on_route(self, key: str) -> None:
        label = self.route_vars[key].get()
        route = next(r for r, text in ROUTE_LABELS.items() if text == label)
        self.gains.routes = {**self.gains.routes, key: route}
        self._changed()

    def open_plugin_dialog(self) -> None:
        PluginDialog(self.root)

    def check_plugins(self) -> None:
        """At startup: open the plugin dialog if no installed game can feed telemetry."""
        games = plugin_install.find_games()
        if games and not any(game.compatible for game in games):
            self.status.set("호환되는 텔레메트리 플러그인이 없습니다 → '텔레메트리 플러그인…'")
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
        self.save_state.set("저장됨")

    def reset(self) -> None:
        defaults = Gains()
        for key, (var, _, scale, _) in self.slider_vars.items():
            var.set(getattr(defaults, key) * scale)
            self._on_slider(key)
        for var in self.enabled_vars.values():
            var.set(True)
        self.gains.disabled = []
        for key, var in self.route_vars.items():
            var.set(ROUTE_LABELS[DEFAULT_ROUTES[key]])
        self.gains.routes = dict(DEFAULT_ROUTES)
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
        self.update_button.config(state="disabled")
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
            self.update_button.config(text=f"업데이트 {release.version}")
            self.update_button.pack(side="right", padx=4)
        if self.update_error:
            messagebox.showerror("업데이트", f"업데이트하지 못했습니다: {self.update_error}")
            self.update_error = self.update_progress = None
            self.update_button.config(state="normal", text=f"업데이트 {release.version}")
        elif self.update_ready:
            source, self.update_ready = self.update_ready, None
            self.update_button.config(text="설치 중…")
            self.on_close()
            if self.closed:
                updater.apply_on_exit(source)  # copies once this process has exited
            else:  # save prompt cancelled
                self.update_progress = None
                self.update_button.config(state="normal", text=f"업데이트 {release.version}")
        elif self.update_progress is not None:
            self.update_button.config(text=f"내려받는 중 {self.update_progress:.0%}")

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
        if running and runner:
            device = self.connected or ("컨트롤러 없음" if self.dry_run else "컨트롤러 미사용")
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
            if runner and runner.game_ended and self.exit_with_game:
                self.exit_with_game = False  # ask at most once if the save prompt is cancelled
                self.on_close()
                if self.closed:
                    return
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


class PluginDialog:
    """Lists ATS/ETS2 installs with their telemetry plugin and installs the bundled one."""

    def __init__(self, parent: tk.Tk) -> None:
        self.top = tk.Toplevel(parent)
        self.top.title("텔레메트리 플러그인")
        self.top.resizable(False, False)
        self.top.transient(parent)
        self.games = plugin_install.find_games()
        self.body = ttk.Frame(self.top, padding=10)
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
    root.option_add("*Font", ("Malgun Gothic", 10))
    app = App(root, dry_run=args.dry_run, exit_with_game=args.exit_with_game)
    if args.autostart:
        app.start()
    if args.minimized:
        root.iconify()
    else:
        root.after(300, app.check_plugins)
    root.mainloop()


if __name__ == "__main__":
    main()
