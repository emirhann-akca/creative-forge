"""Creative Forge as a desktop app: a double-click opens `forge ui` in its own window.

`creative-forge` (a GUI script: no console window on Windows) and `forge app`:

1. Runs folder: `--root`, else the one remembered in <data dir>/app/app.json, else
   <data dir>/runs. The page's own "Run the demo" button fills an empty folder.
2. One instance: when app.json names a server that still answers, the window opens on
   it instead of starting a second one.
3. The server binds 127.0.0.1 on a free port inside this process (the same app as
   `forge ui`, with its per-process token and loopback Host check).
4. The window: Edge, Chrome, Brave or Chromium in app mode (`--app=URL`, a profile of its
   own), so it has no tabs or address bar; the default browser otherwise. Every full page
   loads /__app/heartbeat.js: a ping every PING_EVERY_S and a beacon when the page goes
   away. The server stops BYE_GRACE_S after the last page left, when the dedicated window
   process ends, or GONE_AFTER_S without a ping, and ends the window process it started.

`forge app --install-shortcut` adds the launcher: Desktop and Start menu shortcuts on
Windows, ~/Applications/Creative Forge.app on macOS, an applications-menu entry on Linux.
The installers call it; FORGE_NO_SHORTCUT=1 skips it.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import plistlib
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

APP_NAME = "Creative Forge"
GUI_SCRIPT = "creative-forge"
BUNDLE_ID = "io.github.creative-forge.app"
PING_EVERY_S = 15
GONE_AFTER_S = 120  # background tabs are throttled to about one timer a minute
BYE_GRACE_S = 20  # longer than PING_EVERY_S: another window or the next page pings first

HEARTBEAT_JS = (
    "(function(){var down=false;function ping(){fetch('/__app/ping',{cache:'no-store'})"
    ".then(function(){if(down){location.reload();}})"
    ".catch(function(){if(down)return;down=true;var b=document.createElement('div');"
    "b.setAttribute('role','alert');b.style.cssText='position:fixed;left:0;right:0;bottom:0;"
    "padding:12px 16px;background:#7f1d1d;color:#fff;font:14px system-ui;z-index:99999';"
    "b.textContent='Creative Forge kapandı / closed: simgeden yeniden açın / reopen it from "
    "its icon.';document.body.appendChild(b);});}ping();setInterval(ping,"
    + str(PING_EVERY_S * 1000)
    + ");addEventListener('pagehide',function(){navigator.sendBeacon('/__app/bye');});})();"
).encode()
HEARTBEAT_TAG = b'<script src="/__app/heartbeat.js" defer></script>'


# -- where things live ------------------------------------------------------------------
def state_dir() -> Path:
    from creative_forge.paths import data_dir

    return data_dir() / "app"


def default_root() -> Path:
    from creative_forge.paths import data_dir

    return data_dir() / "runs"


def _read_state(state: Path) -> dict[str, Any]:
    try:
        return json.loads((state / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_state(state: Path, data: dict[str, Any]) -> None:
    state.mkdir(parents=True, exist_ok=True)
    tmp = state / "app.json.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, state / "app.json")


def pick_root(explicit: str | None, state: Path) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    remembered = _read_state(state).get("root")
    if remembered and Path(remembered).is_dir():
        return Path(remembered)
    return default_root()


# -- the window -------------------------------------------------------------------------
def _windows_candidates(env: Mapping[str, str]) -> list[str]:
    roots = [env.get("ProgramFiles(x86)"), env.get("ProgramFiles"), env.get("LOCALAPPDATA")]
    rel = [
        r"Microsoft\Edge\Application\msedge.exe",
        r"Google\Chrome\Application\chrome.exe",
        r"BraveSoftware\Brave-Browser\Application\brave.exe",
        r"Chromium\Application\chrome.exe",
    ]
    return [str(Path(r) / p) for p in rel for r in roots if r]


def _mac_candidates(home: Path) -> list[str]:
    names = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium")
    return [
        str(root / f"{n}.app" / "Contents" / "MacOS" / n)
        for n in names
        for root in (Path("/Applications"), home / "Applications")
    ]


LINUX_BROWSERS = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "microsoft-edge",
    "brave-browser",
)


def find_browser(
    platform: str = sys.platform,
    env: Mapping[str, str] | None = None,
    exists: Callable[[str], bool] = os.path.exists,
    which: Callable[[str], str | None] = shutil.which,
    home: Path | None = None,
) -> str | None:
    """A Chromium-family browser that can open an app window, or None."""
    env = os.environ if env is None else env
    if env.get("CREATIVE_FORGE_APP_BROWSER"):
        return env["CREATIVE_FORGE_APP_BROWSER"]
    if platform == "win32":
        return next((c for c in _windows_candidates(env) if exists(c)), None)
    if platform == "darwin":
        return next((c for c in _mac_candidates(home or Path.home()) if exists(c)), None)
    return next((p for p in (which(n) for n in LINUX_BROWSERS) if p), None)


def window_command(browser: str, url: str, profile: Path) -> list[str]:
    return [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1320,880",
    ]


def open_window_at(url: str, state: Path) -> subprocess.Popen | None:
    browser = find_browser()
    if browser:
        profile = state / "window"
        profile.mkdir(parents=True, exist_ok=True)
        return subprocess.Popen(
            window_command(browser, url, profile), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    import webbrowser

    webbrowser.open(url)
    return None


# -- the server -------------------------------------------------------------------------
class Heartbeat:
    """Serves /__app/{ping,bye,health,heartbeat.js}; adds the script to every full page."""

    def __init__(self, app: Any, clock: Callable[[], float] = time.monotonic) -> None:
        self.app = app
        self.clock = clock
        self.last_ping = clock()
        self.pinged = False
        self.bye_at: float | None = None

    @staticmethod
    async def _plain(send: Any, body: bytes, ctype: bytes) -> None:
        headers = [(b"content-type", ctype), (b"cache-control", b"no-store")]
        headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/__app/ping":
            self.last_ping, self.pinged = self.clock(), True
            await self._plain(send, b"ok", b"text/plain")
            return
        if path == "/__app/bye":
            self.bye_at = self.clock()
            await self._plain(send, b"ok", b"text/plain")
            return
        if path == "/__app/health":
            await self._plain(send, b'{"ok":true,"app":"creative-forge"}', b"application/json")
            return
        if path == "/__app/heartbeat.js":
            await self._plain(send, HEARTBEAT_JS, b"text/javascript; charset=utf-8")
            return
        held: dict[str, Any] = {}
        chunks: list[bytes] = []

        async def wrapped(msg: dict) -> None:
            if msg["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in msg.get("headers", [])}
                if headers.get(b"content-type", b"").startswith(b"text/html"):
                    held["start"] = msg
                    return
                await send(msg)
                return
            if msg["type"] == "http.response.body" and "start" in held:
                chunks.append(msg.get("body", b""))
                if msg.get("more_body"):
                    return
                body = b"".join(chunks)
                i = body.lower().rfind(b"</body>")
                if i >= 0 and HEARTBEAT_TAG not in body:  # full pages only, not htmx fragments
                    body = body[:i] + HEARTBEAT_TAG + body[i:]
                start = held["start"]
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                headers.append((b"content-length", str(len(body)).encode()))
                await send({**start, "headers": headers})
                await send({"type": "http.response.body", "body": body})
                return
            await send(msg)

        await self.app(scope, receive, wrapped)


def _get(url: str, timeout: float = 2.0) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (OSError, ValueError):
        return 0, b""


def running_url(state: Path) -> str | None:
    url = _read_state(state).get("url")
    if not url:
        return None
    code, body = _get(url + "__app/health", timeout=1.5)
    return url if code == 200 and b"creative-forge" in body else None


class Running:
    def __init__(self, server: Any, thread: threading.Thread, url: str, beat: Heartbeat) -> None:
        self.server, self.thread, self.url, self.beat = server, thread, url, beat

    def stop(self, timeout: float = 10.0) -> None:
        self.server.should_exit = True
        self.thread.join(timeout)


def start_server(root: Path) -> Running:
    import uvicorn

    from creative_forge.ui.app import _bind, create_app

    root.mkdir(parents=True, exist_ok=True)
    beat = Heartbeat(create_app(root))
    sock = _bind("127.0.0.1", 0, True)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    server = uvicorn.Server(uvicorn.Config(beat, log_level="warning", lifespan="off", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("the local server did not start")
    return Running(server, thread, url, beat)


# -- one launch -------------------------------------------------------------------------
def run(
    root: str | None = None,
    *,
    state: Path | None = None,
    open_window: bool = True,
    launch: Callable[[str], subprocess.Popen | None] | None = None,
    gone_after_s: float = GONE_AFTER_S,
    bye_grace_s: float = BYE_GRACE_S,
    tick_s: float = 1.0,
    log: Callable[[str], Any] = print,
) -> int:
    """Open the app; return when its window is gone (with open_window=False: until Ctrl+C)."""
    state = state or state_dir()
    state.mkdir(parents=True, exist_ok=True)
    launch = launch or (lambda url: open_window_at(url, state))
    if root is None:
        url = running_url(state)
        if url:
            log(f"{APP_NAME} is already running at {url}; opening a window on it")
            if open_window:
                launch(url)
            return 0
    runs = pick_root(root, state)
    running = start_server(runs)
    log(f"{APP_NAME}: {running.url}  (runs under {runs})")
    data = _read_state(state)
    data.update(url=running.url, pid=os.getpid(), started=dt.datetime.now().isoformat(timespec="seconds"))
    _write_state(state, data)
    proc = launch(running.url) if open_window else None
    try:
        if not open_window:
            log(f"{APP_NAME}: ready at {running.url} (Ctrl+C to stop)")
            with contextlib.suppress(KeyboardInterrupt):
                while True:
                    time.sleep(1.0)
            return 0
        beat = running.beat
        while True:
            time.sleep(tick_s)
            now = time.monotonic()
            window_gone = proc is not None and proc.poll() is not None and beat.pinged
            if window_gone and now - beat.last_ping > PING_EVERY_S * 2:  # the window was closed
                break
            left = beat.bye_at is not None and beat.last_ping < beat.bye_at
            if left and now - beat.bye_at > bye_grace_s:  # the last page left and none came back
                break
            if now - beat.last_ping > gone_after_s:
                break
        return 0
    finally:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
        running.stop()
        data = _read_state(state)
        if data.get("url") == running.url:
            data.pop("url", None)
            data.pop("pid", None)
            _write_state(state, data)
        log(f"{APP_NAME}: stopped")


def check(root: str | None = None, *, state: Path | None = None) -> int:
    """Start on a free port, load the pages a window would, stop."""
    state = state or state_dir()
    runs = pick_root(root, state)
    running = start_server(runs)
    failures = 0
    try:
        for page, needle in (("", b"creative-forge"), ("__app/health", b'"ok":true'),
                             ("__app/heartbeat.js", b"__app/ping")):  # fmt: skip
            code, body = _get(running.url + page, timeout=30)
            ok = code == 200 and needle in body
            failures += not ok
            print(f"{'ok  ' if ok else 'FAIL'} /{page} ({code})")
        code, body = _get(running.url, timeout=30)
        ok = HEARTBEAT_TAG in body
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} heartbeat script on the page")
        _get(running.url + "__app/ping")
        ok = running.beat.pinged
        failures += not ok
        print(f"{'ok  ' if ok else 'FAIL'} /__app/ping")
    finally:
        running.stop()
    print(f"{APP_NAME} check: {'passed' if not failures else f'{failures} failure(s)'} (runs under {runs})")
    return 1 if failures else 0


# -- shortcuts --------------------------------------------------------------------------
def launcher_program() -> Path:
    """The installed GUI script next to this interpreter (the tool's own venv)."""
    exe_dir = Path(sys.executable).parent
    name = f"{GUI_SCRIPT}.exe" if sys.platform == "win32" else GUI_SCRIPT
    for c in (exe_dir / name, exe_dir / "Scripts" / name):
        if c.exists():
            return c
    found = shutil.which(GUI_SCRIPT)
    if found:
        return Path(found)
    raise FileNotFoundError(f"{name} not found next to {sys.executable}; reinstall creative-forge")


def asset(name: str) -> Path:
    return Path(str(resources.files("creative_forge") / "data" / "desktop" / name))


def mac_bundle(apps_dir: Path, program: Path, icns: Path) -> Path:
    from creative_forge import __version__

    app = apps_dir / f"{APP_NAME}.app"
    macos, res = app / "Contents" / "MacOS", app / "Contents" / "Resources"
    macos.mkdir(parents=True, exist_ok=True)
    res.mkdir(parents=True, exist_ok=True)
    info = {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleExecutable": GUI_SCRIPT,
        "CFBundleIconFile": "creative-forge.icns",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": __version__,
        "LSMinimumSystemVersion": "11.0",
        "NSHighResolutionCapable": True,
    }
    with open(app / "Contents" / "Info.plist", "wb") as fh:
        plistlib.dump(info, fh)
    script = macos / GUI_SCRIPT
    quoted = str(program).replace('"', '\\"')
    script.write_text(f'#!/bin/sh\nexec "{quoted}" "$@"\n', encoding="utf-8")
    script.chmod(0o755)
    shutil.copyfile(icns, res / "creative-forge.icns")
    return app


def linux_entry(apps_dir: Path, program: Path, png: Path) -> Path:
    apps_dir.mkdir(parents=True, exist_ok=True)
    path = apps_dir / "creative-forge.desktop"
    path.write_text(
        "[Desktop Entry]\nType=Application\n"
        f"Name={APP_NAME}\nComment=Video ad pipeline: runs, QA reports, review\n"
        f'Exec="{program.as_posix()}"\nIcon={png.as_posix()}\nTerminal=false\nCategories=AudioVideo;Graphics;\n',
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


WIN_SHORTCUT_PS = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:CF_LNK); "
    "$s.TargetPath = $env:CF_LNK_TARGET; $s.IconLocation = $env:CF_LNK_ICON; "
    "$s.WorkingDirectory = $env:CF_LNK_DIR; $s.Description = $env:CF_LNK_DESC; $s.Save()"
)


