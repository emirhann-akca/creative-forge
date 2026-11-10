"""Thin, explicit wrappers around the ffmpeg / ffprobe command line tools.

Which binaries run is decided in one place, ``find_tools()``, in this order:

1. ``CREATIVE_FORGE_FFMPEG_DIR``: a directory holding both ffmpeg and ffprobe (an explicit choice; if it is wrong
   that is an error, not a reason to fall back);
2. the build ``forge setup ffmpeg`` installed under the user data directory (pinned version, SHA-256 verified);
3. ``PATH``.

Every call decodes ffmpeg's output as UTF-8 explicitly: with a locale default, a path or tag containing "Ş" crashed
ffprobe parsing on a Turkish Windows (cp1254 has no 0x9E).
"""

from __future__ import annotations

import functools
import io
import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from creative_forge.paths import data_dir

ENV_FFMPEG_DIR = "CREATIVE_FORGE_FFMPEG_DIR"
MANAGED_VERSION = "9.0.2"  # the build `forge setup ffmpeg` installs (see setup_ffmpeg.BUILDS)
EXE_SUFFIX = ".exe" if os.name == "nt" else ""

# What the pipeline and the QA checks need from a build. A binary that lacks any of these is refused up front
# instead of failing halfway through a render.
REQUIRED_ENCODERS = ("libx264",)
AAC_ENCODERS = ("aac_at", "aac_mf", "aac")  # best first: AudioToolbox (macOS), Media Foundation (Windows), built-in
REQUIRED_FILTERS = (
    "minterpolate",
    "ebur128",
    "loudnorm",
    "freezedetect",
    "silencedetect",
    "alimiter",
    "sidechaincompress",
    "xfade",
)
REQUIRED_DEVICES = ("lavfi",)  # synthetic media for `forge demo` and the tests
SETUP_HINT = "run `forge setup ffmpeg` (downloads a pinned, SHA-256 verified build; no admin rights needed)"


class FFmpegError(RuntimeError):
    pass


@dataclass(frozen=True)
class Toolchain:
    ffmpeg: Path
    ffprobe: Path
    source: str  # "CREATIVE_FORGE_FFMPEG_DIR", "managed" or "PATH"


def managed_dir() -> Path:
    """Where ``forge setup ffmpeg`` puts ffmpeg and ffprobe for this platform."""
    return data_dir() / "ffmpeg" / MANAGED_VERSION


def _pair(directory: Path) -> tuple[Path, Path]:
    return directory / f"ffmpeg{EXE_SUFFIX}", directory / f"ffprobe{EXE_SUFFIX}"


def _resolve(env_dir: str | None, managed: str, path_var: str | None) -> Toolchain:
    if env_dir:
        ff, fp = _pair(Path(env_dir))
        missing = [p.name for p in (ff, fp) if not p.is_file()]
        if missing:
            raise FFmpegError(f"{ENV_FFMPEG_DIR}={env_dir} does not contain {' and '.join(missing)}")
        return Toolchain(ff, fp, ENV_FFMPEG_DIR)
    ff, fp = _pair(Path(managed))
    if ff.is_file() and fp.is_file():
        return Toolchain(ff, fp, "managed")
    found = {t: shutil.which(t, path=path_var) for t in ("ffmpeg", "ffprobe")}
    if all(found.values()):
        return Toolchain(Path(found["ffmpeg"]), Path(found["ffprobe"]), "PATH")  # type: ignore[arg-type]
    missing = [t for t, p in found.items() if not p]
    raise FFmpegError(f"{' and '.join(missing)} not found (not in {managed}, not on PATH); {SETUP_HINT}")


def find_tools() -> Toolchain:
    """The ffmpeg/ffprobe pair to use. Resolved on every call (two stat calls and a PATH lookup, next to a process
    spawn), so a build installed by ``forge setup ffmpeg`` or a changed environment takes effect at once."""
    return _resolve(os.environ.get(ENV_FFMPEG_DIR) or None, str(managed_dir()), os.environ.get("PATH"))


def reset_tools_cache() -> None:
    """Forget what was learned about binaries (after ``forge setup ffmpeg`` replaced the managed build)."""
    _encoders_of.cache_clear()
    _encoder_works.cache_clear()


def require_tools() -> Toolchain:
    return find_tools()


