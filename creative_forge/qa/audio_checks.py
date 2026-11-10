"""Loudness checks, measured on the delivered (encoded) file."""

from __future__ import annotations

import math

from creative_forge.ffmpeg import MediaInfo
from creative_forge.loudness import Loudness, measure
from creative_forge.qa.result import CheckResult, Status, grade, skipped


def check_loudness(
    info: MediaInfo, target_lufs: float, warn_lu: float, fail_lu: float, measured: Loudness | None = None
) -> CheckResult:
    if not info.has_audio:
        return CheckResult("loudness", Status.FAIL, None, f"{target_lufs:g} LUFS", message="no audio stream")
    i = (measured or measure(info.path)).integrated_lufs
    if math.isinf(i):
        return CheckResult("loudness", Status.FAIL, None, f"{target_lufs:g} LUFS", message="audio is silent")
    return CheckResult(
        "loudness",
        grade(abs(i - target_lufs), warn_lu, fail_lu),
        i,
        f"{target_lufs:g} ± {warn_lu:g} LU (fail beyond ± {fail_lu:g})",
        "LUFS",
    )


def check_true_peak(
    info: MediaInfo, max_dbtp: float, fail_dbtp: float, measured: Loudness | None = None
) -> CheckResult:
    """Measured after AAC encoding: the built-in encoder overshot a limited master by +6.7 dB in production."""
    if not info.has_audio:
        return skipped("true_peak", "no audio stream")
    tp = (measured or measure(info.path)).true_peak_dbtp
    if math.isinf(tp):
        return CheckResult("true_peak", Status.FAIL, None, f"<= {max_dbtp:g} dBTP", message="audio is silent")
    status = Status.PASS if tp <= max_dbtp else (Status.WARN if tp <= fail_dbtp else Status.FAIL)
    return CheckResult("true_peak", status, tp, f"<= {max_dbtp:g} dBTP (fail > {fail_dbtp:g})", "dBTP")


def hook_gameplay_levels(
    info: MediaInfo, hook_end: float, gameplay_start: float, gameplay_end: float
) -> tuple[float, float]:
    hook = measure(info.path, 0.0, hook_end).integrated_lufs
    game = measure(info.path, gameplay_start, gameplay_end).integrated_lufs
    return hook, game


def check_hook_gameplay(
    info: MediaInfo,
    hook_end: float | None,
    gameplay_start: float | None,
    gameplay_end: float | None,
    max_delta: float,
    fail_delta: float,
) -> CheckResult:
    """Defect: hook vs gameplay loudness differed by +5 to -9 LU across a batch of ads."""
    if hook_end is None or gameplay_start is None or gameplay_end is None:
        return skipped("hook_gameplay_loudness", "no timeline (pass --hook-end or keep the .forge.json sidecar)")
    if not info.has_audio:
        return skipped("hook_gameplay_loudness", "no audio stream")
    if hook_end < 0.5 or gameplay_end - gameplay_start < 1.0:
        return skipped("hook_gameplay_loudness", "hook or gameplay window shorter than the 400 ms gating block")
    hook, game = hook_gameplay_levels(info, hook_end, gameplay_start, gameplay_end)
    if math.isinf(hook) or math.isinf(game):
        return CheckResult(
            "hook_gameplay_loudness",
            Status.FAIL,
            None,
            f"|delta| <= {max_delta:g} LU",
            message=f"silent section (hook {hook}, gameplay {game})",
        )
    delta = hook - game
    return CheckResult(
        "hook_gameplay_loudness",
        grade(abs(delta), max_delta, fail_delta),
        delta,
        f"|delta| <= {max_delta:g} LU (fail > {fail_delta:g})",
        "LU",
        f"hook {hook:.1f} LUFS, gameplay {game:.1f} LUFS",
        {"hook_lufs": round(hook, 2), "gameplay_lufs": round(game, 2)},
    )
