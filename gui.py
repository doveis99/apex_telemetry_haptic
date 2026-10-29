"""Live tuning window for the ATS -> APEX 6 Pro haptics.

  pythonw gui.py            normal use (or double-click start_gui.cmd)
  pythonw gui.py --autostart --exit-with-game --minimized
                            how launch.py starts it alongside the game
  python gui.py --dry-run   no controller, for testing the window

Sliders, on/off switches and the 그립 | 트리거 | 둘 다 selectors change the
running effects immediately; 저장 writes config.json, which ats_haptics.py also
reads. Only one window runs at a time; a second launch exits quietly, and
if it came from the Steam launch option the open window starts the game
session instead. At start it looks for a newer GitHub release and offers it
in the top bar. Unexpected errors land in logs\\error.log (errorlog.py).
The ▶ beside each effect plays it alone for a few seconds (preview.py).
The preset bar under it switches between named tunings (a truck, the car of
Trip mode) with one click; 저장 then writes into the preset in use.
Every control explains itself in a tooltip. The language menu in the top
bar rebuilds the window in another language (i18n.py) and remembers it.
The widgets and dark theme live in widgets.py.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
import webbrowser
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import calibrate
import i18n
import plugin_install
import updater
from apex6 import Apex6, DeviceUnavailable, StreamStats
from ats_haptics import Runner, run
from blocklog import LOG_DIR
from errorlog import log_exception
from i18n import N_, t
from version import __version__
from effects import BOTH, DEFAULT_ROUTES, GRIP, TOGGLEABLE, TRIGGER, Equalizer, Gains, route_of
from gamewatch import GameWatcher
from preview import Player, Preview
from settings import (delete_preset, load_app_options, load_eq, load_gains, load_presets, save_app_options, save_eq,
                      save_gains, save_preset)
from widgets import (ACCENT, BG, BOTH_COLOR, CARD, FAINT, FIELD_HI, GO, GRIP_COLOR, MUTED, STOP, TEXT, TILE,
                     TRIGGER_COLOR, WARN, Button, Link, Meter, Segmented, Slider, Switch, apply_theme, card,
                     dark_title_bar, font, make_dpi_aware, px, tip)

POLL_MS = 100
METER_DECAY = 0.8
INSTANCE_MUTEX = "Local\\ApexAtsHapticsGui"
LAUNCH_EVENT = "Local\\ApexAtsHapticsGuiLaunch"

# key, label, slider min, slider max, slider units per gain unit, unit text
OUTPUT_SLIDERS = (
    ("master", N_("전체"), 0, 150, 100, "%"),
    ("grip_level", N_("그립"), 0, 150, 100, "%"),
    ("trigger_level", N_("트리거"), 0, 150, 100, "%"),
)
EFFECT_SLIDERS = (
    ("engine", N_("엔진"), 0, 200, 100, "%"),
    ("road", N_("노면"), 0, 200, 100, "%"),
    ("bumps", N_("요철"), 0, 200, 100, "%"),
    ("rumble", N_("차선 요철"), 0, 200, 100, "%"),
    ("wheelspin", N_("휠스핀"), 0, 200, 100, "%"),
    ("gear", N_("변속"), 0, 200, 100, "%"),
    ("retarder", N_("리타더"), 0, 200, 100, "%"),
    ("impact", N_("충돌"), 0, 200, 100, "%"),
    ("trigger", N_("엑셀/브레이크"), 0, 100, 100, "%"),
    ("throttle_pulse", N_("└ 엔진 박자"), 0, 100, 100, "%"),
    ("steady_fade", N_("정속 시 줄임"), 0, 100, 100, "%"),
)
EVENT_SLIDERS = (
    ("pedal", N_("페달 바닥"), 0, 200, 100, "%"),
    ("squeal", N_("브레이크 스퀼"), 0, 200, 100, "%"),
    ("trailer", N_("트레일러 연결"), 0, 200, 100, "%"),
    ("park_brake", N_("파킹 브레이크"), 0, 200, 100, "%"),
    ("engine_start", N_("시동 켜기/끄기"), 0, 200, 100, "%"),
    ("shifter", N_("레인지/스플리터"), 0, 200, 100, "%"),
    # what a driver only hears, last and softer
    ("air_brake", N_("에어 브레이크"), 0, 200, 100, "%"),
    ("blinker", N_("방향지시등"), 0, 200, 100, "%"),
    ("game_events", N_("게임 알림"), 0, 200, 100, "%"),
    ("ducking", N_("주행 진동 양보"), 0, 100, 100, "%"),
)
# settings for every effect of their card, set off below a line: not one effect's sub-setting
CARD_WIDE = ("steady_fade", "ducking")
EFFECT_NAMES = {key: label for key, label, *_ in EFFECT_SLIDERS + EVENT_SLIDERS}
THRESHOLD_SLIDERS = (
    ("bump_threshold", N_("요철 감지 기준"), 5, 100, 100, "cm/s"),
    ("impact_threshold", N_("충돌 감지 기준"), 4, 40, 1, "m/s²"),
)
SLIDER_TIPS = {
    "master": N_("컨트롤러로 나가는 최종 세기입니다. 효과별 세기를 모두 섞은 다음 그립과 트리거 모두에 적용됩니다. "
                 "100%를 넘기면 약한 효과는 세지고 이미 센 효과는 거의 그대로입니다."),
    "grip_level": N_("좌우 그립 모터에만 적용되는 최종 세기입니다."),
    "trigger_level": N_("트리거 모터에만 적용되는 최종 세기입니다. 0%면 트리거 진동이 완전히 꺼집니다. "
                        "손가락이 부담스러우면 여기서 낮추세요. 효과별 비율은 그대로 유지됩니다."),
    "engine": N_("엔진 점화 주파수(rpm ÷ 60 × 기통 수 ÷ 2)에 2배음을 더한 진동입니다. "
                 "엑셀을 밟을수록, rpm이 높을수록 세집니다. 공회전에서는 실린더마다 폭발이 고르지 않아 울렁이고, "
                 "부하가 크면 거칠어집니다. 시동이 꺼져 있으면 없습니다."),
    "road": N_("노면 재질(얼음 < 아스팔트 < 콘크리트 < 눈 < 나무 < 금속 < 잔디 < 흙 < 자갈)과 속도로 세기와 질감이 정해집니다. "
               "얼음·금속·잔디는 높고 곱게, 나무·눈은 낮고 묵직하게 떨리고, 자갈·흙·눈에는 돌이 튀는 '탁탁'이 섞입니다. "
               "실제 노면처럼 1초에 몇 번씩 출렁이고, 험한 길에서는 캡이 덜컹거립니다. "
               "빠를수록 촘촘해지고, 왼쪽과 오른쪽 바퀴가 따로 갑니다."),
    "bumps": N_("포트홀, 연석, 이음매처럼 서스펜션이 갑자기 움직이면 그 바퀴 쪽 그립에 충격을 냅니다. "
                "작은 이음매는 짧고 높은 '톡', 큰 포트홀은 낮고 묵직한 '쿵'이고, 큰 충격 뒤에는 캡이 잠깐 덜컹거립니다. "
                "민감도는 '요철 감지 기준'에서 정합니다."),
    "rumble": N_("중앙선이나 갓길의 차선 요철(럼블 스트립)을 밟은 쪽 그립에 '드르르륵'. "
                 "게임이 알려 주는 노면 재질로 감지하고, 빠를수록 촘촘해집니다."),
    "wheelspin": N_("엑셀을 밟는데 구동 바퀴가 트럭보다 빨리 헛돌면(얼음, 눈, 진흙, 급출발) 양쪽 그립에 거칠게 긁히는 "
                    "진동을 냅니다. 브레이크로 바퀴가 잠기는 휠락은 '엑셀/브레이크'에서 냅니다."),
    "gear": N_("기어가 바뀔 때 '철컥'. 주행 진동이 잠깐 비었다가 단단하게 맞물리는 느낌입니다. 자동 변속기도 동작합니다."),
    "retarder": N_("리타더 단계에 비례하는 웅웅거림으로, 빠를수록 음이 높아집니다(40~65Hz). "
                     "엔진 브레이크는 엔진 점화 박자에 맞춰 '다다닥' 노크하고, 엑셀을 밟으면 멈춥니다."),
    "impact": N_("가속도가 순간적으로 크게 튀면(충돌, 큰 충격) 강한 50Hz 펄스를 냅니다. "
                 "기준은 '충돌 감지 기준'에서 정합니다."),
    "trigger": N_("지금 밟고 있는 페달을 따라가는 트리거 진동입니다. 엑셀은 밟은 만큼 세지고, "
                  "브레이크는 바퀴가 잠기면(휠락) 끊기는 펄스를 냅니다."),
    "throttle_pulse": N_("엑셀 트리거 진동을 엔진 점화 박자에 맞춰 출렁이게 하는 정도입니다. "
                         "엔진 진동과 같은 폭발을 따라가서 공회전의 울렁임과 폭발마다 다른 세기도 느껴집니다. "
                         "0%면 일정하게 울립니다. 브레이크 쪽에는 영향이 없습니다."),
    "steady_fade": N_("엔진(rpm·부하), 노면(재질·속도), 엑셀/브레이크(페달)가 그대로 유지되면 그 진동을 수십 초에 걸쳐 "
                      "이 비율만큼 줄입니다. 변함없는 진동은 손이 곧 느끼지 못하고 피로만 쌓이기 때문입니다. "
                      "가속·감속, 변속, 노면이 바뀌거나 페달을 움직이면 바로 돌아오고, 크루즈 중 작은 속도 변화나 "
                      "손가락 떨림은 무시합니다."),
    "pedal": N_("엑셀이나 브레이크를 끝까지(97% 이상) 밟는 순간 짧은 '툭'."),
    "squeal": N_("약 14km/h 이하에서 브레이크를 밟으며 서서히 멈출 때 '끼익, 끽' 하고 끊어지며 우는 가는 진동입니다."),
    "trailer": N_("트레일러를 연결할 때 커플러가 0.6초쯤 '덜커덕덜컥' 맞물리고, 분리할 때 '쿵'에 이어 에어 빠지는 느낌."),
    "park_brake": N_("파킹 브레이크를 체결하면 '철컥'에 이어 1초 가까이 에어가 빠지는 '쉬이익', 해제하면 '턱'에 이어 짧은 '푸쉭'."),
    "air_brake": N_("브레이크 페달을 떼면 브레이크 챔버의 공기가 빠지며 '툭' 하고 짧게 '푸쉬익'. 세게 밟았을수록 크고, "
                    "잠깐 톡 친 정도로는 나지 않습니다. 브레이크를 뗄 때마다 나므로 '주행 진동 양보'로 주행 진동을 "
                    "줄이지 않습니다."),
    "blinker": N_("방향지시등이 점멸할 때마다 켜진 쪽 그립에 '틱'."),
    "engine_start": N_("시동을 켤 때 1초쯤 크랭킹하다가 '부웅' 하고 걸리고, 끌 때 잠깐 더 돌다가 '투둑투둑' 잦아듭니다."),
    "shifter": N_("H-시프터의 레인지 셀렉터나 스플리터를 조작할 때 '딸깍'에 이어 변속기가 '철컥' 넘어갑니다(스플리터는 약하게). "
                  "자동/순차 변속에서는 울리지 않습니다."),
    "game_events": N_("톨게이트, 벌금, 주유, 배송 완료, 페리, 기차 이벤트 때 '톡톡' 두 번."),
    "ducking": N_("변속과 충돌을 포함한 모든 이벤트 효과가 울리는 순간, 같은 위치의 주행 진동을 잠깐 줄여 "
                  "이벤트가 묻히지 않게 하는 정도입니다. 0%면 줄이지 않습니다."),
    "bump_threshold": N_("서스펜션이 이 속도보다 갑자기 빨리 움직이면 요철 펄스를 냅니다. "
                         "낮추면 작은 요철에도, 높이면 큰 충격에만 반응합니다."),
    "impact_threshold": N_("게임 화면 한 프레임 사이의 가속도 변화가 이 값을 넘으면 충돌 펄스를 냅니다. "
                           "평소 주행 중 '가속도 최대' 값보다 충분히 높게 두세요."),
}
METER_LABELS = (N_("왼쪽 그립"), N_("오른쪽 그립"), N_("트리거"))
CYLINDERS_LABEL = N_("엔진 기통 수")
CYLINDERS_RANGE = (4, 12)
# where an effect vibrates; each choice has its colour, which its slider takes too
ROUTE_OPTIONS = ((GRIP, N_("그립"), GRIP_COLOR), (TRIGGER, N_("트리거"), TRIGGER_COLOR),
                 (BOTH, N_("둘 다"), BOTH_COLOR))
ROUTE_COLORS = {route: color for route, _, color in ROUTE_OPTIONS}
LEVEL_COLORS = {"master": ACCENT, "grip_level": GRIP_COLOR, "trigger_level": TRIGGER_COLOR,
                "throttle_pulse": TRIGGER_COLOR}
# not translated: whoever cannot read the current language still finds the menu
LANGUAGE_TIP = "Language · 언어 · 言語 · 语言 · Sprache · Langue · Idioma · Język · Язык"


class App:
    def __init__(self, root: tk.Tk, dry_run: bool = False, exit_with_game: bool = False,
                 launch_event: int | None = None) -> None:
        self.root = root
        self.dry_run = dry_run
        self.exit_with_game = exit_with_game
        self.launch_event = launch_event  # set by a later Steam launch (see _request_launch)
        self.gains = load_gains()
        self.saved = asdict(self.gains)
        self.runner: Runner | None = None
        self.thread: threading.Thread | None = None
        self.calibration: CalibrationDialog | None = None
        self.player = Player()  # effect tests while the output is stopped
        self.tested = False  # the status line shows a test, to be put back to 정지됨
        self.error: str | None = None
        self.connected: str | None = None
        self.session_stats: StreamStats | None = None  # set by the worker when it releases the pad
        self.meter_values = [0.0, 0.0, 0.0]
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

        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build()
        threading.Thread(target=self._check_update, daemon=True).start()
        self._poll()

    # -- layout -----------------------------------------------------------------

    def _build(self) -> None:
        root = self.root
        title = f"{t('ATS 햅틱')} {__version__} - APEX 6 Pro" + (" (dry-run)" if self.dry_run else "")
        root.title(title + (" · " + t("게임 종료 시 닫힘") if self.exit_with_game else ""))
        self.slider_vars: dict[str, tuple[tk.DoubleVar, tk.Label, int, str]] = {}
        self.sliders: dict[str, Slider] = {}
        self.enabled_vars: dict[str, tk.BooleanVar] = {}
        self.effect_labels: dict[str, tk.Label] = {}
        self.route_pickers: dict[str, Segmented] = {}
        # Label columns fit the longest name in this language, the same in every card.
        names = [spec[1] for spec in OUTPUT_SLIDERS + EFFECT_SLIDERS + EVENT_SLIDERS + THRESHOLD_SLIDERS]
        measure = tkfont.Font(font=font()).measure
        self.label_width = max(measure(t(name)) for name in names + list(METER_LABELS) + [CYLINDERS_LABEL]) + px(12)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)

        # header: name, version and repository link on the left; plugin, language and update on the right
        header = tk.Frame(root, bg=BG)
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=px(18), pady=(px(14), px(8)))
        tk.Label(header, text=t("ATS 햅틱"), bg=BG, fg=TEXT, font=font(16, "bold")).pack(side="left")
        subtitle = f"APEX 6 Pro · v{__version__}" + (" · dry-run" if self.dry_run else "")
        tk.Label(header, text=subtitle, bg=BG, fg=MUTED, font=font(9)).pack(side="left", padx=(px(10), 0), pady=(px(6), 0))
        tip(Link(header, "GitHub ↗", lambda: webbrowser.open(updater.PAGE_URL)),
            t("GitHub 저장소 열기: 소스 코드, 릴리스, 문의(Issues)") + f"\n{updater.PAGE_URL}").pack(
            side="left", padx=(px(12), 0), pady=(px(6), 0))
        tip(Button(header, t("텔레메트리 플러그인"), self.open_plugin_dialog, size=9, height=30),
            t("게임에 설치된 텔레메트리 플러그인을 확인하고, 없으면 동봉한 플러그인을 설치합니다.")).pack(side="right")
        self.language_button = Button(header, f"{dict(i18n.LANGUAGES)[i18n.language()]}  ▾", self._language_menu,
                                      size=9, height=30)
        tip(self.language_button, LANGUAGE_TIP).pack(side="right", padx=(0, px(8)))
        self.update_button = Button(header, "", self.update, kind="primary", size=9, height=30)  # packed once found
        tip(self.update_button, t("새 버전의 릴리스 노트를 보고, 내려받아 설치한 뒤 다시 시작합니다. 설정은 그대로입니다."))

        # control strip: what drives the vibration, start/stop, auto start, status
        control = card(root, t("출력 제어"))
        control.outer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=px(18))
        control.head.pack_forget()
        control.pack_configure(pady=px(12))
        tk.Label(control, text=t("입력"), bg=CARD, fg=MUTED, font=font(9)).pack(side="left", padx=(0, px(8)))
        if not hasattr(self, "source"):  # kept across a language change
            self.source = tk.StringVar(value="game")
            self.source.trace_add("write", lambda *_: self._show_run_state())
        self.source_picker = Segmented(control, (("game", t("ATS/ETS2 게임"), ACCENT), ("sim", t("테스트 주행"), ACCENT)),
                                       self._on_source, segment_width=104, height=32, size=10)
        tip(self.source_picker, t("ATS/ETS2 게임: 게임 텔레메트리로 진동을 만듭니다. 게임이 실행 중일 때만 컨트롤러를 씁니다.\n"
                                  "테스트 주행: 게임 없이 60초 시나리오를 재생합니다. 효과 확인과 튜닝용입니다.\n"
                                  "실행 중에는 바꿀 수 없습니다.")).pack(side="left")
        self.run_button = Button(control, t("▶  시작"), self.toggle_run, kind="go", size=11, height=36,
                                 min_width=120, bg=CARD)
        tip(self.run_button, t("진동 출력을 시작하거나 멈춥니다. 정지하면 모터를 끄고 컨트롤러를 놓습니다.")).pack(
            side="left", padx=(px(14), px(18)))
        self.auto_start = tk.BooleanVar(value=self.options.auto_start_on_game)
        auto_tip = t("창이 정지 상태일 때 게임이 실행되면 자동으로 시작합니다. 게임 도중 직접 정지하면 "
                     "그 게임이 끝날 때까지는 다시 켜지지 않습니다. 바꾸면 바로 저장됩니다.")
        tip(Switch(control, self.auto_start, self._on_auto_start, color=ACCENT), auto_tip).pack(side="left")
        tip(tk.Label(control, text=t("게임 감지 시 자동 시작"), bg=CARD, fg=TEXT), auto_tip).pack(side="left", padx=(px(8), 0))
        # Status sits on the right of the strip and wraps, so long messages are not cut off.
        if not hasattr(self, "status"):
            self.status = tk.StringVar(value=t("정지됨"))
        status_tip = t("컨트롤러 상태 · 텔레메트리 상태\n점 색: 초록 출력 중, 노랑 컨트롤러 대기, 빨강 오류, 회색 정지")
        tip(tk.Label(control, textvariable=self.status, bg=CARD, fg=TEXT, anchor="e", justify="right",
                     wraplength=px(380)), status_tip).pack(side="right")
        self.status_dot = tk.Canvas(control, width=px(12), height=px(12), bg=CARD, highlightthickness=0)
        tip(self.status_dot, status_tip).pack(side="right", padx=(0, px(8)))
        self._show_dot(FAINT)

        # preset bar: one button per preset, the one in use lit
        self.preset_bar = tk.Frame(root, bg=BG)
        self.preset_bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=px(18), pady=(px(12), 0))
        self._build_presets()

        # footer first: _changed (run while the sliders are built) styles the save button
        footer = tk.Frame(root, bg=BG)
        footer.grid(row=4, column=0, columnspan=2, sticky="ew", padx=px(18), pady=(px(10), px(14)))
        self.save_button = Button(footer, t("저장"), self.save, size=10, min_width=84)
        tip(self.save_button, t("슬라이더, 켜기/끄기, 진동 위치를 %APPDATA%\\ATSHaptics\\config.json에 저장합니다. "
                                "프리셋을 쓰는 중이면 그 프리셋에도 저장합니다. "
                                "저장하지 않은 변경이 있으면 버튼이 파랗게 바뀝니다.")).pack(side="left")
        tip(Button(footer, t("기본값"), self.reset, size=10, min_width=84),
            t("모든 세기와 기준, 진동 위치를 기본값으로 되돌리고 모든 효과를 켭니다. 저장은 따로 눌러야 합니다.")).pack(
            side="left", padx=(px(8), 0))
        self.save_label = tk.Label(footer, textvariable=self.save_state, bg=BG, fg=WARN, font=font(9))
        self.save_label.pack(side="left", padx=px(12))
        if not hasattr(self, "timing"):
            self.timing = tk.StringVar()
        tip(tk.Label(footer, textvariable=self.timing, bg=BG, fg=FAINT, font=font(9)),
            t("직전 세션에서 8ms 출력 주기를 놓친 횟수와 가장 늦었던 시간입니다. 0에 가까우면 정상입니다.")).pack(
            side="right")
        # debug log: switch, name and folder link, packed right to left
        log_tip = t("켜 두면 시작할 때마다 컨트롤러로 보낸 8ms 블록을 하나씩 CSV로 남깁니다(시각, 주기 간격, "
                    "트리거 경로, 채널별 세기, 속도·노면·이벤트). 진동이 이상하면 그 시각을 기억해 두고 이 파일을 "
                    "보세요. 1시간에 약 30MB, 최근 10개만 남깁니다. 주행 중에도 켜고 끄는 즉시 적용되고 바로 저장됩니다.")
        tip(Link(footer, t("기록 폴더 ↗"), self._open_log_dir),
            t("디버그 기록이 쌓이는 폴더를 엽니다.") + f"\n{LOG_DIR}").pack(side="right", padx=(0, px(18)))
        tip(tk.Label(footer, text=t("디버그 기록"), bg=BG, fg=TEXT, font=font(9)), log_tip).pack(
            side="right", padx=(px(8), px(10)))
        if not hasattr(self, "debug_log"):
            self.debug_log = tk.BooleanVar(value=self.options.debug_log)
        tip(Switch(footer, self.debug_log, self._on_debug_log, color=ACCENT, bg=BG), log_tip).pack(side="right")

        left = tk.Frame(root, bg=BG)
        left.grid(row=3, column=0, sticky="nsew", padx=(px(18), px(6)), pady=(px(12), 0))
        effects = card(left, t("주행 효과"), t("달리는 동안 계속 나는 진동"))
        effects.outer.pack(fill="x")
        self._route_header(effects)
        self._sliders(effects, EFFECT_SLIDERS)
        events = card(left, t("이벤트 효과"), t("순간적으로 한 번 나는 진동"))
        events.outer.pack(fill="x", pady=(px(12), 0))
        self._route_header(events)
        self._sliders(events, EVENT_SLIDERS)

        right = tk.Frame(root, bg=BG)
        right.grid(row=3, column=1, sticky="nsew", padx=(px(6), px(18)), pady=(px(12), 0))

        output = card(right, t("출력 세기"))
        output.outer.pack(fill="x")
        for row, spec in enumerate(OUTPUT_SLIDERS):
            self._slider(output, row, *spec)
        tk.Frame(output, bg="#252a34", height=1).grid(row=len(OUTPUT_SLIDERS), column=0, columnspan=4,
                                                     sticky="ew", pady=px(8))
        self.meters = []
        meter_tip = t("이 모터로 실제로 나가는 세기입니다. 진동이 약하거나 없을 때 어느 단계에서 줄어드는지 확인하세요.")
        for row, (label, color) in enumerate(zip(METER_LABELS, (GRIP_COLOR, GRIP_COLOR, TRIGGER_COLOR)),
                                             start=len(OUTPUT_SLIDERS) + 1):
            tip(tk.Label(output, text=t(label), bg=CARD, fg=MUTED, anchor="w"), meter_tip).grid(
                row=row, column=1, sticky="w")
            meter = Meter(output, color, length=200)
            tip(meter, meter_tip).grid(row=row, column=2, padx=px(6), pady=px(4))
            self.meters.append(meter)
        tip(Button(output, t("모터 보정…"), self.open_calibration, size=9, height=28, bg=CARD),
            t("모터가 주파수마다 다르게 울리는 정도를 손으로 느끼며 맞춥니다. 결과는 모든 효과에 주파수별로 "
              "적용됩니다. 출력을 정지한 상태에서 씁니다.")).grid(
            row=len(OUTPUT_SLIDERS) + 1 + len(METER_LABELS), column=1, columnspan=2, sticky="w", pady=(px(8), 0))

        info = card(right, t("텔레메트리"), t("게임에서 받고 있는 값"))
        info.outer.pack(fill="x", pady=(px(12), 0))
        self.info = {}
        tiles = tk.Frame(info, bg=CARD)
        tiles.pack(fill="x")
        tile_tip = t("게임 값 그대로입니다. 후진 기어는 음수, 중립은 0입니다.")
        for column, (key, label) in enumerate((("speed", t("속도")), ("rpm", "RPM"), ("gear", t("기어")))):
            tiles.columnconfigure(column, weight=1, uniform="tile")
            tile = tk.Frame(tiles, bg=TILE)
            tile.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else px(6), 0))
            tip(tile, tile_tip)
            tip(tk.Label(tile, text=label, bg=TILE, fg=MUTED, font=font(8)), tile_tip).pack(
                anchor="w", padx=px(10), pady=(px(6), 0))
            var = tk.StringVar(value="-")
            tip(tk.Label(tile, textvariable=var, bg=TILE, fg=TEXT, font=font(15, "bold")), tile_tip).pack(
                anchor="w", padx=px(10), pady=(0, px(6)))
            self.info[key] = var
        rows = tk.Frame(info, bg=CARD)
        rows.pack(fill="x", pady=(px(8), 0))
        for row, (key, label, text) in enumerate((
                ("pedal", t("페달"), t("엑셀/브레이크 입력(0~1)입니다. 크루즈 컨트롤처럼 게임이 대신 밟는 값은 "
                                     "포함하지 않습니다.")),
                ("surface", t("노면"), t("첫 번째 바퀴 아래 노면 재질의 게임 내부 이름입니다(예: road, gravel, grass).")),
                ("trigger", t("트리거"), t("트리거 채널이 지금 따라가는 페달입니다.")),
                ("accel", t("가속도 최대"), t("최근 0.1초 동안 수평 가속도 최대값입니다. 충돌 감지 기준을 정할 때 "
                                          "참고하세요.")))):
            tip(tk.Label(rows, text=label, bg=CARD, fg=MUTED, anchor="w"), text).grid(
                row=row, column=0, sticky="w", padx=(0, px(12)))
            var = tk.StringVar(value="-")
            tip(tk.Label(rows, textvariable=var, bg=CARD, fg=TEXT, anchor="w"), text).grid(
                row=row, column=1, sticky="w")
            self.info[key] = var

        thresholds = card(right, t("감지 기준"))
        thresholds.outer.pack(fill="x", pady=(px(12), 0))
        for row, spec in enumerate(THRESHOLD_SLIDERS):
            self._slider(thresholds, row, *spec)
        cylinders_tip = t("엔진 진동 주파수 계산에 씁니다. ATS 순정 트럭은 대부분 직렬 6기통이고, "
                          "ETS2의 Scania V8 같은 트럭은 8로 두세요. 텔레메트리에는 기통 수가 없습니다.")
        tip(tk.Label(thresholds, text=t(CYLINDERS_LABEL), bg=CARD, fg=TEXT, anchor="w"), cylinders_tip).grid(
            row=len(THRESHOLD_SLIDERS), column=1, sticky="w")
        self.cylinders = tk.IntVar(value=self.gains.cylinders)
        # Traced rather than the spinbox command, which only sees the arrows, not typing.
        self.cylinders.trace_add("write", lambda *_: self._set_cylinders())
        spinbox = ttk.Spinbox(thresholds, from_=CYLINDERS_RANGE[0], to=CYLINDERS_RANGE[1], increment=2, width=5,
                              textvariable=self.cylinders)
        # text left unusable shows the count in use again
        spinbox.bind("<FocusOut>", lambda _: self.cylinders.set(self.gains.cylinders))
        tip(spinbox, cylinders_tip).grid(row=len(THRESHOLD_SLIDERS), column=2, sticky="w", padx=px(6), pady=(px(4), 0))

        log = card(right, t("이벤트"))
        log.outer.pack(fill="both", expand=True, pady=(px(12), 0))
        self.events = tk.Listbox(log, height=6, width=40, bg=TILE, fg=TEXT, bd=0, highlightthickness=0,
                                 selectbackground="#2e3441", selectforeground=TEXT, activestyle="none")
        tip(self.events, t("변속, 충돌, 페달, 트레일러, 파킹 브레이크, 방향지시등, 시동, 게임 알림과 "
                           "컨트롤러 연결 문제가 시간과 함께 기록됩니다. 최근 50개까지 보입니다.")).pack(
            fill="both", expand=True)

    def _sliders(self, parent: tk.Frame, specs: tuple) -> None:
        """One row per slider under the route header, a line above each CARD_WIDE one."""
        row = 1
        for spec in specs:
            if spec[0] in CARD_WIDE:
                tk.Frame(parent, bg="#252a34", height=1).grid(row=row, column=0, columnspan=6, sticky="ew",
                                                             pady=px(6))
                row += 1
            self._slider(parent, row, *spec)
            row += 1

    def _slider(self, parent: tk.Frame, row: int, key: str, label: str,
                low: int, high: int, scale: int, unit: str) -> None:
        parent.columnconfigure(1, minsize=self.label_width)
        text = t(SLIDER_TIPS[key])
        if key in TOGGLEABLE:
            enabled = tk.BooleanVar(value=key not in self.gains.disabled)
            tip(Switch(parent, enabled, lambda k=key: self._on_toggle(k)),
                t("이 효과 켜기/끄기. 꺼도 세기와 진동 위치는 남아서 다시 켜면 그대로 돌아옵니다.")).grid(
                row=row, column=0, padx=(0, px(10)))
            self.enabled_vars[key] = enabled
        name = tk.Label(parent, text=t(label), bg=CARD, fg=TEXT, anchor="w")
        tip(name, text).grid(row=row, column=1, sticky="w")
        var = tk.DoubleVar(value=getattr(self.gains, key) * scale)
        value_label = tk.Label(parent, bg=CARD, fg=TEXT, width=7, anchor="e", font=font(9, "bold"))
        slider = Slider(parent, var, low, high, lambda k=key: self._on_slider(k),
                        color=LEVEL_COLORS.get(key, ACCENT), length=200)
        tip(slider, text).grid(row=row, column=2, padx=px(6), pady=px(4))
        value_label.grid(row=row, column=3, sticky="e", padx=(0, px(4)))
        self.slider_vars[key] = (var, value_label, scale, unit)
        self.sliders[key] = slider
        if key in TOGGLEABLE:
            self.effect_labels[key] = name
            picker = Segmented(parent, [(route, t(option), color) for route, option, color in ROUTE_OPTIONS],
                               lambda route, k=key: self._on_route(k, route))
            tip(picker, self._route_tip()).grid(row=row, column=4, padx=(px(10), 0), pady=px(3))
            self.route_pickers[key] = picker
            tip(Button(parent, "▶", lambda k=key: self.test_effect(k), size=8, height=26, bg=CARD),
                t("이 효과만 몇 초 동안 울려 봅니다. 지금 세기, 진동 위치, 모터 보정 그대로이고, 꺼 둔 효과도 울립니다. "
                  "요철, 차선 요철, 방향지시등은 왼쪽 다음 오른쪽 순서입니다. 출력 중에는 그동안 주행 진동 대신 "
                  "울립니다.")).grid(row=row, column=5, padx=(px(8), 0))
            self._show_effect(key)
        self._on_slider(key)

    def _route_tip(self) -> str:
        return t("이 효과의 진동이 갈 곳: 그립(파랑), 트리거(주황), 둘 다(보라). "
                 "꺼진 효과에서 위치를 누르면 그 위치로 다시 켜집니다.")

    def _route_header(self, parent: tk.Frame) -> None:
        tip(tk.Label(parent, text=t("진동 위치"), bg=CARD, fg=FAINT, font=font(8)), self._route_tip()).grid(
            row=0, column=4, padx=(px(10), 0))

    def _show_dot(self, color: str) -> None:
        self.status_dot.delete("all")
        self.status_dot.create_oval(px(1), px(1), px(11), px(11), fill=color, outline="")

    # -- language ---------------------------------------------------------------

    def _language_menu(self) -> None:
        menu = tk.Menu(self.root, tearoff=False, bg=TILE, fg=TEXT, activebackground=FIELD_HI,
                       activeforeground=TEXT, selectcolor=ACCENT, bd=0)
        current = tk.StringVar(value=i18n.language())
        for code, name in i18n.LANGUAGES:
            # after the menu has closed: the rebuild destroys it along with the window's contents
            menu.add_radiobutton(label=name, value=code, variable=current,
                                 command=lambda c=code: self.root.after_idle(self._set_language, c))
        button = self.language_button
        try:
            menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())
        finally:
            menu.grab_release()
        self.root.after_idle(menu.destroy)

    def _set_language(self, code: str) -> None:
        """Remember the language and lay the window out again in it; the tuning is untouched."""
        if code == i18n.language():
            return
        self.options.language = code
        save_app_options(self.options)
        i18n.set_language(code)
        apply_theme(self.root)
        log = self.events.get(0, "end")
        for child in self.root.winfo_children():
            child.destroy()
        self._build()
        self.events.insert("end", *log)
        self._changed()
        self._show_run_state()
        if self.runner is None and not self.error:
            self.status.set(t("정지됨"))
        fit_window(self.root)

    # -- controls -------------------------------------------------------------

    def _changed(self) -> None:
        if self.runner:
            self.runner.apply(self.gains)
        dirty = asdict(self.gains) != self.saved
        self.save_state.set(t("변경됨 · 저장 안 됨") if dirty else "")
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

    def test_effect(self, key: str) -> None:
        """Play this effect alone for a few seconds (preview.py), in place of the drive while it runs."""
        if self.thread and self.thread.is_alive():
            if self.runner and (self.connected or self.dry_run):
                self.runner.preview = Preview(key, self.gains, self.runner.eq)
            else:
                messagebox.showinfo(t("효과 테스트"), t("출력이 게임을 기다리는 중이라 지금은 컨트롤러를 쓰지 않습니다. "
                                                     "정지한 다음 테스트하세요."))
            return
        if self.dry_run or (self.calibration and self.calibration.busy):  # no controller, or it holds it
            return
        self.error = None
        self.player.play(Preview(key, self.gains, Equalizer.for_player(load_eq())))

    def open_calibration(self) -> None:
        if self.calibration and self.calibration.top.winfo_exists():
            self.calibration.top.lift()
            return
        if self.thread and self.thread.is_alive():
            messagebox.showinfo(t("모터 보정"), t("출력을 정지한 다음 보정하세요."))
            return
        self.calibration = CalibrationDialog(self.root)

    def open_plugin_dialog(self) -> None:
        PluginDialog(self.root)

    def check_plugins(self) -> None:
        """At startup: open the plugin dialog if no installed game can feed telemetry."""
        games = plugin_install.find_games()
        if games and not any(game.compatible for game in games):
            self.status.set(t("호환되는 텔레메트리 플러그인이 없습니다 → '텔레메트리 플러그인'"))
            self.open_plugin_dialog()

    def _on_auto_start(self) -> None:
        # An app option, not a tuning value: saved right away.
        self.options.auto_start_on_game = self.auto_start.get()
        save_app_options(self.options)
        self.last_game = None  # a game that is already running counts as newly detected

    def _on_debug_log(self) -> None:
        # Saved right away like auto start, and a running drive starts or stops its log on the next block.
        self.options.debug_log = self.debug_log.get()
        save_app_options(self.options)
        if self.runner is not None:
            self.runner.log_blocks = self.options.debug_log

    def _open_log_dir(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(LOG_DIR)

    def _set_cylinders(self) -> None:
        # Runs on every keystroke: text that is not (yet) a count in range waits.
        try:
            value = int(self.cylinders.get())
        except (tk.TclError, ValueError):
            return
        if CYLINDERS_RANGE[0] <= value <= CYLINDERS_RANGE[1] and value != self.gains.cylinders:
            self.gains.cylinders = value
            self._changed()

    def save(self) -> None:
        save_gains(self.gains)
        preset = self._active_preset()
        if preset:
            save_preset(preset, self.gains)
        self.saved = asdict(self.gains)
        self._changed()
        self.save_state.set(t("✓ '{name}'에 저장됨", name=preset) if preset else t("✓ 저장됨"))
        self.save_label.config(fg=GO)

    def reset(self) -> None:
        self._apply(Gains())

    def _apply(self, gains: Gains) -> None:
        """Set every slider, switch, route and the cylinder count to these gains; saving stays separate."""
        for key, (var, _, scale, _) in self.slider_vars.items():
            var.set(getattr(gains, key) * scale)
            self._on_slider(key)
        for key, var in self.enabled_vars.items():
            var.set(key not in gains.disabled)
        self.gains.disabled = sorted(key for key in gains.disabled if key in self.enabled_vars)
        self.gains.routes = {**DEFAULT_ROUTES, **gains.routes}
        for key in self.route_pickers:
            self._show_effect(key)
        self.cylinders.set(gains.cylinders)  # traced
        self._changed()

    # -- presets ----------------------------------------------------------------

    def _active_preset(self) -> str:
        """The preset in use, or "" when none is or it was removed from config.json by hand."""
        name = self.options.preset
        return name if name and name in load_presets() else ""

    def _build_presets(self) -> None:
        bar = self.preset_bar
        for child in bar.winfo_children():
            child.destroy()
        bar_tip = t("프리셋마다 세기, 기준, 켜기/끄기, 진동 위치, 기통 수를 통째로 따로 둡니다. 트럭과 트립 모드의 "
                    "승용차처럼 탈것이 바뀔 때 눌러서 바로 바꿉니다. 주행 중에도 즉시 적용되고 다음 실행에도 이어집니다.")
        tip(tk.Label(bar, text=t("프리셋"), bg=BG, fg=MUTED, font=font(9)), bar_tip).pack(side="left", padx=(0, px(8)))
        active = self._active_preset()
        names = sorted(load_presets(), key=str.casefold)
        button_tip = t("이 프리셋으로 바꿉니다. 오른쪽 클릭: 이름 바꾸기, 삭제")
        for name in names:
            button = Button(bar, name, lambda n=name: self._switch_preset(n),
                            kind="primary" if name == active else "ghost", size=9, height=30)
            button.bind("<Button-3>", lambda event, n=name: self._preset_actions(event, n))
            tip(button, button_tip).pack(side="left", padx=(0, px(6)))
        if not names:
            tip(tk.Label(bar, text=t("없음"), bg=BG, fg=FAINT, font=font(9)), bar_tip).pack(side="left", padx=(0, px(6)))
        tip(Button(bar, t("＋ 지금 설정으로 새 프리셋"), self._new_preset, size=9, height=30),
            t("지금 설정(저장하지 않은 변경 포함)을 새 이름으로 보관하고 그 프리셋으로 바꿉니다.")).pack(
            side="left", padx=(px(6), 0))

    def _keep_changes(self) -> bool:
        """Before leaving a tuning with unsaved changes: save them, drop them, or stay (False)."""
        if asdict(self.gains) == self.saved:
            return True
        answer = messagebox.askyesnocancel(t("프리셋 전환"), t("저장하지 않은 변경이 있습니다. 바꾸기 전에 저장할까요?"))
        if answer:
            self.save()
        return answer is not None

    def _switch_preset(self, name: str) -> None:
        if name == self._active_preset() and asdict(self.gains) == self.saved:
            return
        if name not in load_presets() or not self._keep_changes():
            self._build_presets()
            return
        # read after _keep_changes: saving there writes into the preset in use, which may be this one
        self._use_preset(name, load_presets()[name])

    def _use_preset(self, name: str, gains: Gains) -> None:
        """Make this preset the tuning in use, and the one the next start opens with."""
        self._apply(gains)
        self._set_active_preset(name)
        save_gains(self.gains)  # the next start opens with it
        self.saved = asdict(self.gains)
        self._changed()
        self.save_state.set(t("✓ '{name}'(으)로 바꿈", name=name))
        self.save_label.config(fg=GO)

    def _set_active_preset(self, name: str) -> None:
        self.options.preset = name
        save_app_options(self.options)
        self._build_presets()

    def _ask_preset_name(self, title: str, initial: str = "") -> str | None:
        """A new name from the player, or None; taking another preset's name replaces it once confirmed."""
        name = (simpledialog.askstring(title, t("프리셋 이름"), initialvalue=initial, parent=self.root) or "").strip()
        if not name or name == initial:
            return None
        if name in load_presets() and not messagebox.askyesno(
                title, t("'{name}' 프리셋이 이미 있습니다. 덮어쓸까요?", name=name)):
            return None
        return name

    def _new_preset(self) -> None:
        name = self._ask_preset_name(t("새 프리셋"))
        if name:
            save_preset(name, self.gains)
            self._set_active_preset(name)
            self.save()  # the startup tuning follows it

    def _preset_actions(self, event: tk.Event, name: str) -> None:
        menu = tk.Menu(self.root, tearoff=False, bg=TILE, fg=TEXT, activebackground=FIELD_HI,
                       activeforeground=TEXT, bd=0)
        # after the menu has closed: both rebuild the bar the menu came from
        menu.add_command(label=t("이름 바꾸기…"), command=lambda: self.root.after_idle(self._rename_preset, name))
        menu.add_command(label=t("삭제"), command=lambda: self.root.after_idle(self._delete_preset, name))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        self.root.after_idle(menu.destroy)

    def _rename_preset(self, name: str) -> None:
        gains = load_presets().get(name)
        new = self._ask_preset_name(t("이름 바꾸기"), name)
        if gains is None or not new:
            return
        save_preset(new, gains)
        delete_preset(name)
        if self.options.preset == name:
            self._set_active_preset(new)
        elif self.options.preset == new:  # it replaced the preset in use: the window takes what that holds now
            self._use_preset(new, gains)
        else:
            self._build_presets()

    def _delete_preset(self, name: str) -> None:
        if not messagebox.askyesno(t("삭제"), t("'{name}' 프리셋을 지울까요? 지금 설정은 그대로 남습니다.", name=name)):
            return
        delete_preset(name)
        if self.options.preset == name:
            self._set_active_preset("")
        else:
            self._build_presets()

    def toggle_run(self) -> None:
        if self.thread and self.thread.is_alive():
            self.stop()
        else:
            self.start()

    def _show_run_state(self) -> None:
        running = bool(self.thread and self.thread.is_alive())
        if not running:
            self.run_button.set(t("▶  시작"), kind="go", state="normal")
        elif self.runner and self.runner.stop_requested:
            self.run_button.set(t("정지 중…"), kind="ghost", state="disabled")
        else:
            self.run_button.set(t("■  정지"), kind="danger", state="normal")
        self.source_picker.set(self.source.get(), locked=running)

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        if self.calibration and self.calibration.busy:  # it holds the controller; a game launch waits too
            return
        self.player.stop()  # a test takes no longer than letting the controller go
        self.error = None
        self.connected = None
        self.events.delete(0, "end")
        self.runner = Runner(self.gains, simulate=self.source.get() == "sim", log_blocks=self.options.debug_log,
                             eq=Equalizer.for_player(load_eq()))  # read at each start: a new calibration applies
        self.thread = threading.Thread(target=self._worker, args=(self.runner,), daemon=True)
        self.thread.start()
        self._show_run_state()

    def _worker(self, runner: Runner) -> None:
        try:
            run(runner, dry_run=self.dry_run, on_connected=self._on_connected,
                exit_with_game=self.exit_with_game)
        except Exception as exc:  # shown in the status line, the traceback in error.log
            log_exception(exc, "session")
            # Our own failures are RuntimeErrors with a readable message; for a bug
            # the bare message (a KeyError's 'x') says little without its type.
            self.error = str(exc) if isinstance(exc, RuntimeError) else f"{type(exc).__name__}: {exc}"
        finally:
            runner.close()

    def _on_connected(self, name: str | None) -> None:
        # Runs on the worker thread, so plain attributes only; _poll shows them.
        # A Tk call from here waits for the main thread, which on_close keeps
        # in join() until this thread ends: both would wait out the timeout.
        self.connected = name
        stats = self.runner.last_stats if self.runner else None
        if name is None and stats:
            self.session_stats = stats

    def stop(self) -> None:
        if self.runner:
            self.runner.stop_requested = True
            self._show_run_state()

    def on_close(self) -> None:
        if asdict(self.gains) != self.saved:
            answer = messagebox.askyesnocancel(t("저장"), t("바뀐 설정을 저장할까요?"))
            if answer is None:
                return
            if answer:
                self.save()
        self.stop()
        self.player.stop()
        if self.calibration:
            self.calibration.abort()
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
        text = (t("새 버전 {new}이 있습니다 (현재 {current}).", new=release.version, current=__version__)
                + f"\n\n{notes}").rstrip()
        if not updater.can_self_update(release):
            if messagebox.askyesno(t("업데이트"), text + "\n\n" + t("릴리스 페이지를 열까요?")):
                webbrowser.open(release.page)
            return
        if self.watcher.game:
            messagebox.showinfo(t("업데이트"), t("게임이 실행 중입니다. 게임을 끝낸 뒤 업데이트하세요."))
            return
        if not messagebox.askyesno(t("업데이트"), text + "\n\n" + t("내려받아 설치하고 프로그램을 다시 시작할까요?")):
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
            self.update_button.set(text=t("업데이트 {version}", version=release.version))
            self.update_button.pack(side="right", padx=(0, px(8)))
        if self.update_error:
            messagebox.showerror(t("업데이트"), t("업데이트하지 못했습니다: {error}", error=self.update_error))
            self.update_error = self.update_progress = None
            self.update_button.set(state="normal", text=t("업데이트 {version}", version=release.version))
        elif self.update_ready:
            source, self.update_ready = self.update_ready, None
            self.update_button.set(text=t("설치 중…"))
            self.on_close()
            if self.closed:
                updater.apply_on_exit(source)  # copies once this process has exited
            else:  # save prompt cancelled
                self.update_progress = None
                self.update_button.set(state="normal", text=t("업데이트 {version}", version=release.version))
        elif self.update_progress is not None:
            self.update_button.set(text=t("내려받는 중 {percent}", percent=f"{self.update_progress:.0%}"),
                                   state="disabled")

    # -- periodic UI refresh ------------------------------------------------------

    def _poll(self) -> None:
        self._poll_update()
        if self.closed:
            return
        running = bool(self.thread and self.thread.is_alive())
        # Auto start on the game appearing, not on every poll, so a manual
        # 정지 holds until the next launch. A Steam launch while this window
        # was already open starts it too: that launch's own window bowed out.
        game = self.watcher.game
        launched = _launch_requested(self.launch_event)  # read every poll, so a stale request never lingers
        if not running and (launched or (game and not self.last_game and self.auto_start.get())):
            self.source.set("game")
            self.start()
            running = True
        self.last_game = game
        runner = self.runner
        self._show_run_state()
        if self.session_stats:
            stats, self.session_stats = self.session_stats, None
            self.timing.set(t("지난 세션: 지연 프레임 {late} / 최대 {max} ms", late=stats.late_frames,
                              max=f"{stats.max_lateness_ms:.1f}"))
        if running and runner:
            device = self.connected or (t("컨트롤러 없음") if self.dry_run else t("컨트롤러 미사용"))
            self.status.set(f"{device} · {runner.state or t('시작 중')}")
            self._show_dot(GO if self.connected or self.dry_run else WARN)
            self._refresh(runner)
        else:
            if self.player.error:
                self.error, self.player.error = self.player.error, None
            testing = self.player.busy
            if testing:
                self.status.set(t("효과 테스트: {name}", name=t(EFFECT_NAMES[self.player.name])))
                self._show_dot(GO)
                self.tested = True
            elif self.error:
                self.status.set(t("오류: {error}", error=self.error))
                self._show_dot(STOP)
            else:
                if runner or self.tested:
                    self.status.set(t("정지됨"))
                    self.tested = False
                self._show_dot(FAINT)
            peaks = self.player.take_peaks()
            for i, meter in enumerate(self.meters):
                self.meter_values[i] = max(peaks[i], self.meter_values[i] * METER_DECAY) if testing else 0.0
                meter.set(self.meter_values[i])
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
        self.info["pedal"].set(t("엑셀 {throttle} / 브레이크 {brake}", throttle=f"{frame.user_throttle:.2f}",
                                 brake=f"{frame.user_brake:.2f}"))
        self.info["surface"].set(levels.surface or "-")
        self.info["trigger"].set({"throttle": t("엑셀"), "brake": t("브레이크")}.get(levels.trigger_mode, "-"))
        self.info["accel"].set(f"{runner.take_max_accel():.1f} m/s²")


