"""Analyse a microphone recording of `tone.py dropout`: how often does the tone drop out, per mode?

  python tools/dropout_analyze.py REC.wav [--channel N]

Reads REC.json beside it (tone.py writes both) and writes REC-report.txt and REC.png.

Over the 2.4 GHz dongle the pad's tone falls out for 4-20 ms at a time (rate-2k recordings of
2026-11-08), in 1k as well as 2k; wired never. The check plays one long tone per section, each
mode and primer count in turn, every one starting with the pad's queue dry. This reports:
  1. every gap per section (the tone falling DIP_DB under its median), and how the gaps spread
     over the section's quarters: spare that melts away shows as gaps piling up late
  2. gaps per minute for each mode and primer count, rounds kept apart
  3. the pad's clock against the PC's. The tone's pitch gives the pad against the microphone,
     the clicks at both ends give the microphone against the PC. A pad that drains faster than
     the PC sends eats its spare by that many microseconds a second, and then primers only
     put the gaps off
  4. late or lost: a report that comes late pushes the rest of the tone back, so the tone grows
     by the gap (and the queue keeps that as spare); a report that never comes leaves a hole and
     the length stays. How long after its report went out the tone starts shows whether the
     spare that was sent (read from the send times, not the primer count) stayed in the queue

Needs numpy, scipy, matplotlib and ffmpeg, as pad_sound_analyze.py. Not part of the app.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from scipy import signal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pad_sound_analyze import FS, load  # noqa: E402
from rate2k_analyze import DIP_DB, SUBFRAMES, click_times, dips, items, locate  # noqa: E402

END_SEARCH_S = 0.15   # the end clicks are looked for this far either side of where the start puts them
EARLY_S = 5.0         # gaps in the first seconds of a section, against the rest
# A tone that barely stands out of the room's noise in its own band reads as a stream of gaps:
# 2026-11-08 14:27 (wired) and 14:30 (wireless) had the 310 Hz tone 0-3 dB over the quiet and
# counted ~1450 gaps a minute alike, while wired at 15-18 dB counted none. Sections under this
# are left out of every count.
SNR_MIN_DB = 15.0
PITCH_WINDOW_S = 1.0
EDGE_SEARCH_S = 0.15  # a tone's end is looked for from this far before where the PC's pace puts it
LOUD_RUN_S = 0.03     # a tone has started once its band stays this long over half its level
QUIET_RUN_S = 0.06    # and ended once it stays this long under (longer than any gap seen)
INK, INK_2, SURFACE, SERIES = "#0b0b0b", "#52514e", "#fcfcfb", "#2a78d6"


def mapping(x: np.ndarray, meta: dict) -> tuple[float, float, list[float]]:
    """Recording second = a + b * host second, fitted through every set of clicks; and each
    set's distance from the fit (ms). A set whose first report the radio held back sounds late
    as a whole; the fit takes medians, so one or two such sets do not tilt it."""
    groups = [click_times(meta, i) for i, s in enumerate(meta["sections"]) if not s["round"]]
    # All sets look alike; the whole run must fit in the recording, which keeps the first set
    # from matching a later one. Each later set is looked for near where the one before put it.
    found = []
    lo, hi = 0.0, len(x) / FS - groups[-1][-1] - 0.05
    for clicks in groups:
        offset, _, ratio, scatter = locate(x, clicks, lo, hi)
        if ratio < 4 and scatter > 3:
            if not found:
                sys.exit(f"첫 딸깍을 찾지 못함 (최고 점수가 중앙값의 {ratio:.1f}배): 녹음에 패드 소리가 없는 듯")
            continue
        found.append((float(np.mean(clicks)), offset))
        lo, hi = offset - END_SEARCH_S, offset + END_SEARCH_S
    c = np.array([f[0] for f in found])
    off = np.array([f[1] for f in found])
    if len(found) < 2:
        slope = 0.0
    else:  # Theil-Sen: the median of the slopes between every two sets
        i, j = np.triu_indices(len(found), 1)
        slope = float(np.median((off[j] - off[i]) / (c[j] - c[i])))
    intercept = float(np.median(off - slope * c))
    residual = [round(float(v) * 1000, 1) for v in off - (intercept + slope * c)]
    return intercept, 1.0 + slope, residual


def pitch_offset(seg: np.ndarray, freq: float) -> float:
    """How far the tone sits from freq (Hz, recording clock): the median slope of its phase over
    one-second windows, so a gap's jump spoils one window and not the answer. NaN when too short."""
    t = np.arange(len(seg)) / FS
    sos = signal.butter(4, 20.0, "low", fs=FS, output="sos")
    base = seg * np.exp(-2j * np.pi * freq * t)
    z = signal.sosfiltfilt(sos, base.real) + 1j * signal.sosfiltfilt(sos, base.imag)
    step = FS // 100
    z = z[::step]
    phase = np.unwrap(np.angle(z))
    level = np.abs(z)
    good = level > np.median(level) / 2  # 6 dB
    n = int(PITCH_WINDOW_S * 100)
    slopes = []
    for i in range(0, len(z) - n + 1, n):
        if good[i:i + n].all():
            slopes.append(np.polyfit(np.arange(n) / 100, phase[i:i + n], 1)[0] / (2 * np.pi))
    return float(np.median(slopes)) if slopes else float("nan")


