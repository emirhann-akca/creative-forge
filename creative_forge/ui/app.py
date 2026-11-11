"""``forge ui``: a local web page over the same functions the CLI uses.

* Runs list: every directory under ``--root`` that holds a ``report.json``.
* A run: its QA report, the exports in a player (HTTP range requests, so seeking works), and approve / reject with a
  note. Decisions go through ``review.decide``, the function behind ``forge review``: the decision is tied to the
  file's SHA-256 and ``forge publish`` gates on it exactly as before.
* ``forge demo`` in the background, with its log.
* ``forge doctor``.
* Creative performance from ``forge perf`` (per creative and per tag), read from the local store.

Binds 127.0.0.1 unless ``--host`` says otherwise. Requests whose ``Host`` header is not a loopback name are refused
(DNS rebinding), and every state-changing request must carry the per-process token that the pages embed, so another
web page in the same browser cannot post decisions.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from creative_forge import __version__
from creative_forge.review import ExportRecord, ReviewError, decide, load_exports, load_review

HERE = Path(__file__).resolve().parent
TOKEN_HEADER = "X-Forge-Token"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
SKIP_DIRS = {"inputs", "__pycache__", "node_modules"}


@dataclass
class RunInfo:
    rid: str  # path relative to the root, with "/" separators
    path: Path
    status: str
    exports: list[ExportRecord]
    decisions: dict[str, Any]
    mtime: float

    @property
    def approved(self) -> int:
        return sum(1 for d in self.decisions.values() if d.get("decision") == "approve")


def _run_status(exports: list[ExportRecord]) -> str:
    order = {"fail": 3, "warn": 2, "skip": 1, "pass": 0}
    return max((e.status for e in exports), key=lambda s: order.get(s, 0), default="skip")


def load_run(root: Path, rid: str) -> RunInfo:
    if not rid or rid.startswith("/") or any(part in ("", ".", "..") for part in rid.split("/")):
        raise HTTPException(404, "no such run")
    path = (root / rid).resolve()
    if not path.is_relative_to(root.resolve()) or not (path / "report.json").is_file():
        raise HTTPException(404, "no such run")
    try:
        exports = load_exports(path)
    except (ReviewError, KeyError, ValueError) as e:
        raise HTTPException(422, f"unreadable report: {e}") from e
    return RunInfo(
        rid,
        path,
        _run_status(exports),
        exports,
        load_review(path)["decisions"],
        (path / "report.json").stat().st_mtime,
    )


def find_runs(root: Path, max_depth: int = 3) -> list[RunInfo]:
    root = root.resolve()
    found: list[RunInfo] = []

    def walk(d: Path, depth: int) -> None:
        if (d / "report.json").is_file() and d != root:
            rid = d.relative_to(root).as_posix()
            with contextlib.suppress(HTTPException):  # an unreadable report is left out of the list
                found.append(load_run(root, rid))
            return
        if depth >= max_depth:
            return
        try:
            children = sorted(p for p in d.iterdir() if p.is_dir())
        except OSError:
            return
        for c in children:
            if not c.name.startswith(".") and c.name not in SKIP_DIRS:
                walk(c, depth + 1)

    if root.is_dir():
        walk(root, 0)
    return sorted(found, key=lambda r: r.mtime, reverse=True)


class DemoJob:
    """One background ``forge demo`` at a time; its combined output is kept for the page."""

    def __init__(self, cmd: list[str], out_dir: Path) -> None:
        self.cmd = cmd
        self.out_dir = out_dir
        self.lines: deque[str] = deque(maxlen=400)
        self.proc: subprocess.Popen | None = None
        self.returncode: int | None = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        if self.proc is None:
            return "idle"
        if self.returncode is None:
            return "running"
        return "done" if self.returncode == 0 else "failed"

    def start(self) -> bool:
        with self._lock:
            if self.state == "running":
                return False
            self.lines.clear()
            self.returncode = None
            env = os.environ | {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            self.proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env
            )
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
            return True

    def _pump(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            self.lines.append(raw.decode("utf-8", errors="replace").rstrip())
        self.returncode = proc.wait()

    def wait(self, timeout: float | None = None) -> int | None:
        if self.proc is not None:
            self.proc.wait(timeout)
            for _ in range(200):  # the pump thread records the code right after the pipe closes
                if self.returncode is not None:
                    break
                time.sleep(0.01)
        return self.returncode


def create_app(
    root: Path,
    *,
    token: str | None = None,
    allowed_hosts: frozenset[str] | None = LOOPBACK,
    demo_cmd: list[str] | None = None,
    demo_out: Path | None = None,
    perf_db: Path | None = None,
) -> FastAPI:
    root = Path(root).resolve()
    if (root / "report.json").is_file():  # `--root out/demo` names a run itself: list it from its parent
        root = root.parent
    token = token or secrets.token_urlsafe(24)
    demo_out = (demo_out or root / "demo").resolve()
    demo = DemoJob(demo_cmd or [sys.executable, "-m", "creative_forge", "demo", "--out", str(demo_out)], demo_out)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app = FastAPI(title="creative-forge", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.state.token = token
    app.state.demo = demo
    app.state.root = root

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        if allowed_hosts is not None:
            host = urlsplit("//" + request.headers.get("host", "")).hostname
            if host not in allowed_hosts:
                return PlainTextResponse(f"host {host!r} is not allowed; start with --host to expose", 400)
        return await call_next(request)

    async def require_token(request: Request) -> dict[str, str]:
        form = {k: str(v) for k, v in (await request.form()).items()}
        sent = request.headers.get(TOKEN_HEADER) or form.get("token", "")
        if not secrets.compare_digest(sent, token):
            raise HTTPException(403, "missing or wrong token; reload the page")
        return form

    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        ctx |= {"token": token, "version": __version__, "root": root, "demo": demo}
        return templates.TemplateResponse(request, name, ctx, status_code=status_code)

    def demo_rid() -> str | None:
        try:
            return demo.out_dir.relative_to(root).as_posix()
        except ValueError:
            return None

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        return page(request, "index.html", runs=find_runs(root), demo_rid=demo_rid())

    @app.get("/runs/{rid:path}/report.html")
    def report(rid: str) -> FileResponse:
        run = load_run(root, rid)
        html = run.path / "report.html"
        if not html.is_file():
            raise HTTPException(404, "this run has no report.html")
        return FileResponse(html, media_type="text/html; charset=utf-8")

    @app.get("/runs/{rid:path}/media/{name}")
    def media(rid: str, name: str) -> FileResponse:
        run = load_run(root, rid)
        if name not in {e.file for e in run.exports}:
            raise HTTPException(404, "not an export of this run")
        return FileResponse(run.path / name, media_type="video/mp4")  # Starlette answers Range with 206

    @app.post("/runs/{rid:path}/review", response_class=HTMLResponse)
    async def review(request: Request, rid: str) -> HTMLResponse:
        form = await require_token(request)
        run = load_run(root, rid)
        file = form.get("file", "")
        try:
            decide(run.path, file, form.get("decision", ""), form.get("note", ""), form.get("reviewer") or None)
        except ReviewError as e:
            export = next((x for x in run.exports if x.file == file), None)
            if export is None:
                raise HTTPException(422, str(e)) from e
            return page(request, "_export.html", 422, run=run, e=export, d=run.decisions.get(file, {}), error=str(e))
        run = load_run(root, rid)
        export = next(x for x in run.exports if x.file == file)
        return page(request, "_export.html", run=run, e=export, d=run.decisions.get(file, {}), error=None)

    @app.get("/runs/{rid:path}", response_class=HTMLResponse)
    def run_page(request: Request, rid: str) -> HTMLResponse:
        run = load_run(root, rid)
        return page(request, "run.html", run=run, has_report=(run.path / "report.html").is_file())

    @app.post("/demo", response_class=HTMLResponse)
    async def start_demo(request: Request) -> HTMLResponse:
        await require_token(request)
        demo.start()
        return page(request, "_demo.html", demo_rid=demo_rid())

    @app.get("/demo/status", response_class=HTMLResponse)
    def demo_status(request: Request) -> HTMLResponse:
        return page(request, "_demo.html", demo_rid=demo_rid())

    @app.get("/perf", response_class=HTMLResponse)
    def perf_page(request: Request, metric: str = "ipm", min_impressions: float = 10000) -> HTMLResponse:
        from markupsafe import Markup

        from creative_forge.perf.report import METRICS, build_report, render_tables
        from creative_forge.perf.store import PerfStore, default_db

        db = Path(perf_db or default_db())
        rep, tables = None, Markup("")
        if db.is_file():  # read only: the page never creates a store
            try:
                rep = build_report(PerfStore(db), metric=metric, min_impressions=max(0.0, min_impressions))
            except ValueError as e:
                raise HTTPException(422, str(e)) from e
            tables = Markup(render_tables(rep))
        return page(
            request,
            "perf.html",
            rep=rep,
            tables=tables,
            db=db,
            metric=metric,
            metrics=list(METRICS),
            min_impressions=min_impressions,
        )

    @app.get("/doctor", response_class=HTMLResponse)
    def doctor_page(request: Request, partial: bool = False) -> HTMLResponse:
        from creative_forge.doctor import failed, run_doctor

        items = run_doctor()
        name = "_doctor_table.html" if partial else "doctor.html"  # the runs page loads the table on its own
        return page(request, name, items=items, blocked=failed(items))

    @app.get("/doctor.json")
    def doctor_json() -> Response:
        from creative_forge.doctor import run_doctor, to_json

        return Response(to_json(run_doctor()), media_type="application/json")

    return app


def _bind(host: str, port: int, explicit_port: bool) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if os.name != "nt":  # on Windows SO_REUSEADDR lets another process take the port
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError:
        if explicit_port:
            sock.close()
            raise
        sock.bind((host, 0))  # the default port is taken: any free one will do
    sock.listen(64)
    return sock


def serve(root: Path, *, host: str = "127.0.0.1", port: int | None = None, open_browser: bool = True) -> int:
    import uvicorn

    local = host in LOOPBACK
    app = create_app(root, allowed_hosts=LOOPBACK if local else None)
    sock = _bind(host, 8765 if port is None else port, port is not None)
    bound = sock.getsockname()[1]
    shown = f"[{host}]" if ":" in host else host
    url = f"http://{shown}:{bound}/"
    print(f"creative-forge UI: {url}  (runs under {Path(root).resolve()}; Ctrl+C to stop)", flush=True)
    if not local:
        print("warning: reachable from other machines; anyone who can open the page can approve exports", flush=True)
    if open_browser:
        threading.Timer(0.6, webbrowser.open, [url.replace("0.0.0.0", "127.0.0.1")]).start()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    server.run(sockets=[sock])
    return 0
