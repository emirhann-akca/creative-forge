"""`forge setup ffmpeg`: pins, verified download, extraction and refusal, against a local HTTP server.

The real downloads (Gyan on Windows, Martin Riedl on macOS and Linux) run in CI as `forge setup ffmpeg` steps."""

from __future__ import annotations

import functools
import hashlib
import http.server
import json
import os
import re
import threading
import zipfile
from pathlib import Path

import pytest

from creative_forge import paths
from creative_forge.cli import main
from creative_forge.ffmpeg import ENV_FFMPEG_DIR, MANAGED_VERSION, FFmpegError, Toolchain
from creative_forge.setup_ffmpeg import BUILDS, Archive, Build, SetupError, install, platform_key, setup_ffmpeg


@pytest.fixture
def server(tmp_path):
    """Serve ``tmp_path/www`` on 127.0.0.1; yields (folder, base URL)."""
    www = tmp_path / "www"
    www.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(www))
    handler.log_message = lambda *a, **k: None  # type: ignore[attr-defined]
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield www, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def make_zip(path: Path, members: dict[str, bytes]) -> Archive:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    data = path.read_bytes()
    return Archive("", hashlib.sha256(data).hexdigest(), len(data), ())


def archive_for(www: Path, base: str, name: str, members: dict[str, bytes], mapping) -> Archive:
    a = make_zip(www / name, members)
    return Archive(f"{base}/{name}", a.sha256, a.size, tuple(mapping))


def exe(name: str) -> str:
    return f"{name}.exe" if os.name == "nt" else name


@pytest.fixture
def two_member_build(server) -> Build:
    www, base = server
    a = archive_for(
        www,
        base,
        "ff.zip",
        {"pkg/bin/ffmpeg": b"FFMPEG", "pkg/bin/ffprobe": b"FFPROBE", "pkg/LICENSE": b"GPL", "pkg/../evil": b"x"},
        [("pkg/bin/ffmpeg", exe("ffmpeg")), ("pkg/bin/ffprobe", exe("ffprobe")), ("pkg/LICENSE", "LICENSE.txt")],
    )
    return Build("test", MANAGED_VERSION, "test", (a,))


def test_install_extracts_only_named_members_and_records_the_pin(two_member_build, tmp_path):
    seen: list[Toolchain] = []
    dest = tmp_path / "data" / "ffmpeg" / MANAGED_VERSION
    out = install(two_member_build, dest=dest, log=lambda _: None, verify=seen.append)
    assert out == dest
    assert sorted(p.name for p in dest.iterdir()) == sorted(
        [exe("ffmpeg"), exe("ffprobe"), "LICENSE.txt", "build.json"]
    )
    assert (dest / exe("ffmpeg")).read_bytes() == b"FFMPEG"
    if os.name != "nt":
        assert os.access(dest / "ffmpeg", os.X_OK) and os.access(dest / "ffprobe", os.X_OK)
    record = json.loads((dest / "build.json").read_text(encoding="utf-8"))
    assert (
        record["version"] == MANAGED_VERSION and record["archives"][0]["sha256"] == two_member_build.archives[0].sha256
    )
    assert seen and seen[0].ffmpeg.name == exe("ffmpeg")  # probed before it was activated
    assert not list(dest.parent.glob(".setup-*"))  # no staging left behind


def test_a_hash_mismatch_installs_nothing(two_member_build, tmp_path):
    a = two_member_build.archives[0]
    bad = Build("test", MANAGED_VERSION, "test", (Archive(a.url, "0" * 64, a.size, a.members),))
    dest = tmp_path / "ffmpeg" / MANAGED_VERSION
    with pytest.raises(SetupError, match="does not match its pin"):
        install(bad, dest=dest, log=lambda _: None, verify=lambda t: None)
    assert not dest.exists() and not list(dest.parent.glob(".setup-*"))