def tone_edges(x: np.ndarray, start: float, end: float, freq: float) -> tuple[float, float] | None:
    """Where a tone starts and ends in the recording (seconds; start and end are where the PC's
    pace puts them). Measured in the tone's own band at half its level, so a room noise after
    the end does not count; the end is the first quiet stretch after end - EDGE_SEARCH_S, so a
    tone the radio pushed later is followed to where it really stops. None if it cannot tell."""
    lo = max(0, int((start - EDGE_SEARCH_S) * FS))
    hi = min(len(x), int((end + 1.0) * FS))
    sos = signal.butter(4, [freq - 50.0, freq + 50.0], "bandpass", fs=FS, output="sos")
    env = np.abs(signal.hilbert(signal.sosfiltfilt(sos, x[lo:hi])))
    width = int(0.001 * FS)
    env = np.convolve(env, np.ones(width) / width, "same")
    body = env[int((start + 0.5) * FS) - lo:int((end - 0.5) * FS) - lo]
    if not len(body):
        return None
    half = np.median(body) / 2

    def first_run(mask: np.ndarray, seconds: float, frm: int) -> int | None:
        n = int(seconds * FS)
        hits = np.flatnonzero(np.convolve(mask[frm:].astype(int), np.ones(n, int), "valid") == n)
        return frm + int(hits[0]) if len(hits) else None

    on = first_run(env > half, LOUD_RUN_S, 0)
    off = first_run(env < half, QUIET_RUN_S, max(0, int((end - EDGE_SEARCH_S) * FS) - lo))
    if on is None or off is None:
        return None
    return (lo + on) / FS, (lo + off) / FS


def tone_gaps(x: np.ndarray, meta: dict, a: float, b: float):
    """For every tone: (section, item, host start, host end, its stretch of the recording, gaps
    as dips() gives them, seconds searched). Recording second = a + b * host second."""
    for section, item, s, e in items(meta):
        if item["kind"] == "sine":
            seg = x[int((a + b * (s + 0.05)) * FS):int((a + b * (e - 0.05)) * FS)]
            found, seconds = dips(seg, item["freq"])
            yield section, item, s, e, seg, found, seconds


def tone_snr(x: np.ndarray, seg: np.ndarray, freq: float, end: int) -> float:
    """How far the tone stands over the room in its own band (dB): its stretch against the quiet
    from 0.15 to 0.75 s after it ends (end: recording sample of the tone's end)."""
    sos = signal.butter(4, [freq - 50.0, freq + 50.0], "bandpass", fs=FS, output="sos")
    tone = signal.sosfiltfilt(sos, seg)[int(0.5 * FS):-int(0.5 * FS)]
    quiet = signal.sosfiltfilt(sos, x[end + int(0.15 * FS):end + int(0.75 * FS)])
    if not len(tone) or not len(quiet):
        return float("nan")
    return float(20 * np.log10(np.sqrt(np.mean(tone ** 2)) / (np.sqrt(np.mean(quiet ** 2)) + 1e-12)))


