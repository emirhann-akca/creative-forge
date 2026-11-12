"""The desktop app: window lookup, heartbeat, lifecycle, single instance, launchers."""

from __future__ import annotations

import plistlib
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from creative_forge import desktop


def test_find_browser_prefers_edge_on_windows_and_chrome_on_macos(tmp_path):
    env = {"ProgramFiles(x86)": r"C:\PF86", "ProgramFiles": r"C:\PF", "LOCALAPPDATA": r"C:\U\L"}
    edge = str(Path(r"C:\PF86") / r"Microsoft\Edge\Application\msedge.exe")
    chrome = str(Path(r"C:\U\L") / r"Google\Chrome\Application\chrome.exe")
    assert desktop.find_browser("win32", env, exists={edge, chrome}.__contains__) == edge
    assert desktop.find_browser("win32", env, exists={chrome}.__contains__) == chrome
    assert desktop.find_browser("win32", env, exists=lambda p: False) is None
    mac_edge = "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"
    mac_chrome = str(tmp_path / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    assert desktop.find_browser("darwin", {}, exists={mac_edge, mac_chrome}.__contains__, home=tmp_path) == mac_chrome
    which = {"chromium": "/usr/bin/chromium"}.get
    assert desktop.find_browser("linux", {}, which=which) == "/usr/bin/chromium"
    assert desktop.find_browser("linux", {"CREATIVE_FORGE_APP_BROWSER": "/x/b"}, which=which) == "/x/b"


def test_window_command_is_an_app_window_with_its_own_profile(tmp_path):
    cmd = desktop.window_command("/b/chrome", "http://127.0.0.1:5/", tmp_path / "w")
    assert cmd[0] == "/b/chrome" and "--app=http://127.0.0.1:5/" in cmd
    assert f"--user-data-dir={tmp_path / 'w'}" in cmd


async def _page(scope, receive, send):
    body = {"/": b"<html><body><h1>x</h1></body></html>", "/frag": b"<li>htmx</li>", "/api": b"{}"}[scope["path"]]
    ctype = b"application/json" if scope["path"] == "/api" else b"text/html; charset=utf-8"
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", ctype), (b"content-length", str(len(body)).encode())]})  # fmt: skip
    await send({"type": "http.response.body", "body": body[:5], "more_body": True})
    await send({"type": "http.response.body", "body": body[5:]})


def test_heartbeat_adds_its_script_to_full_pages_only():
    now = [10.0]
    beat = desktop.Heartbeat(_page, clock=lambda: now[0])
    c = TestClient(beat, base_url="http://127.0.0.1")
    page = c.get("/")
    assert desktop.HEARTBEAT_TAG.decode() in page.text and int(page.headers["content-length"]) == len(page.content)
    assert desktop.HEARTBEAT_TAG.decode() not in c.get("/frag").text
    assert c.get("/api").text == "{}"
    assert "creative-forge" in c.get("/__app/health").text
    now[0] = 20.0
    assert c.get("/__app/ping").status_code == 200 and beat.pinged and beat.last_ping == 20.0
    now[0] = 30.0
    assert c.post("/__app/bye").status_code == 200 and beat.bye_at == 30.0


def test_pick_root_order(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATIVE_FORGE_HOME", str(tmp_path / "home"))
    state = tmp_path / "state"
    assert desktop.pick_root(None, state) == desktop.default_root()
    other = tmp_path / "runs"
    other.mkdir()
    assert desktop.pick_root(str(other), state) == other.resolve()
    desktop._write_state(state, {"root": str(other)})
    assert desktop.pick_root(None, state) == other


class FakeWindow:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.procs: list[subprocess.Popen] = []

    def __call__(self, url: str) -> subprocess.Popen:
        self.urls.append(url)
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        self.procs.append(p)
        return p


def _get(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status
    except OSError:
        return 0


def _start(tmp_path: Path, **kw) -> tuple[threading.Thread, list[int], Path, str]:
    state, rc = tmp_path / "state", []
    t = threading.Thread(target=lambda: rc.append(desktop.run(str(tmp_path / "runs"), state=state,
                                                               log=lambda m: None, tick_s=0.05, **kw)),
                         daemon=True)  # fmt: skip
    t.start()
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        url = desktop._read_state(state).get("url")
        if url and _get(url + "__app/health") == 200:
            return t, rc, state, url
        time.sleep(0.05)
    raise AssertionError("the app did not start")


def test_closing_the_last_page_stops_the_server_and_the_window(tmp_path):
    win = FakeWindow()
    t, rc, state, url = _start(tmp_path, launch=win, bye_grace_s=0.5)
    assert win.urls == [url] and _get(url) == 200 and _get(url + "__app/ping") == 200
    urllib.request.urlopen(urllib.request.Request(url + "__app/bye", method="POST"), timeout=5)
    t.join(15)
    assert not t.is_alive() and rc == [0] and _get(url + "__app/health") == 0
    win.procs[0].wait(10)
    assert "url" not in desktop._read_state(state)


def test_a_second_launch_opens_a_window_on_the_running_server(tmp_path):
    first, second = FakeWindow(), FakeWindow()
    t, _, state, url = _start(tmp_path, launch=first, gone_after_s=5.0)
    assert desktop.running_url(state) == url
    assert desktop.run(None, state=state, launch=second, log=lambda m: None) == 0
    assert second.urls == [url]
    for p in second.procs:
        p.kill()
    t.join(20)
    assert not t.is_alive()


def test_check_loads_the_pages(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CREATIVE_FORGE_HOME", str(tmp_path / "home"))
    assert desktop.check(None, state=tmp_path / "state") == 0
    assert "check: passed" in capsys.readouterr().out


def test_macos_bundle(tmp_path):
    program = tmp_path / "bin" / "creative-forge"
    app = desktop.mac_bundle(tmp_path / "Applications", program, desktop.asset("creative-forge.icns"))
    assert app.name == "Creative Forge.app"
    with open(app / "Contents" / "Info.plist", "rb") as fh:
        info = plistlib.load(fh)
    assert info["CFBundleExecutable"] == "creative-forge" and info["CFBundlePackageType"] == "APPL"
    script = (app / "Contents" / "MacOS" / "creative-forge").read_text(encoding="utf-8")
    assert f'exec "{program}" "$@"' in script
    assert (app / "Contents" / "Resources" / "creative-forge.icns").stat().st_size > 10_000


def test_linux_desktop_entry(tmp_path):
    text = desktop.linux_entry(tmp_path / "a", Path("/opt/bin/creative-forge"), Path("/i.png")).read_text(
        encoding="utf-8"
    )
    assert "Name=Creative Forge" in text and 'Exec="/opt/bin/creative-forge"' in text and "Terminal=false" in text


def test_assets_ship_in_the_package():
    for name in ("creative-forge.ico", "creative-forge.icns", "creative-forge.png"):
        assert desktop.asset(name).stat().st_size > 1000


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcut (WScript.Shell)")
def test_windows_shortcut_points_at_the_launcher(tmp_path):
    lnk = desktop.windows_shortcut(
        tmp_path / "Creative Forge.lnk", Path(sys.executable), desktop.asset("creative-forge.ico")
    )
    target = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(New-Object -ComObject WScript.Shell).CreateShortcut('{lnk}').TargetPath"],
        capture_output=True, encoding="utf-8", errors="replace", check=True,
    ).stdout.strip()  # fmt: skip
    assert Path(target) == Path(sys.executable)
