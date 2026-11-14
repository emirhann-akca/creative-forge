"""``forge ui``: Creative Forge, the ads app. A local web page over the same functions the CLI uses.

Pages (Turkish by default, English with ``?lang=en``; the choice is remembered in a cookie):

* **Overview**: what needs a decision today (pending approvals, QA failures, tests that reached sufficiency), counts,
  recent runs, and the next setup step when something is missing.
* **Creatives**: every export of every run under ``--root`` as a playable card with its QA and review status, its tags
  and, when the performance store knows its creative ID, IPM / CTR / CPI / spend and a confidence badge. The detail
  view shows the checks, the lineage (spec, concept, variant, format) and approve / reject. Decisions go through
  ``review.decide``, the function behind ``forge review``: the decision is tied to the file's SHA-256 and
  ``forge publish`` gates on it exactly as before.
* **Concepts**: each concept with its hypothesis and tags, and its variants underneath.
* **Tests**: test cells (``perf.cells``), 2-4 variants against a control with a rule locked when the test starts and
  install-based sufficiency.
* **Performance**: ``forge perf report`` per creative and per tag, and the import status per network.
* **Playables**: results of ``forge playable check --json`` saved under ``<root>/playables`` and the rules per network.
* **Campaigns** (Kampanya): ``forge campaign`` behind one primary button. The page shows the dry-run preview of a
  campaign YAML under ``<root>/campaigns/``, "Kampanyayı hazırla (duraklatılmış)" creates everything PAUSED, each
  campaign has its own "Yayına al" form that needs the typed phrase with the budget in it, and the status table is
  synced from the networks. Mock mode is the default; a live network needs its own tick box.
* **Settings**: network credentials present or missing (never their values), CSV import, folders, ``forge doctor``.

Binds 127.0.0.1 unless ``--host`` says otherwise. Requests whose ``Host`` header is not a loopback name are refused
(DNS rebinding), and every state-changing request must carry the per-process token that the pages embed, so another
web page in the same browser cannot post decisions.
"""

from __future__ import annotations

import contextlib
import functools
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from creative_forge import __version__
from creative_forge.review import ExportRecord, ReviewError, decide, load_exports, load_review
from creative_forge.ui import catalog
from creative_forge.ui.i18n import LANGS, T, fmt_date, fmt_num, fmt_pct, fmt_prob, review_error

HERE = Path(__file__).resolve().parent
TOKEN_HEADER = "X-Forge-Token"
LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
SKIP_DIRS = {"inputs", "__pycache__", "node_modules", "playables", "campaigns"}
LANG_COOKIE = "forge_lang"
MAX_UPLOAD = 50 * 1024 * 1024
# The networks of the settings page: how data gets in and which environment variable (if any) holds a credential.
CONNECTIONS = (
    ("meta", "Meta", "api", "CREATIVE_FORGE_META_TOKEN"),
    ("applovin", "AppLovin", "api", "CREATIVE_FORGE_APPLOVIN_KEY"),
    ("tiktok", "TikTok", "csv", None),
    ("unity", "Unity", "csv", None),
    ("google", "Google Ads", "csv", None),
    ("mintegral", "Mintegral", "csv", None),
)


# The networks of a config written by the Kampanya page: mock mode with synthetic ids, to be replaced by real ones.
STARTER_NETWORKS: dict[str, Any] = {
    "meta": {"mode": "mock", "ad_account": "act_100200300", "page_id": "100000000000001",
             "app_id": "200000000000002", "message": ""},
    "tiktok": {"mode": "mock", "advertiser_id": "7000000000000000001", "app_id": "7100000000000000001",
               "location_ids": ["298795"], "identity_id": "identity-1", "ad_text": "Play now"},
    "google": {"mode": "mock", "customer_id": "1234567890", "headlines": ["Play now"],
               "descriptions": ["A short, calm puzzle."], "location_ids": ["2792"], "language_ids": ["1037"]},
    "applovin": {"mode": "manual"},
}  # fmt: skip


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


def _int(v: Any, default: int) -> int:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def _float(v: Any, default: float) -> float:
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return default


def qlink(base: str, current: dict[str, str], **changes: str) -> str:
    """``base`` with the current filters, some of them changed; empty values are left out."""
    merged = {k: v for k, v in ({**current, **changes}).items() if v}
    return base + ("?" + urlencode(sorted(merged.items())) if merged else "")


