"""Record what the Python synth makes of known drives, for the Rust port to match sample by sample.

  .venv\\Scripts\\python.exe rust\\golden\\make_golden.py            every run below
  .venv\\Scripts\\python.exe rust\\golden\\make_golden.py sim60      only runs whose name starts so

Writes rust/golden-data/<run>.jsonl (gitignored): a header line with the settings, the
global random seed and, for the scripted drive, the clock it was read at; then one line
per 8 ms block with the telemetry frame the synth was given ("f": 1 when it equals the
block before's), the three channels as floats, the route, the trigger side, the events and
a few internals to find where a difference starts. The Rust test (rust/tests/parity.rs)
feeds the same frames to its synth and compares.

Debug logs (blocks-*.csv beside a .json) under %APPDATA%\\ATSHaptics\\logs give real drives:
the first LOG_BLOCKS rows of the newest LOGS of them, with the settings they were recorded with.
"""

from __future__ import annotations

import json
import random
import sys
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import i18n  # noqa: E402
from apex6 import FRAME_PERIOD  # noqa: E402
from blocklog import LOG_DIR  # noqa: E402
from effects import DEFAULT_ROUTES, Equalizer, Gains, Synth, effective_gains  # noqa: E402
from replay import LogReader, settings_of  # noqa: E402
from scenarios import SCENARIOS, ScenarioTelemetry  # noqa: E402
from telemetry import Frame, SimulatedTelemetry  # noqa: E402

OUT = ROOT / "rust" / "golden-data"
SEED = 20261108  # the global random module's, which NoiseBurst draws its seeds from
LOGS = 4
LOG_BLOCKS = 30_000  # 4 minutes of each


class Clock:
    """What the scripted sources read as the time: block n is read at n * FRAME_PERIOD."""

    def __init__(self) -> None:
        self.block = 0

    def __call__(self) -> float:
        return self.block * FRAME_PERIOD


def frame_dict(frame: Frame | None):
    if frame is None:
        return None
    data = asdict(frame)
    data["game_events"] = sorted(frame.game_events)
    return data


def write_run(name: str, frames, gains: Gains, eq: Equalizer, extra: dict | None = None) -> None:
    """frames yields (frame or None); each one is rendered as one block."""
    gains = effective_gains(gains)
    random.seed(SEED)
    synth = Synth(gains, eq)
    path = OUT / f"{name}.jsonl"
    blocks = 0
    with path.open("w", encoding="utf-8", newline="\n") as out:
        header = {"name": name, "seed": SEED, "gains": asdict(gains), "eq_curves": eq.curves, **(extra or {})}
        out.write(json.dumps(header, ensure_ascii=False, allow_nan=False) + "\n")
        previous = object()
        for frame in frames:
            block = synth.render(frame)
            same = frame is not None and isinstance(previous, Frame) and frame == previous
            lv = synth.levels
            row = {
                "f": 1 if same else frame_dict(frame),
                "t": list(block.trigger), "l": list(block.left_grip), "r": list(block.right_grip),
                "route": int(block.route), "side": None if block.side is None else int(block.side),
                "ev": list(lv.events),
                "lv": [lv.engine, lv.road_left, lv.road_right, lv.trigger, lv.trigger_mode, lv.surface,
                       lv.max_accel],
                "fr": [synth.fresh["engine"], synth.fresh["road"], synth.fresh["trigger"]],
                "dk": list(synth.duck),
            }
            out.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
            previous = frame
            blocks += 1
    print(f"{name}: {blocks} blocks -> {path.relative_to(ROOT)}")


def scripted(source_factory, seconds: float):
    """A scripted source read once per block on the fake clock."""
    clock = Clock()
    source = source_factory(clock)
    for n in range(round(seconds / FRAME_PERIOD)):
        clock.block = n
        yield source.read()


def runs():
    default_eq = Equalizer.for_player({})
    for fps, label in ((60.0, "sim60"), (30.0, "sim30"), (None, "simall")):
        yield (f"{label}_default", lambda fps=fps: scripted(lambda c: SimulatedTelemetry(fps, c), 70.0),
               Gains(), default_eq, {"sim_fps": fps, "seconds": 70.0})
    routes = dict(DEFAULT_ROUTES, engine="both", road="trigger", bumps="both", gear="trigger", squeal="grip",
                  trigger="both", blinker="both", wheelspin="both")
    variants = Gains(gearbox="converter", cylinders=8, steady_fade=0.0, routes=routes, disabled=["engine_start"],
                     bump_threshold=0.15)
    yield ("sim60_variants", lambda: scripted(lambda c: SimulatedTelemetry(60.0, c), 62.0), variants,
           Equalizer(), {"sim_fps": 60.0, "seconds": 62.0})
    custom = Equalizer.for_player({"grip": {"40": 2.5, "55": 1.4, "70": 1.0, "90": 1.8, "120": 2.6, "180": 3.0},
                                   "trigger": {"60": 3.0, "90": 1.6, "110": 1.0, "150": 2.0, "220": 2.4}})
    louder = Gains(master=1.4, grip_level=0.8, ducking=0.0, throttle_pulse=0.3, cylinders=4, road=0.9, engine=0.4,
                   steady_fade=0.3)
    yield ("sim60_custom", lambda: scripted(lambda c: SimulatedTelemetry(60.0, c), 62.0), louder, custom,
           {"sim_fps": 60.0, "seconds": 62.0})
    for name, scenario in SCENARIOS.items():
        seconds = scenario.seconds + 2.0
        yield (f"scenario_{name}", lambda s=scenario, sec=seconds: scripted(lambda c: ScenarioTelemetry(s, 60.0, c),
                                                                              sec),
               Gains(), default_eq, {"scenario": name, "seconds": seconds})
    logs = sorted(LOG_DIR.glob("blocks-*.csv"), reverse=True)
    for log in [log for log in logs if log.with_suffix(".json").exists()][:LOGS]:
        gains, eq, _ = settings_of(log)

        def rows(log=log):
            for n, (_, _, frame) in enumerate(LogReader(log).rows()):
                if n >= LOG_BLOCKS:
                    return
                yield frame

        yield (f"log_{log.stem}", rows, gains, eq, {"log": log.name})


def main() -> None:
    i18n.set_language("ko")  # events in the source language, as the Rust port writes them for now
    OUT.mkdir(parents=True, exist_ok=True)
    wanted = sys.argv[1:]
    for name, frames, gains, eq, extra in runs():
        if wanted and not any(name.startswith(w) for w in wanted):
            continue
        write_run(name, frames(), replace(gains), eq, extra)


if __name__ == "__main__":
    main()