class PluginDialog:
    """Lists ATS/ETS2 installs with their telemetry plugin and installs the bundled one."""

    def __init__(self, parent: tk.Tk) -> None:
        self.top = tk.Toplevel(parent, bg=BG)
        self.top.title(t("텔레메트리 플러그인"))
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
        ttk.Label(self.body, text=t("진동은 게임의 텔레메트리 플러그인(RenCloud 계열 rev 12) 데이터를 사용합니다."),
                  ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, px(8)))
        if not self.games:
            ttk.Label(self.body, text=t("Steam에서 ATS/ETS2 설치를 찾지 못했습니다. 게임 폴더를 직접 선택하세요."),
                      ).grid(row=1, column=0, columnspan=3, sticky="w")
        for row, game in enumerate(self.games, start=1):
            tip(ttk.Label(self.body, text=game.name, width=24), str(game.path)).grid(row=row, column=0, sticky="w")
            ttk.Label(self.body, text=game.describe(), width=34).grid(row=row, column=1, sticky="w", padx=px(8))
            text = t("재설치") if game.compatible else t("설치")
            tip(ttk.Button(self.body, text=text, command=lambda g=game: self._install(g)),
                t("동봉한 RenCloud scs-telemetry.dll을 이 게임의 plugins 폴더에 복사합니다. "
                  "다른 플러그인은 건드리지 않습니다.")).grid(row=row, column=2, pady=px(2))
        bottom = ttk.Frame(self.body)
        bottom.grid(row=len(self.games) + 2, column=0, columnspan=3, sticky="ew", pady=(px(10), 0))
        tip(ttk.Button(bottom, text=t("게임 폴더 직접 선택…"), command=self._pick_folder),
            t("Steam 밖에 설치했거나 찾지 못한 경우, bin\\win_x64\\amtrucks.exe 또는 eurotrucks2.exe가 있는 "
              "게임 폴더를 지정합니다.")).pack(side="left")
        ttk.Button(bottom, text=t("닫기"), command=self.top.destroy).pack(side="right")

    def _pick_folder(self) -> None:
        folder = filedialog.askdirectory(parent=self.top, title=t("ATS 또는 ETS2 설치 폴더"))
        if not folder:
            return
        game = plugin_install.game_from_folder(Path(folder))
        if game is None:
            messagebox.showerror(t("플러그인"), t("bin\\win_x64\\amtrucks.exe 또는 eurotrucks2.exe가 있는 "
                                                "게임 폴더를 선택하세요."), parent=self.top)
            return
        self.games = [g for g in self.games if g.key != game.key] + [game]
        self._render()

    def _install(self, game: plugin_install.GameInstall) -> None:
        if not messagebox.askokcancel(
                t("플러그인 설치"),
                t("{game}의 plugins 폴더에 RenCloud scs-telemetry.dll(v1.12.1, MIT)을 복사합니다.", game=game.name)
                + f"\n\n{game.plugins_dir}\n\n" + t("게임이 실행 중이면 다음 실행부터 적용됩니다."), parent=self.top):
            return
        try:
            target = plugin_install.install(game)
        except PermissionError:
            messagebox.showerror(t("플러그인 설치"), t("폴더에 쓸 권한이 없습니다. 프로그램을 관리자 권한으로 실행하거나 "
                                                   "아래 파일을 직접 복사하세요.")
                                 + f"\n\n{plugin_install.bundled_plugin()}\n→ {game.plugins_dir}", parent=self.top)
            return
        except OSError as exc:
            messagebox.showerror(t("플러그인 설치"), t("복사하지 못했습니다: {error}", error=exc), parent=self.top)
            return
        messagebox.showinfo(t("플러그인 설치"), t("설치했습니다.") + f"\n{target}", parent=self.top)
        self._render()