def create_app(
    root: Path,
    *,
    token: str | None = None,
    allowed_hosts: frozenset[str] | None = LOOPBACK,
    demo_cmd: list[str] | None = None,
    demo_out: Path | None = None,
    perf_db: Path | None = None,
    poster_cache: Path | None = None,
    campaign_state: Path | None = None,
    campaign_demo_cmd: list[str] | None = None,
) -> FastAPI:
    from creative_forge.perf.cells import CELL_METRICS, CellError, CellStore, Rule, evaluate
    from creative_forge.perf.store import default_db

    root = Path(root).resolve()
    if (root / "report.json").is_file():  # `--root out/demo` names a run itself: list it from its parent
        root = root.parent
    token = token or secrets.token_urlsafe(24)
    demo_out = (demo_out or root / "demo").resolve()
    demo = DemoJob(demo_cmd or [sys.executable, "-m", "creative_forge", "demo", "--out", str(demo_out)], demo_out)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.filters["num"] = fmt_num
    templates.env.filters["when"] = fmt_date
    templates.env.filters["pct"] = fmt_pct
    templates.env.filters["prob"] = fmt_prob
    templates.env.filters["adname"] = catalog.ad_parts
    templates.env.filters["q"] = lambda s: quote(str(s), safe="")
    templates.env.filters["qp"] = lambda s: quote(str(s), safe="/")
    templates.env.globals["qlink"] = qlink
    app = FastAPI(title="creative-forge", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.state.token = token
    app.state.demo = demo
    app.state.root = root
    camp_demo_out = (root / "campaign-demo").resolve()
    camp_demo = DemoJob(
        campaign_demo_cmd or [sys.executable, "-m", "creative_forge", "campaign", "demo", "--out", str(camp_demo_out)],
        camp_demo_out,
    )
    app.state.campaign_demo = camp_demo

    def db_path() -> Path:
        return Path(perf_db or default_db())

    def cells() -> CellStore:
        return CellStore(db_path())

    def posters_dir() -> Path:
        if poster_cache is not None:
            return Path(poster_cache)
        from creative_forge.paths import cache_dir

        return cache_dir() / "ui-posters"

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):  # type: ignore[no-untyped-def]
        if allowed_hosts is not None:
            host = urlsplit("//" + request.headers.get("host", "")).hostname
            if host not in allowed_hosts:
                return PlainTextResponse(f"host {host!r} is not allowed; start with --host to expose", 400)
        return await call_next(request)

    async def require_token(request: Request) -> dict[str, Any]:
        form = await request.form()
        values: dict[str, Any] = {"variants": []}
        for k, v in form.multi_items():
            if not isinstance(v, str):
                values[k] = v  # an upload
            elif k == "variants":
                values[k].append(v)
            else:
                values.setdefault(k, v)
        sent = request.headers.get(TOKEN_HEADER) or str(values.get("token", ""))
        if not secrets.compare_digest(sent, token):
            raise HTTPException(403, "missing or wrong token; reload the page")
        return values

    def lang_of(request: Request) -> str:
        q = request.query_params.get("lang")
        if q in LANGS:
            return q
        c = request.cookies.get(LANG_COOKIE)
        return c if c in LANGS else "tr"

    # ------------------------------------------------------------------------------------------------- page helper
    def load_perf() -> tuple[dict[str, Any] | None, dict[str, list[dict[str, Any]]]]:
        try:
            return catalog.perf_index(db_path())
        except sqlite3.DatabaseError:
            return None, {}

    def list_cells() -> list[Any]:
        try:
            return cells().list()
        except sqlite3.DatabaseError:
            return []

    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        lang = lang_of(request)
        t = T[lang]

        @functools.cache
        def shell() -> dict[str, Any]:
            """Sidebar counts and the command palette; only full pages call it."""
            runs = find_runs(root)
            tests = list_cells()
            other = "en" if lang == "tr" else "tr"
            cmds = [
                {"label": t.nav_overview, "href": "/", "hint": t.cmd_page},
                {"label": t.nav_creatives, "href": "/creatives", "hint": t.cmd_page},
                {"label": t.nav_concepts, "href": "/concepts", "hint": t.cmd_page},
                {"label": t.nav_tests, "href": "/tests", "hint": t.cmd_page},
                {"label": t.nav_perf, "href": "/perf", "hint": t.cmd_page},
                {"label": t.nav_playables, "href": "/playables", "hint": t.cmd_page},
                {"label": t.nav_campaigns, "href": "/campaigns", "hint": t.cmd_page},
                {"label": t.nav_settings, "href": "/settings", "hint": t.cmd_page},
                {"label": t.cmd_pending, "href": "/creatives?review=pending", "hint": t.cmd_filter},
                {"label": t.cmd_failed, "href": "/creatives?qa=fail", "hint": t.cmd_filter},
                {"label": t.cmd_new_test, "href": "/tests/new", "hint": t.cmd_action},
                {"label": t.cmd_import_csv, "href": "/settings#import", "hint": t.cmd_action},
                {"label": t.cmd_doctor, "href": "/doctor", "hint": t.cmd_action},
                {"label": t.lang_switch_to, "href": f"{request.url.path}?lang={other}", "hint": t.cmd_action},
            ]
            cmds += [{"label": r.rid, "href": f"/runs/{quote(r.rid)}", "hint": t.cmd_run} for r in runs[:20]]
            cmds += [{"label": c.name, "href": f"/tests/{c.id}", "hint": t.cmd_test} for c in tests[:20]]
            return {
                "exports": sum(len(r.exports) for r in runs),
                "concepts": len({(r.rid, e.concept) for r in runs for e in r.exports}),
                "running": sum(1 for c in tests if c.status == "running"),
                "commands": cmds,
            }

        ua = request.headers.get("user-agent", "")
        ctx = {
            "token": token,
            "version": __version__,
            "root": root,
            "demo": demo,
            "lang": lang,
            "t": t,
            "shell": shell,
            "is_mac": "Mac" in ua or "iPhone" in ua or "iPad" in ua,
            "path": request.url.path,
            **ctx,
        }
        resp = templates.TemplateResponse(request, name, ctx, status_code=status_code)
        if request.query_params.get("lang") in LANGS:
            resp.set_cookie(LANG_COOKIE, lang, max_age=365 * 86400, samesite="lax", httponly=True)
        return resp

    def demo_rid() -> str | None:
        try:
            return demo.out_dir.relative_to(root).as_posix()
        except ValueError:
            return None

    # -------------------------------------------------------------------------------------------------- overview
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        runs = find_runs(root)
        rep, idx = load_perf()
        creatives = catalog.all_creatives(runs, idx)
        results = [evaluate(c, db_path()) for c in list_cells()]
        return page(
            request,
            "index.html",
            runs=runs,
            creatives=creatives,
            pending=[c for c in creatives if c.review in ("pending", "stale") and c.rec.status != "fail"],
            failed=[c for c in creatives if c.rec.status == "fail" and c.review != "reject"],
            stale=[c for c in creatives if c.review == "stale"],
            ready=[r for r in results if r.ready],
            tampered=[r for r in results if r.cell.tampered],
            results=results,
            running=[r for r in results if r.cell.status == "running"],
            matched=sum(1 for c in creatives if c.perf),
            has_perf=bool(rep and rep["networks"]),
            demo_rid=demo_rid(),
        )

    # ------------------------------------------------------------------------------------------------- creatives
    @app.get("/creatives", response_class=HTMLResponse)
    def creatives_page(
        request: Request,
        review: str = "",
        qa: str = "",
        hook: str = "",
        mechanic: str = "",
        fmt: str = "",
        run: str = "",
        concept: str = "",
    ) -> HTMLResponse:
        runs = find_runs(root)
        _, idx = load_perf()
        everything = catalog.all_creatives(runs, idx)
        shown = everything
        if review in catalog.REVIEW_STATES:
            shown = [c for c in shown if c.review == review or (review == "pending" and c.review == "stale")]
        if qa in ("pass", "warn", "fail", "skip"):
            shown = [c for c in shown if c.rec.status == qa]
        if hook:
            shown = [c for c in shown if c.hook_type == hook]
        if mechanic:
            shown = [c for c in shown if mechanic in c.mechanics]
        if fmt:
            shown = [c for c in shown if c.rec.format == fmt]
        if run:
            shown = [c for c in shown if c.rid == run]
        if concept:
            shown = [c for c in shown if c.rec.concept == concept]
        facets = {
            "hook": sorted({c.hook_type for c in everything if c.hook_type}),
            "mechanic": sorted({m for c in everything for m in c.mechanics}),
            "fmt": sorted({c.rec.format for c in everything}),
            "run": [r.rid for r in runs],
            "concept": sorted({c.rec.concept for c in everything}),
        }
        counts = Counter(c.review if c.review != "stale" else "pending" for c in everything)
        filters = {"review": review, "qa": qa, "hook": hook, "mechanic": mechanic, "fmt": fmt, "run": run,
                   "concept": concept}  # fmt: skip
        return page(
            request,
            "creatives.html",
            creatives=shown,
            total=len(everything),
            runs=runs,
            facets=facets,
            counts=counts,
            failed=sum(1 for c in everything if c.rec.status == "fail"),
            f=filters,
            filtered=any(filters.values()),
            demo_rid=demo_rid(),
        )

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

    def creative_or_404(rid: str, name: str) -> tuple[RunInfo, catalog.Creative]:
        run = load_run(root, rid)
        found = [c for c in catalog.creatives_of(run) if c.file == name]
        if not found:
            raise HTTPException(404, "not an export of this run")
        return run, found[0]

    @app.get("/runs/{rid:path}/poster/{name}")
    def poster(rid: str, name: str) -> Response:
        run, cr = creative_or_404(rid, name)
        jpg = catalog.poster(run.path / name, cr.poster_time, posters_dir())
        if jpg is None:
            raise HTTPException(404, "no frame could be read from this export")
        return FileResponse(jpg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/runs/{rid:path}/exports/{name}", response_class=HTMLResponse)
    def creative_page(request: Request, rid: str, name: str) -> HTMLResponse:
        run, cr = creative_or_404(rid, name)
        _, idx = load_perf()
        if cr.rec.creative_id:
            cr.perf = idx.get(cr.rec.creative_id, [])
        in_tests = [c for c in list_cells() if cr.rec.creative_id and cr.rec.creative_id in c.creatives]
        siblings = [c for c in catalog.creatives_of(run) if c.rec.concept == cr.rec.concept and c.file != cr.file]
        return page(
            request,
            "creative.html",
            run=run,
            c=cr,
            in_tests=in_tests,
            siblings=siblings,
            has_report=(run.path / "report.html").is_file(),
        )

    @app.post("/runs/{rid:path}/review", response_class=HTMLResponse)
    async def review(request: Request, rid: str) -> HTMLResponse:
        form = await require_token(request)
        run = load_run(root, rid)
        file = str(form.get("file", ""))
        view = "_review_panel.html" if form.get("view") == "detail" else "_export.html"
        try:
            decide(
                run.path,
                file,
                str(form.get("decision", "")),
                str(form.get("note", "")),
                str(form.get("reviewer") or "") or None,
            )
        except ReviewError as e:
            if file not in {x.file for x in run.exports}:
                raise HTTPException(422, str(e)) from e
            _, cr = creative_or_404(rid, file)
            return page(request, view, 422, run=run, c=cr, error=review_error(str(e), lang_of(request)))
        run, cr = creative_or_404(rid, file)
        return page(request, view, run=run, c=cr, error=None, saved=True)

    @app.get("/runs/{rid:path}", response_class=HTMLResponse)
    def run_page(request: Request, rid: str) -> HTMLResponse:
        run = load_run(root, rid)
        return page(
            request,
            "run.html",
            run=run,
            creatives=catalog.creatives_of(run),
            has_report=(run.path / "report.html").is_file(),
        )

    # -------------------------------------------------------------------------------------------------- concepts
    @app.get("/concepts", response_class=HTMLResponse)
    def concepts_page(request: Request) -> HTMLResponse:
        _, idx = load_perf()
        concepts = catalog.concepts_of(catalog.all_creatives(find_runs(root), idx))
        return page(request, "concepts.html", concepts=concepts, demo_rid=demo_rid())

    # ----------------------------------------------------------------------------------------------------- tests
    def creative_lookup() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        try:
            known = catalog.known_creatives(catalog.all_creatives(find_runs(root)), db_path())
        except sqlite3.DatabaseError:
            known = []
        return known, {k["creative_id"]: k for k in known}

    @app.get("/tests", response_class=HTMLResponse)
    def tests_page(request: Request) -> HTMLResponse:
        results = [evaluate(c, db_path()) for c in list_cells()]
        known, lookup = creative_lookup()
        return page(request, "tests.html", results=results, lookup=lookup, known=known)

    def new_test_page(request: Request, status_code: int = 200, error: str | None = None, **form: Any) -> HTMLResponse:
        known, _ = creative_lookup()
        defaults = Rule()
        values = {
            "name": "",
            "network": "meta",
            "metric": "ipm",
            "control": "",
            "variants": [],
            "scale_pct": defaults.scale_pct,
            "kill_pct": defaults.kill_pct,
            "screen_installs": defaults.screen_installs,
            "decide_installs": defaults.decide_installs,
            "confidence": round(defaults.confidence * 100),
            "text": "",
        } | form
        networks = [n for n, *_ in CONNECTIONS] + ["moloco", "liftoff", "other"]
        return page(
            request,
            "test_new.html",
            status_code,
            known=known,
            v=values,
            error=error,
            networks=networks,
            metrics=CELL_METRICS,
            today=date.today().isoformat(),
        )

    @app.get("/tests/new", response_class=HTMLResponse)
    def tests_new(request: Request, control: str = "", variant: str = "") -> HTMLResponse:
        return new_test_page(request, control=control, variants=[variant] if variant else [])

    @app.post("/tests")
    async def tests_create(request: Request) -> Response:
        form = await require_token(request)
        rule = Rule(
            scale_pct=_float(form.get("scale_pct"), -1),
            kill_pct=_float(form.get("kill_pct"), -1),
            screen_installs=_int(form.get("screen_installs"), -1),
            decide_installs=_int(form.get("decide_installs"), -1),
            confidence=_float(form.get("confidence"), -1) / 100,
            text=str(form.get("text", "")).strip(),
        )
        if not rule.text and not rule.problems():  # the locked rule always carries its sentence
            t = T[lang_of(request)]
            metric = str(form.get("metric", "ipm"))
            label = t.get(f"m_{metric}", metric)
            n = functools.partial(fmt_num, lang=lang_of(request), digits=0)
            rule = replace(
                rule,
                text=t.rule_auto
                % (
                    label,
                    n(rule.scale_pct),
                    n(rule.confidence * 100),
                    n(rule.kill_pct),
                    rule.screen_installs,
                    rule.decide_installs,
                ),
            )
        variants = [str(v) for v in form.get("variants", [])]
        try:
            cell = cells().create(
                str(form.get("name", "")),
                str(form.get("network", "")),
                str(form.get("control", "")),
                variants,
                rule,
                metric=str(form.get("metric", "ipm")),
            )
            if form.get("start") == "1":
                on = None
                with contextlib.suppress(ValueError):
                    on = date.fromisoformat(str(form.get("start_on", "")))
                cell = cells().start(cell.id, on)
        except CellError as e:
            keep = ("name", "network", "metric", "control", "text", "scale_pct", "kill_pct", "screen_installs",
                    "decide_installs", "confidence")  # fmt: skip
            values = {k: form.get(k, "") for k in keep} | {"variants": variants}
            return new_test_page(request, 422, error=str(e), **values)
        return RedirectResponse(f"/tests/{cell.id}", status_code=303)

    def test_view(request: Request, cell_id: int, status_code: int = 200, error: str | None = None) -> HTMLResponse:
        cell = cells().get(cell_id)
        if cell is None:
            raise HTTPException(404, "no such test")
        _, lookup = creative_lookup()
        return page(request, "test.html", status_code, r=evaluate(cell, db_path()), lookup=lookup, error=error,
                    today=date.today().isoformat())  # fmt: skip

    @app.get("/tests/{cell_id}", response_class=HTMLResponse)
    def test_page(request: Request, cell_id: int) -> HTMLResponse:
        return test_view(request, cell_id)

    @app.post("/tests/{cell_id}/{action}")
    async def test_action(request: Request, cell_id: int, action: str) -> Response:
        form = await require_token(request)
        store = cells()
        if action not in ("start", "close", "delete"):
            raise HTTPException(404, "no such action")
        try:
            if action == "start":
                on = None
                with contextlib.suppress(ValueError):
                    on = date.fromisoformat(str(form.get("on", "")))
                store.start(cell_id, on)
            elif action == "close":
                store.close(cell_id)
            else:
                store.delete(cell_id)
                return RedirectResponse("/tests", status_code=303)
        except CellError as e:
            if store.get(cell_id) is None:
                raise HTTPException(404, "no such test") from e
            return test_view(request, cell_id, 409, error=str(e))
        return RedirectResponse(f"/tests/{cell_id}", status_code=303)

    # ------------------------------------------------------------------------------------------------ performance
    @app.get("/perf", response_class=HTMLResponse)
    def perf_page(
        request: Request, metric: str = "ipm", min_impressions: float = 10000, network: str = ""
    ) -> HTMLResponse:
        from creative_forge.perf.report import METRICS, build_report
        from creative_forge.perf.store import PerfStore

        if metric not in METRICS:
            raise HTTPException(422, f"metric must be one of {', '.join(METRICS)}")
        db = db_path()
        rep = None
        if db.is_file():  # read only: the page never creates a store
            rep = build_report(
                PerfStore(db), network=network or None, metric=metric, min_impressions=max(0.0, min_impressions)
            )
        return page(
            request,
            "perf.html",
            rep=rep,
            db=db,
            metric=metric,
            metrics=list(METRICS),
            unit=METRICS[metric].unit,
            min_impressions=min_impressions,
            network=network,
            status=catalog.import_status(db),
            connections=CONNECTIONS,
            lookup=creative_lookup()[1] if rep else {},
        )

    # -------------------------------------------------------------------------------------------------- playables
    @app.get("/playables", response_class=HTMLResponse)
    def playables_page(request: Request) -> HTMLResponse:
        from creative_forge.playable.rules import CTA_LABELS, NETWORKS, SOURCES

        folder = root / "playables"
        reports = []
        files = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
        for p in files:
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, list) or not data or not all(isinstance(r, dict) and "network" in r for r in data):
                continue  # not `forge playable check --json` output
            by_file: dict[str, list[dict[str, Any]]] = {}
            for r in data:
                by_file.setdefault(Path(str(r.get("file", "?"))).name, []).append(r)
            reports.append({"name": p.name, "mtime": p.stat().st_mtime, "files": by_file})
        rules = [
            {"net": n, "source": SOURCES.get(n.source, ""), "cta": [CTA_LABELS.get(c, c) for c in n.cta]}
            for n in NETWORKS.values()
        ]
        return page(request, "playables.html", reports=reports, rules=rules, folder=folder)

    # -------------------------------------------------------------------------------------------------- campaigns
    camp_dir = root / "campaigns"

    def camp_state() -> Any:
        from creative_forge.campaigns.state import CampaignState

        return CampaignState(campaign_state)

    def camp_configs() -> list[Path]:
        if not camp_dir.is_dir():
            return []
        return sorted(p for p in camp_dir.iterdir() if p.suffix in (".yaml", ".yml") and p.is_file())

    def camp_config(name: str) -> Path | None:
        files = camp_configs()
        if name:
            return next((p for p in files if p.name == name), None)
        return files[0] if files else None

    def publish_dirs() -> list[str]:
        found = []
        for depth in ("*/manifest.json", "*/*/manifest.json", "*/*/*/manifest.json"):
            found += [m.parent.relative_to(root).as_posix() for m in root.glob(depth)]
        return sorted(set(found))

    def camp_engine(live: bool = False) -> Any:
        from creative_forge.campaigns.engine import CampaignEngine

        return CampaignEngine(camp_state(), allow_live=live, perf_db=db_path())

    def campaigns_view(request: Request, status_code: int = 200, config: str = "", **ctx: Any) -> HTMLResponse:
        from creative_forge.campaigns.audit import AuditLog
        from creative_forge.campaigns.config import CampaignConfigError, load_config
        from creative_forge.campaigns.creatives import CreativeGateError
        from creative_forge.campaigns.engine import confirmation_phrase
        from creative_forge.campaigns.plan import build_plan
        from creative_forge.campaigns.store_url import StoreUrlError
        from creative_forge.campaigns.versions import VERSIONS

        lang = lang_of(request)
        st = camp_state()
        chosen = camp_config(config)
        plan = plan_error = cfg = None
        if chosen is not None:
            try:
                cfg = load_config(chosen)
                plan = build_plan(cfg, date.today()).to_dict(st)
            except (CampaignConfigError, StoreUrlError, CreativeGateError, ValueError) as e:
                plan_error = str(e)
        recs = st.campaigns()
        rows = [{"r": r, "phrase": confirmation_phrase(r, lang)} for r in recs]
        audit = AuditLog(st.audit_path).verify()
        live = bool(plan and any(n["mode"] == "live" for n in plan["networks"]))
        return page(
            request,
            "campaigns.html",
            status_code,
            files=[p.name for p in camp_configs()],
            chosen=chosen.name if chosen else "",
            cfg=cfg,
            plan=plan,
            plan_error=plan_error,
            rows=rows,
            audit=audit,
            audit_path=st.audit_path,
            state_dir=st.dir,
            camp_dir=camp_dir,
            live=live,
            versions=VERSIONS,
            publish_dirs=publish_dirs(),
            cdemo=camp_demo,
            **ctx,
        )

    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns_page(request: Request, config: str = "") -> HTMLResponse:
        return campaigns_view(request, config=config)

    def camp_errors() -> tuple[type[BaseException], ...]:
        from creative_forge.campaigns.config import CampaignConfigError
        from creative_forge.campaigns.creatives import CreativeGateError
        from creative_forge.campaigns.http import AmbiguousWrite, NetworkError
        from creative_forge.campaigns.networks import SafetyError
        from creative_forge.campaigns.store_url import StoreUrlError
        from creative_forge.config import ConfigError

        return (CampaignConfigError, CreativeGateError, NetworkError, AmbiguousWrite, SafetyError, StoreUrlError,
                ConfigError, ValueError)  # fmt: skip

    @app.post("/campaigns/prepare", response_class=HTMLResponse)
    async def campaigns_prepare(request: Request) -> HTMLResponse:
        from creative_forge.campaigns.config import load_config

        form = await require_token(request)
        name = str(form.get("config", ""))
        path = camp_config(name)
        if path is None:
            raise HTTPException(404, "no such campaign config")
        try:
            with camp_engine(live=form.get("live") == "1") as eng:
                rep = eng.create(load_config(path))
        except camp_errors() as e:
            return campaigns_view(request, 409, config=path.name, error=str(e))
        created = sum(n.created for n in rep.networks)
        existing = sum(n.existing + n.reconciled for n in rep.networks)
        stopped = [(n.network, n.stopped) for n in rep.networks if n.stopped]
        return campaigns_view(request, 200 if not stopped else 409, config=path.name, created=(created, existing),
                              stopped=stopped, packs=rep.packs)  # fmt: skip

    @app.post("/campaigns/activate", response_class=HTMLResponse)
    async def campaigns_activate(request: Request) -> HTMLResponse:
        from creative_forge.campaigns.config import load_config

        form = await require_token(request)
        name, ref = str(form.get("config", "")), str(form.get("ref", ""))
        path = camp_config(name) if name else None
        try:
            cfg = load_config(path) if path else None
            with camp_engine(live=form.get("live") == "1") as eng:
                rec = eng.activate(ref, str(form.get("phrase", "")), cfg)
        except camp_errors() as e:
            return campaigns_view(request, 409, config=name, error=str(e), error_ref=ref)
        return campaigns_view(request, config=name, done=("activated", rec.name))

    @app.post("/campaigns/pause", response_class=HTMLResponse)
    async def campaigns_pause(request: Request) -> HTMLResponse:
        form = await require_token(request)
        name, ref = str(form.get("config", "")), str(form.get("ref", ""))
        try:
            with camp_engine() as eng:  # pausing is always allowed, also on a live account
                rec = eng.pause(ref, "paused from the UI")
        except camp_errors() as e:
            return campaigns_view(request, 409, config=name, error=str(e), error_ref=ref)
        return campaigns_view(request, config=name, done=("paused", rec.name))

    @app.post("/campaigns/sync", response_class=HTMLResponse)
    async def campaigns_sync(request: Request) -> HTMLResponse:
        form = await require_token(request)
        name = str(form.get("config", ""))
        try:
            with camp_engine() as eng:  # reads, and pauses at most
                results = eng.sync()
        except camp_errors() as e:
            return campaigns_view(request, 409, config=name, error=str(e))
        return campaigns_view(request, config=name, done=("synced", str(len(results))))

    @app.post("/campaigns/starter")
    async def campaigns_starter(request: Request) -> Response:
        import yaml

        from creative_forge.campaigns.config import CampaignConfigError, parse_config
        from creative_forge.campaigns.store_url import StoreUrlError, parse_store_url

        form = await require_token(request)
        deliver = str(form.get("deliver", ""))
        url = str(form.get("store_url", "")).strip()
        data: dict[str, Any] = {}
        try:
            if deliver not in publish_dirs():
                raise ValueError("pick a `forge publish` folder")
            listing = parse_store_url(url)
            data = {
                "version": 1,
                "name": "kampanya-" + date.today().strftime("%Y%m%d"),
                "creatives": f"../{deliver}",
                "platform": listing.platform,
                "store_url": url,
                "countries": [c.strip().upper() for c in str(form.get("countries", "TR")).split(",") if c.strip()],
                "currency": "USD",
                "budget": {"daily": _float(form.get("daily"), -1), "total_cap": _float(form.get("total_cap"), -1)},
                "limits": {
                    "max_daily": _float(form.get("max_daily"), -1),
                    "max_total": _float(form.get("max_total"), -1),
                },
                "automation": {"auto_pause": False},
                "formats": ["9:16"],
                "networks": STARTER_NETWORKS,
            }
            parse_config(data, camp_dir)
        except (CampaignConfigError, StoreUrlError, ValueError) as e:
            return campaigns_view(request, 422, starter_error=str(e), starter=dict(form))
        camp_dir.mkdir(parents=True, exist_ok=True)
        target = camp_dir / f"{data['name']}.yaml"
        n = 2
        while target.exists():
            target = camp_dir / f"{data['name']}-{n}.yaml"
            n += 1
        header = "# forge campaign config written by the Kampanya page: mock mode. Put real ids here and set mode.\n"
        target.write_text(header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return RedirectResponse(f"/campaigns?config={quote(target.name)}", status_code=303)

    @app.post("/campaigns/demo", response_class=HTMLResponse)
    async def campaigns_demo(request: Request) -> HTMLResponse:
        await require_token(request)
        camp_demo.start()
        return page(request, "_campaign_demo.html", cdemo=camp_demo)

    @app.get("/campaigns/demo/status", response_class=HTMLResponse)
    def campaigns_demo_status(request: Request) -> HTMLResponse:
        return page(request, "_campaign_demo.html", cdemo=camp_demo)

    # --------------------------------------------------------------------------------------------------- settings
    def settings_view(request: Request, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        """Credentials are reported present or missing by variable name; a value is never read into the page."""
        from creative_forge import paths
        from creative_forge.config import ENV_COMFYUI_URL, ENV_PROJECT
        from creative_forge.doctor import _adc_path
        from creative_forge.perf.csvimport import NETWORKS as CSV_NETWORKS
        from creative_forge.perf.lessons import lessons_path
        from creative_forge.providers.comfyui import ENV_TOKEN

        def present(var: str | None) -> bool:
            return bool(var and os.environ.get(var, "").strip())

        status = catalog.import_status(db_path())
        conns = [
            {"key": key, "label": label, "kind": kind, "var": var, "present": present(var), "last": status.get(key)}
            for key, label, kind, var in CONNECTIONS
        ]
        explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        adc = Path(explicit) if explicit else _adc_path()
        production = [
            {"label": "Vertex AI", "what": "adc", "present": adc.is_file(), "var": "GOOGLE_APPLICATION_CREDENTIALS"},
            {"label": "Vertex AI", "what": "project", "var": ENV_PROJECT,
             "present": present(ENV_PROJECT) or present("GOOGLE_CLOUD_PROJECT")},
            {"label": "ComfyUI", "what": "url", "present": present(ENV_COMFYUI_URL), "var": ENV_COMFYUI_URL},
            {"label": "ComfyUI", "what": "token", "present": present(ENV_TOKEN), "var": ENV_TOKEN},
        ]  # fmt: skip
        folders = [
            ("runs", root),
            ("data", paths.data_dir()),
            ("cache", paths.cache_dir()),
            ("perf_db", db_path()),
            ("lessons", lessons_path()),
            ("playables", root / "playables"),
        ]
        return page(
            request,
            "settings.html",
            status_code,
            conns=conns,
            production=production,
            folders=[(k, p, p.exists()) for k, p in folders],
            csv_networks=[n for n in CSV_NETWORKS if n not in ("meta", "applovin")] + ["meta", "applovin"],
            demo_rid=demo_rid(),
            **ctx,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> HTMLResponse:
        return settings_view(request)

    @app.post("/settings/import", response_class=HTMLResponse)
    async def settings_import(request: Request) -> HTMLResponse:
        from creative_forge.perf.commands import import_csv
        from creative_forge.perf.csvimport import NETWORKS as CSV_NETWORKS
        from creative_forge.perf.store import PerfStore

        form = await require_token(request)
        upload = form.get("file")
        network = str(form.get("network", ""))
        account = (str(form.get("account") or "").strip() or "csv")[:64]
        filename = Path(str(getattr(upload, "filename", "") or "")).name
        error: str | None = None
        detail: str | None = None
        summary = None
        if upload is None or isinstance(upload, str) or not filename:
            error = "no_file"
        elif network not in CSV_NETWORKS:
            error = "bad_network"
        else:
            data = await upload.read(MAX_UPLOAD + 1)
            if len(data) > MAX_UPLOAD:
                error = "too_large"
            else:
                tmpdir = Path(tempfile.mkdtemp(prefix="forge-upload-"))
                tmp = tmpdir / filename
                try:
                    tmp.write_bytes(data)
                    summary = import_csv(PerfStore(db_path()), tmp, network, account)
                except (ValueError, OSError) as e:
                    error, detail = "unreadable", str(e)
                finally:
                    with contextlib.suppress(OSError):
                        tmp.unlink()
                    with contextlib.suppress(OSError):
                        tmpdir.rmdir()
        return settings_view(
            request,
            422 if error else 200,
            import_error=error,
            import_detail=detail,
            summary=summary,
            uploaded=filename,
        )

    # ------------------------------------------------------------------------------------------------ demo, doctor
    @app.post("/demo", response_class=HTMLResponse)
    async def start_demo(request: Request) -> HTMLResponse:
        await require_token(request)
        demo.start()
        return page(request, "_demo.html", demo_rid=demo_rid())

    @app.get("/demo/status", response_class=HTMLResponse)
    def demo_status(request: Request) -> HTMLResponse:
        return page(request, "_demo.html", demo_rid=demo_rid())

    @app.get("/doctor", response_class=HTMLResponse)
    def doctor_page(request: Request, partial: bool = False) -> HTMLResponse:
        from creative_forge.doctor import failed, run_doctor

        items = run_doctor()
        name = "_doctor_table.html" if partial else "doctor.html"  # the settings page loads the table on its own
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
