"""Thin, explicit wrappers around the ffmpeg / ffprobe command line tools."""

from __future__ import annotations

import functools
import io
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image


class FFmpegError(RuntimeError):
    pass


def require_tools() -> None:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        raise FFmpegError(f"missing on PATH: {', '.join(missing)} (install ffmpeg)")


def run(
    args: list[str],
    *,
    quiet: bool = True,
    check: bool = True,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run ffmpeg. ``quiet`` uses ``-v error``; measurement filters need ``quiet=False`` to print their summaries."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"] + (["-v", "error"] if quiet else []) + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, input=input_bytes, timeout=timeout)
    if check and p.returncode != 0:
        err = p.stderr.decode(errors="replace")[-2000:]
        raise FFmpegError(f"ffmpeg failed ({p.returncode}): {' '.join(cmd[:60])}\n{err}")
    return p


def stderr_of(args: list[str]) -> str:
    """Run an analysis pass (``-f null -``) and return its stderr at info level."""
    return run(args, quiet=False).stderr.decode(errors="replace")


@functools.cache
def encoders() -> frozenset[str]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    names = set()
    for line in out.splitlines():
        m = re.match(r"\s*[VAS][.A-Z]{5}\s+(\S+)", line)
        if m:
            names.add(m.group(1))
    return frozenset(names)


def aac_encoder(preference: str = "auto") -> str:
    """AudioToolbox AAC (macOS) when available: ffmpeg's built-in ``aac`` overshot true peak by up to +6.7 dB on
    limited signals in production. Either way the master stage measures true peak after encoding."""
    if preference != "auto":
        if preference not in encoders():
            raise FFmpegError(f"audio encoder {preference!r} is not available in this ffmpeg build")
        return preference
    return "aac_at" if "aac_at" in encoders() else "aac"


@dataclass(frozen=True)
class MediaInfo:
    path: Path
    duration: float
    size_bytes: int
    width: int | None
    height: int | None
    fps: float | None
    video_duration: float | None
    audio_duration: float | None
    sample_rate: int | None
    channels: int | None

    @property
    def has_audio(self) -> bool:
        return self.audio_duration is not None

    @property
    def has_video(self) -> bool:
        return self.video_duration is not None


def _rate(s: str | None) -> float | None:
    if not s or s in ("0/0", "0"):
        return None
    n, _, d = s.partition("/")
    return float(n) / float(d or 1)


def _stream_duration(st: dict, fmt_duration: float) -> float:
    if st.get("duration") not in (None, "N/A"):
        return float(st["duration"])
    tags = st.get("tags") or {}
    if "DURATION" in tags:  # Matroska
        h, m, s = tags["DURATION"].split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    return fmt_duration


def probe(path: str | Path) -> MediaInfo:
    path = Path(path)
    p = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
        text=True,
    )
    if p.returncode != 0:
        raise FFmpegError(f"ffprobe failed on {path}: {p.stderr.strip()[:500]}")
    d = json.loads(p.stdout)
    fmt_dur = float(d.get("format", {}).get("duration", 0.0) or 0.0)
    v = next(
        (
            s
            for s in d.get("streams", [])
            if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    return MediaInfo(
        path=path,
        duration=fmt_dur,
        size_bytes=path.stat().st_size,
        width=int(v["width"]) if v else None,
        height=int(v["height"]) if v else None,
        fps=_rate(v.get("avg_frame_rate")) if v else None,
        video_duration=_stream_duration(v, fmt_dur) if v else None,
        audio_duration=_stream_duration(a, fmt_dur) if a else None,
        sample_rate=int(a["sample_rate"]) if a and a.get("sample_rate") else None,
        channels=int(a["channels"]) if a and a.get("channels") else None,
    )


def extract_frame(path: str | Path, t: float, size: tuple[int, int] | None = None) -> Image.Image:
    """Decode one frame at ``t`` seconds (accurate seek) as RGB."""
    vf = ["-vf", f"scale={size[0]}:{size[1]}:flags=lanczos"] if size else []
    p = run(
        ["-ss", f"{max(0.0, t):.4f}", "-i", str(path), "-frames:v", "1", *vf, "-f", "image2pipe", "-vcodec", "png", "-"]
    )
    if not p.stdout:
        raise FFmpegError(f"no frame at t={t:.3f}s in {path}")
    return Image.open(io.BytesIO(p.stdout)).convert("RGB")


def last_frame(path: str | Path) -> Image.Image:
    """Decode the final frame of a video (seek from the end; robust to inexact durations)."""
    p = run(["-sseof", "-0.5", "-i", str(path), "-f", "image2pipe", "-vcodec", "png", "-"])
    if not p.stdout:
        raise FFmpegError(f"could not read last frame of {path}")
    data = p.stdout  # every frame of the last 0.5 s as concatenated PNGs; keep the final one
    start = data.rfind(b"\x89PNG\r\n\x1a\n")
    return Image.open(io.BytesIO(data[start:])).convert("RGB")


def pipe_writer(
    out: Path, width: int, height: int, fps: int, crf: int = 12, preset: str = "medium"
) -> subprocess.Popen:
    """An ffmpeg process that encodes raw RGB24 frames written to its stdin."""
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{width}x{height}",
        "-r",
        str(fps),
        "-i",
        "-",
        "-c:v",
        "libx264",
        "-crf",
        str(crf),
        "-preset",
        preset,
        "-pix_fmt",
        "yuv420p",
        str(out),
    ]
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
