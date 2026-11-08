"""Analyse a microphone recording of `tone.py rate-2k`: does the pad play 2 kHz data?

  python tools/rate2k_analyze.py REC.wav [--channel N]

Reads REC.json beside it (tone.py writes both) and writes REC-report.txt and REC.png.

The check plays the same tones as the app sends them (1k: bit 7 set, a report every 8 ms), in
the 2 kHz mode of a reading of firmware 9.0.6.8 (2k: bit 7 clear, every 4 ms, primer reports
first), in 1k again, then as report-rate's 4 ms run (old4: bit 7 set, every 4 ms). The room and
the shell colour every frequency, but alike in every mode, so this compares:
  1. the pitch each tone comes out at: sent at 1 kHz, 700 Hz is the samples of 300 Hz and
     900 those of 100; played at 2 kHz they are themselves
  2. each component's level, mode against mode, at the same frequency
  3. whether the sweep to 950 Hz keeps rising past 500 Hz or folds back
  4. all of it against simulations of the pad, from the samples and send times tone.py logged:
       claim        the reading: bit 7 joins 8 samples into 16 by straight lines, clear queues
                    the 8 as they are; a 96-sample queue drained at 2 kHz drops what overflows
                    and holds the last value 2 ms when empty; the output joins its 2 kHz
                    samples by straight lines
       dup-bit7     the same, but bit 7 doubles each sample instead of joining them: the 1 kHz
                    sample-and-hold the 2026-11-04 recording seemed to fit
       ignore-bit7  the same pad, but bit 7 changes nothing: every report is joined into 16
     Each model predicts how much each component changes from 1k to 2k; the tone itself and
     its fold decide. The output stage is the same in every mode, so whether it joins or holds
     its 2 kHz samples (the reading's -3.6 dB at 700 Hz) cancels out here and stays open.
  5. what no model has: moments the tone drops out for a few ms (the pad ran out of data).
     None on the wired run; on wireless they hit 1k as well as 2k

Needs numpy, scipy, matplotlib and ffmpeg, as pad_sound_analyze.py. Not part of the app.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.ndimage import maximum_filter1d

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pad_sound_analyze import FS, band_power, db, load, psd  # noqa: E402

SUBFRAMES = 8
EDGE_IN = 0.25    # s skipped at a tone's start: onset, fade, queue latency, alignment
EDGE_OUT = 0.10
SNR_MIN = 10.0    # dB over the room for a component to count
QUEUE = 96        # samples, per the reading
DRAIN_HZ = 2000
HOLD_S = 0.002
TICK_PHASE_S = 0.00025  # where the pad's 2 kHz clock sits between two host ticks: unknown, mid-way
MODELS = {
    "claim": {"output": "linear", "join": "line", "ignore_bit7": False},
    "dup-bit7": {"output": "linear", "join": "double", "ignore_bit7": False},
    "ignore-bit7": {"output": "linear", "join": "line", "ignore_bit7": True},
}
PEAK_BAND = (60.0, 1500.0)
DIP_DB = 6.0      # a drop of the tone this far under its median counts as a gap


# --- timeline -------------------------------------------------------------------------------------

def host_time(section: dict, tau: float) -> float:
    """Host seconds (from the first report) at which the data at tau seconds of this section went out."""
    per_block = SUBFRAMES / section["rate"]
    sent = section["sent"]
    b = min(len(sent) - 1, max(0, int(tau / per_block + 1e-9)))
    return sent[b] + (tau - b * per_block) * (section["period"] / per_block)


def items(meta: dict):
    """(section, item, host start, host end) for every item but the end marks."""
    for section in meta["sections"]:
        for item in section["items"]:
            if item["kind"] != "end":
                yield section, item, host_time(section, item["start"]), host_time(section, item["start"] + item["length"])


# --- the pad, simulated ---------------------------------------------------------------------------

def simulate(meta: dict, model: dict) -> tuple[np.ndarray, float]:
    """The whole run's output by one model, at FS, and the host second its first sample stands for."""
    arrivals, blocks, joined = [], [], []
    for section in meta["sections"]:
        samples = np.asarray(section["samples"], float).reshape(-1, SUBFRAMES)
        arrivals += section["sent"]
        blocks += list(samples)
        joined += [section["upsample"] or model["ignore_bit7"]] * len(samples)
    t0 = arrivals[0] + TICK_PHASE_S
    ticks = int((arrivals[-1] - arrivals[0] + 0.2) * DRAIN_HZ)
    out = np.zeros(ticks)
    queue: deque = deque()
    prev = last = 0.0
    empty = i = 0
    hold_ticks = HOLD_S * DRAIN_HZ
    for j in range(ticks):
        t = t0 + j / DRAIN_HZ
        while i < len(arrivals) and arrivals[i] <= t:
            block = blocks[i]
            if joined[i]:
                values = np.empty(2 * SUBFRAMES)
                values[1::2] = block
                values[0::2] = (np.concatenate(([prev], block[:-1])) + block) / 2 if model["join"] == "line" else block
            else:
                values = block
            prev = block[-1]
            room = QUEUE - len(queue)
            queue.extend(values[:max(0, room)])
            i += 1
        if queue:
            last, empty = queue.popleft(), 0
        else:
            empty += 1
            if empty > hold_ticks:
                last = 0.0
        out[j] = last
    t_out = np.arange(int(ticks / DRAIN_HZ * FS)) / FS
    if model["output"] == "linear":  # straight lines from each sample to the next, one sample late
        y = np.interp(t_out, (np.arange(ticks) + 1) / DRAIN_HZ, out, left=0.0)
    else:
        y = out[np.minimum(ticks - 1, (t_out * DRAIN_HZ).astype(int))]
    return y, t0


