"""Debug log of what went to the controller, one CSV row per 8 ms block.

Off by default (app option debug_log, or --log-blocks on the command line).
Each session writes %APPDATA%\\ATSHaptics\\logs\\blocks-<date>-<time>.csv;
only the newest KEEP files are kept. About 30 MB per hour.

Columns: wall-clock time, gap since the previous block (over 8 ms means the
output loop ran late), trigger route, peak sample per channel (+-112 full
scale), what the game reported and the events of that block. When something
feels wrong, note the time on the clock and look up that row.
"""

from __future__ import annotations

import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from apex6 import Block
from settings import CONFIG_PATH
from telemetry import Frame

LOG_DIR = CONFIG_PATH.parent / "logs"
KEEP = 10
FLUSH_S = 1.0
COLUMNS = ("time", "gap_ms", "route", "trigger", "left_grip", "right_grip",
           "speed_kmh", "gear", "gear_engine", "throttle", "brake", "surface", "events")


def _peak(samples: Sequence[float]) -> int:
    return round(max((abs(x) for x in samples), default=0.0))


class BlockLog:
    def __init__(self, directory: Path = LOG_DIR) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        old = sorted(directory.glob("blocks-*.csv"))
        for path in old[:max(0, len(old) - (KEEP - 1))]:
            path.unlink(missing_ok=True)
        self.path = directory / f"blocks-{datetime.now():%Y%m%d-%H%M%S}.csv"
        self._file = self.path.open("w", newline="", encoding="utf-8")
        self._csv = csv.writer(self._file)
        self._csv.writerow(COLUMNS)
        self._last: float | None = None
        self._flushed = time.monotonic()

    def add(self, block: Block, frame: Frame | None, events: Sequence[str]) -> None:
        now = time.perf_counter()
        gap = (now - self._last) * 1000.0 if self._last is not None else 0.0
        self._last = now
        if frame is not None:
            wheel = frame.wheels[0].substance if frame.wheels else ""
            game = (f"{frame.speed * 3.6:.1f}", frame.gear, frame.gear_engine, f"{frame.user_throttle:.2f}",
                    f"{frame.user_brake:.2f}", wheel)
        else:
            game = ("", "", "", "", "", "")
        self._csv.writerow((f"{datetime.now():%H:%M:%S.%f}"[:-3], f"{gap:.1f}", block.route.name,
                            _peak(block.trigger), _peak(block.left_grip), _peak(block.right_grip),
                            *game, "; ".join(events)))
        if time.monotonic() - self._flushed > FLUSH_S:
            self._file.flush()
            self._flushed = time.monotonic()

    def close(self) -> None:
        self._file.close()
