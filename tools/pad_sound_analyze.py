"""Analyse microphone recordings of `tone.py pad-sound`.

  python tools/pad_sound_analyze.py REC.wav                 one recording: full report and a plot
  python tools/pad_sound_analyze.py back.wav front.wav      several: their levels side by side

Each recording's timeline is the .json of the same name next to it (tone.py writes both).
RECORDING is anything ffmpeg reads (wav, a phone's m4a). One recording's report goes to
REC-report.txt and REC.png beside it.

What it reads from a recording:
  1. how loud each sine is in the air, with its harmonics and its images around 1, 2, 3 kHz
  2. whether the pad holds each sample (images about f/(1000-f) of the tone) or joins them
  3. per frequency, how the buzz above 500 Hz grows across the strength ladder: images of a
     held sample keep their share of the tone at any strength, distortion grows with it
  4. what a one-sample impulse and a click sound like, band by band

Needs numpy, scipy and matplotlib (tools/requirements.txt), and ffmpeg/ffprobe on PATH.
Not part of the app.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
from scipy import signal

FS = 48000
EDGE_IN_MS = 250     # skip the start of each tone (onset, fade, alignment error)
EDGE_OUT_MS = 100
BAND_HZ = 8          # +- around a component
NFFT = 16384
SNR_MIN = 6.0        # dB over the room for a component to count
ONSET_DB = 6.0       # a harmonic this much more of the tone than at the quietest strength: distortion


def load(path: Path, channel: int | None) -> tuple[np.ndarray, int, list[float]]:
    """One channel at 48 kHz: the given one (1-based), else the loudest (an empty input stays near -100 dBFS)."""
    channels = int(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                                   "stream=channels", "-of", "csv=p=0", str(path)],
                                  capture_output=True, text=True, check=True).stdout.strip())
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-ar", str(FS),
                          "-f", "f32le", "-"], capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32).astype(np.float64).reshape(-1, channels)
    rms = [20 * np.log10(np.sqrt(np.mean(x[:, c] ** 2)) + 1e-12) for c in range(channels)]
    pick = channel - 1 if channel else int(np.argmax(rms))
    return x[:, pick], pick + 1, rms


def timeline(path: Path) -> tuple[dict, list[dict]]:
    """The recording's json and its plan, every sine with its strength (older timelines had one for all)."""
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    plan = meta["plan"]
    for seg in plan:
        if seg["kind"] == "sine":
            seg.setdefault("amp", meta.get("amp"))
    return meta, plan


def find_sync(x: np.ndarray, plan: list[dict]) -> float:
    """Seconds into the recording where the timeline's sample 0 falls.

    Slides the whole pattern of tones (each frequency loud only in its own segment) over
    narrowband levels in 10 ms steps, then refines to 1 ms; a short burst alone drowns in a
    noisy room.
    """
    hop = 0.010
    nper = 4800  # 10 Hz bins
    f, t, Z = signal.stft(x, FS, nperseg=nper, noverlap=nper - int(hop * FS), boundary=None)
    t = t - nper / FS / 2  # frame start, not centre
    level = 20 * np.log10(np.abs(Z) + 1e-12)
    tones = [s for s in plan if s["kind"] in ("sine", "sync")]
    rows = {s["freq"]: level[int(round(s["freq"] / 10))] for s in tones}
    rows = {k: (v - np.median(v)) for k, v in rows.items()}
    total = plan[-1]["start"] / 1000

    def score(t0: float) -> float:
        acc = 0.0
        for seg in tones:
            a = np.searchsorted(t, t0 + (seg["start"] + 60) / 1000)
            b = np.searchsorted(t, t0 + (seg["start"] + seg["length"] - 60) / 1000 - nper / FS)
            if b <= a:
                return -np.inf
            row = rows[seg["freq"]]
            inside = row[a:b].mean()
            # the same frequency just before and after must be quiet, so the edges count
            pre = row[max(0, a - 30):max(0, a - 5)]
            post = row[b + 5:b + 30]
            acc += inside - 0.5 * ((pre.mean() if len(pre) else 0) + (post.mean() if len(post) else 0))
        return acc

    candidates = np.arange(-2.0, len(x) / FS - total, hop)
    if not len(candidates):
        sys.exit("recording is shorter than the timeline")
    scores = np.array([score(c) for c in candidates])
    best = candidates[int(np.argmax(scores))]
    fine = np.arange(best - hop, best + hop, 0.001)
    best = fine[int(np.argmax([score(c) for c in fine]))]
    spread = np.sort(scores)[-1] - np.median(scores)
    if spread < 3 * len(tones):
        sys.exit(f"no tone pattern found (best match only {spread:.0f} dB over the median)")
    return float(best)