def cut(y: np.ndarray, t_first: float, a: float, b: float) -> np.ndarray:
    return y[max(0, int((a - t_first) * FS)):max(0, int((b - t_first) * FS))]


# --- the recording --------------------------------------------------------------------------------

def click_times(meta: dict, index: int = 0, last: bool = False) -> list[float]:
    """Host seconds of one section's clicks: its first clicks item, or its last one."""
    section = meta["sections"][index]
    found = [i for i in section["items"] if i["kind"] == "clicks"]
    item = found[-1] if last else found[0]
    return [host_time(section, item["start"] + k * item["spacing"]) for k in range(item["count"])]


def locate(x: np.ndarray, clicks: list[float], lo: float = 0.0,
           hi: float | None = None) -> tuple[float, list[float], float, float]:
    """Where host second 0 sits in the recording, judged from these clicks alone and searched
    from lo to hi seconds: offset, each click's residual (ms), best score over the median, and
    the residuals' largest spread from their median (ms)."""
    hp = signal.sosfiltfilt(signal.butter(4, 300, "high", fs=FS, output="sos"), x)
    env = np.convolve(np.abs(hp), np.ones(48) / 48, mode="same")
    wide = maximum_filter1d(env, size=int(0.006 * FS))
    clicks = np.asarray(clicks)
    top = len(x) / FS - clicks[-1] - 0.05
    candidates = np.arange(max(0.0, lo), top if hi is None else min(hi, top), 0.001)
    idx = (candidates[:, None] + clicks[None, :]) * FS
    scores = wide[idx.astype(int)].sum(axis=1)
    best = candidates[int(np.argmax(scores))]
    residual = []
    for c in clicks:
        a, b = int((best + c - 0.010) * FS), int((best + c + 0.010) * FS)
        residual.append((a + int(np.argmax(env[a:b]))) / FS - (best + c))
    ratio = float(scores.max() / np.median(scores))
    scatter = float(np.max(np.abs(np.array(residual) - np.median(residual))) * 1000)
    offset = best + float(np.median(residual))
    return offset, [round(float(r - np.median(residual)) * 1000, 1) for r in residual], ratio, scatter


def align(x: np.ndarray, meta: dict) -> tuple[float, list[float]]:
    """Seconds into the recording of host second 0, from the clicks; and each click's residual (ms).

    The whole run must fit in the recording, so the clicks are only looked for that early: on
    2026-11-08 14:53 they stood 22 dB over the median and a knock pattern 18 s in scored higher."""
    span = meta["sections"][-1]["sent"][-1] - meta["sections"][0]["sent"][0]
    hi = len(x) / FS - span
    offset, residual, ratio, scatter = locate(x, click_times(meta), 0.0, hi if hi > 0.5 else None)
    if ratio < 4 and scatter > 3:  # 2026-11-08 07:36 had no pad sound at all: 3.6x, clicks 5-6 ms apart
        sys.exit(f"딸깍을 찾지 못함 (최고 점수가 중앙값의 {ratio:.1f}배, 딸깍 위치가 {scatter:.0f} ms까지 어긋남): "
                 "녹음에 패드 소리가 없는 듯. 마이크 위치나 패드 연결을 확인하고 다시 녹음")
    if ratio < 4:
        print(f"(딸깍 정렬이 약함: 최고 점수가 중앙값의 {ratio:.1f}배)")
    return offset, residual


