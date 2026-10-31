"""Debug log of what went to the controller, one CSV row per 8 ms block.

Off by default (app option debug_log, or --log-blocks on the command line).
Each session writes %APPDATA%\\ATSHaptics\\logs\\blocks-<date>-<time>.csv and
beside it a .json with the version, the telemetry source, the settings and
the motor curves it started with; only the newest KEEP sessions are kept.
About 80 MB per hour of driving.

When something feels wrong, note the time on the clock and look up that row.
The columns, so a row answers what the game said, what the synth made of it
and what reached the motors:

  time, gap_ms   wall clock; over 8 ms since the previous block means the
                 output loop ran late
  new_frame      1 when the game had rendered a new frame since the last
                 block (at 60 fps about every other one)
  route, trigger, left_grip, right_grip
                 what was sent: the trigger route and each channel's peak
                 sample (+-112 full scale)
  speed_kmh ... trailer
                 the game's values the effects follow; blinkers L/R as the
                 lamp shows, shifter R (range) / S (splitter) while switched
  surface_l, surface_r
                 under the front axle's left and right wheel
  deflection_mm  suspension of every wheel, truck then trailers, left to
                 right axle by axle; only on a new frame, to see what a bump
                 was made of (or why one fired on a smooth road)
  engine, road_l, road_r, trigger_level, trigger_mode
                 the continuous effects' levels before the output stage
  fresh          engine/road/trigger steady fade: 1 just after a change,
                 falling while it holds (effects.STEADY_BEDS)
  duck           left/right/trigger ducking of the bed under a knock
  sounding       effects with a knock or burst still ringing; a ▶ name is
                 an effect test playing in place of the drive
  events         what the synth noticed this block, settings changed from
                 the window, and why there was no frame (pause, menu, ...)
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from apex6 import Block
from settings import CONFIG_PATH
from telemetry import Frame

if TYPE_CHECKING:
    from effects import Synth

LOG_DIR = CONFIG_PATH.parent / "logs"
KEEP = 10
FLUSH_S = 1.0
COLUMNS = ("time", "gap_ms", "new_frame", "route", "trigger", "left_grip", "right_grip",
           "speed_kmh", "rpm", "gear", "gear_engine", "throttle", "brake", "retarder", "engine_brake", "park_brake",
           "blinkers", "shifter", "engine_on", "trailer", "surface_l", "surface_r", "deflection_mm",
           "engine", "road_l", "road_r", "trigger_level", "trigger_mode", "fresh", "duck", "sounding", "events")
GAME_COLUMNS = COLUMNS.index("deflection_mm") + 1 - COLUMNS.index("speed_kmh")


def _peak(samples: Sequence[float]) -> int:
    return round(max((abs(x) for x in samples), default=0.0))


def _flag(on: bool) -> str:
    return "1" if on else ""


def _side(frame: Frame, side: int) -> str:
    """The substance under the front axle's wheel on that side (-1 left, 1 right)."""
    return next((w.substance for w in frame.wheels if (w.pos_x < 0) == (side < 0)), "")


class BlockLog:
    def __init__(self, directory: Path = LOG_DIR, info: dict | None = None) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        old = sorted(directory.glob("blocks-*.csv"))
        for path in old[:max(0, len(old) - (KEEP - 1))]:
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
        self.path = directory / f"blocks-{datetime.now():%Y%m%d-%H%M%S}.csv"
        if info is not None:
            self.path.with_suffix(".json").write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                                      encoding="utf-8")
        self._file = self.path.open("w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._file)
        self._csv.writerow(COLUMNS)
        self._last: float | None = None
        self._last_render_time: int | None = None
        self._flushed = time.monotonic()

    def add(self, block: Block, frame: Frame | None, events: Sequence[str], synth: Synth | None = None,
            preview: str = "") -> None:
        now = time.perf_counter()
        gap = (now - self._last) * 1000.0 if self._last is not None else 0.0
        self._last = now
        new_frame = ""
        if frame is not None:
            new_frame = _flag(frame.render_time != self._last_render_time)
            self._last_render_time = frame.render_time
            deflection = "/".join(f"{w.deflection * 1000:.1f}" for w in frame.wheels + frame.trailer_wheels)
            game = (f"{frame.speed * 3.6:.1f}", f"{frame.rpm:.0f}", frame.gear, frame.gear_engine,
                    f"{frame.user_throttle:.2f}", f"{frame.user_brake:.2f}", frame.retarder or "",
                    _flag(frame.motor_brake), _flag(frame.park_brake),
                    "L" * frame.blinker_left + "R" * frame.blinker_right,
                    "R" * frame.shifter_toggle[0] + "S" * frame.shifter_toggle[1],
                    _flag(frame.engine_on), _flag(frame.trailer_attached), _side(frame, -1), _side(frame, 1),
                    deflection if new_frame else "")
        else:
            game = ("",) * GAME_COLUMNS
        if synth is not None:
            lv = synth.levels
            sounding = sorted({effect for effect, _, burst in synth.bursts if not burst.done})
            inner = (f"{lv.engine:.2f}", f"{lv.road_left:.2f}", f"{lv.road_right:.2f}", f"{lv.trigger:.2f}",
                     lv.trigger_mode, "/".join(f"{synth.fresh[bed]:.2f}" for bed in ("engine", "road", "trigger")),
                     "/".join(f"{d:.2f}" for d in synth.duck),
                     " ".join(([f"▶{preview}"] if preview else []) + sounding))
        else:
            inner = ("",) * 8
        self._csv.writerow((f"{datetime.now():%H:%M:%S.%f}"[:-3], f"{gap:.1f}", new_frame, block.route.name,
                            _peak(block.trigger), _peak(block.left_grip), _peak(block.right_grip),
                            *game, *inner, "; ".join(events)))
        if time.monotonic() - self._flushed > FLUSH_S:
            self._file.flush()
            self._flushed = time.monotonic()

    def close(self) -> None:
        self._file.close()