class CalibrationDialog:
    """Matches each frequency's strength by feel (calibrate.Session) on a worker thread.

    The answers come from the buttons here, the keyboard or the pad's own
    buttons; _poll shows where the worker stands.
    """

    NAMES = {"grip": N_("그립"), "trigger": N_("트리거")}
    SOURCES = (N_("{name} (기본값)"), N_("{name} (직접 측정)"))  # by whether the player measured it

    def __init__(self, parent: tk.Tk) -> None:
        self.top = tk.Toplevel(parent, bg=BG)
        self.top.title(t("모터 보정"))
        self.top.resizable(False, False)
        self.top.transient(parent)
        dark_title_bar(self.top)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.answers: queue.SimpleQueue[str] = queue.SimpleQueue()
        self.thread: threading.Thread | None = None
        self.session: calibrate.Session | None = None
        self.cancelled = False
        self.phase = "start"  # start, playing, waiting, stopping, done, error: set by the worker
        self.error = ""
        self.shown = ""
        self.channels = {channel: tk.BooleanVar(value=True) for channel in calibrate.FREQS}
        for key, answer in (("<Left>", calibrate.FIRST), ("1", calibrate.FIRST), ("<Right>", calibrate.SECOND),
                            ("2", calibrate.SECOND), ("r", calibrate.REPLAY), ("<Escape>", calibrate.CANCEL)):
            self.top.bind(key, lambda _event, a=answer: self.answer(a))
        self.body = tk.Frame(self.top, bg=BG)
        self.body.pack(fill="both", padx=px(18), pady=px(16))
        self._poll()

    @property
    def busy(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    # -- screens --------------------------------------------------------------

    def _clear(self) -> tk.Frame:
        for child in self.body.winfo_children():
            child.destroy()
        return self.body

    def _text(self, parent: tk.Misc, text: str, **options) -> tk.Label:
        options = {"bg": BG, "fg": TEXT, "justify": "left", "anchor": "w", "wraplength": px(470), **options}
        return tk.Label(parent, text=text, **options)

    def _render_start(self) -> None:
        body = self._clear()
        self._text(body, t("모터는 그립 약 65Hz, 트리거 약 100Hz에서 가장 세게 울려서, 같은 세기로 보내도 주파수마다 "
                           "다르게 느껴집니다. 이웃한 두 주파수가 차례로 울리면 어느 쪽이 더 셌는지 고르세요. 조금이라도 "
                           "센 쪽이 있으면 그쪽을, 정말 구분이 안 되면 아무 쪽이나 고르면 됩니다. 구분이 안 된다는 건 거의 "
                           "같아졌다는 뜻이라 결과가 틀어지지 않습니다. 답에 따라 세기를 좁혀 가며 같게 느껴지는 지점을 "
                           "찾고, 모든 효과가 주파수별로 그만큼 보정됩니다. 모터 하나에 3~5분 걸립니다.")).pack(fill="x")
        own = load_eq()
        sources = [t(self.SOURCES[c in own], name=t(self.NAMES[c])) for c in calibrate.FREQS]
        self._text(body, t("지금 보정: {channels}", channels=", ".join(sources)), fg=MUTED).pack(
            fill="x", pady=(px(10), 0))
        picks = tk.Frame(body, bg=BG)
        picks.pack(fill="x", pady=(px(12), 0))
        for channel, var in self.channels.items():
            Switch(picks, var, lambda: None, color=GRIP_COLOR if channel == "grip" else TRIGGER_COLOR,
                   bg=BG).pack(side="left")
            self._text(picks, t("{name} ({count}개 주파수)", name=t(self.NAMES[channel]),
                                count=len(calibrate.FREQS[channel]))).pack(side="left", padx=(px(8), px(22)))
        self._text(body, self._controls_hint(), fg=MUTED).pack(fill="x", pady=(px(12), 0))
        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill="x", pady=(px(16), 0))
        Button(buttons, t("시작"), self.begin, kind="primary", min_width=96).pack(side="left")
        tip(Button(buttons, t("보정 지우기"), self.reset), t("저장된 보정을 지워 기본값으로 되돌립니다.")).pack(
            side="left", padx=(px(8), 0))
        Button(buttons, t("닫기"), self.close).pack(side="right")

    def _controls_hint(self) -> str:
        return t("컨트롤러를 평소처럼 잡으세요. 패드 버튼으로 답할 수 있습니다: 십자키 ← 또는 LB 첫 번째, → 또는 RB "
                 "두 번째, X 다시 듣기, B 그만. 키보드는 ← → (또는 1 2) R Esc입니다. 재생 중에 눌러도 바로 반영됩니다.")

    def _render_session(self) -> None:
        body = self._clear()
        self.progress = self._text(body, "", font=font(13, "bold"))
        self.progress.pack(fill="x")
        self.trial = self._text(body, "", fg=MUTED)
        self.trial.pack(fill="x", pady=(px(4), 0))
        self.state = self._text(body, "")
        self.state.pack(fill="x", pady=(px(10), 0))
        self._text(body, t("조금이라도 센 쪽이 있으면 그쪽을, 구분이 안 되면 아무 쪽이나 고르세요. 거의 같아졌다는 뜻이라 "
                           "결과가 틀어지지 않습니다. 재생 중에 눌러도 바로 반영됩니다."), fg=MUTED, font=font(9)).pack(
            fill="x", pady=(px(4), 0))
        row = tk.Frame(body, bg=BG)
        row.pack(fill="x", pady=(px(14), 0))
        for text, answer, kind, pad, keys in ((t("◀ 첫 번째"), calibrate.FIRST, "primary", "← / LB", "← / 1"),
                                              (t("두 번째 ▶"), calibrate.SECOND, "primary", "→ / RB", "→ / 2"),
                                              (t("↻ 다시 듣기"), calibrate.REPLAY, "ghost", "X", "R")):
            self._answer_button(row, text, answer, kind, pad, keys).pack(side="left", padx=(0, px(8)))
        self._answer_button(row, t("그만"), calibrate.CANCEL, "danger", "B", "Esc", min_width=0).pack(side="right")

    def _answer_button(self, parent: tk.Misc, text: str, answer: str, kind: str, pad: str, keys: str,
                       min_width: int = 120) -> tk.Frame:
        """A button with the pad and keyboard buttons that give the same answer written under it."""
        column = tk.Frame(parent, bg=BG)
        Button(column, text, lambda: self.answer(answer), kind=kind, min_width=min_width).pack()
        tk.Label(column, text=t("패드 {buttons}", buttons=pad), bg=BG, fg=MUTED, font=font(9, "bold")).pack(pady=(px(5), 0))
        tk.Label(column, text=t("키보드 {keys}", keys=keys), bg=BG, fg=FAINT, font=font(8)).pack()
        return column

    def _render_end(self) -> None:
        body = self._clear()
        session = self.session
        if self.phase == "error":
            text = t("보정하지 못했습니다: {error}", error=self.error)
        elif session and session.finished:
            text = t("보정을 저장했습니다: {channels}. 다음 시작부터 적용됩니다.",
                     channels=", ".join(t(self.NAMES[c]) for c in session.finished))
        else:
            text = t("저장한 보정이 없습니다. 모터 하나를 끝까지 맞추면 저장됩니다.")
        self._text(body, text).pack(fill="x")
        for channel in (session.finished if session else []):  # no-break spaces keep "50 Hz ×1.19" whole
            curve = sorted(session.curves[channel].items(), key=lambda kv: float(kv[0]))
            self._text(body, f"{t(self.NAMES[channel])}: " + "  ".join(f"{f} Hz ×{g:.2f}" for f, g in curve),
                       fg=MUTED, font=font(9)).pack(fill="x", pady=(px(6), 0))
        buttons = tk.Frame(body, bg=BG)
        buttons.pack(fill="x", pady=(px(16), 0))
        Button(buttons, t("다시 보정"), self._restart).pack(side="left")
        Button(buttons, t("닫기"), self.close, kind="primary", min_width=96).pack(side="right")

    def _restart(self) -> None:
        self.phase = "start"

    # -- actions ----------------------------------------------------------------

    def begin(self) -> None:
        channels = [c for c, var in self.channels.items() if var.get()]
        if not channels or self.busy:
            return
        self.session = calibrate.Session(channels)
        self.cancelled = False
        self.error = ""
        while not self.answers.empty():
            self.answers.get_nowait()
        self.phase = "playing"
        self.thread = threading.Thread(target=self._work, args=(self.session,), daemon=True)
        self.thread.start()

    def answer(self, answer: str) -> None:
        if self.busy:
            if answer == calibrate.CANCEL:
                self.cancelled = True
            self.answers.put(answer)
        elif answer == calibrate.CANCEL:
            self.close()

    def reset(self) -> None:
        if messagebox.askyesno(t("모터 보정"), t("저장된 보정을 지우고 기본값으로 되돌릴까요?"), parent=self.top):
            save_eq({})
            self.shown = ""  # redraw "지금 보정"

    def abort(self) -> None:
        """Stop a running calibration and wait for the controller to be let go."""
        if self.busy:
            self.cancelled = True
            self.answers.put(calibrate.CANCEL)
            self.thread.join(timeout=4.0)

    def close(self) -> None:
        if self.busy:
            self.answer(calibrate.CANCEL)
            self.top.after(100, self.close)  # once the current tone has stopped
            return
        self.top.destroy()

    # -- worker -------------------------------------------------------------------

    def _work(self, session: calibrate.Session) -> None:
        buttons = calibrate.PadButtons()

        def listen() -> str | None:
            try:
                return self.answers.get_nowait()
            except queue.Empty:
                return buttons.poll()

        try:
            with Apex6() as pad:
                while session.current and not self.cancelled:
                    self.phase = "playing"
                    answer = calibrate.play(pad, session, listen)
                    if answer is None:
                        self.phase = "waiting"
                        answer = calibrate.wait(listen, lambda: self.cancelled)
                    if answer == calibrate.CANCEL:
                        break
                    if session.answer(answer):
                        calibrate.save(session)
            self.phase = "done"
        except DeviceUnavailable as exc:
            self.error, self.phase = str(exc), "error"
        except Exception as exc:  # the traceback goes to error.log
            log_exception(exc, "calibration")
            self.error, self.phase = f"{type(exc).__name__}: {exc}", "error"

    # -- refresh --------------------------------------------------------------------

    def _poll(self) -> None:
        if not self.top.winfo_exists():
            return
        screen = {"playing": "session", "waiting": "session"}.get(self.phase, self.phase)
        if screen in ("done", "error"):
            screen = "end"
        if screen != self.shown:
            self.shown = screen
            {"start": self._render_start, "session": self._render_session, "end": self._render_end}[screen]()
        if screen == "session" and self.session and self.session.current:
            session = self.session
            channel, freq, neighbour = session.current
            first = next(i for i, step in enumerate(session.steps) if step[0] == channel)
            self.progress.config(text=t("{name} {index}/{count} · {freq} Hz ↔ {neighbour} Hz", name=t(self.NAMES[channel]),
                                        index=session.index - first + 1, count=len(calibrate.FREQS[channel]),
                                        freq=f"{freq:g}", neighbour=f"{neighbour:g}"))
            self.trial.config(text=t("비교 {trial}번째", trial=session.staircase.trials + 1))
            self.state.config(text=t("그만두는 중…") if self.cancelled else
                              t("첫 번째 → 두 번째 재생 중…") if self.phase == "playing" else
                              t("어느 쪽이 더 셌나요?"))
        self.top.after(50, self._poll)


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.restype = ctypes.c_void_p
_kernel32.CreateEventW.restype = ctypes.c_void_p
_kernel32.SetEvent.argtypes = (ctypes.c_void_p,)
_kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
_kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)


