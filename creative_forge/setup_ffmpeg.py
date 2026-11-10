"""``forge setup ffmpeg``: a pinned, SHA-256 verified ffmpeg + ffprobe in the user data directory.

The same FFmpeg release (9.0.2) on every supported platform, so filter behaviour and QA thresholds do not drift
between machines. Each archive is pinned by URL, size and SHA-256; a download that does not match is deleted and
nothing is installed. The binaries are then probed for every feature the pipeline uses before they are activated.

Sources (re-verified on 11 Nov 2026):

* Windows x64: Gyan Doshi's "essentials" build from the GyanD/codexffmpeg GitHub release, the publisher winget's
  ``Gyan.FFmpeg`` package installs from. The SHA-256 matches the checksum gyan.dev publishes for the release.
* macOS arm64 and Linux x64: Martin Riedl's static builds (macOS binaries are signed with his Developer ID,
  team KU3N25YGLU). The SHA-256 values match the ``.sha256`` files published next to each archive.

All three are GPL builds (libx264). They are downloaded from their publishers onto your machine; creative-forge
does not redistribute them.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

from creative_forge import fsutil
from creative_forge.ffmpeg import (
    EXE_SUFFIX,
    MANAGED_VERSION,
    Features,
    FFmpegError,
    Toolchain,
    check_build,
    find_tools,
    managed_dir,
    reset_tools_cache,
)

Log = Callable[[str], None]


class SetupError(RuntimeError):
    """Nothing was installed (or the previous install was left as it was)."""


@dataclass(frozen=True)
class Archive:
    url: str
    sha256: str
    size: int
    members: tuple[tuple[str, str], ...]  # (path inside the zip, installed file name)


@dataclass(frozen=True)
class Build:
    key: str
    version: str
    publisher: str
    archives: tuple[Archive, ...]


_GYAN = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
_GYAN_DIR = "ffmpeg-9.0.2-essentials_build"
_RIEDL_MAC = "https://ffmpeg.martin-riedl.de/download/macos/arm64/1789931890_9.0.2"
_RIEDL_LINUX = "https://ffmpeg.martin-riedl.de/download/linux/amd64/1789931100_9.0.2"

BUILDS: dict[str, Build] = {
    "windows-x86_64": Build(
        "windows-x86_64",
        "9.0.2",
        "Gyan Doshi, gyan.dev essentials build (GitHub release GyanD/codexffmpeg 9.0.2)",
        (
            Archive(
                _GYAN,
                "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba",
                114_768_076,
                (
                    (f"{_GYAN_DIR}/bin/ffmpeg.exe", "ffmpeg.exe"),
                    (f"{_GYAN_DIR}/bin/ffprobe.exe", "ffprobe.exe"),
                    (f"{_GYAN_DIR}/LICENSE", "LICENSE.txt"),
                ),
            ),
        ),
    ),
    "macos-arm64": Build(
        "macos-arm64",
        "9.0.2",
        "Martin Riedl, ffmpeg.martin-riedl.de (signed, Developer ID KU3N25YGLU)",
        (
            Archive(
                f"{_RIEDL_MAC}/ffmpeg.zip",
                "c8ed4c4e6978a03c485edbfe4e0a5dc2380f8a30bba5150531b31b094492d924",
                28_395_699,
                (("ffmpeg", "ffmpeg"),),
            ),
            Archive(
                f"{_RIEDL_MAC}/ffprobe.zip",
                "fcbe839537485eaee7a7a8bc5cbc0f90d53617e80943e8a5b2e31cb851197ea6",
                28_317_701,
                (("ffprobe", "ffprobe"),),
            ),
        ),
    ),
    "linux-x86_64": Build(
        "linux-x86_64",
        "9.0.2",
        "Martin Riedl, ffmpeg.martin-riedl.de",
        (
            Archive(
                f"{_RIEDL_LINUX}/ffmpeg.zip",
                "fa8ecf4abbd290d98f7d188b8649cc6b391ae209a98452be955a15aab1909d7f",
                33_326_445,
                (("ffmpeg", "ffmpeg"),),
            ),
            Archive(
                f"{_RIEDL_LINUX}/ffprobe.zip",
                "3f428c49070be3d24ec338602b76d412e401ffcb8a5641ef0e729181a232fc32",
                33_223_234,
                (("ffprobe", "ffprobe"),),
            ),
        ),
    ),
}


def platform_key(system: str | None = None, machine: str | None = None) -> str | None:
    """The pinned build for this machine, or ``None`` when there is none (install ffmpeg yourself then)."""
    system = system or sys.platform
    m = (machine or platform.machine()).lower()
    if system == "win32" and m in ("amd64", "x86_64"):
        return "windows-x86_64"
    if system == "darwin" and m == "arm64":
        return "macos-arm64"
    if system.startswith("linux") and m in ("x86_64", "amd64"):
        return "linux-x86_64"
    return None


def download(archive: Archive, folder: Path, log: Log, attempts: int = 3) -> Path:
    """Stream ``archive`` into ``folder`` while hashing it; refuse (and delete) anything that does not match."""
    out = folder / Path(urlparse(archive.url).path).name
    for attempt in range(1, attempts + 1):
        try:
            return _download_once(archive, out, log)
        except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as e:
            out.unlink(missing_ok=True)
            if attempt == attempts:
                raise SetupError(f"download of {archive.url} failed after {attempts} attempts: {e}") from e
            log(f"  connection problem ({type(e).__name__}); retrying ({attempt + 1}/{attempts})")
    raise AssertionError("unreachable")


def _download_once(archive: Archive, out: Path, log: Log) -> Path:
    h = hashlib.sha256()
    n = 0
    next_report = 0.25
    with requests.get(archive.url, stream=True, timeout=(20, 120)) as r:
        if r.status_code != 200:
            raise SetupError(f"download of {archive.url} failed: HTTP {r.status_code}")
        with out.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                h.update(chunk)
                n += len(chunk)
                if n > archive.size:
                    break
                if n >= next_report * archive.size:
                    log(f"  {out.name}: {n / 1e6:.0f} of {archive.size / 1e6:.0f} MB")
                    next_report += 0.25
    if n != archive.size or h.hexdigest() != archive.sha256:
        out.unlink(missing_ok=True)
        raise SetupError(
            f"{archive.url} does not match its pin (got {n} bytes, SHA-256 {h.hexdigest()}; expected "
            f"{archive.size} bytes, SHA-256 {archive.sha256}). Nothing was installed."
        )
    return out


def extract(zpath: Path, archive: Archive, dest: Path) -> list[Path]:
    """Copy only the named members out, under our own file names (no path from the archive is trusted)."""
    out = []
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        for member, name in archive.members:
            if member not in names:
                raise SetupError(f"{zpath.name} has no {member}")
            target = dest / name
            with z.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)
            if os.name != "nt" and not name.endswith(".txt"):
                target.chmod(0o755)
            out.append(target)
    return out


def install(
    build: Build,
    *,
    dest: Path | None = None,
    log: Log = print,
    verify: Callable[[Toolchain], object] = check_build,
) -> Path:
    """Download, verify, extract, probe, then swap into ``dest`` in one rename. A failure leaves ``dest`` alone."""
    dest = dest or managed_dir()
    dest.parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".setup-", dir=dest.parent))
    try:
        staging = work / "bin"
        staging.mkdir()
        for a in build.archives:
            log(f"downloading {a.url} ({a.size / 1e6:.0f} MB, SHA-256 pinned)")
            z = download(a, work, log)
            extract(z, a, staging)
            z.unlink()
        tools = Toolchain(staging / f"ffmpeg{EXE_SUFFIX}", staging / f"ffprobe{EXE_SUFFIX}", "managed")
        try:
            verify(tools)
        except FFmpegError as e:
            raise SetupError(f"the downloaded build was refused: {e}") from e
        record = {
            "version": build.version,
            "platform": build.key,
            "publisher": build.publisher,
            "archives": [{"url": a.url, "sha256": a.sha256, "size": a.size} for a in build.archives],
            "installed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        (staging / "build.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        fsutil.rmtree(dest)
        fsutil.replace(staging, dest)
    finally:
        fsutil.rmtree(work)
    reset_tools_cache()
    return dest


@dataclass(frozen=True)
class SetupResult:
    action: str  # "kept" (the resolved ffmpeg already passed the probe) or "installed"
    tools: Toolchain
    features: Features
    ffprobe_version: str

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "ffmpeg": str(self.tools.ffmpeg),
            "ffprobe": str(self.tools.ffprobe),
            "source": self.tools.source,
            "version": self.features.version,
            "ffprobe_version": self.ffprobe_version,
            "missing": self.features.missing(),
        }


def setup_ffmpeg(force: bool = False, log: Log = print) -> SetupResult:
    """Keep a usable ffmpeg if one resolves (``CREATIVE_FORGE_FFMPEG_DIR``, the managed build, ``PATH``); otherwise,
    or with ``force``, install the pinned build for this platform."""
    if not force:
        try:
            tools = find_tools()
            feats, pv = check_build(tools)
            return SetupResult("kept", tools, feats, pv)
        except FFmpegError as e:
            log(f"no usable ffmpeg yet: {e}")
    key = platform_key()
    if key is None:
        raise SetupError(
            f"no pinned ffmpeg build for {sys.platform}/{platform.machine()} (pinned: {', '.join(BUILDS)}). "
            "Install ffmpeg 6 or newer with libx264 (a GPL build) and put it on PATH, or point "
            "CREATIVE_FORGE_FFMPEG_DIR at the directory holding ffmpeg and ffprobe."
        )
    build = BUILDS[key]
    if build.version != MANAGED_VERSION:
        raise SetupError(f"pinned build {build.version} differs from the managed version {MANAGED_VERSION}")
    path = install(build, log=log)
    log(f"installed ffmpeg {build.version} into {path}")
    try:
        tools = find_tools()
        feats, pv = check_build(tools)
    except FFmpegError as e:
        raise SetupError(f"installed into {path}, but the ffmpeg that resolves is not usable: {e}") from e
    if tools.source != "managed":
        log(f"note: {tools.source} takes precedence over the managed build; using {tools.ffmpeg}")
    return SetupResult("installed", tools, feats, pv)