def test_a_larger_download_is_refused(server, tmp_path):
    www, base = server
    a = archive_for(www, base, "big.zip", {"ffmpeg": b"x" * 1000}, [("ffmpeg", "ffmpeg")])
    short = Build("t", MANAGED_VERSION, "t", (Archive(a.url, a.sha256, a.size - 10, a.members),))
    with pytest.raises(SetupError, match="does not match its pin"):
        install(short, dest=tmp_path / "d", log=lambda _: None, verify=lambda t: None)


def test_a_refused_build_leaves_the_previous_install_alone(two_member_build, tmp_path):
    dest = tmp_path / "ffmpeg" / MANAGED_VERSION
    dest.mkdir(parents=True)
    (dest / "previous").write_text("keep me", encoding="utf-8")

    def lacks_libx264(t: Toolchain) -> None:
        raise FFmpegError("lacks encoder libx264")

    with pytest.raises(SetupError, match="refused: lacks encoder libx264"):
        install(two_member_build, dest=dest, log=lambda _: None, verify=lacks_libx264)
    assert (dest / "previous").read_text(encoding="utf-8") == "keep me"


def test_reinstall_replaces_the_previous_install(two_member_build, tmp_path):
    dest = tmp_path / "ffmpeg" / MANAGED_VERSION
    dest.mkdir(parents=True)
    (dest / "stale").write_text("old", encoding="utf-8")
    install(two_member_build, dest=dest, log=lambda _: None, verify=lambda t: None)
    assert not (dest / "stale").exists() and (dest / "build.json").exists()


def test_missing_member_and_http_errors_are_reported(server, tmp_path):
    www, base = server
    a = archive_for(www, base, "x.zip", {"other": b"1"}, [("ffmpeg", "ffmpeg")])
    with pytest.raises(SetupError, match="has no ffmpeg"):
        install(Build("t", MANAGED_VERSION, "t", (a,)), dest=tmp_path / "d", log=lambda _: None, verify=lambda t: None)
    gone = Archive(f"{base}/missing.zip", "0" * 64, 1, (("ffmpeg", "ffmpeg"),))
    with pytest.raises(SetupError, match="HTTP 404"):
        install(Build("t", MANAGED_VERSION, "t", (gone,)), dest=tmp_path / "d", log=lambda _: None)


def test_pins_are_complete_and_https():
    assert set(BUILDS) == {"windows-x86_64", "macos-arm64", "linux-x86_64"}
    for key, b in BUILDS.items():
        assert b.version == MANAGED_VERSION
        names = {name for a in b.archives for _, name in a.members}
        suffix = ".exe" if key.startswith("windows") else ""
        assert {f"ffmpeg{suffix}", f"ffprobe{suffix}"} <= names
        for a in b.archives:
            assert a.url.startswith("https://") and MANAGED_VERSION in a.url
            assert re.fullmatch(r"[0-9a-f]{64}", a.sha256) and a.size > 10_000_000


@pytest.mark.parametrize(
    ("system", "machine", "key"),
    [
        ("win32", "AMD64", "windows-x86_64"),
        ("darwin", "arm64", "macos-arm64"),
        ("linux", "x86_64", "linux-x86_64"),
        ("darwin", "x86_64", None),
        ("linux", "aarch64", None),
        ("win32", "ARM64", None),
    ],
)
def test_platform_keys(system, machine, key):
    assert platform_key(system, machine) == key


def test_setup_keeps_a_usable_ffmpeg(capsys):
    res = setup_ffmpeg(log=lambda _: None)
    assert res.action == "kept" and res.features.missing() == []
    assert main(["setup", "ffmpeg"]) == 0
    assert "already usable, kept" in capsys.readouterr().out


def test_setup_without_a_pin_says_what_to_do(tmp_path, monkeypatch, capsys):
    import creative_forge.setup_ffmpeg as sf

    monkeypatch.delenv(ENV_FFMPEG_DIR, raising=False)
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(sf, "platform_key", lambda: None)
    with pytest.raises(SetupError, match="CREATIVE_FORGE_FFMPEG_DIR"):
        setup_ffmpeg(log=lambda _: None)
    assert main(["setup", "ffmpeg"]) == 1
    assert "setup failed" in capsys.readouterr().err
