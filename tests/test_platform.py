"""Cross-platform plumbing: retrying renames, tool directories, ffmpeg resolution and feature probing, non-ASCII paths,
the console encoding. The Windows-only tests run on the Windows CI runners."""

from __future__ import annotations

import io
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from creative_forge import fsutil, paths
from creative_forge.ffmpeg import (
    AAC_ENCODERS,
    ENV_FFMPEG_DIR,
    EXE_SUFFIX,
    Features,
    FFmpegError,
    aac_bitrate,
    aac_encoder,
    aac_encoders,
    check_build,
    find_tools,
    managed_dir,
    parse_devices,
    parse_encoders,
    parse_filters,
    probe,
    run,
)

TURKISH = "Çalışma Ş/İğ"  # the directory the CI demo runs in, too

ENCODERS_TEXT = """Encoders:
 V..... = Video
 A..... = Audio
 ------
 V....D libx264              libx264 H.264 / AVC / MPEG-4 AVC / MPEG-4 part 10 (codec h264)
 A....D aac                  AAC (Advanced Audio Coding)
 A..... aac_mf               AAC via MediaFoundation (codec aac)
"""
FILTERS_TEXT = """Filters:
  T.. = Timeline support
  ------
 T. alimiter          A->A       Audio lookahead limiter.
 .. ebur128           A->N       EBU R128 scanner.
 .. loudnorm          A->A       EBU R128 loudness normalization
 .. silencedetect     A->A       Detect silence.
 .. freezedetect      V->V       Detects frozen video input.
 .. minterpolate      V->V       Frame rate conversion using Motion Interpolation.
 .. sidechaincompress AA->A      Sidechain compressor.
 .S xfade             VV->V      Cross fade one video with another video.
 TSC scale            V->V       Older three-flag layout.
"""
DEVICES_TEXT = """Devices:
 D. = Demuxing supported
 .E = Muxing supported
 ---
  E audiotoolbox    AudioToolbox output device
 D  lavfi           Libavfilter virtual input device
 DE alsa            ALSA audio output
"""


# ------------------------------------------------------------------------------------------------- fsutil
def test_replace_retries_a_transient_permission_error(tmp_path, monkeypatch):
    src, dst = tmp_path / "a", tmp_path / "b"
    src.write_bytes(b"new")
    real = os.replace
    calls = {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(32, "The process cannot access the file because it is being used by another process")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    fsutil.replace(src, dst, retry=True)
    assert dst.read_bytes() == b"new" and calls["n"] == 3


def test_replace_does_not_retry_when_retry_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError(13, "denied")))
    t0 = time.monotonic()
    with pytest.raises(PermissionError):
        fsutil.replace(tmp_path / "a", tmp_path / "b", retry=False)
    assert time.monotonic() - t0 < 0.5


