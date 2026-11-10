"""Container-level and signal-level checks: duration, format, A/V length, file size, frozen/silent intro, cadence."""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from creative_forge.ffmpeg import MediaInfo, run, stderr_of
from creative_forge.qa.result import CheckResult, Status, grade, skipped

ASPECTS = {"9:16": 9 / 16, "4:5": 4 / 5, "1:1": 1.0, "16:9": 16 / 9}


def check_duration(info: MediaInfo, lo: float, hi: float) -> CheckResult:
    d = info.duration
    status = Status.PASS if lo <= d <= hi else Status.FAIL
    return CheckResult(
        "duration", status, d, f"{lo:g}-{hi:g} s", "s", "" if status is Status.PASS else "outside the delivery window"
    )


def check_format(info: MediaInfo, expect: str | None = None, size: tuple[int, int] | None = None) -> CheckResult:
    if not info.has_video or not info.width or not info.height:
        return CheckResult("format", Status.FAIL, None, expect or "video stream", message="no video stream")
    w, h = info.width, info.height
    ratio = w / h
    value = f"{w}x{h} @ {info.fps:.2f} fps" if info.fps else f"{w}x{h}"
    if size is not None and (w, h) != tuple(size):
        return CheckResult("format", Status.FAIL, value, f"{size[0]}x{size[1]}", message="unexpected resolution")
    if expect is not None:
        ok = abs(ratio - ASPECTS[expect]) < 0.01
        return CheckResult(
            "format",
            Status.PASS if ok else Status.FAIL,
            value,
            expect,
            "" if ok else f"aspect {ratio:.3f} is not {expect}",
        )
    known = [k for k, r in ASPECTS.items() if abs(ratio - r) < 0.01]
    return CheckResult(
        "format",
        Status.PASS if known else Status.WARN,
        value,
        "a standard ad aspect",
        f"aspect {known[0]}" if known else f"non-standard aspect {ratio:.3f}",
    )


def check_av_duration(info: MediaInfo, warn_s: float, fail_s: float) -> CheckResult:
    """Defect: the engine's audio ended 1.6 s before the picture (sidechain FX bus shorter than the video)."""
    if not info.has_video:
        return skipped("av_duration", "no video stream")
    if not info.has_audio:
        return CheckResult("av_duration", Status.FAIL, None, "audio present", message="no audio stream")
    diff = abs(float(info.video_duration) - float(info.audio_duration))
    return CheckResult(
        "av_duration",
        grade(diff, warn_s, fail_s),
        diff,
        f"<= {warn_s:g} s (fail > {fail_s:g} s)",
        "s",
        f"video {info.video_duration:.2f} s, audio {info.audio_duration:.2f} s",
    )


def check_file_size(info: MediaInfo, max_mb: float) -> CheckResult:
    mb = info.size_bytes / 1_000_000
    status = Status.FAIL if mb > max_mb else (Status.WARN if mb > 0.95 * max_mb else Status.PASS)
    return CheckResult("file_size", status, mb, f"<= {max_mb:g} MB", "MB")


