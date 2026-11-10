"""Run every check on one delivered video. Timeline-dependent checks read the ``.forge.json`` sidecar the pipeline
writes next to each export (or an explicit timeline); without one they report ``skip`` with the reason."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from creative_forge.ffmpeg import probe
from creative_forge.loudness import measure
from creative_forge.qa.audio_checks import check_hook_gameplay, check_loudness, check_true_peak
from creative_forge.qa.media_checks import (
    check_av_duration,
    check_cadence,
    check_duration,
    check_file_size,
    check_format,
    check_intro_freeze,
    check_intro_silence,
)
from creative_forge.qa.ocr import OcrBackend, check_hook_text
from creative_forge.qa.result import CheckResult, skipped
from creative_forge.qa.seam import check_seam
from creative_forge.spec import AudioSpec, ExportSpec, QASpec

SIDECAR_SUFFIX = ".forge.json"


def sidecar_path(video: Path) -> Path:
    return video.parent / (video.name + SIDECAR_SUFFIX)


def write_sidecar(video: Path, data: dict[str, Any]) -> Path:
    p = sidecar_path(video)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def read_sidecar(video: Path) -> dict[str, Any] | None:
    p = sidecar_path(video)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def run_video_checks(
    video: Path,
    qa: QASpec,
    audio: AudioSpec,
    export: ExportSpec,
    *,
    timeline: dict[str, float] | None = None,
    expect_format: str | None = None,
    expect_size: tuple[int, int] | None = None,
    ocr: OcrBackend | None = None,
    derived: bool = False,
) -> list[CheckResult]:
    """``derived=True`` (the 4:5 variant re-encoded from the 9:16 master) skips the picture-content checks that
    were already run on the master: seam, cadence and OCR."""
    video = Path(video)
    side = read_sidecar(video)
    known = list(qa.known_text)
    ignore = None
    if side:
        known += side.get("overlay_text", [])
        ignore = [tuple(b) for b in side.get("ocr_ignore", [])] or None
        timeline = timeline or side.get("timeline")
        expect_format = expect_format or side.get("format")
        if expect_size is None and side.get("size"):
            expect_size = tuple(side["size"])  # type: ignore[assignment]
    info = probe(video)
    t = timeline or {}
    hook_end = t.get("hook_end")
    g_start = t.get("gameplay_start")
    g_end = t.get("gameplay_end")
    x = t.get("crossfade", 0.3)
    measured = measure(video) if info.has_audio else None

    out = [
        check_duration(info, *qa.duration_s),
        check_format(info, expect_format, expect_size),
        check_av_duration(info, qa.av_mismatch_warn_s, qa.av_mismatch_fail_s),
        check_file_size(info, export.max_file_mb),
        check_loudness(info, audio.target_lufs, qa.loudness_warn_lu, qa.loudness_fail_lu, measured),
        check_true_peak(info, audio.true_peak_max_dbtp, qa.true_peak_fail_dbtp, measured),
        check_hook_gameplay(
            info,
            hook_end,
            None if g_start is None else g_start + audio.gameplay_skip_s,
            g_end,
            audio.hook_gameplay_max_delta_lu,
            qa.hook_gameplay_fail_lu,
        ),
        check_intro_freeze(info, qa.intro_window_s, qa.freeze_min_s, qa.freeze_noise_db),
        check_intro_silence(info, qa.intro_window_s, qa.silence_min_s, qa.silence_noise_db),
    ]
    if derived:
        reason = "derived from the 9:16 master; checked there"
        out += [skipped("seam", reason), skipped("cadence", reason), skipped("hook_text", reason)]
        return out
    out.append(
        check_seam(info, hook_end, x, qa.seam_mean_warn, qa.seam_mean_fail, qa.seam_block_warn, qa.seam_block_fail)
    )
    if hook_end is None:
        out.append(skipped("cadence", "no timeline; hook span unknown"))
        out.append(skipped("hook_text", "no timeline; hook span unknown"))
    else:
        out.append(check_cadence(info, 0.0, hook_end, qa.duplicate_warn_ratio, qa.duplicate_fail_ratio))
        out.append(check_hook_text(video, 0.0, hook_end, qa.ocr_fps, known, ocr, ignore))  # type: ignore[arg-type]
    return out