def windows_shortcut(lnk: Path, program: Path, ico: Path) -> Path:
    lnk.parent.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CF_LNK": str(lnk),
        "CF_LNK_TARGET": str(program),
        "CF_LNK_ICON": f"{ico},0",
        "CF_LNK_DIR": str(Path.home()),
        "CF_LNK_DESC": "Creative Forge: video ad runs, QA reports, review",
    }
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_SHORTCUT_PS],
        check=True,
        env=env,
        capture_output=True,
    )
    return lnk


def _windows_dirs() -> list[Path]:
    out = []
    for folder in ("Desktop", "Programs"):
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"[Console]::OutputEncoding = [Text.Encoding]::UTF8; [Environment]::GetFolderPath('{folder}')",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if res.stdout.strip():
            out.append(Path(res.stdout.strip()))
    return out


def install_shortcuts(
    *, platform: str = sys.platform, home: Path | None = None, program: Path | None = None
) -> list[Path]:
    home = home or Path.home()
    program = program or launcher_program()
    if platform == "darwin":
        return [mac_bundle(home / "Applications", program, asset("creative-forge.icns"))]
    if platform == "win32":
        icons = state_dir() / "icons"
        icons.mkdir(parents=True, exist_ok=True)
        ico = icons / "creative-forge.ico"
        shutil.copyfile(asset("creative-forge.ico"), ico)  # survives reinstalls of the package
        return [windows_shortcut(d / f"{APP_NAME}.lnk", program, ico) for d in _windows_dirs()]
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    icons = data / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    png = icons / "creative-forge.png"
    shutil.copyfile(asset("creative-forge.png"), png)
    return [linux_entry(data / "applications", program, png)]