def _freezes(path: Path, window: float, min_s: float, noise_db: float) -> list[tuple[float, float]]:
    err = stderr_of(
        [
            "-t",
            f"{window:.3f}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            f"freezedetect=n={noise_db:g}dB:d={min_s:g}",
            "-f",
            "null",
            "-",
        ]
    )
    starts = [float(x) for x in re.findall(r"freeze_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"freeze_end: (-?[\d.]+)", err)]
    return [(s, ends[i] if i < len(ends) else window) for i, s in enumerate(starts)]


def _silences(path: Path, window: float, min_s: float, noise_db: float) -> list[tuple[float, float]]:
    err = stderr_of(
        [
            "-t",
            f"{window:.3f}",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-af",
            f"silencedetect=n={noise_db:g}dB:d={min_s:g}",
            "-f",
            "null",
            "-",
        ]
    )
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: (-?[\d.]+)", err)]
    return [(s, ends[i] if i < len(ends) else window) for i, s in enumerate(starts)]


def check_intro_freeze(info: MediaInfo, window_s: float, min_s: float, noise_db: float) -> CheckResult:
    """Defect: one ad opened on a frozen, silent first 0.5 s."""
    if not info.has_video:
        return skipped("intro_freeze", "no video stream")
    win = window_s + min_s
    hits = [(s, e) for s, e in _freezes(info.path, win, min_s, noise_db) if s <= 0.1]
    if hits:
        s, e = hits[0]
        return CheckResult(
            "intro_freeze",
            Status.FAIL,
            e - s,
            f"no freeze >= {min_s:g} s in the first {window_s:g} s",
            "s",
            f"frozen from {s:.2f} s to {e:.2f} s",
        )
    return CheckResult("intro_freeze", Status.PASS, 0.0, f"no freeze >= {min_s:g} s in the first {window_s:g} s", "s")


def check_intro_silence(info: MediaInfo, window_s: float, min_s: float, noise_db: float) -> CheckResult:
    if not info.has_audio:
        return CheckResult("intro_silence", Status.FAIL, None, "audio present", message="no audio stream")
    win = window_s + min_s
    hits = [(s, e) for s, e in _silences(info.path, win, min_s, noise_db) if s <= 0.1]
    threshold = f"no silence (< {noise_db:g} dB) >= {min_s:g} s at the start"
    if hits:
        s, e = hits[0]
        return CheckResult("intro_silence", Status.FAIL, e - s, threshold, "s", f"silent from {s:.2f} s to {e:.2f} s")
    return CheckResult("intro_silence", Status.PASS, 0.0, threshold, "s")


def frame_diffs(path: Path, start: float, end: float, size: tuple[int, int] = (64, 112)) -> np.ndarray:
    """Mean absolute luma difference between consecutive frames of ``[start, end)`` at low resolution."""
    w, h = size
    p = run(
        [
            "-ss",
            f"{start:.3f}",
            "-t",
            f"{max(0.05, end - start):.3f}",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-vf",
            f"scale={w}:{h}:flags=area,format=gray",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ]
    )
    frames = np.frombuffer(p.stdout, np.uint8)
    n = frames.size // (w * h)
    if n < 3:
        return np.zeros(0)
    f = frames[: n * w * h].reshape(n, h, w).astype(np.float32)
    return np.abs(np.diff(f, axis=0)).mean(axis=(1, 2))


def count_repeats(d: np.ndarray) -> int:
    """Isolated near-zero steps between moving steps: the signature of frame repetition (24 -> 30 fps repeats every
    5th frame). Consecutive still steps are a held shot, not judder, and are not counted."""
    med = float(np.median(d))
    small = d < max(0.05, 0.15 * med)
    big = d > 0.5 * med
    return int(sum(1 for i in range(1, d.size - 1) if small[i] and big[i - 1] and big[i + 1]))


def check_cadence(info: MediaInfo, start: float, end: float, warn_ratio: float, fail_ratio: float) -> CheckResult:
    """Defect: Veo renders 24 fps; a plain ``fps`` conversion repeats frames and the motion judders."""
    if not info.has_video:
        return skipped("cadence", "no video stream")
    d = frame_diffs(info.path, start, end)
    if d.size < 4:
        return skipped("cadence", "segment too short")
    med = float(np.median(d))
    if med < 0.2:
        return skipped("cadence", "segment is mostly static; cadence not measurable")
    repeats = count_repeats(d)
    ratio = repeats / d.size
    return CheckResult(
        "cadence",
        grade(ratio, warn_ratio, fail_ratio),
        ratio,
        f"repeated frames <= {warn_ratio:.0%} (fail > {fail_ratio:.0%})",
        "",
        f"{repeats} repeated of {d.size} frame steps in {start:.2f}-{end:.2f} s",
        {"median_motion": round(med, 3)},
    )
