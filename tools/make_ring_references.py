"""Build the reference sounds for the ring-level check (examples/ring_level.rs) from the sound
mods in "sound sample".

Each reference lays the mods' own clips on the timeline of one effect test (preview.rs scripts),
so the truck's click and the pad's come at the same moments:

  gear     gear shifts at 0.3 / 0.9 / 1.5 s (gear_shift_01, 05, 10)
  shifter  range on at 0.3, splitter at 0.9, range off at 1.5 (knob + gearbox clips)
  blinker  relay on/off edges, left side first, then right (panned)
  trailer  coupling at 0.3, uncoupling at 1.5 (Sound Fixes Pack attach5, attach3)
  road     3 s of gravel (Sound Fixes Pack gravel_ext_01, from 2 s)

Writes "sound sample/ring-reference/<effect>.wav" (48 kHz, 16-bit stereo, peak -6 dBFS). The
folder is git-ignored: the audio stays on this PC and is never committed or shipped.

Needs fsb5 (pip install fsb5) and libvorbis/libogg DLLs whose folder is passed with --dlls
(Anaconda's site-packages/av.libs has them), plus numpy and soundfile.

  python tools/make_ring_references.py --dlls C:\\Users\\me\\anaconda3\\Lib\\site-packages\\av.libs
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOUNDS = ROOT / "sound sample"
OUT = SOUNDS / "ring-reference"
RATE = 48000
PEAK_DB = -6.0

COMMON = "zeemods.common_v1.3.1/_unknown/30aa00cb78b5fefe.bank"
WS = "zeemods.ws.effects_g5p_v1.0.1/sound/truck/zeemods.ws.effects.bank"
COUPLING = "Sound Fixes Pack/sound_fixes_pack/truck/trailer_coupling.bank"
ROAD = "Sound Fixes Pack/sound_fixes_pack/tires/road_noise.bank"

# effect: (seconds, [(bank, sample, at s, pan -1 left .. 1 right, from s, length s or None)])
REFERENCES = {
    "gear": (2.0, [(COMMON, "gear_shift_01", 0.3, 0.0, 0.0, None),
                   (COMMON, "gear_shift_05", 0.9, 0.0, 0.0, None),
                   (COMMON, "gear_shift_10", 1.5, 0.0, 0.0, None)]),
    "shifter": (2.2, [(COMMON, "stick_gear_range_01b", 0.3, 0.0, 0.0, None),
                      (COMMON, "gear_range_01", 0.3, 0.0, 0.0, None),
                      (COMMON, "stick_gear_split_02", 0.9, 0.0, 0.0, None),
                      (COMMON, "gear_split_02", 0.9, 0.0, 0.0, None),
                      (COMMON, "stick_gear_range_02b", 1.5, 0.0, 0.0, None),
                      (COMMON, "gear_range_02", 1.5, 0.0, 0.0, None)]),
    "blinker": (3.6, [(WS, "blinker_relay_on" if on else "blinker_relay_off", at, pan, 0.0, None)
                      for pan, edges in ((-0.8, (0.4, 0.8, 1.2, 1.6)), (0.8, (2.0, 2.4, 2.8, 3.2)))
                      for on, at in zip((True, False, True, False), edges)]),
    # the test couples at 0.3 and uncouples at 1.5; the pack has no uncoupling sound of its own
    "trailer": (2.4, [(COUPLING, "attach5", 0.3, 0.0, 0.0, None), (COUPLING, "attach3", 1.5, 0.0, 0.0, None)]),
    "road": (3.0, [(ROAD, "gravel_ext_01", 0.0, 0.0, 2.0, 3.0)]),
}


def decoder(dlls: Path):
    """A function (bank, sample) -> mono float samples at RATE, through fsb5 and libvorbis."""
    import fsb5
    import numpy as np
    import soundfile as sf

    work = Path(tempfile.mkdtemp(prefix="ringref-"))
    for dll in dlls.glob("*.dll"):  # libvorbis imports libogg by its own (hashed) file name
        shutil.copy(dll, work / dll.name)
    for prefix, name in (("libogg", "libogg.dll"), ("libvorbis-", "libvorbis.dll"), ("libvorbisenc", "libvorbisenc.dll")):
        found = next((p for p in work.iterdir() if p.name.startswith(prefix)), None)
        if found and not (work / name).exists():
            shutil.copy(found, work / name)
    os.add_dll_directory(str(work))
    os.chdir(work)  # fsb5 looks for lib<name>.dll in the working directory
    banks: dict[str, list] = {}

    def fsbs(bank: str) -> list:
        if bank not in banks:
            data = (SOUNDS / bank).read_bytes()
            found, at = [], 0
            while (at := data.find(b"FSB5", at)) >= 0:
                try:
                    found.append(fsb5.FSB5(data[at:]))
                except Exception:
                    pass
                at += 4
            banks[bank] = found
        return banks[bank]

    def decode(bank: str, name: str):
        for fsb in fsbs(bank):
            for sample in fsb.samples:
                if sample.name == name:
                    ogg = work / "clip.ogg"
                    ogg.write_bytes(fsb.rebuild_sample(sample))
                    x, sr = sf.read(str(ogg))
                    if sr != RATE:
                        sys.exit(f"{name}: {sr} Hz, expected {RATE}")
                    return x.mean(axis=1) if x.ndim > 1 else np.asarray(x)
        sys.exit(f"no sample {name} in {bank}")

    return decode


def build(seconds: float, clips: list, decode):
    import numpy as np

    out = np.zeros((int(seconds * RATE), 2))
    for bank, name, at, pan, start, length in clips:
        x = decode(bank, name)[int(start * RATE):]
        if length is not None:
            x = x[: int(length * RATE)].copy()
            fade = int(0.1 * RATE)
            x[:fade] *= np.linspace(0, 1, fade)
            x[-fade:] *= np.linspace(1, 0, fade)
        begin = int(at * RATE)
        x = x[: len(out) - begin]
        gains = (np.sqrt((1 - pan) / 2), np.sqrt((1 + pan) / 2))  # constant power
        for channel, gain in enumerate(gains):
            out[begin: begin + len(x), channel] += gain * x
    tail = int(0.05 * RATE)
    out[-tail:] *= np.linspace(1, 0, tail)[:, None]
    return out * (10 ** (PEAK_DB / 20) / np.abs(out).max())


def main() -> None:
    import soundfile as sf

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dlls", type=Path, required=True, help="folder with the libvorbis and libogg DLLs")
    args = parser.parse_args()
    decode = decoder(args.dlls.resolve())
    OUT.mkdir(parents=True, exist_ok=True)
    for effect, (seconds, clips) in REFERENCES.items():
        path = OUT / f"{effect}.wav"
        sf.write(str(path), build(seconds, clips, decode), RATE, subtype="PCM_16")
        print(path)


if __name__ == "__main__":
    main()