def tap_correction(x: np.ndarray, t0: float, plan: list[dict]) -> float:
    """Seconds to add to t0 so the click taps land where the timeline puts them.

    The tone pattern pins the timeline only to some tens of ms; the clicks are sharp.
    """
    seg = next(s for s in plan if s["kind"] == "click")
    hp = signal.sosfiltfilt(signal.butter(2, 40, "high", fs=FS, output="sos"), x)
    env = np.convolve(np.abs(hp), np.ones(48) / 48, mode="same")
    offsets = []
    for k in range(seg["taps"]):
        expected = t0 + (seg["start"] + k * seg["spacing"] + seg["spacing"] // 2) / 1000
        a, b = int((expected - 0.1) * FS), int((expected + 0.1) * FS)
        if a < 0 or b > len(env):
            continue
        offsets.append((a + int(np.argmax(env[a:b]))) / FS - expected)
    if len(offsets) < 3:
        return 0.0
    offsets = np.array(offsets)
    med = float(np.median(offsets))
    if np.median(np.abs(offsets - med)) > 0.005:
        print(f"(탭 위치가 들쭉날쭉해 정렬 보정 안 함: {np.round(offsets * 1000).astype(int)} ms)")
        return 0.0
    return med


def align(x: np.ndarray, plan: list[dict]) -> float:
    t0 = find_sync(x, plan)
    return t0 + tap_correction(x, t0, plan)


def psd(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    nper = min(len(x), 8192)
    return signal.welch(x, FS, window="hann", nperseg=nper, noverlap=nper // 2, nfft=NFFT,
                        scaling="spectrum")


def band_power(freqs, spec, f0, half=BAND_HZ) -> float:
    m = (freqs >= f0 - half) & (freqs <= f0 + half)
    return float(spec[m].sum()) if m.any() else 0.0


def db(p: float) -> float:
    return 10 * np.log10(max(p, 1e-30))


def segment(x, t0, seg, edge_in=EDGE_IN_MS, edge_out=EDGE_OUT_MS):
    a = int((t0 + (seg["start"] + edge_in) / 1000) * FS)
    b = int((t0 + (seg["start"] + seg["length"] - edge_out) / 1000) * FS)
    return x[max(0, a):max(0, b)]


def gaps(plan):
    """Silent stretches between segments, inner parts only (ring-down skipped)."""
    out = []
    for before, after in zip(plan, plan[1:]):
        start = before["start"] + before["length"] + 300
        stop = after["start"] - 50
        if stop - start >= 150:
            out.append({"start": start, "length": stop - start})
    return out


def noise_psd(x, t0, plan):
    return psd(np.concatenate([segment(x, t0, g, 0, 0) for g in gaps(plan)]))


def components(f: float, rate: int) -> list[tuple[str, float]]:
    comps = [("f", f)] + [(f"{k}f", k * f) for k in (2, 3, 4, 5)]
    for m in (1, 2, 3):
        comps += [(f"{m}k-f", m * rate - f), (f"{m}k+f", m * rate + f)]
    return [(n, v) for n, v in comps if 20 < v < FS / 2 - 100]


def analyse_sines(x, t0, plan, noise_f, noise_p, rate):
    rows = []
    for seg in (s for s in plan if s["kind"] == "sine"):
        part = segment(x, t0, seg)
        freqs, spec = psd(part)
        f = seg["freq"]
        row = {"freq": f, "amp": seg["amp"], "comps": {}}
        comps = components(f, rate)
        for name, fc in comps:
            p = band_power(freqs, spec, fc)
            n = band_power(noise_f, noise_p, fc)
            clash = [o for o, v in comps if o != name and abs(v - fc) < 2 * BAND_HZ]
            row["comps"][name] = {"hz": fc, "db": db(p), "snr": db(p) - db(n), "clash": clash}
        above = (freqs > 500)
        extra = spec[above].sum() - noise_p[above].sum()
        row["above500_db"] = db(extra) - row["comps"]["f"]["db"] if extra > 0.25 * noise_p[above].sum() else None
        rows.append(row)
    return rows


def analyse_taps(x, t0, plan, kind):
    seg = next(s for s in plan if s["kind"] == kind)
    pre, post = int(0.005 * FS), int(0.150 * FS)
    specs, envs, offsets = [], [], []
    for k in range(seg["taps"]):
        expected = t0 + (seg["start"] + k * seg["spacing"] + seg["spacing"] // 2) / 1000
        c = int(expected * FS)
        win = x[c - int(0.04 * FS):c + int(0.04 * FS)]  # +-40 ms around where the tap should be
        hp = signal.sosfiltfilt(signal.butter(2, 80, "high", fs=FS, output="sos"), win)
        peak = c - int(0.04 * FS) + int(np.argmax(np.abs(hp)))
        offsets.append((peak / FS - expected) * 1000)
        clip = x[peak - pre:peak + post] * signal.windows.tukey(pre + post, 0.1)
        specs.append(np.abs(np.fft.rfft(clip, NFFT)) ** 2)
        envs.append(np.abs(clip))
    freqs = np.fft.rfftfreq(NFFT, 1 / FS)
    spec = np.mean(specs, axis=0)
    # noise of the same length, from the gaps
    nspecs = []
    for g in gaps(plan):
        a = int((t0 + g["start"] / 1000) * FS)
        while a + pre + post <= int((t0 + (g["start"] + g["length"]) / 1000) * FS):
            nspecs.append(np.abs(np.fft.rfft(x[a:a + pre + post] * signal.windows.tukey(pre + post, 0.1),
                                             NFFT)) ** 2)
            a += pre + post
    nspec = np.mean(nspecs, axis=0)
    env = np.mean(envs, axis=0)
    env = np.convolve(env, np.ones(48) / 48, mode="same")
    top = env.max()
    decay_ms = (np.argmax(env[pre:] < top * 0.1) / FS * 1000) if (env[pre:] < top * 0.1).any() else None
    bands = [(0, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000), (8000, 16000)]
    clean = np.clip(spec - nspec, 0, None)
    total = clean[(freqs > 40) & (freqs < 16000)].sum()
    band_db = {f"{lo}-{hi}": db(clean[(freqs >= max(lo, 40)) & (freqs < hi)].sum() / total) for lo, hi in bands}
    snr_db = {f"{lo}-{hi}": db(spec[(freqs >= max(lo, 40)) & (freqs < hi)].sum())
              - db(nspec[(freqs >= max(lo, 40)) & (freqs < hi)].sum()) for lo, hi in bands}

    def level(f0, half=40):
        return db(band_power(freqs, clean, f0, half))
    notch = {m: level(m * 1000) - (level(m * 1000 - 250) + level(m * 1000 + 250)) / 2 for m in (1, 2)}
    return {"freqs": freqs, "spec": spec, "noise": nspec, "bands": band_db, "snr": snr_db,
            "decay_ms": decay_ms, "offsets_ms": offsets, "notch_db": notch}


def hold_verdict(rows, rate) -> list[str]:
    """Compare image pairs around 1 kHz with hold (ZOH) and straight-line predictions."""
    lines = []
    for r in rows:
        lo, hi = r["comps"]["1k-f"], r["comps"]["1k+f"]
        if lo["snr"] < SNR_MIN or hi["snr"] < SNR_MIN or lo["clash"] or hi["clash"]:
            continue
        f = r["freq"]
        measured = hi["db"] - lo["db"]
        zoh = 20 * np.log10((rate - f) / (rate + f))
        lin = 2 * zoh
        lines.append(f"  {f:5.0f} Hz: {rate + f:.0f}/{rate - f:.0f} Hz 영상 비 {measured:+6.1f} dB "
                     f"(그대로 유지 {zoh:+.1f}, 직선 연결 {lin:+.1f})")
    return lines


def rel(c: dict, name: str, width: int = 5) -> str:
    """A component against the fundamental, '.' under the room, '*' when it shares its frequency."""
    v = c.get(name)
    if v is None:
        return " " * (width + 1)
    if v["snr"] < SNR_MIN:
        return " " * (width - 1) + ". "
    return f"{v['db'] - c['f']['db']:+{width}.0f}" + ("*" if v["clash"] else " ")


def ladder(rows) -> list[str]:
    """Per frequency, across strengths: how loud the tone came out and how the buzz grew with it."""
    amps = sorted({r["amp"] for r in rows})
    if len(amps) < 2:
        return []
    by = {(r["freq"], r["amp"]): r for r in rows}
    head = "  ".join(f"{a:>4g}%" + " " * 13 for a in amps)
    L = ["3. 세기에 따라 (각 세기에서: 기본음 SNR / 3f / 5f / 500 Hz 위 합, 모두 기본음 대비 dB)",
         f"   freq   {head}| 왜곡 시작",
         "   기본음 세기가 보낸 세기만큼 늘지 않으면(괄호 안 = 기대보다 얼마나 덜 큼) 껍데기가 포화되는 것"]
    for f in sorted({r["freq"] for r in rows}):
        cells, onset = [], None
        first = next((by[(f, a)] for a in amps if by[(f, a)]["comps"]["f"]["snr"] >= SNR_MIN), None)
        # each odd harmonic's share of the tone at the quietest strength heard; under the room
        # there, the most it can have been
        base = {}
        for a in amps:
            r = by[(f, a)]
            c = r["comps"]
            if c["f"]["snr"] < SNR_MIN:
                cells.append("     잡음 수준    ")
                continue
            shortfall = ""
            if first is not None and r is not first:
                expected = 20 * np.log10(a / first["amp"])
                got = c["f"]["db"] - first["comps"]["f"]["db"]
                if expected - got >= 3:
                    shortfall = f"({got - expected:+.0f})"
            buzz = r["above500_db"]
            for name in ("3f", "5f"):
                h = c.get(name)
                if h is None or h["clash"]:
                    continue
                share = h["db"] - c["f"]["db"]
                if name not in base:
                    base[name] = share if h["snr"] >= SNR_MIN else SNR_MIN - c["f"]["snr"]
                elif onset is None and h["snr"] >= SNR_MIN and share - base[name] >= ONSET_DB:
                    onset = a
            cells.append(f"{c['f']['snr']:3.0f}{rel(c, '3f', 3)}{rel(c, '5f', 3)}"
                         + (f"{buzz:+4.0f}" if buzz is not None else "   .") + f"{shortfall:<4s}")
        L.append(f"  {f:5.0f}  " + " ".join(cells) + f"| {f'{onset:g}%' if onset else '-'}")
    L.append(f"   왜곡 시작 = 3f나 5f의 기본음 대비 크기가 가장 약한 세기(잡음 위로 잡힌 첫 세기)보다 {ONSET_DB:g} dB 넘게"
             " 커진 첫 세기. 그 아래에서 잡음에 묻혔으면 묻힌 만큼을 기준으로 삼음")
    L.append("   500 Hz 위 합에는 세기와 상관없는 거울 성분이 들어 있어, 그것만 커지면 왜곡이고 그대로면 거울 성분")
    return L


def report_one(path: Path, channel: int | None) -> None:
    meta, plan = timeline(path)
    rate = meta["sample_rate"]
    x, picked, levels = load(path, channel)
    clip = np.mean(np.abs(x) > 0.99)
    t0 = align(x, plan)
    end = t0 + plan[-1]["start"] / 1000
    if end > len(x) / FS:
        sys.exit(f"recording ends {end - len(x) / FS:.1f} s before the timeline does")

    noise_f, noise_p = noise_psd(x, t0, plan)
    rows = analyse_sines(x, t0, plan, noise_f, noise_p, rate)
    taps = {k: analyse_taps(x, t0, plan, k) for k in ("impulse", "click")}
    loudest = max(r["amp"] for r in rows)
    top = [r for r in rows if r["amp"] == loudest]

    ref = max(r["comps"]["f"]["db"] for r in top)
    amps = sorted({r["amp"] for r in rows})
    L = [f"녹음: {path.name}  ({len(x) / FS:.1f} s, 타임라인 시작 {t0:.3f} s, 클리핑 {clip:.4%})",
         f"채널 {picked} 사용 (채널별 RMS: {', '.join(f'{v:.0f}' for v in levels)} dBFS)",
         f"패드: target {meta['target']}, 사인 {'/'.join(f'{a:g}' for a in amps)}%, 탭 {meta['tap_amp']}%, "
         f"connection_mode {meta.get('connection_mode')}, fw {meta.get('firmware', {}).get('controller', '-')}",
         "",
         f"1. 공기 중 소리 세기, {loudest:g}% (가장 큰 기본음 = 0 dB, 마이크 특성 포함)  / 배음 / 1 kHz 영상",
         "   freq   기본음  SNR |   2f    3f    4f    5f  (기본음 대비) |  1k-f  1k+f  2k-f  2k+f  | >500 Hz 합"]
    for r in top:
        c = r["comps"]
        L.append(f"  {r['freq']:5.0f}  {c['f']['db'] - ref:+6.1f} {c['f']['snr']:4.0f} | "
                 f"{rel(c, '2f')}{rel(c, '3f')}{rel(c, '4f')}{rel(c, '5f')}             | "
                 f"{rel(c, '1k-f')}{rel(c, '1k+f')}{rel(c, '2k-f')}{rel(c, '2k+f')} | "
                 + (f"{r['above500_db']:+6.1f}" if r["above500_db"] is not None else "  잡음 수준"))
    L += ["   (. = 잡음보다 6 dB 미만, * = 다른 성분과 주파수가 겹쳐 구분 불가)", "",
          "2. 샘플을 어떻게 이어 재생하나 (1 kHz 위아래 영상의 비)"]
    L += hold_verdict(top, rate) or ["  영상이 잡음 위로 안 보임: 필터로 걸러내거나, 마이크/패드가 그 대역을 못 냄"]
    L += [""] + ladder(rows) + ["", "4. 탭 (평균 스펙트럼, 대역별 에너지 비율 / 잡음 대비)"]
    for kind, t in taps.items():
        name = {"impulse": "1샘플 임펄스", "click": "클릭 (4+/4-)"}[kind]
        L.append(f"  {name}: 감쇠(-20 dB) {t['decay_ms'] and round(t['decay_ms'])} ms, "
                 f"1 kHz 홈 {t['notch_db'][1]:+.1f} dB, 2 kHz 홈 {t['notch_db'][2]:+.1f} dB, "
                 f"시각 오차 {np.median(t['offsets_ms']):+.1f} ms")
        L.append("    " + "  ".join(f"{b}: {v:+5.1f}({t['snr'][b]:+.0f})" for b, v in t["bands"].items()))
    report = "\n".join(L)
    print(report)
    text = path.with_name(path.stem + "-report.txt")
    text.write_text(report + "\n", encoding="utf-8")
    image = path.with_suffix(".png")
    plot(x, t0, plan, top, taps, ref, image)
    print(f"\n저장: {text}, {image}")


def report_many(paths: list[Path], channel: int | None) -> None:
    """Absolute levels (dBFS) side by side: meaningful only with the same mic gain."""
    runs = []
    for path in paths:
        meta, plan = timeline(path)
        x, _, _ = load(path, channel)
        t0 = align(x, plan)
        runs.append((path.stem.removeprefix("pad-sound-"), meta, x, t0, plan, *noise_psd(x, t0, plan)))

    def bands(freqs, spec, nspec):
        out = {}
        for lo, hi in [(40, 500), (500, 1000), (1000, 2000), (2000, 4000), (4000, 8000)]:
            m = (freqs >= lo) & (freqs < hi)
            out[f"{lo}-{hi}"] = (db(spec[m].sum()), db(spec[m].sum()) - db(nspec[m].sum()))
        return out

    print("잡음 바닥, 대역별 (dBFS):")
    for name, meta, x, t0, plan, nf, npw in runs:
        print(f"  {name:16s} " + "  ".join(f"{k}:{v[0]:6.1f}" for k, v in bands(nf, npw, npw).items()))
    shared = None
    for *_, plan, _, _ in runs:
        keys = {(s["freq"], s["amp"]) for s in plan if s["kind"] == "sine"}
        shared = keys if shared is None else shared & keys
    print("\n사인: 기본음 dBFS (SNR) | 500 Hz 위 합 dBFS | 3f 5f 1k-f, 기본음 대비")
    for f0, amp in sorted(shared or ()):
        cells = []
        for name, meta, x, t0, plan, nf, npw in runs:
            seg = next(s for s in plan if s["kind"] == "sine" and s["freq"] == f0 and s["amp"] == amp)
            fr, sp = psd(segment(x, t0, seg))
            fund = band_power(fr, sp, f0)
            hi = fr > 500
            extra = sp[hi].sum() - npw[hi].sum()

            def r(fc):
                p, n = band_power(fr, sp, fc), band_power(nf, npw, fc)
                return f"{db(p) - db(fund):+4.0f}" if db(p) - db(n) > SNR_MIN else "   ."
            cells.append(f"{db(fund):6.1f}({db(fund) - db(band_power(nf, npw, f0)):3.0f}) "
                         f"{db(extra) if extra > 0 else float('nan'):6.1f} {r(3 * f0)} {r(5 * f0)} {r(1000 - f0)}")
        print(f"{f0:4.0f} {amp:3g}%: " + " || ".join(cells))
    print("        열: " + " || ".join(run[0] for run in runs))
    print("\n탭: 평균 스펙트럼의 대역별 dBFS (SNR)")
    for kind in ("impulse", "click"):
        for name, meta, x, t0, plan, nf, npw in runs:
            t = analyse_taps(x, t0, plan, kind)
            print(f"  {kind:7s} {name:16s} " + "  ".join(f"{k}:{v[0]:6.1f}({v[1]:+3.0f})"
                                                       for k, v in bands(t["freqs"], t["spec"], t["noise"]).items()))


def plot(x, t0, plan, rows, taps, ref, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(3, 1, figsize=(11, 11))
    a = int(max(0, t0) * FS)
    b = int((t0 + plan[-1]["start"] / 1000) * FS)
    f, t, S = signal.spectrogram(x[a:b], FS, nperseg=4096, noverlap=3072)
    keep = f < 4000
    axes[0].pcolormesh(t, f[keep], 10 * np.log10(S[keep] + 1e-14), shading="auto", cmap="magma")
    axes[0].set_ylabel("Hz")
    axes[0].set_title("spectrogram (timeline)")
    fr = [r["freq"] for r in rows]
    axes[1].plot(fr, [r["comps"]["f"]["db"] - ref for r in rows], "o-", label=f"fundamental (air), {rows[0]['amp']:g}%")
    for name in ("2f", "3f", "5f", "1k-f", "1k+f"):
        axes[1].plot(fr, [r["comps"][name]["db"] - ref if name in r["comps"] and r["comps"][name]["snr"] >= SNR_MIN
                          else np.nan for r in rows], ".--", label=name)
    axes[1].set_xlabel("sent sine Hz")
    axes[1].set_ylabel("dB")
    axes[1].legend()
    axes[1].grid(alpha=0.3)
    for kind, tap in taps.items():
        m = (tap["freqs"] > 30) & (tap["freqs"] < 8000)
        axes[2].semilogx(tap["freqs"][m], 10 * np.log10(tap["spec"][m] + 1e-14), label=kind, lw=0.8)
    m = (taps["impulse"]["freqs"] > 30) & (taps["impulse"]["freqs"] < 8000)
    axes[2].semilogx(taps["impulse"]["freqs"][m], 10 * np.log10(taps["impulse"]["noise"][m] + 1e-14),
                     color="gray", label="noise", lw=0.8)
    for k in (500, 1000, 2000, 3000):
        axes[2].axvline(k, color="k", alpha=0.2)
    axes[2].set_xlabel("Hz")
    axes[2].legend()
    axes[2].grid(alpha=0.3, which="both")
    axes[2].set_title("tap spectra")
    fig.tight_layout()
    fig.savefig(path, dpi=110)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("recordings", type=Path, nargs="+")
    ap.add_argument("--channel", type=int, help="1-based input channel (default: the loudest)")
    args = ap.parse_args()
    if len(args.recordings) == 1:
        report_one(args.recordings[0], args.channel)
    else:
        report_many(args.recordings, args.channel)


if __name__ == "__main__":
    main()