def uninstall_shortcuts(*, platform: str = sys.platform, home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    if platform == "darwin":
        targets = [home / "Applications" / f"{APP_NAME}.app"]
    elif platform == "win32":
        targets = [d / f"{APP_NAME}.lnk" for d in _windows_dirs()]
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
        targets = [data / "applications" / "creative-forge.desktop"]
    removed = []
    for t in targets:
        if t.is_dir():
            shutil.rmtree(t)
            removed.append(t)
        elif t.exists():
            t.unlink()
            removed.append(t)
    return removed


# -- entry points -----------------------------------------------------------------------
def add_parser(sub: argparse._SubParsersAction) -> None:
    a = sub.add_parser("app", help="open the review UI in its own window (what the desktop icon runs)")
    a.add_argument("--root", help="runs folder to open (remembered for the icon; default: the data dir's runs)")
    a.add_argument("--no-window", action="store_true", help="serve only; print the URL")
    a.add_argument("--check", action="store_true", help="start, load the pages, stop")
    a.add_argument(
        "--install-shortcut", action="store_true", help="add the desktop / Start menu / Applications launcher"
    )
    a.add_argument("--uninstall-shortcut", action="store_true", help="remove that launcher")
    a.set_defaults(fn=cmd_app)


def cmd_app(a: argparse.Namespace) -> int:
    if a.install_shortcut:
        for p in install_shortcuts():
            print(f"launcher: {p}")
        return 0
    if a.uninstall_shortcut:
        for p in uninstall_shortcuts():
            print(f"removed: {p}")
        return 0
    if a.check:
        return check(a.root)
    if a.root:
        state = state_dir()
        data = _read_state(state)
        data["root"] = str(Path(a.root).expanduser().resolve())
        _write_state(state, data)
    return run(a.root, open_window=not a.no_window)


def _rotate(path: Path, limit: int = 5 * 1024 * 1024, keep: int = 3) -> None:
    try:
        if path.stat().st_size < limit:
            return
    except FileNotFoundError:
        return
    for i in range(keep - 1, 0, -1):
        older = path.with_name(f"{path.name}.{i}")
        if older.exists():
            os.replace(older, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def gui_main(argv: list[str] | None = None) -> int:
    """The `creative-forge` GUI script: output goes to a log file (there is no console)."""
    args = list(sys.argv[1:] if argv is None else argv)
    log_file = state_dir() / "logs" / "app.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    _rotate(log_file)
    with open(log_file, "a", encoding="utf-8", errors="backslashreplace", buffering=1) as log:
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = log
        try:
            stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            print(f"=== {stamp} {GUI_SCRIPT} {' '.join(args)} (pid {os.getpid()})")
            from creative_forge.cli import main as cli_main

            rc = cli_main(["app", *args])
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except BaseException:
            traceback.print_exc()
            rc = 1
        finally:
            sys.stdout, sys.stderr = old_out, old_err
    return int(rc or 0)