def section_name(section: dict) -> str:
    return f"{section['mode']} 프라이머 {section['primers']}"


def analyse(path: Path, channel: int | None) -> None:
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    if meta.get("check") != "dropout":
        sys.exit(f"{path.name}은 dropout 녹음이 아님 (check={meta.get('check')}); rate-2k는 rate2k_analyze.py로")
    x, used, rms = load(path, channel)
    a, b, residual = mapping(x, meta)
    mic_ppm = (b - 1.0) * 1e6

    def rec(t: float) -> int:
        return int((a + b * t) * FS)

    freq = meta["freq"]
    lines = [
        f"dropout 분석: {path.name}",
        f"펌웨어 {meta['firmware'].get('controller', '?')}, 동글 {meta['firmware'].get('dongle', '?')}, "
        f"connection_mode={meta['connection_mode']}, 대상 {meta['target']}, {freq:g} Hz {meta['amp']:g}%, "
        f"구간마다 {meta['seconds']:g}초 × {len(meta['sections'])}",
        f"채널 {used} (채널별 RMS {', '.join(f'{r:.0f}' for r in rms)} dBFS)",
        f"정렬: 첫 보고는 녹음 {a:.3f}초, 마이크 시계는 PC보다 {mic_ppm:+.0f} ppm, "
        f"딸깍 묶음 {len(residual)}개의 맞춤 잔차 {residual} ms (+면 그 묶음이 늦게 울림)",
    ]
    if meta["connection_mode"] == 0:
        lines.append("!! 유선 녹음: 대조용 (지금까지 유선에서는 끊김 0)")
    def spare_sent(section: dict) -> float:  # seconds the middle block went out ahead of its place
        k = len(section["sent"]) // 2
        return k * section["period"] - (section["sent"][k] - section["sent"][0])

    short = sorted({section_name(sec) for sec in meta["sections"]
                    if sec["round"] and spare_sent(sec) < sec["primers"] * sec["period"] - 0.002})
    if short:
        lines.append(f"!! 프라이머 수만큼 여유가 안 나간 구간: {', '.join(short)} (2026-11-08 낮까지의 stream은 하나 적게, "
                     "2k는 몰아 보내다 늦음으로 잡혀 더 적게 보냄). 실제로 보낸 여유는 4절")
    gaps = []
    for section, item, s, e in items(meta):
        if item["kind"] == "sine":
            g = x[rec(e) + int(0.15 * FS):rec(e) + int(0.6 * FS)]
            gaps.append(20 * np.log10(np.sqrt(np.mean(g ** 2)) + 1e-12) if len(g) else np.nan)
    quiet = float(np.nanpercentile(gaps, 25))
    if quiet < -85:
        lines.append(f"!! 톤 사이 무음이 {quiet:.0f} dBFS: 마이크의 노이즈 게이트·잡음 제거가 켜진 듯. "
                     "톤을 깎아 가짜 끊김을 만들 수 있음")
    warn_at = len(lines)
    lines.append("")

    lines.append(f"1. 구간별 끊김: 톤이 중앙값보다 {DIP_DB:g} dB 넘게 내려간 순간 (시각/길이/깊이), "
                 "구간을 4등분한 횟수, 톤 세기, 톤 대역 SNR, 패드 시계")
    rows = []  # (section index, section, gap starts, seconds, ppm)
    noisy: dict[int, float] = {}  # section index -> SNR, for tones too weak to count
    timing = []  # (section index, section, item, recording start, recording end, gap seconds)
    for section, item, s, e, seg, found, seconds in tone_gaps(x, meta, a, b):
        quarters = np.bincount(np.minimum(3, (np.array([f[0] for f in found]) / seconds * 4).astype(int)),
                               minlength=4) if found else np.zeros(4, int)
        body = seg[int(0.3 * FS):len(seg) - int(0.1 * FS)]
        level = 20 * np.log10(np.sqrt(np.mean(body ** 2)) + 1e-12)
        offset = pitch_offset(body, item["freq"])
        ppm = ((item["freq"] + offset) / item["freq"] * b - 1.0) * 1e6
        index = meta["sections"].index(section)
        snr = tone_snr(x, seg, item["freq"], rec(e))
        if not snr >= SNR_MIN_DB:
            noisy[index] = snr
        rows.append((index, section, [f[0] for f in found], seconds, ppm))
        timing.append((index, section, item, a + b * s, a + b * e, sum(f[1] for f in found)))
        lines.append(f"   [{index + 1:2d}] {section['round']}바퀴 {section_name(section):<12} {len(found):3d}번  "
                     f"4등분 {' '.join(f'{q:2d}' for q in quarters)}  {level:5.1f} dBFS  SNR {snr:4.1f} dB  "
                     f"패드 {ppm:+5.0f} ppm" + ("   !! 잡음에 묻힘, 셈에서 뺌" if index in noisy else ""))
        if found:
            lines.append("        " + " ".join(f"{t:.2f}s/{d * 1000:.0f}ms/-{depth:.0f}dB" for t, d, depth in found[:8])
                         + (f" … 외 {len(found) - 8}번" if len(found) > 8 else ""))
    lines.append("")
    if noisy:
        lines.insert(warn_at, f"!! 톤이 제 대역의 방 잡음보다 {SNR_MIN_DB:g} dB 넘게 크지 않은 구간 {len(noisy)}/{len(rows)}개 "
                              f"(SNR {min(noisy.values()):.0f}~{max(noisy.values()):.0f} dB): 그 끊김은 잡음이라 셈에서 뺌. "
                              "마이크를 그립 5~10 cm 앞에 두고, 패드는 손에 쥐거나 수건 위에(책상에 바로 두면 그립이 덜덜거려 "
                              "톤이 줄고 930 Hz가 커짐)")
    counted = [r for r in rows if r[0] not in noisy]

    lines.append(f"2. 모드별 분당 끊김 (바퀴별), 처음 {EARLY_S:g}초 / 나머지" + (" — 잡음에 묻힌 구간 뺌" if noisy else ""))
    names = list(dict.fromkeys(section_name(sec) for _, sec, _, _, _ in rows))
    summary = {}
    for name in names:
        mine = [r for r in counted if section_name(r[1]) == name]
        if not mine:
            lines.append(f"   {name:<12} 셀 구간 없음 (모두 잡음에 묻힘)")
            continue
        count = sum(len(r[2]) for r in mine)
        seconds = sum(r[3] for r in mine)
        early = sum(sum(1 for t in r[2] if t < EARLY_S) for r in mine)
        late = count - early
        early_s = sum(min(EARLY_S, r[3]) for r in mine)
        rounds = [len(r[2]) / r[3] * 60 for r in mine]
        summary[name] = (count / seconds * 60, rounds)
        lines.append(f"   {name:<12} {count / seconds * 60:6.1f}/분  ({', '.join(f'{v:.1f}' for v in rounds)})   "
                     f"처음 {early / early_s * 60:5.1f}/분, 나머지 {late / max(1e-9, seconds - early_s) * 60:5.1f}/분")
    lines.append("")

    ppms = np.array([r[4] for r in counted])
    ppms = ppms[np.isfinite(ppms)]
    lines.append("3. 패드 시계 (PC 대비, 톤 높이 + 양 끝 딸깍)")
    if len(ppms):
        mid = float(np.median(ppms))
        lines.append(f"   중앙값 {mid:+.0f} ppm, 구간별 {ppms.min():+.0f} ~ {ppms.max():+.0f}")
        if mid > 0:
            lines.append(f"   + 쪽: 패드가 PC가 보내는 것보다 빨리 꺼냄 → 여유가 초당 {mid:.0f} µs씩 줄어듦. "
                         f"1k 프라이머 하나(8 ms)는 약 {8000 / mid:.0f}초, 2k 프라이머 하나(4 ms)는 약 {4000 / mid:.0f}초 버팀")
        else:
            lines.append(f"   − 쪽: 패드가 더 느리게 꺼냄 → 큐가 초당 {-mid:.0f} µs씩 차오름. 여유는 줄지 않지만 "
                         f"오래 틀면 96칸(48 ms)이 차서 샘플을 버리게 됨: 1k 프라이머 0에서 약 {48000 / max(1.0, -mid):.0f}초")
    else:
        lines.append("   잴 수 없음 (톤이 너무 약하거나 끊김이 많음)")
    lines.append("")

    lines.append("4. 늦게 왔나, 사라졌나")
    lines.append("   늦게 온 보고는 뒤를 통째로 밀어 톤이 끊긴 만큼 길어지고(밀림), 사라진 보고는 그 자리만 비어 길이가 그대로. "
                 "보낸 여유가 패드 큐에 남으면 톤이 제 보고를 보낸 뒤 그만큼 늦게 시작함")
    measured = []  # (section index, section, gap ms, start ms, length ms, spare sent ms)
    for index, section, item, start, end, gap in timing:
        if index in noisy:
            continue
        edges = tone_edges(x, start, end, item["freq"])
        if edges is None:
            continue
        on, off = edges
        per_block = SUBFRAMES / section["rate"]
        first = min(len(section["sent"]) - 1, int(item["start"] / per_block + 1e-9))
        spare = (first * per_block - (section["sent"][first] - section["sent"][0])) * 1000
        measured.append((index, section, gap * 1000, (on - start) / b * 1000,
                         ((off - on) / b - item["length"]) * 1000, spare))
    if measured:
        zero = [m for m in measured if m[5] < 1.0] or measured
        base_on = min(m[3] for m in zero)
        # A start that sounds well after its spare fell in a gap: the length is short by it
        hit = {m[0] for m in measured if m[3] - base_on > m[5] + 8.0}
        calm = sorted((m for m in measured if m[0] not in hit), key=lambda m: m[2])
        base_length = float(np.median([m[4] for m in calm[:max(1, len(calm) // 3)]]))
        for index, section, gap, on, length, spare in measured:
            lines.append(f"   [{index + 1:2d}] {section['round']}바퀴 {section_name(section):<12} 끊김 합 {gap:4.0f} ms   "
                         f"보낸 여유 {spare:4.1f} ms   시작 {on - base_on:+5.1f} ms   밀림 {length - base_length:+6.1f} ms"
                         + ("   (시작이 끊김에 걸려 짧게 잼)" if index in hit else ""))
        lines.append("   프라이머별 보낸 여유 → 시작 (바퀴 중 이른 쪽; 여유가 큐에 남으면 시작이 그만큼 늦음):")
        for name in dict.fromkeys(section_name(m[1]) for m in measured):
            mine = [m for m in measured if section_name(m[1]) == name]
            lines.append(f"     {name:<12} {float(np.median([m[5] for m in mine])):4.1f} ms → {min(m[3] for m in mine) - base_on:+5.1f} ms")
        checked = [m for m in calm if m[2] >= 10]
        pushed = [m for m in checked if m[4] - base_length >= m[2] / 2]
        if checked and not pushed:
            lines.append(f"   → 끊김 합이 10 ms 넘는 {len(checked)}개 구간 모두 끊김 합의 절반도 밀리지 않음: 끊긴 자리의 "
                         "내용은 늦게 온 게 아니라 사라짐. PC 쪽 여유로는 못 메움")
        elif checked and len(pushed) == len(checked):
            lines.append(f"   → {len(checked)}개 구간 모두 끊긴 만큼 밀림: 보고가 늦게 왔음 (큐에 여유가 있으면 막을 수 있는 쪽)")
        elif checked:
            lines.append(f"   → {len(checked)}개 중 {len(pushed)}개 구간만 끊긴 만큼 밀림: 늦게 온 것과 사라진 것이 섞임")
        spared = [m for m in measured if m[5] >= 6.0 and m[0] not in hit]
        if spared:
            kept = [m for m in spared if m[3] - base_on >= m[5] / 2]
            if not kept:
                lines.append(f"   → 여유를 6 ms 넘게 보낸 {len(spared)}개 구간 모두 시작이 제때: 보낸 여유가 패드 큐에 남지 않음 "
                             "(몰아 보낸 보고를 동글이 버리거나 패드가 도착 즉시 재생)")
            elif len(kept) == len(spared):
                lines.append(f"   → 여유를 6 ms 넘게 보낸 {len(spared)}개 구간 모두 시작이 여유만큼 늦음: 보낸 여유가 패드 큐에 남음")
            else:
                lines.append(f"   → 여유를 6 ms 넘게 보낸 {len(spared)}개 구간 중 {len(kept)}개만 시작이 여유만큼 늦음")
        lines.append("   (시작은 여유 없는 구간 중 가장 이른 것, 밀림은 끊김이 적은 구간들의 길이에 대한 차)")
    else:
        lines.append("   잴 수 없음 (톤의 시작·끝을 못 찾음)")
    lines.append("")

    report = "\n".join(lines)
    print(report)
    out = path.with_name(path.stem + "-report.txt")
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\n저장: {out}")
    plot(rows, summary, meta, path.with_suffix(".png"))


def plot(rows, summary, meta, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams["font.family"] = ["Malgun Gothic", "sans-serif"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, (top, bottom) = plt.subplots(2, 1, figsize=(11, 3 + 0.32 * len(rows)),
                                      gridspec_kw={"height_ratios": [len(rows), 6]}, facecolor=SURFACE)
    for ax in (top, bottom):
        ax.set_facecolor(SURFACE)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        for side in ("left", "bottom"):
            ax.spines[side].set_color(INK_2)
        ax.tick_params(colors=INK_2)
    for y, (index, section, starts, seconds, _) in enumerate(rows):
        top.hlines(y, 0, seconds, color="#e4e3df", linewidth=6)
        top.vlines(starts, y - 0.35, y + 0.35, color=SERIES, linewidth=2)
    top.set_yticks(range(len(rows)))
    top.set_yticklabels([f"{s['round']}바퀴 {section_name(s)}" for _, s, _, _, _ in rows], color=INK)
    top.invert_yaxis()
    top.set_xlabel("구간 안 시각 (초)", color=INK_2)
    top.set_title(f"톤 끊김 ({meta['freq']:g} Hz, connection_mode={meta['connection_mode']})", color=INK, loc="left")

    names = list(summary)
    rate = [summary[n][0] for n in names]
    bottom.bar(range(len(names)), rate, width=0.5, color=SERIES)
    for i, n in enumerate(names):
        bottom.plot([i] * len(summary[n][1]), summary[n][1], "o", markersize=6, color=SURFACE,
                    markeredgecolor=INK, markeredgewidth=1.2)
    bottom.set_xticks(range(len(names)))
    bottom.set_xticklabels([f"{n}\n{r:.1f}/분" for n, r in zip(names, rate)], color=INK)
    bottom.set_ylabel("분당 끊김 (막대 평균, 점 바퀴별)", color=INK_2)
    bottom.grid(axis="y", color="#e4e3df", linewidth=0.8)
    bottom.set_axisbelow(True)
    fig.tight_layout()
    fig.savefig(out, dpi=110, facecolor=SURFACE)
    print(f"그림: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--channel", type=int, help="1-based input channel (default: the louder one)")
    args = parser.parse_args()
    analyse(args.recording, args.channel)


if __name__ == "__main__":
    main()