def run(
    args: list[str],
    *,
    quiet: bool = True,
    check: bool = True,
    input_bytes: bytes | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """Run ffmpeg. ``quiet`` uses ``-v error``; measurement filters need ``quiet=False`` to print their summaries.

    Output stays bytes (``stdout`` often carries PNG frames); decode text with ``.decode("utf-8", "replace")``."""
    exe = str(find_tools().ffmpeg)
    cmd = [exe, "-hide_banner", "-nostdin", "-y"] + (["-v", "error"] if quiet else []) + [str(a) for a in args]
    p = subprocess.run(cmd, capture_output=True, input=input_bytes, timeout=timeout)
    if check and p.returncode != 0:
        err = p.stderr.decode("utf-8", errors="replace")[-2000:]
        raise FFmpegError(f"ffmpeg failed ({p.returncode}): {' '.join(cmd[:60])}\n{err}")
    return p


def stderr_of(args: list[str]) -> str:
    """Run an analysis pass (``-f null -``) and return its stderr at info level."""
    return run(args, quiet=False).stderr.decode("utf-8", errors="replace")


def _text(cmd: list[str], timeout: float = 60) -> str:
    p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise FFmpegError(f"{Path(cmd[0]).name} {' '.join(cmd[1:])} failed ({p.returncode})")
    return p.stdout.decode("utf-8", errors="replace")


# ------------------------------------------------------------------------------------------------ feature probe
_ENCODER_LINE = re.compile(r"^\s*[VAS][.A-Z]{5}\s+(\w[\w-]*)", re.M)
_FILTER_LINE = re.compile(r"^\s*[A-Z.]{2,3}\s+(\w+)\s+\S*->\S*\s", re.M)
_DEVICE_LINE = re.compile(r"^\s*D[E.]?\s+([a-z0-9_,]+)\s", re.M)


def parse_encoders(text: str) -> frozenset[str]:
    return frozenset(_ENCODER_LINE.findall(text))


def parse_filters(text: str) -> frozenset[str]:
    return frozenset(_FILTER_LINE.findall(text))


def parse_devices(text: str) -> frozenset[str]:
    return frozenset(n for names in _DEVICE_LINE.findall(text) for n in names.split(","))


@dataclass(frozen=True)
class Features:
    version: str
    encoders: frozenset[str] = field(repr=False)
    filters: frozenset[str] = field(repr=False)
    devices: frozenset[str] = field(repr=False)

    def missing(self) -> list[str]:
        out = [f"encoder {e}" for e in REQUIRED_ENCODERS if e not in self.encoders]
        if not any(a in self.encoders for a in AAC_ENCODERS):
            out.append(f"an AAC encoder ({', '.join(AAC_ENCODERS)})")
        out += [f"filter {f}" for f in REQUIRED_FILTERS if f not in self.filters]
        out += [f"input device {d}" for d in REQUIRED_DEVICES if d not in self.devices]
        return out


def inspect(ffmpeg: Path) -> Features:
    """List what a build can do (runs ``-version``, ``-encoders``, ``-filters`` and ``-devices``)."""
    exe = str(ffmpeg)
    lines = _text([exe, "-hide_banner", "-version"]).splitlines()
    version = lines[0].strip() if lines else "unknown"
    return Features(
        version=version,
        encoders=parse_encoders(_text([exe, "-hide_banner", "-encoders"])),
        filters=parse_filters(_text([exe, "-hide_banner", "-filters"])),
        devices=parse_devices(_text([exe, "-hide_banner", "-devices"])),
    )


def probe_version(ffprobe: Path) -> str:
    lines = _text([str(ffprobe), "-hide_banner", "-version"]).splitlines()
    return lines[0].strip() if lines else "unknown"


def check_build(tools: Toolchain) -> tuple[Features, str]:
    """Raise ``FFmpegError`` naming every missing feature; return the features and the ffprobe version line."""
    feats = inspect(tools.ffmpeg)
    missing = feats.missing()
    if missing:
        raise FFmpegError(
            f"{tools.ffmpeg} ({feats.version}) lacks {', '.join(missing)}. creative-forge needs a GPL build with "
            f"libx264; {SETUP_HINT}"
        )
    return feats, probe_version(tools.ffprobe)


@functools.lru_cache(maxsize=8)
def _encoders_of(ffmpeg: str) -> frozenset[str]:
    return parse_encoders(_text([ffmpeg, "-hide_banner", "-encoders"]))


def encoders() -> frozenset[str]:
    return _encoders_of(str(find_tools().ffmpeg))


@functools.lru_cache(maxsize=32)
def _encoder_works(ffmpeg: str, name: str) -> bool:
    """Listed is not the same as usable: aac_mf needs Media Foundation, which some Windows editions lack."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-f", "lavfi", "-i"]
    cmd += ["sine=frequency=1000:sample_rate=48000:duration=0.25", "-ac", "2", "-c:a", name, "-b:a", "192k"]
    try:
        return subprocess.run([*cmd, "-f", "null", "-"], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


_aac_lock = threading.Lock()


def aac_encoders() -> list[str]:
    """The AAC encoders this ffmpeg lists AND can open, best first."""
    exe = str(find_tools().ffmpeg)
    listed = _encoders_of(exe)
    with _aac_lock:
        return [n for n in AAC_ENCODERS if n in listed and _encoder_works(exe, n)]


def aac_encoder(preference: str = "auto") -> str:
    """AudioToolbox AAC (macOS), then Media Foundation AAC (Windows), then ffmpeg's built-in ``aac``, which overshot
    true peak by up to +6.7 dB on limited signals in production. Whichever runs, the master stage measures true peak
    after encoding and pulls the limiter down if the encoder overshoots."""
    working = aac_encoders()
    if preference != "auto":
        if preference not in working:
            raise FFmpegError(
                f"audio encoder {preference!r} is not usable in this ffmpeg build (usable: {', '.join(working)})"
            )
        return preference
    if not working:
        raise FFmpegError(f"no usable AAC encoder in {find_tools().ffmpeg}")
    return working[0]


# Media Foundation's AAC encoder accepts only these bit rates (12000/16000/20000/24000 bytes per second).
MF_AAC_KBPS = (96, 128, 160, 192)


def aac_bitrate(encoder: str, kbps: int) -> int:
    return min(MF_AAC_KBPS, key=lambda b: abs(b - kbps)) if encoder == "aac_mf" else kbps


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
        [str(find_tools().ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
        capture_output=True,
    )
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", errors="replace").strip()[:500]
        raise FFmpegError(f"ffprobe failed on {path}: {err}")
    d = json.loads(p.stdout.decode("utf-8", errors="replace"))
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
        str(find_tools().ffmpeg),
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
