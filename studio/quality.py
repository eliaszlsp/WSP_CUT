"""Ad breaks and static screens.

Twitch and YouTube stitch commercials into a stream, and a "be right back" slate
can sit where the good moment is. A clip taken from there is worthless, so those
seconds are found and left out.

Metric: mean absolute luma difference between consecutive frames
(tblend=difference + signalstats). Measured on real captures:

    ad slate         ~0.19
    gameplay clip    ~1.6 - 2.5

A flat 0-255 luma difference separates them with a wide margin, so no OCR, no
reference image and no model is needed. The frames are read once for the whole
video, and clips are then checked against that map.
"""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path

import numpy as np

from . import config

_FFMPEG = "ffmpeg"

_PTS = re.compile(r"pts_time:([0-9.]+)")
_YAVG = re.compile(r"lavfi\.signalstats\.YAVG=([0-9.]+)")


def motion_map(path: str | Path, bucket: float = 2.0) -> list[tuple[float, float]]:
    """(time, motion) for every bucket of the video, or nothing if it cannot be read.

    Motion is the mean luma difference between consecutive frames: near zero for a
    frozen screen, high for anything actually moving."""
    try:
        proc = subprocess.run(
            [_FFMPEG, "-hide_banner", "-loglevel", "error",
             "-i", str(path),
             "-vf", "tblend=all_mode=difference,signalstats,metadata=print:file=-",
             "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=900,
        )
    except Exception:  # noqa: BLE001 - a missing ffmpeg must not stop a job
        return []
    times: list[float] = []
    vals: list[float] = []
    at = 0.0
    for line in (proc.stdout or "").splitlines():
        m = _PTS.search(line)
        if m:
            at = float(m.group(1))
            continue
        m = _YAVG.search(line)
        if m:
            times.append(at)
            vals.append(float(m.group(1)))
    if len(vals) > 1:
        times, vals = times[1:], vals[1:]      # the first blended frame is against black
    if not vals:
        return []
    buckets: dict[int, list[float]] = {}
    for t, v in zip(times, vals):
        buckets.setdefault(int(t / bucket), []).append(v)
    return [(i * bucket, sum(v) / len(v)) for i, v in sorted(buckets.items())]


def static_ranges(path: str | Path, bucket: float = 2.0, threshold: float | None = None,
                  min_seconds: float | None = None) -> list[tuple[float, float]]:
    """The stretches that look like an ad break, a slate or a frozen screen."""
    threshold = config.STATIC_THRESHOLD if threshold is None else threshold
    min_seconds = config.STATIC_MIN_SECONDS if min_seconds is None else min_seconds
    pairs = motion_map(path, bucket)
    if not pairs:
        return []
    runs: list[tuple[float, float]] = []
    start = None
    for t, v in pairs:
        if v < threshold:
            if start is None:
                start = t
        elif start is not None:
            runs.append((start, t))
            start = None
    if start is not None:
        runs.append((start, pairs[-1][0] + bucket))
    return [(a, b) for a, b in runs if b - a >= min_seconds]


def static_overlap(start: float, end: float, ranges: list[tuple[float, float]]) -> float:
    """How much of a clip (in seconds) sits inside a static stretch."""
    total = 0.0
    for a, b in ranges:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            total += hi - lo
    return total


_MUSIC_CUE = re.compile(
    r"(?i)(?:♪|♫|\[(?:music|musica|música|singing|cantando|instrumental|song)\]|"
    r"\((?:music|musica|música|singing|cantando|instrumental|song)\))"
)


def detect_music_transcript(words: list[dict] | None, start: float = 0.0, end: float | None = None,
                            min_seconds: float = 5.0) -> tuple[bool, float, str]:
    """Detect explicit music/lyric cues in transcript word tokens spanning > `min_seconds`."""
    if not words:
        return False, 0.0, ""
    spans: list[list[float]] = []
    for w in words:
        s = float(w.get("s", 0.0))
        e = float(w.get("e", s + 0.5))
        if s < start or (end is not None and s > end):
            continue
        txt = str(w.get("w") or "")
        if _MUSIC_CUE.search(txt):
            if spans and s - spans[-1][1] <= 3.0:
                spans[-1][1] = max(spans[-1][1], e)
            else:
                spans.append([s, max(e, s + 0.5)])
    if not spans:
        return False, 0.0, ""
    max_run = max(b - a for a, b in spans)
    total = sum(b - a for a, b in spans)
    if max_run > min_seconds or (len(spans) >= 3 and total > min_seconds):
        dur = round(max(max_run, total), 1)
        return True, dur, f"Marcadores musicais na transcrição (~{dur}s)"
    return False, round(max_run, 1), ""


def detect_music_audio(audio: np.ndarray | None, words: list[dict] | None = None,
                       start: float = 0.0, end: float | None = None,
                       sr: int = 16000, min_seconds: float = 5.0) -> tuple[bool, float, str]:
    """Detect sustained tonal music (> `min_seconds`) in audio outside spoken words."""
    if audio is None or len(audio) < sr * 2 or sr <= 0:
        return False, 0.0, ""
    lo = max(0, int(start * sr))
    hi = len(audio) if end is None else min(len(audio), int(end * sr))
    hop = max(1, int(0.5 * sr))
    if hi - lo < int((min_seconds + 0.5) * sr):
        return False, 0.0, ""
    seg = audio[lo:hi]
    if np.issubdtype(seg.dtype, np.integer):
        seg = seg.astype(np.float32) / 32768.0
    else:
        seg = seg.astype(np.float32)

    n_frames = (len(seg) - hop) // hop + 1
    # Merge speech intervals for O(N + W) masking of spoken frames
    speech: list[tuple[float, float]] = []
    for w in words or ():
        ws = float(w.get("s", 0.0)) - 0.15
        we = float(w.get("e", ws)) + 0.15
        if we < start or (end is not None and ws > end):
            continue
        if speech and ws <= speech[-1][1]:
            speech[-1] = (speech[-1][0], max(speech[-1][1], we))
        else:
            speech.append((ws, we))

    win = np.hanning(hop).astype(np.float32)
    freq_res = sr / hop
    lo_a, lo_b = max(1, int(50 / freq_res)), max(2, int(200 / freq_res))
    mi_a, mi_b = max(lo_b + 1, int(220 / freq_res)), max(lo_b + 10, int(3500 / freq_res))

    tonal = [False] * n_frames
    audible_nonspeech = [False] * n_frames
    sp_idx = 0
    for i in range(n_frames):
        t0 = start + i * 0.5
        t1 = t0 + 0.5
        while sp_idx < len(speech) and speech[sp_idx][1] < t0:
            sp_idx += 1
        if sp_idx < len(speech) and speech[sp_idx][0] <= t1:
            continue
        chunk = seg[i * hop: i * hop + hop]
        rms = float(math.sqrt(float(np.mean(chunk * chunk)) + 1e-12))
        rms_db = 20.0 * math.log10(rms)
        if rms_db <= -38.0:
            continue
        audible_nonspeech[i] = True
        mag2 = np.abs(np.fft.rfft(chunk * win)) ** 2
        low_e = float(mag2[lo_a:lo_b].sum())
        mid = mag2[mi_a:mi_b]
        if len(mid) < 12:
            continue
        mid_e = float(mid.sum())
        if mid_e / (low_e + 1e-12) <= 0.12:
            continue
        geom = float(np.exp(np.mean(np.log(mid + 1e-12))))
        arith = float(np.mean(mid)) + 1e-12
        sfm = geom / arith
        top12 = float(np.sort(mid)[-12:].sum()) / (mid_e + 1e-12)
        if sfm < 0.25 and top12 > 0.35:
            tonal[i] = True

    # 1) Contiguous tonal stretch > min_seconds
    need_frames = int(math.floor(min_seconds / 0.5)) + 1
    run = max_run = 0
    for flag in tonal:
        run = run + 1 if flag else 0
        if run > max_run:
            max_run = run
    if max_run >= need_frames:
        dur = round(max_run * 0.5, 1)
        return True, dur, f"Áudio musical contínuo detectado (~{dur}s)"

    # 2) Rolling 30s window with brief spoken pauses while music continues in background
    win_frames = 60  # 30 seconds
    for start_i in range(0, max(1, n_frames - win_frames + 1)):
        sub_t = tonal[start_i: start_i + win_frames]
        t_cnt = sum(1 for x in sub_t if x)
        if t_cnt >= need_frames:
            a_cnt = sum(1 for x in audible_nonspeech[start_i: start_i + win_frames] if x)
            if a_cnt and t_cnt / a_cnt >= 0.60:
                dur = round(t_cnt * 0.5, 1)
                return True, dur, f"Fundo musical contínuo detectado (~{dur}s)"

    return False, round(max_run * 0.5, 1), ""


def detect_music(words: list[dict] | None = None, audio: np.ndarray | None = None,
                 start: float = 0.0, end: float | None = None,
                 sr: int = 16000, min_seconds: float = 5.0) -> dict:
    """Check both transcript cues and audio spectrum for music lasting > `min_seconds`."""
    hit_t, dur_t, why_t = detect_music_transcript(words, start=start, end=end, min_seconds=min_seconds)
    if hit_t:
        return {"has_music": True, "duration": dur_t, "source": "transcript", "reason": why_t}
    hit_a, dur_a, why_a = detect_music_audio(audio, words=words, start=start, end=end, sr=sr, min_seconds=min_seconds)
    if hit_a:
        return {"has_music": True, "duration": dur_a, "source": "audio", "reason": why_a}
    return {"has_music": False, "duration": max(dur_t, dur_a), "source": "", "reason": ""}
