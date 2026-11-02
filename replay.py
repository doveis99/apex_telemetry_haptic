"""Play a debug log (blocklog.py) back as telemetry: feel a recorded drive again, or run it
through today's synth and see what a change does to a real drive.

  python replay.py LOG.csv                       the drive through the synth with the log's settings
  python replay.py LOG.csv --from 19:17:50 --to 19:18:10
  python replay.py LOG.csv --settings current    with config.json's settings and motor curves
  python replay.py LOG.csv --knocks 0.5          also list every bump knock of at least 0.5
  python ats_haptics.py --replay LOG.csv         feel it on the pad (the window: 테스트 주행 ▾)

Each row was one 8 ms block, so a row is read per block and the drive keeps its own
pace and frame timing. The summary sets what the log recorded against the replay:
bump knocks and the strong ones, road beds brought back from their steady fade, and
how loud each channel ran.

Logs from before 0.16.0 hold only what the columns up to deflection_mm show: no
acceleration (no impacts), no wheel speeds (no wheelspin or lock), the front axle's
substance for every wheel on its side, and the game's clock from the wall clock.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import fields
from pathlib import Path
from typing import Iterator

from apex6 import MAX_SAMPLE
from effects import DT, Equalizer, Gains, Synth, effective_gains
from telemetry import Frame, Wheel

TAU = 2.0 * math.pi
WHEEL_RADIUS = 0.5  # m; the rim speed is what the log keeps, any radius gives it back
OLD_TRUCK_WHEELS = 6  # how many of an old log's wheels are the truck's (a 6x4 tractor)


def _seconds(clock: str) -> float:
    """HH:MM:SS(.fff) as seconds of the day."""
    h, m, s = clock.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _floats(text: str) -> list[float]:
    return [float(x) for x in text.split("/")] if text else []


class LogReader:
    """A block log's rows as the frames the game sent, one per 8 ms block.

    A row without a new frame gives the frame before it again, render_time and all,
    which is what the synth sees between game frames. A row without game data
    (pause, menu, loading) gives a paused frame.
    """

    def __init__(self, path: Path | str, start: str | None = None, end: str | None = None) -> None:
        self.path = Path(path)
        self.start = _seconds(start) if start else None
        self.end = _seconds(end) if end else None

    def rows(self) -> Iterator[tuple[str, dict[str, str], Frame]]:
        """(time of day, row, frame) for each block in the chosen stretch."""
        frame: Frame | None = None
        state = _State()
        first: float | None = None
        with self.path.open(encoding="utf-8", newline="") as file:
            for row in csv.DictReader(file):
                at = _seconds(row["time"])
                first = at if first is None else first
                if at < first - 43200:  # past midnight
                    at += 86400
                if row["new_frame"] == "1" or frame is None or frame.paused or not row["speed_kmh"]:
                    frame = state.frame(row, at)
                if self.start is not None and at < self.start:
                    continue
                if self.end is not None and at > self.end:
                    return
                yield row["time"], row, frame


class _State:
    """What a log holds over from one new frame to the next."""

    def __init__(self) -> None:
        self.substances: list[str] = []
        self.render_time = 0
        self.deflections: list[float] = []
        self.extra: dict[str, str] = {}  # the frame columns from 0.16.0 on, last written

    def frame(self, row: dict[str, str], at: float) -> Frame:
        if not row["speed_kmh"]:
            self.render_time += 1  # moves on, so the next live frame is new
            return Frame(active=True, paused=True, render_time=self.render_time)
        full = "render_us" in row
        if full and row["render_us"]:
            self.render_time = int(row["render_us"])
            self.extra = row
        elif not full and row["new_frame"] == "1":
            self.render_time = round(at * 1e6)
        x = self.extra if full else {}
        flag = lambda name: row[name] == "1"
        speed = float(row["speed_kmh"]) / 3.6
        throttle, brake = float(row["throttle"]), float(row["brake"])
        retarder = int(row["retarder"] or 0)
        frame = Frame(active=True, paused=False, render_time=self.render_time, speed=speed, rpm=float(row["rpm"]),
                      rpm_max=float(x.get("rpm_max") or 2100.0), user_throttle=throttle, user_brake=brake,
                      game_throttle=float(x["game_throttle"]) if x.get("game_throttle") else throttle,
                      game_brake=float(x["game_brake"]) if x.get("game_brake") else brake,
                      gear=int(row["gear"]), gear_engine=int(row["gear_engine"]), retarder=retarder,
                      retarder_steps=int(x["retarder_steps"]) if x.get("retarder_steps") else max(4, retarder),
                      engine_on=flag("engine_on"), motor_brake=flag("engine_brake"), park_brake=flag("park_brake"),
                      accel=tuple(_floats(x.get("accel", ""))) or (0.0, 0.0, 0.0),
                      air_pressure=float(x.get("air_pressure") or 120.0),
                      blinker_left="L" in row["blinkers"], blinker_right="R" in row["blinkers"],
                      shifter_toggle=("R" in row["shifter"], "S" in row["shifter"]),
                      trailer_attached=flag("trailer"), game_events=frozenset(x.get("game_events", "").split()))
        deflections = self.deflections = _floats(row["deflection_mm"]) or self.deflections
        rims = _floats(x.get("wheel_speed", ""))
        truck = len(rims) if full else OLD_TRUCK_WHEELS if frame.trailer_attached else len(deflections)
        if x.get("substances"):
            self.substances = x["substances"].split("/")
        airborne = {int(i) for i in x.get("airborne", "").split("/") if i}
        wheels = []
        for i, deflection in enumerate(deflections):
            side = -1 if i % 2 == 0 else 1  # left then right, axle by axle
            if full and i < len(self.substances):
                substance = self.substances[i]
            else:
                substance = row["surface_l"] if side < 0 else row["surface_r"]
            rim = rims[i] if i < len(rims) else speed
            wheels.append(Wheel(deflection=deflection / 1000.0, on_ground=i not in airborne, substance=substance,
                                pos_x=float(side), velocity=rim / (TAU * WHEEL_RADIUS), radius=WHEEL_RADIUS))
        frame.wheels, frame.trailer_wheels = wheels[:truck], wheels[truck:]
        return frame


class LogTelemetry:
    """A telemetry source that plays a block log, a row per read, in a loop with a pause between rounds."""

    PAUSE_BLOCKS = round(1.0 / DT)

    def __init__(self, path: Path | str, start: str | None = None, end: str | None = None) -> None:
        self.reader = LogReader(path, start, end)
        self.name = Path(path).name
        self._rows: Iterator | None = None
        self._pause = 0
        self._render_time = 0

    def close(self) -> None:
        pass

    def read(self) -> Frame:
        if self._pause:
            self._pause -= 1
            self._render_time += 1
            return Frame(active=True, paused=True, render_time=self._render_time)
        if self._rows is None:
            self._rows = self.reader.rows()
        try:
            _, _, frame = next(self._rows)
        except StopIteration:
            self._rows, self._pause = None, self.PAUSE_BLOCKS
            return self.read()
        self._render_time = frame.render_time
        return frame


def settings_of(log: Path) -> tuple[Gains, Equalizer, str]:
    """The gains and motor curves a log was recorded with, and its version; defaults where it has none."""
    info_path = log.with_suffix(".json")
    info = json.loads(info_path.read_text(encoding="utf-8")) if info_path.exists() else {}
    known = {f.name for f in fields(Gains)}
    gains = Gains(**{k: v for k, v in info.get("gains", {}).items() if k in known})
    curves = info.get("eq_curves")
    if curves is None and info.get("eq"):
        # older logs kept the curves as (ln Hz, ln gain + ln LEVEL) points
        curves = {channel: {f"{math.exp(x):.6g}": math.exp(y) / Equalizer.LEVEL[channel] for x, y in points}
                  for channel, points in info["eq"].items() if channel in Equalizer.LEVEL}
    return gains, Equalizer(curves or None), info.get("version", "?")


class Tally:
    """What one side of the comparison did: knocks, fade resets, how loud each channel ran.

    The log keeps no knock strengths, only which effects rang, so both sides count the
    times bumps began to ring (knocks) and only the replay its pulses and the strong ones.
    """

    def __init__(self) -> None:
        self.knocks = self.pulses = self.strong = self.fade_resets = 0
        self.peaks: tuple[list[int], list[int], list[int]] = ([], [], [])  # left, right, trigger
        self._knocking = False
        self._road = 1.0

    def block(self, peaks: tuple[int, int, int], pulses: list[float], road_fresh: float,
              bumps_sounding: bool) -> None:
        for out, peak in zip(self.peaks, peaks):
            out.append(peak)
        self.pulses += len(pulses)
        self.strong += sum(p >= 0.5 for p in pulses)
        self.knocks += bumps_sounding and not self._knocking
        self._knocking = bumps_sounding
        if road_fresh > 0.95 and self._road < 0.5:
            self.fade_resets += 1
        self._road = road_fresh

    def line(self, name: str) -> str:
        def pct(values: list[int], q: float) -> int:
            ordered = sorted(values)
            return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else 0

        grips = self.peaks[0] + self.peaks[1]
        limited = sum(p > 0.9 * MAX_SAMPLE for p in grips + self.peaks[2]) / max(1, len(grips) + len(self.peaks[2]))
        pulses = f"{self.pulses:6d} {self.strong:6d}" if name != "log" else "     -      -"
        return (f"{name:6s} {self.knocks:6d} {pulses} {self.fade_resets:7d}   {pct(grips, 0.5):4d} {pct(grips, 0.99):4d}"
                f"   {pct(self.peaks[2], 0.5):4d} {pct(self.peaks[2], 0.99):4d}   {100 * limited:5.2f}%")


def replay(path: Path, gains: Gains, eq: Equalizer, start: str | None = None, end: str | None = None,
           knocks: float | None = None) -> tuple[Tally, Tally, int]:
    """Run the log through a fresh synth; (the log's tally, the replay's, blocks)."""
    synth = Synth(effective_gains(gains), eq)
    logged, replayed = Tally(), Tally()
    seen: set[int] = set()
    blocks = 0
    for clock, row, frame in LogReader(path, start, end).rows():
        blocks += 1
        block = synth.render(frame)
        fresh = row["fresh"].split("/")
        logged.block((int(row["left_grip"]), int(row["right_grip"]), int(row["trigger"])), [],
                     float(fresh[1]) if len(fresh) == 3 else 1.0, "bumps" in row["sounding"].split())
        pulses = []
        for effect, _, burst in synth.bursts:
            if effect == "bumps" and burst.decay_s > 0.01 and id(burst) not in seen:  # pulses, not the cab's rattle
                seen.add(id(burst))
                pulses.append(burst.amplitude)
                if knocks is not None and burst.amplitude >= knocks:
                    print(f"  {clock}  knock {burst.amplitude:.2f} at {burst.freq:.0f} Hz, "
                          f"{float(row['speed_kmh'] or 0):.0f} km/h")
        seen &= {id(burst) for _, _, burst in synth.bursts}
        sounding = any(effect == "bumps" for effect, _, _ in synth.bursts)
        replayed.block(tuple(round(max(abs(x) for x in channel))
                             for channel in (block.left_grip, block.right_grip, block.trigger)),
                       pulses, synth.fresh["road"], sounding)
    return logged, replayed, blocks


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log", type=Path, help="a blocks-*.csv from the debug log")
    parser.add_argument("--from", dest="start", help="time of day to start at, HH:MM:SS")
    parser.add_argument("--to", dest="end", help="time of day to stop at, HH:MM:SS")
    parser.add_argument("--settings", choices=("log", "current"), default="log",
                        help="the gains and motor curves the log was recorded with, or config.json's")
    parser.add_argument("--knocks", type=float, metavar="STRENGTH", help="list the replay's bump knocks this strong")
    args = parser.parse_args()
    gains, eq, version = settings_of(args.log)
    if args.settings == "current":
        from settings import load_eq, load_gains
        gains, eq = load_gains(), Equalizer.for_player(load_eq())
    logged, replayed, blocks = replay(args.log, gains, eq, args.start, args.end, args.knocks)
    minutes, seconds = divmod(round(blocks * DT), 60)
    print(f"{args.log.name}: {blocks} blocks, {minutes}:{seconds:02d}, recorded with {version}; "
          f"settings from {'the log' if args.settings == 'log' else 'config.json'}")
    print("       knocks pulses strong fade-up   grip p50/p99  trig p50/p99  limited")
    print(logged.line("log"))
    print(replayed.line("replay"))


if __name__ == "__main__":
    sys.exit(main())