def _claim_single_instance() -> object | None:
    """Hold a named mutex; None if another window already holds it."""
    handle = _kernel32.CreateMutexW(None, False, INSTANCE_MUTEX)
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def _launch_event() -> int | None:
    """The auto-reset event a Steam launch sets when this window is already open."""
    return _kernel32.CreateEventW(None, False, False, LAUNCH_EVENT)


def _request_launch() -> None:
    """From a second instance: have the open window start its game session."""
    event = _launch_event()
    if event:
        _kernel32.SetEvent(event)
        _kernel32.CloseHandle(event)


def _launch_requested(event: int | None) -> bool:
    return bool(event) and _kernel32.WaitForSingleObject(event, 0) == 0  # WAIT_OBJECT_0, resets it


def fit_window(root: tk.Tk) -> None:
    # Started via WMI (app.launch), a fixed-size Tk window shrinks to its
    # title bar when restored from minimized; a minimum size keeps it whole.
    root.minsize(1, 1)
    root.update_idletasks()
    root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="run without the controller")
    parser.add_argument("--autostart", action="store_true", help="start the game session immediately")
    parser.add_argument("--exit-with-game", action="store_true", help="close the window when the game quits")
    parser.add_argument("--minimized", action="store_true", help="open minimized")
    args = parser.parse_args(argv)
    if _claim_single_instance() is None:
        if args.autostart:  # a Steam launch: the window already open runs the session
            _request_launch()
        return
    i18n.set_language(load_app_options().language or i18n.system_language())
    make_dpi_aware()  # sharp at 125 % and up instead of a stretched 96 DPI bitmap
    root = tk.Tk()
    # windowed build: no console for Tk's own traceback printout
    root.report_callback_exception = lambda _type, exc, _tb: log_exception(exc, "window")
    apply_theme(root)
    app = App(root, dry_run=args.dry_run, exit_with_game=args.exit_with_game, launch_event=_launch_event())
    dark_title_bar(root)
    if args.autostart:
        app.start()
    fit_window(root)
    if args.minimized:
        root.iconify()
    else:
        root.after(300, app.check_plugins)
    root.mainloop()


if __name__ == "__main__":
    main()
