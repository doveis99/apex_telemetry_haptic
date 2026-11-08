"""Set dongle placements side by side: how often the pad's tone drops out with the dongle at each place.

  python tools/dropout_compare.py [SERIES] [--dir DIR] [--channel N]

Reads every run of one `tone.py placement` series (the latest one in DIR when SERIES is not
given). Each run is the dropout check's tone in the app's mode (1k, no primers) with the dongle
at one place, the places taken in turn A B A B so the radio's bad spells, which come and go over
seconds, fall on every place. Gaps are counted as in dropout_analyze.py. Shows, per run and per
place, gaps per minute and the share of tone time lost; then for two places, how often a tone
at the first had fewer gaps than one at the second, over every such pair (0.5 = no difference,
1.0 = always fewer), which a single bad spell cannot swing much. Writes placement-SERIES.txt
beside the recordings.

Needs numpy, scipy and ffmpeg, as pad_sound_analyze.py. Not part of the app.
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dropout_analyze import SNR_MIN_DB, mapping, tone_gaps, tone_snr  # noqa: E402
from pad_sound_analyze import FS  # noqa: E402
from pad_sound_analyze import load  # noqa: E402

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "sound sample" / "pad-sound"
CLEAR, LEANING = 0.9, 0.75  # share of pairs with fewer gaps for "clearly" and "tends to"


def series_runs(folder: Path, series: str | None) -> tuple[str, list[tuple[Path, dict]]]:
    found = []
    for path in sorted(folder.glob("dropout-*.json")):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if meta.get("series") and meta.get("recording"):
            found.append((path, meta))
    if not found:
        sys.exit(f"{folder}에 위치 비교 녹음이 없음: python tone.py placement --record \"<마이크>\"로 녹음")
    series = series or max(meta["series"] for _, meta in found)
    runs = sorted(((p, m) for p, m in found if m["series"] == series), key=lambda pm: pm[1]["run"])
    if not runs:
        sys.exit(f"시리즈 {series} 없음. 있는 것: {', '.join(sorted({m['series'] for _, m in found}))}")
    return series, runs


def measure(path: Path, meta: dict, channel: int | None) -> tuple[list[tuple[int, float, float]], int] | str:
    """(gaps, seconds lost, seconds searched) for every tone of a run that stands out of the
    room's noise, and how many were left out; or why the run cannot be read."""
    wav = path.with_name(meta["recording"])
    if not wav.exists():
        return f"녹음 파일 없음 ({wav.name})"
    x, _, _ = load(wav, channel)
    try:
        a, b, _ = mapping(x, meta)
    except SystemExit as exc:  # no clicks found: nothing of the pad in this recording
        return str(exc)
    kept, dropped = [], 0
    for _, item, _, e, seg, found, seconds in tone_gaps(x, meta, a, b):
        if tone_snr(x, seg, item["freq"], int((a + b * e) * FS)) >= SNR_MIN_DB:
            kept.append((len(found), sum(f[1] for f in found), seconds))
        else:
            dropped += 1
    return kept, dropped


def per_minute(tones: list[tuple[int, float, float]]) -> float:
    return sum(t[0] for t in tones) / max(1e-9, sum(t[2] for t in tones)) * 60


def lost_share(tones: list[tuple[int, float, float]]) -> float:
    return sum(t[1] for t in tones) / max(1e-9, sum(t[2] for t in tones)) * 100


def fewer_share(first: list[float], second: list[float]) -> float:
    """How often a tone from first had fewer gaps per minute than one from second (ties count half)."""
    pairs = [(f < s) + 0.5 * (f == s) for f, s in itertools.product(first, second)]
    return float(np.mean(pairs))


def compare(folder: Path, series: str | None, channel: int | None) -> None:
    series, runs = series_runs(folder, series)
    meta0 = runs[0][1]
    lines = [f"동글 위치 비교 {series}: {meta0['freq']:g} Hz {meta0['amp']:g}%, 앱 모드(1k, 프라이머 0), "
             f"톤 {meta0['seconds']:g}초 × {meta0['rounds']}번씩, 계획한 순서 {' → '.join(meta0['order'])}"]
    if len(runs) < len(meta0["order"]):
        lines.append(f"!! {len(meta0['order'])}회 중 {len(runs)}회만 녹음됨")
    places: dict[str, list[tuple[int, float, float]]] = {}
    lines.append("")
    lines.append("회차별: 분당 끊김 (톤마다), 끊겨서 빈 시간")
    for path, meta in runs:
        measured = measure(path, meta, channel)
        name = f"   {meta['run']}. {meta['place']:<10}"
        if isinstance(measured, str):
            lines.append(f"{name} 읽지 못함: {measured}")
            continue
        tones, dropped = measured
        if not tones:
            lines.append(f"{name} 톤 {dropped}개 모두 잡음에 묻힘 (SNR {SNR_MIN_DB:g} dB 미만): 마이크를 가까이")
            continue
        warn = ("  !! 유선" if meta.get("connection_mode") == 0 else "") + (
            f"  (톤 {dropped}개는 잡음에 묻혀 뺌)" if dropped else "")
        lines.append(f"{name} {per_minute(tones):6.1f}/분 ({', '.join(f'{t[0] / t[2] * 60:.0f}' for t in tones)}), "
                     f"{lost_share(tones):.2f}%   {meta['recording']}{warn}")
        places.setdefault(meta["place"], []).extend(tones)
    lines.append("")
    lines.append("위치별")
    for place, tones in places.items():
        rates = [t[0] / t[2] * 60 for t in tones]
        lines.append(f"   {place:<10} {per_minute(tones):6.1f}/분, 빈 시간 {lost_share(tones):.2f}%  "
                     f"(톤 {len(tones)}개, 톤마다 {min(rates):.0f}~{max(rates):.0f}/분)")
    lines.append("")
    if len(places) >= 2:
        lines.append(f"짝 비교: 앞 위치의 톤이 뒤 위치의 톤보다 끊김이 적은 비율 (톤 짝 전부; 0.5면 차이 없음, "
                     f"{CLEAR:g} 넘으면 뚜렷)")
        for (p, pt), (q, qt) in itertools.combinations(places.items(), 2):
            share = fewer_share([t[0] / t[2] * 60 for t in pt], [t[0] / t[2] * 60 for t in qt])
            better, worse, strength = (p, q, share) if share >= 0.5 else (q, p, 1.0 - share)
            if strength >= CLEAR:
                verdict = f"'{better}'가 뚜렷이 적음"
            elif strength >= LEANING:
                verdict = f"'{better}'가 적은 편 (회차를 늘려 확인)"
            else:
                verdict = "차이 없음: 간섭이 오락가락하는 폭 안"
            lines.append(f"   {p} 대 {q}: {share:.2f} → {verdict}")
        if any(len(t) < 6 for t in places.values()):
            lines.append("   (위치마다 톤이 6개 안 되면 판정이 흔들림: --cycles를 늘리세요)")
    report = "\n".join(lines)
    print(report)
    out = folder / f"placement-{series}.txt"
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\n저장: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("series", nargs="?", help="series stamp tone.py printed (default: the latest)")
    parser.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="where the recordings are")
    parser.add_argument("--channel", type=int, help="1-based input channel (default: the louder one)")
    args = parser.parse_args()
    compare(args.dir, args.series, args.channel)


if __name__ == "__main__":
    main()