def test_rmtree_removes_read_only_files_and_tolerates_missing(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    f = d / "x.txt"
    f.write_text("x", encoding="utf-8")
    f.chmod(0o444)
    fsutil.rmtree(d)
    assert not d.exists()
    fsutil.rmtree(tmp_path / "never-existed")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing semantics")
def test_windows_replace_waits_for_another_handle_to_close(tmp_path):
    """A reader (player, indexer, antivirus) holds the target open: a plain os.replace fails, the retry succeeds."""
    src, dst = tmp_path / "new.json", tmp_path / "review.json"
    src.write_text("new", encoding="utf-8")
    dst.write_text("old", encoding="utf-8")
    holder = dst.open("rb")
    with pytest.raises(PermissionError):
        os.replace(src, dst)
    threading.Timer(0.3, holder.close).start()
    fsutil.replace(src, dst)
    assert dst.read_text(encoding="utf-8") == "new"


# ------------------------------------------------------------------------------------------------- paths
def test_creative_forge_home_overrides_platform_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    assert paths.data_dir() == tmp_path / "data" and paths.cache_dir() == tmp_path / "cache"
    assert managed_dir() == tmp_path / "data" / "ffmpeg" / "9.0.2"


def test_default_dirs_follow_the_platform(monkeypatch):
    monkeypatch.delenv(paths.ENV_HOME, raising=False)
    data = str(paths.data_dir())
    assert data.endswith("creative-forge")
    if sys.platform == "win32":
        assert data.lower().startswith(os.environ["LOCALAPPDATA"].lower())
    elif sys.platform == "darwin":
        assert "Library/Application Support" in data


# ------------------------------------------------------------------------------------------------- ffmpeg
def test_parsers_read_real_listing_layouts():
    assert {"libx264", "aac", "aac_mf"} == parse_encoders(ENCODERS_TEXT)
    filters = parse_filters(FILTERS_TEXT)
    assert {"alimiter", "ebur128", "minterpolate", "xfade", "sidechaincompress", "scale"} <= filters
    assert "=" not in filters and "Timeline" not in filters
    assert parse_devices(DEVICES_TEXT) == {"lavfi", "alsa"}


def test_missing_features_are_named():
    full = Features("v", parse_encoders(ENCODERS_TEXT), parse_filters(FILTERS_TEXT), parse_devices(DEVICES_TEXT))
    assert full.missing() == []
    lgpl = Features("v", frozenset({"aac"}), frozenset({"loudnorm"}), frozenset())
    missing = lgpl.missing()
    assert "encoder libx264" in missing and "filter minterpolate" in missing and "input device lavfi" in missing
    no_aac = Features("v", frozenset({"libx264"}), full.filters, full.devices)
    assert any("AAC encoder" in m for m in no_aac.missing())


def test_resolved_ffmpeg_has_every_required_feature():
    feats, ffprobe_version = check_build(find_tools())
    assert feats.missing() == [] and ffprobe_version.startswith("ffprobe version")


def _fake_pair(d: Path) -> None:
    d.mkdir(parents=True, exist_ok=True)
    for name in ("ffmpeg", "ffprobe"):
        (d / f"{name}{EXE_SUFFIX}").write_bytes(b"")


def test_resolution_order_env_then_managed_then_path(tmp_path, monkeypatch):
    real = find_tools()
    monkeypatch.delenv(ENV_FFMPEG_DIR, raising=False)
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(real.ffmpeg.parent))
    t = find_tools()
    assert t.source == "PATH" and t.ffmpeg.parent == real.ffmpeg.parent

    _fake_pair(managed_dir())  # resolution only looks; it never runs the files
    t = find_tools()
    assert t.source == "managed" and t.ffmpeg.parent == managed_dir()

    monkeypatch.setenv(ENV_FFMPEG_DIR, str(real.ffmpeg.parent))
    t = find_tools()
    assert t.source == ENV_FFMPEG_DIR and t.ffmpeg == real.ffmpeg.parent / f"ffmpeg{EXE_SUFFIX}"


def test_a_wrong_ffmpeg_dir_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_FFMPEG_DIR, str(tmp_path))
    with pytest.raises(FFmpegError, match=ENV_FFMPEG_DIR):
        find_tools()


def test_nothing_found_points_at_setup(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_FFMPEG_DIR, raising=False)
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(FFmpegError, match="forge setup ffmpeg"):
        find_tools()


def test_aac_preference_order_and_fallback(monkeypatch):
    import creative_forge.ffmpeg as ff

    usable = aac_encoders()
    assert usable and usable[-1] == "aac" and usable == [n for n in AAC_ENCODERS if n in usable]
    assert aac_encoder() == usable[0]
    if sys.platform == "darwin":
        assert usable[0] == "aac_at"
    monkeypatch.setattr(ff, "aac_encoders", lambda: ["aac_mf", "aac"])
    assert aac_encoder() == "aac_mf"
    with pytest.raises(FFmpegError, match="aac_at"):
        aac_encoder("aac_at")


def test_media_foundation_bitrates_snap_to_supported_values():
    assert aac_bitrate("aac_mf", 192) == 192 and aac_bitrate("aac_mf", 256) == 192 and aac_bitrate("aac_mf", 100) == 96
    assert aac_bitrate("aac_at", 256) == 256


def test_probe_and_encode_in_a_turkish_directory(tmp_path):
    """ffprobe's JSON names the file; decoded with a Windows ANSI code page this crashed on 'Ş' (0x9E in cp1254)."""
    d = tmp_path / TURKISH
    d.mkdir(parents=True)
    clip = d / "kısa çekim ğüşöç.mp4"
    run(["-f", "lavfi", "-i", "testsrc2=s=64x64:r=10:d=1", "-c:v", "libx264", "-metadata", "title=Şık İğne", str(clip)])
    info = probe(clip)
    assert info.path == clip and info.width == 64 and info.duration > 0.5


def test_cli_switches_a_non_utf8_stream_to_utf8(monkeypatch):
    from creative_forge.cli import _utf8_console

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    _utf8_console()
    print(f"report: C:\\{TURKISH}\\report.html")
    stream.flush()
    assert raw.getvalue().decode("utf-8").startswith("report: C:\\Çalışma Ş")
