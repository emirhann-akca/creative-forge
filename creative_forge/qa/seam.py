"""Seam check at the hook -> gameplay cut.

The hook is generated with Veo's ``lastFrame`` set to the real first gameplay frame, so the AI shot should land on
real gameplay. This measures how well it did: mean absolute difference (0-255) between the two frames, plus the
worst 60 px block (at 1080 px width), which catches a small invented UI element that a global mean hides.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from creative_forge.ffmpeg import MediaInfo, extract_frame
from creative_forge.qa.result import CheckResult, Status, skipped

SEAM_SIZE = (1080, 1920)


@dataclass(frozen=True)
class SeamDiff:
    mean: float
    worst_block: float
    worst_at: tuple[int, int]  # x, y of the worst block, in 1080x1920 coordinates
    heatmap: Image.Image


def seam_diff(a: Image.Image, b: Image.Image, block: int = 60) -> SeamDiff:
    """Port of the production seam probe: both frames at 1080x1920, per-pixel mean over channels."""
    fa = np.asarray(a.convert("RGB").resize(SEAM_SIZE, Image.BILINEAR), dtype=np.float32)
    fb = np.asarray(b.convert("RGB").resize(SEAM_SIZE, Image.BILINEAR), dtype=np.float32)
    d = np.abs(fa - fb).mean(axis=2)
    by, bx = SEAM_SIZE[1] // block, SEAM_SIZE[0] // block
    blocks = d[: by * block, : bx * block].reshape(by, block, bx, block).mean(axis=(1, 3))
    iy, ix = np.unravel_index(int(blocks.argmax()), blocks.shape)
    heat = Image.fromarray(np.clip(d * 3, 0, 255).astype(np.uint8)).resize((270, 480))
    return SeamDiff(float(d.mean()), float(blocks.max()), (int(ix * block), int(iy * block)), heat)


def _over(value: float, limit: float | None) -> bool:
    return limit is not None and value > limit


def _status(
    diff: SeamDiff, mean_warn: float, mean_fail: float, block_warn: float | None, block_fail: float | None
) -> Status:
    if diff.mean > mean_fail or _over(diff.worst_block, block_fail):
        return Status.FAIL
    if diff.mean > mean_warn or _over(diff.worst_block, block_warn):
        return Status.WARN
    return Status.PASS


def check_seam_frames(
    name: str,
    a: Image.Image,
    b: Image.Image,
    mean_warn: float,
    mean_fail: float,
    block_warn: float | None,
    block_fail: float | None,
) -> CheckResult:
    diff = seam_diff(a, b)
    block = f", worst 60px block <= {block_warn:g}" if block_warn is not None else ""
    return CheckResult(
        name,
        _status(diff, mean_warn, mean_fail, block_warn, block_fail),
        diff.mean,
        f"mean <= {mean_warn:g}{block} (0-255)",
        "",
        f"worst block {diff.worst_block:.1f} at x={diff.worst_at[0]}, y={diff.worst_at[1]}",
        {"worst_block": round(diff.worst_block, 2), "worst_at": list(diff.worst_at)},
    )


def check_seam(
    info: MediaInfo,
    cut_s: float | None,
    crossfade_s: float,
    mean_warn: float,
    mean_fail: float,
    block_warn: float | None = None,
    block_fail: float | None = None,
) -> CheckResult:
    """Last pure-hook frame (just before the crossfade) vs first pure-gameplay frame (just after it)."""
    if cut_s is None:
        return skipped("seam", "no timeline (pass --hook-end or keep the .forge.json sidecar)")
    fps = info.fps or 30.0
    before = extract_frame(Path(info.path), max(0.0, cut_s - 1.0 / fps))
    after = extract_frame(Path(info.path), cut_s + crossfade_s + 1.0 / fps)
    return check_seam_frames("seam", before, after, mean_warn, mean_fail, block_warn, block_fail)
