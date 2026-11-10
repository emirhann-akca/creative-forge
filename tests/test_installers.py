"""Static checks on install.sh / install.ps1. The real installs run in CI's user-install jobs on each OS."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from creative_forge import __version__
from creative_forge.ffmpeg import MANAGED_VERSION

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / "install.sh"
PS1 = ROOT / "install.ps1"


def test_versions_agree_everywhere():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    sh = re.search(r'^FORGE_VERSION="([^"]+)"', SH.read_text(encoding="utf-8"), re.M)
    ps = re.search(r"^\s*\$ForgeVersion = '([^']+)'", PS1.read_text(encoding="utf-8"), re.M)
    assert sh and ps
    assert pyproject == __version__ == sh.group(1) == ps.group(1)


def test_uv_pin_is_the_same_version_in_both_installers():
    sh = re.search(r'^UV_VERSION="([^"]+)"', SH.read_text(encoding="utf-8"), re.M)
    ps = re.search(r"^\s*\$UvVersion = '([^']+)'", PS1.read_text(encoding="utf-8"), re.M)
    assert sh and ps and sh.group(1) == ps.group(1)
    hashes = re.findall(r"[0-9a-f]{64}", SH.read_text(encoding="utf-8") + PS1.read_text(encoding="utf-8"))
    assert len(set(hashes)) == 3  # macOS arm64, Linux x64, Windows x64


def test_powershell_installer_is_ascii_and_never_exits():
    """irm | iex: PowerShell 5.1 reads a BOM-less script as ANSI, and `exit` would close the user's window."""
    raw = PS1.read_bytes()
    assert all(b < 128 for b in raw), "install.ps1 must be ASCII"
    code = "\n".join(line.split("#", 1)[0] for line in raw.decode("ascii").splitlines())
    assert not re.search(r"(^|[;{}])\s*exit\b", code, re.I | re.M), "a bare `exit` statement"
    assert code.lstrip().startswith("& {") and code.rstrip().endswith("}")
    for operator in ("??", "?.", "&&", "||"):  # PowerShell 7-only syntax
        assert operator not in code, operator


def test_shell_installer_is_posix_sh():
    raw = SH.read_bytes()
    assert raw.startswith(b"#!/bin/sh\n") and all(b < 128 for b in raw) and b"\r\n" not in raw
    if sys.platform != "win32":
        subprocess.run(["sh", "-n", str(SH)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", "-s", "sh", str(SH)], check=True)


def test_installers_run_setup_and_doctor():
    for text in (SH.read_text(encoding="utf-8"), PS1.read_text(encoding="utf-8")):
        assert "setup" in text and "ffmpeg" in text and "doctor" in text and "[ocr]" in text
        assert "--compile-bytecode" in text


@pytest.mark.parametrize("path", [SH, PS1])
def test_installers_name_the_release_assets_that_release_yml_attaches(path):
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert path.name in release
    assert "releases/latest/download/" + path.name in path.read_text(encoding="utf-8")
    assert MANAGED_VERSION in (ROOT / "README.md").read_text(encoding="utf-8")
