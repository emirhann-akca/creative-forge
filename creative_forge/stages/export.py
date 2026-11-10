"""Delivery encodes: 9:16 master and 4:5 feed variant, both under the file-size cap."""

from __future__ import annotations

from pathlib import Path

from creative_forge.ffmpeg import run
from creative_forge.spec import ExportSpec


def maxrate_for(duration_s: float, e: ExportSpec) -> int:
    """Video maxrate (kbps) that keeps video + audio under ``max_file_mb`` with 5 % container headroom.
    Production: crf 20 + maxrate 3.6M kept a 60 s ad at ~27 MB (crf 18 alone was 46 MB)."""
    budget_kbps = e.max_file_mb * 8000 * 0.95 / max(1.0, duration_s) - e.audio_bitrate_kbps
    return int(max(300, min(e.maxrate_kbps, budget_kbps)))


def size_4x5(width: int) -> tuple[int, int]:
    h = round(width * 5 / 4)
    return width, h - h % 2


def _encode(inputs: list[str], vf_args: list[str], out: Path, duration_s: float, e: ExportSpec) -> Path:
    rate = maxrate_for(duration_s, e)
    run(
        [
            *inputs,
            *vf_args,
            "-c:v",
            "libx264",
            "-preset",
            e.preset,
            "-crf",
            str(e.crf),
            "-maxrate",
            f"{rate}k",
            "-bufsize",
            f"{2 * rate}k",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-t",
            f"{duration_s:.3f}",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


def export_9x16(master: Path, out: Path, duration_s: float, e: ExportSpec) -> Path:
    return _encode(["-i", str(master)], ["-map", "0:v:0", "-map", "0:a:0"], out, duration_s, e)


def export_4x5(master: Path, out: Path, duration_s: float, width: int, e: ExportSpec) -> Path:
    """4:5 must not crop gameplay: the full 9:16 frame sits in the centre, the sides are a blurred, darkened copy."""
    w, h = size_4x5(width)
    fg_w = round(h * 9 / 16)
    fg_w -= fg_w % 2
    sigma = max(4, w // 40)
    graph = (
        f"[0:v]split=2[bg][fg];"
        f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma={sigma},"
        f"eq=brightness=-0.06,setsar=1[bgb];"
        f"[fg]scale={fg_w}:{h}:flags=lanczos,setsar=1[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:0,format=yuv420p[v]"
    )
    return _encode(["-i", str(master)], ["-filter_complex", graph, "-map", "[v]", "-map", "0:a:0"], out, duration_s, e)
