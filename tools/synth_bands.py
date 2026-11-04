"""Where the synth's output sits in frequency and how hard it plays, per channel.

  python tools/synth_bands.py                         the three scenarios (scenarios.py), default settings
  python tools/synth_bands.py --scenario mountain --each
  python tools/synth_bands.py --log LOG.csv --from 19:17:50 --to 19:18:10

The pad holds each 1 kHz sample, so everything sent comes with its images above 500 Hz,
and the shell radiates 300-500 Hz toward the player clearly (tone.py pad-sound). This
shows how much of each channel's energy lies in each band below 500 Hz, and how often
the samples pass the strengths of pad-sound's ladder, where distortion can set in.
--each runs every effect alone (the others switched off).

Needs numpy (tools/requirements.txt). Not part of the app.
"""

from __future__ import annotations

import argparse
import sys
import types
from dataclasses import replace
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    import hid  # noqa: F401
except ImportError:  # only the pad needs it; the synth's constants live in the same module
    sys.modules["hid"] = types.ModuleType("hid")

from apex6 import MAX_SAMPLE, SAMPLE_RATE  # noqa: E402
from effects import DT, TOGGLEABLE, Equalizer, Gains, Synth, effective_gains  # noqa: E402

BANDS = ((0, 150), (150, 300), (300, 450), (450, 500))
LADDER = (0.2, 0.35, 0.5)  # of MAX_SAMPLE, as tone.py pad-sound's --amp
CHANNELS = ("left_grip", "trigger")


def frames_of_scenario(name: str):
    from scenarios import SCENARIOS, ScenarioTelemetry
    clock = [0.0]
    telemetry = ScenarioTelemetry(SCENARIOS[name], clock=lambda: clock[0])
    for _ in range(int(SCENARIOS[name].seconds / DT)):
        yield telemetry.read()
        clock[0] += DT


def frames_of_log(path: Path, start: str | None, end: str | None):
    from replay import LogReader
    for _, _, frame in LogReader(path, start, end).rows():
        yield frame


def render(frames, gains: Gains, eq: Equalizer) -> dict[str, np.ndarray]:
    synth = Synth(effective_gains(gains), eq)
    out: dict[str, list[float]] = {channel: [] for channel in CHANNELS}
    for frame in frames:
        block = synth.render(frame)
        for channel in CHANNELS:
            out[channel] += getattr(block, channel)
    return {channel: np.array(samples) for channel, samples in out.items()}


def line(label: str, x: np.ndarray) -> str | None:
    if not np.any(x):
        return None
    spec = np.abs(np.fft.rfft(x)) ** 2
    freqs = np.fft.rfftfreq(len(x), 1.0 / SAMPLE_RATE)
    total = spec[1:].sum()
    shares = [spec[(freqs >= lo) & (freqs < hi)].sum() / total * 100 for lo, hi in BANDS]
    a = np.abs(x)
    over = " ".join(f"{np.mean(a > level * MAX_SAMPLE) * 100:5.1f}" for level in LADDER)
    return (f"{label:30s} " + " ".join(f"{share:6.1f}" for share in shares)
            + f" | {np.percentile(a, 99):5.1f} {a.max():5.1f} | {over}")


def header() -> str:
    bands = " ".join(f"{f'{lo}-{hi}':>6s}" for lo, hi in BANDS)
    over = " ".join(f">{level * 100:3.0f}%" for level in LADDER)
    return f"{'% of energy below 500 Hz':30s} {bands} |   p99   max | {over}  (of samples; {MAX_SAMPLE} = 100%)"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scenario", action="append", help="highway, city or mountain (default: all three)")
    parser.add_argument("--log", type=Path, help="a blocks-*.csv from the debug log, instead of the scenarios")
    parser.add_argument("--from", dest="start", help="time of day to start the log at, HH:MM:SS")
    parser.add_argument("--to", dest="end", help="time of day to stop the log at, HH:MM:SS")
    parser.add_argument("--settings", choices=("default", "log", "current"), default="default",
                        help="default gains and motor curves, the log's, or config.json's")
    parser.add_argument("--each", action="store_true", help="also every effect alone")
    args = parser.parse_args()

    gains, eq = Gains(), Equalizer.for_player({})
    if args.settings == "log":
        if not args.log:
            parser.error("--settings log needs --log")
        from replay import settings_of
        gains, eq, _ = settings_of(args.log)
    elif args.settings == "current":
        from settings import load_eq, load_gains
        gains, eq = load_gains(), Equalizer.for_player(load_eq())

    if args.log:
        drives = {args.log.stem: lambda: frames_of_log(args.log, args.start, args.end)}
    else:
        from scenarios import SCENARIOS
        names = args.scenario or list(SCENARIOS)
        drives = {name: (lambda name=name: frames_of_scenario(name)) for name in names}

    print(header())
    for name, frames in drives.items():
        for channel, x in render(frames(), gains, eq).items():
            if text := line(f"{name} {channel}", x):
                print(text)
        if not args.each:
            continue
        for effect in TOGGLEABLE:
            alone = replace(gains, disabled=[n for n in TOGGLEABLE if n != effect])
            for channel, x in render(frames(), alone, eq).items():
                if text := line(f"  {effect} {channel}", x):
                    print(text)


if __name__ == "__main__":
    main()