def components(f: float) -> list[tuple[str, float]]:
    found: list[tuple[str, float]] = []
    for name, value in (("f", f), ("1000-f", abs(1000 - f)), ("1000+f", 1000 + f), ("2000-f", abs(2000 - f)),
                        ("f/2", f / 2)):
        if 40 <= value <= 2500 and all(abs(value - v) > 15 for _, v in found):
            found.append((name, value))
    return found


def levels(seg: np.ndarray, freqs) -> dict[float, float]:
    fr, spec = psd(seg)
    return {f: db(band_power(fr, spec, f)) for f in freqs}


def peak(seg: np.ndarray, band=PEAK_BAND) -> float:
    fr, spec = psd(seg)
    m = (fr >= band[0]) & (fr <= band[1])
    return float(fr[m][np.argmax(spec[m])])


def follow(seg: np.ndarray, item: dict, start_skip: float) -> dict[str, float]:
    """Share of a sweep's frames above 550 Hz whose peak is the sent frequency, its fold (1000 - f), or neither."""
    nper, hop = 2048, 960
    fr, tt, Z = signal.stft(seg, FS, nperseg=nper, noverlap=nper - hop, boundary=None)
    m = (fr >= 80) & (fr <= 1500)
    counts = {"따라감": 0, "접힘": 0, "기타": 0}
    for k, t in enumerate(tt):
        tau = start_skip + t
        sent = item["freq_from"] + (item["freq_to"] - item["freq_from"]) * tau / item["length"]
        if sent < 550:
            continue
        p = fr[m][np.argmax(np.abs(Z[m, k]))]
        counts["따라감" if abs(p - sent) < 30 else "접힘" if abs(p - (1000 - sent)) < 30 else "기타"] += 1
    total = max(1, sum(counts.values()))
    return {k: v / total for k, v in counts.items()}


# --- report ---------------------------------------------------------------------------------------

def dips(seg: np.ndarray, tone: float) -> tuple[list[tuple[float, float, float]], float]:
    """Moments the tone falls DIP_DB under its median: (start s, length s, depth dB), seconds."""
    half = 25.0 if tone < 130 else 50.0
    sos = signal.butter(4, [tone - half, tone + half], "bandpass", fs=FS, output="sos")
    envelope = np.abs(signal.hilbert(signal.sosfiltfilt(sos, seg)))
    width = int(0.004 * FS)
    level = 20 * np.log10(np.convolve(envelope, np.ones(width) / width, "same") + 1e-12)[::width // 2]
    step = width // 2 / FS
    edge = int(0.08 / step)  # the filter rings in and out at the ends
    level = level[edge:-edge]
    median = np.median(level)
    low = level < median - DIP_DB
    found = []
    i = 0
    while i < len(low):
        if low[i]:
            j = i
            while j < len(low) and low[j]:
                j += 1
            found.append((i * step, (j - i) * step, float(median - level[i:j].min())))
            i = j
        else:
            i += 1
    return found, len(level) * step


def label(section: dict, item: dict) -> str:
    if item["kind"] == "sine":
        return f"{section['mode']} {item['freq']:g}"
    if item["kind"] == "sweep":
        return f"{section['mode']} 스윕"
    return f"{section['mode']} 딸깍"


def analyse(path: Path, channel: int | None) -> None:
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    x, used, rms = load(path, channel)
    offset, residual = align(x, meta)
    sims = {name: simulate(meta, model) for name, model in MODELS.items()}
    first_click = click_times(meta)[0] + offset
    noise = x[int(0.1 * FS):max(int(0.2 * FS), int((first_click - 0.35) * FS))]
    lines = [
        f"rate-2k 분석: {path.name}",
        f"펌웨어 {meta['firmware'].get('controller', '?')}, 동글 {meta['firmware'].get('dongle', '?')}, "
        f"connection_mode={meta['connection_mode']}, 대상 {meta['target']}, 사인 {meta['amp']:g}%",
        f"채널 {used} (채널별 RMS {', '.join(f'{r:.0f}' for r in rms)} dBFS), 무음 구간 {len(noise) / FS:.2f}초",
        f"정렬: 첫 보고는 녹음 {offset:.3f}초, 딸깍 잔차 {residual} ms",
        "구간: " + " / ".join(f"{s['mode']} {s['period'] * 1000:g} ms bit7={int(s['upsample'])} "
                             f"프라이머 {s['primers']} 늦음 {s.get('late', '?')}" for s in meta["sections"]),
    ]
    # The silence after each item is the room the comb (section 5) is held against. A room is never
    # digital silence: the RODE VideoMic GO II over USB (2026-11-08) fell to -96 dBFS within 0.2 s
    # of every sound, a gate or noise suppression that also eats faint lines. Speech or a TV in a gap
    # shows as a gap far above the rest.
    gap_db = []
    for section, item, a, b in items(meta):
        gap = x[int((b + 0.15 + offset) * FS):int((b + 0.6 + offset) * FS)]
        name = f"{meta['sections'].index(section) + 1}구간 {label(section, item)}"
        gap_db.append((name, 20 * np.log10(np.sqrt(np.mean(gap ** 2)) + 1e-12) if len(gap) else np.nan))
    quiet = float(np.nanpercentile([level for _, level in gap_db], 25))  # noise may fill half the gaps
    if quiet < -85:
        lines.append(f"!! 톤 사이 무음이 {quiet:.0f} dBFS: 디지털 무음에 가까움. 마이크 쪽 노이즈 게이트·잡음 제거가 켜진 듯 "
                     "(5절의 방 잡음 비교와 약한 성분은 믿을 수 없음, 윈도 마이크 '오디오 향상' 끄기)")
    loud = [name for name, level in gap_db if level > max(quiet, -70) + 20]
    if loud:
        lines.append(f"!! 다음 항목 직후 무음에 큰 소리가 섞임 (생활 소음?): {', '.join(loud)}")
    lines.append("")

    rows = []  # (section, item, measured levels, noise levels, simulated levels per model, peaks)
    for section, item, a, b in items(meta):
        if item["kind"] != "sine":
            continue
        freqs = [v for _, v in components(item["freq"])]
        seg = x[int((a + EDGE_IN + offset) * FS):int((b - EDGE_OUT + offset) * FS)]
        meas = levels(seg, freqs)
        floor = levels(noise, freqs)
        sim = {}
        peaks = {"녹음": peak(seg)}
        for name, (y, t_first) in sims.items():
            s = cut(y, t_first, a + EDGE_IN, b - EDGE_OUT)
            sim[name] = levels(s, freqs)
            peaks[name] = peak(s)
        rows.append((section, item, meas, floor, sim, peaks))

    lines.append("1. 음높이: 60-1500 Hz에서 가장 센 성분 (Hz). old4는 큐가 넘쳐 타이밍 따라 달라짐")
    lines.append("   톤            녹음   " + "  ".join(f"{n:>11}" for n in MODELS))
    for section, item, meas, floor, sim, peaks in rows:
        lines.append(f"   {label(section, item):<12} {peaks['녹음']:>5.0f}   "
                     + "  ".join(f"{peaks[n]:>11.0f}" for n in MODELS))
    lines.append("")

    def find(mode: str, freq: float, nth: int = 0):
        hits = [r for r in rows if r[0]["mode"] == mode and r[1]["freq"] == freq]
        return hits[nth] if len(hits) > nth else None

    def show(level: float, noise_level: float) -> str:
        return f"{level:6.1f}" if level - noise_level >= SNR_MIN else f"<{noise_level + SNR_MIN:5.0f}"

    lines.append(f"2. 같은 주파수에서 모드끼리 비교 (dB, 무음 + {SNR_MIN:g} dB 미만은 '<'). "
                 "차이 = 비교 모드 - 1k(첫 번째)")
    for other, nth in (("2k", 0), ("1k", 1), ("old4", 0)):
        for freq in sorted({r[1]["freq"] for r in rows}):
            base, cmp_ = find("1k", freq), find(other, freq, nth)
            if not base or not cmp_:
                continue
            title = "1k 다시 (대조: 0이어야 함)" if other == "1k" else other
            lines.append(f"   {freq:g} Hz 톤, {title}:")
            lines.append(f"     성분           1k    {other:>4}   차이  " + "  ".join(f"{n:>11}" for n in MODELS))
            for name, value in components(freq):
                lb, lc = base[2][value], cmp_[2][value]
                nb, nc = base[3][value], cmp_[3][value]
                if lb - nb < SNR_MIN and lc - nc < SNR_MIN:
                    diff = "   둘 다 잡음"
                elif lb - nb < SNR_MIN:
                    diff = f">{lc - (nb + SNR_MIN):+5.1f}"
                elif lc - nc < SNR_MIN:
                    diff = f"<{(nc + SNR_MIN) - lb:+5.1f}"
                else:
                    diff = f"{lc - lb:+6.1f}"
                pred = "  ".join(f"{cmp_[4][n][value] - base[4][n][value]:>+11.1f}" for n in MODELS)
                lines.append(f"     {name:>6} {value:5.0f}  {show(lb, nb)} {show(lc, nc)} {diff:>7}  {pred}")
    lines.append("")

    lines.append("3. 스윕 200→950 Hz, 보낸 주파수가 550 Hz를 넘은 뒤: 가장 센 성분이 보낸 주파수를 따라감 / "
                 "1000-f로 접힘 / 기타")
    for section, item, a, b in items(meta):
        if item["kind"] != "sweep":
            continue
        seg = x[int((a + 0.05 + offset) * FS):int((b - 0.05 + offset) * FS)]
        share = follow(seg, item, 0.05)
        sim_share = {n: follow(cut(y, tf, a + 0.05, b - 0.05), item, 0.05) for n, (y, tf) in sims.items()}
        lines.append(f"   {label(section, item):<8} 녹음 " + " / ".join(f"{k} {v:.0%}" for k, v in share.items())
                     + "   |  " + "  ".join(f"{n}: 따라감 {s['따라감']:.0%}" for n, s in sim_share.items()))
    lines.append("")

    # 4. The models disagree most about the tone itself and, above 500 Hz, its fold (1000 - f):
    # those decide. Below 500 Hz 1000 - f is an image, which in 2k falls in section 5's comb that
    # no model has (first recording: 840 Hz of the 160 Hz tone only 5 dB under 1k's).
    lines.append("4. 모델 맞춤: 2k - 1k 차이를 톤 자신(f)과, 500 Hz 위 톤은 접힌 성분(1000-f)에서 비교 "
                 "(예측과 괄호 안 |녹음 - 예측| dB, 잡음에 묻힌 쪽은 부등호를 지키면 0)")
    lines.append("   톤 / 성분             녹음  " + "  ".join(f"{n:>14}" for n in MODELS))
    error = {n: [] for n in MODELS}
    for freq in sorted({r[1]["freq"] for r in rows}):
        base, cmp_ = find("1k", freq), find("2k", freq)
        if not base or not cmp_:
            continue
        for name, value in components(freq)[:2 if freq > 500 else 1]:
            lb, lc = base[2][value], cmp_[2][value]
            nb, nc = base[3][value], cmp_[3][value]
            hb, hc = lb - nb >= SNR_MIN, lc - nc >= SNR_MIN
            if not hb and not hc:
                continue
            low = lc - lb if hb and hc else (lc - (nb + SNR_MIN) if hc else -np.inf)
            high = lc - lb if hb and hc else ((nc + SNR_MIN) - lb if hb else np.inf)
            shown = f"{low:+6.1f}" if low == high else (f">{low:+5.1f}" if np.isinf(high) else f"<{high:+5.1f}")
            cells = []
            for n in MODELS:
                pred = cmp_[4][n][value] - base[4][n][value]
                miss = max(0.0, low - pred, pred - high)
                error[n].append(miss)
                cells.append(f"{pred:+6.1f} ({miss:4.1f})")
            lines.append(f"   {freq:4g} Hz {name:>6} {value:5.0f}  {shown}  " + "  ".join(f"{c:>14}" for c in cells))
    lines.append("   평균 오차                    " + "  ".join(f"{np.mean(error[n]):>14.1f}" for n in MODELS))
    lines.append("")

    # 5. What none of the models has: the tone itself dropping out for a few ms. The pad ran out of
    # data (the radio delivered a report late); each gap clicks, rings the actuator near 66 Hz and
    # spreads noise round the tone, which an earlier version of this section read as a comb.
    lines.append(f"5. 끊김: 톤 자체가 중앙값보다 {DIP_DB:g} dB 넘게 내려간 순간 (시각/길이/깊이). "
                 "유선과 가짜 녹음은 0")
    per_section: dict[int, list[float]] = {}
    for section, item, a, b in items(meta):
        if item["kind"] != "sine" or section["mode"] == "old4":
            continue
        tone = item["freq"] if not (section["rate"] == 1000 and item["freq"] > 500) else 1000 - item["freq"]
        seg = x[int((a + 0.05 + offset) * FS):int((b - 0.05 + offset) * FS)]
        found, seconds = dips(seg, tone)
        index = meta["sections"].index(section)
        count = per_section.setdefault(index, [0, 0.0])
        count[0] += len(found)
        count[1] += seconds
        lines.append(f"   {label(section, item):<12} {len(found):2d}  "
                     + " ".join(f"{s:.2f}s/{d * 1000:.0f}ms/-{depth:.0f}dB" for s, d, depth in found[:6])
                     + (" …" if len(found) > 6 else ""))
    lines.append("   구간별: " + "  /  ".join(
        f"{meta['sections'][i]['mode']} 프라이머 {meta['sections'][i].get('primers', 0)}: "
        f"{n}번 / {s:.1f}초" for i, (n, s) in per_section.items()))
    lines.append("")

    report = "\n".join(lines)
    print(report)
    out = path.with_name(path.stem + "-report.txt")
    out.write_text(report + "\n", encoding="utf-8")
    print(f"\n저장: {out}")
    plot(x, offset, meta, rows, path.with_suffix(".png"))


def plot(x, offset, meta, rows, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = ["Malgun Gothic", "DejaVu Sans"]
    fig = plt.figure(figsize=(16, 9))
    ax = fig.add_subplot(2, 1, 1)
    start = max(0, int((offset - 0.3) * FS))
    seg = x[start:]
    f, t, S = signal.spectrogram(seg, FS, nperseg=4096, noverlap=4096 - 480)
    m = f <= 2200
    ax.pcolormesh(t + start / FS - offset, f[m], 10 * np.log10(S[m] + 1e-14), shading="auto", cmap="magma",
                  vmin=np.percentile(10 * np.log10(S[m] + 1e-14), 40))
    for section, item, a, b in items(meta):
        ax.axvspan(a, b, ymin=0.97, ymax=1.0, color="white", alpha=0.6)
        ax.text((a + b) / 2, 2150, label(section, item), ha="center", va="top", fontsize=7, color="white",
                rotation=90 if item["kind"] == "sine" else 0)
    ax.set_ylabel("Hz")
    ax.set_xlabel("첫 보고부터 초")
    ax.set_title("rate-2k: 녹음 스펙트로그램")
    for k, freq in enumerate(sorted({r[1]["freq"] for r in rows}, reverse=True)):
        ax2 = fig.add_subplot(2, 4, 5 + k)
        for section, item, a, b in items(meta):
            if item["kind"] != "sine" or item["freq"] != freq:
                continue
            s = x[int((a + EDGE_IN + offset) * FS):int((b - EDGE_OUT + offset) * FS)]
            fr, spec = psd(s)
            mm = fr <= 2200
            ax2.plot(fr[mm], 10 * np.log10(spec[mm] + 1e-14), lw=0.8, label=label(section, item))
        ax2.set_title(f"{freq:g} Hz 톤")
        ax2.set_xlabel("Hz")
        ax2.legend(fontsize=7)
        for v in (freq, abs(1000 - freq)):
            ax2.axvline(v, color="gray", lw=0.5, ls=":")
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    print(f"그림: {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--channel", type=int, help="1-based input channel (default: the loudest)")
    args = parser.parse_args()
    analyse(args.recording, args.channel)


if __name__ == "__main__":
    main()
