"""Dynamic checks: load the playable in headless Chromium with every network SDK stubbed, and watch it.

The bundle is served from a private ``http.server`` on 127.0.0.1; any request to another origin is recorded and
aborted (network SDK scripts such as ``exitapi.js`` or ``playable-sdk.js`` are answered with a stub instead), so a
playable that would phone home is caught without it ever reaching the internet. ``stubs.js`` runs before the page's
own scripts and records CTA/exit calls with the number of user interactions before them, ``window.open`` and
navigation attempts, and sound started before the first interaction. The browser is muted.

What is checked: no JavaScript errors; a non-blank frame within N seconds; no CTA before any interaction; the first
tap does not open the store; a CTA reached by tapping calls the network's exit API; no external requests; no sound
before the first tap; still rendering, without errors, after a turn to landscape.

The CTA is searched for by tapping: first the centre (that tap must not open the store), then elements whose text
reads like a CTA (install, download, play, ...), then a grid. A playable whose CTA needs a finished level is reported
``uncertain`` (CTA not reached), not failed.

Browser: ``--browser-channel chrome`` drives an installed Google Chrome (no download); otherwise Playwright's own
Chromium (``python -m playwright install chromium``).
"""

from __future__ import annotations

import functools
import http.server
import io
import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

from PIL import Image, ImageStat

from creative_forge.playable.rules import CTA_LABELS, FAIL, INFO, PASS, UNCERTAIN, WARN, Finding, Network, source_of
from creative_forge.playable.static import Bundle

ENV_CHANNEL = "CREATIVE_FORGE_BROWSER_CHANNEL"
SDK_STUBS = {"exitapi.js": "", "playable-sdk.js": "", "mraid.js": ""}  # the init script already defines them
CTA_TEXT = r"install|download|play|get it|try|start|yükle|indir|oyna|jugar|spielen|jouer"


class BrowserUnavailable(RuntimeError):
    pass


@dataclass
class Trace:
    url: str = ""
    errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)
    stubbed: list[str] = field(default_factory=list)
    navigations: list[str] = field(default_factory=list)
    rendered_after_s: float | None = None
    landscape_ok: bool | None = None
    taps: int = 0
    record: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    extensions_map = {  # noqa: RUF012 - the base class declares it the same way
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "application/javascript",
        ".mjs": "application/javascript",
        ".html": "text/html",
        ".json": "application/json",
        ".css": "text/css",
        ".svg": "image/svg+xml",
        ".wasm": "application/wasm",
    }

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def serve(root: Path):
    handler = functools.partial(_Quiet, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def unpack(bundle: Bundle, into: Path) -> str:
    """Write the bundle into a directory; returns the main HTML's relative path (posix)."""
    if bundle.kind == "html":
        (into / bundle.index).write_text(bundle.html, encoding="utf-8")
        return bundle.index
    for name, data in bundle.files.items():
        target = (into / PurePosixPath(name)).resolve()
        if not target.is_relative_to(into.resolve()):
            raise ValueError(f"zip member escapes the folder: {name}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return bundle.index


def _stub_script(net: Network) -> str:
    js = resources.files("creative_forge.playable").joinpath("stubs.js").read_text(encoding="utf-8")
    cfg = {"network": net.name, "mraidVersion": net.mraid_version or "3.0"}
    return js.replace("__FORGE_CONFIG__", json.dumps(cfg))


def _nonblank(png: bytes) -> bool:
    im = Image.open(io.BytesIO(png)).convert("L")
    return ImageStat.Stat(im).stddev[0] > 3.0


def run(
    bundle: Bundle,
    net: Network,
    *,
    channel: str | None = None,
    render_timeout_s: float = 8.0,
    settle_s: float = 1.5,
    tap_gap_s: float = 0.45,
    max_taps: int = 12,
) -> tuple[list[Finding], Trace]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        raise BrowserUnavailable('Playwright is not installed: pip install "creative-forge[playable]"') from e
    channel = channel if channel is not None else (os.environ.get(ENV_CHANNEL) or None)
    trace = Trace()
    work = Path(tempfile.mkdtemp(prefix="forge-playable-"))
    try:
        index = unpack(bundle, work)
        with serve(work) as origin, sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(
                    channel=channel, headless=True, args=["--autoplay-policy=user-gesture-required", "--mute-audio"]
                )
            except PlaywrightError as e:
                hint = "install Chrome or run `python -m playwright install chromium`"
                raise BrowserUnavailable(f"cannot start Chromium{' (channel ' + channel + ')' if channel else ''}: "
                                         f"{str(e).splitlines()[0]}; {hint}") from None  # fmt: skip
            try:
                _drive(browser, origin, index, bundle, net, trace, render_timeout_s, settle_s, tap_gap_s, max_taps)
            finally:
                browser.close()
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return _findings(net, trace, render_timeout_s), trace


def _drive(browser, origin, index, bundle, net, trace, render_timeout_s, settle_s, tap_gap_s, max_taps) -> None:
    ctx = browser.new_context(
        viewport={"width": 360, "height": 640}, is_mobile=True, has_touch=True, service_workers="block"
    )
    ctx.add_init_script(_stub_script(net))
    present = {PurePosixPath(n).name.lower() for n in bundle.files} | {bundle.index.lower()}

    def route(r) -> None:
        url = r.request.url
        name = url.split("?")[0].rsplit("/", 1)[-1].lower()
        if url.startswith(origin):
            if name in SDK_STUBS and name not in present:
                trace.stubbed.append(name)
                return r.fulfill(status=200, content_type="application/javascript", body=SDK_STUBS[name])
            return r.continue_()
        if name in SDK_STUBS:
            trace.stubbed.append(url)
            return r.fulfill(status=200, content_type="application/javascript", body=SDK_STUBS[name])
        if r.request.is_navigation_request():
            trace.navigations.append(url)
        else:
            trace.external.append(url)
        return r.abort()

    ctx.route("**/*", route)
    page = ctx.new_page()
    page.on("pageerror", lambda e: trace.errors.append(str(e)[:300]))
    page.on("console", lambda m: trace.console_errors.append(m.text[:300]) if m.type == "error" else None)
    trace.url = f"{origin}/{index}"
    t0 = time.monotonic()
    try:
        page.goto(trace.url, wait_until="load", timeout=int(render_timeout_s * 1000) + 15000)
    except Exception as e:  # a page that never fires load still gets the render check
        trace.errors.append(f"load: {str(e).splitlines()[0][:200]}")
    while time.monotonic() - t0 < render_timeout_s:
        if _nonblank(page.screenshot()):
            trace.rendered_after_s = round(time.monotonic() - t0, 2)
            break
        page.wait_for_timeout(250)
    page.wait_for_timeout(int(settle_s * 1000))
    targets = [(180, 320)]
    try:
        boxes = page.evaluate(
            """(pattern) => {
                const re = new RegExp(pattern, "i"), out = [];
                for (const el of document.querySelectorAll("button, a, div, span, p, img")) {
                    const t = (el.innerText || el.alt || el.getAttribute("aria-label") || "").trim();
                    if (!t || t.length > 40 || !re.test(t)) continue;
                    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                    if (r.width < 8 || r.height < 8 || s.visibility === "hidden" || s.display === "none") continue;
                    out.push([r.x + r.width / 2, r.y + r.height / 2, r.width * r.height]);
                }
                return out.sort((a, b) => a[2] - b[2]).slice(0, 3).map(b => [b[0], b[1]]);
            }""",
            CTA_TEXT,
        )
        targets += [(float(x), float(y)) for x, y in boxes]
    except Exception:
        pass
    grid = [(0.5, 0.86), (0.5, 0.78), (0.5, 0.68), (0.3, 0.55), (0.7, 0.55), (0.5, 0.45), (0.3, 0.35), (0.7, 0.35)]
    targets += [(x * 360, y * 640) for x, y in grid]
    for x, y in targets[:max_taps]:
        page.touchscreen.tap(min(max(x, 1), 359), min(max(y, 1), 639))
        trace.taps += 1
        page.wait_for_timeout(int(tap_gap_s * 1000))
        calls = page.evaluate("window.__forge ? window.__forge.calls : []")
        if any(c["interactions"] >= 2 and c["api"] in CTA_LABELS for c in calls):
            break
    errors_before = len(trace.errors)
    page.set_viewport_size({"width": 640, "height": 360})
    page.wait_for_timeout(800)
    trace.landscape_ok = len(trace.errors) == errors_before and _nonblank(page.screenshot())
    trace.record = page.evaluate("window.__forge || {}")
    ctx.close()


def _findings(net: Network, t: Trace, render_timeout_s: float) -> list[Finding]:
    src = source_of(net)
    out: list[Finding] = []

    def add(rule: str, status: str, message: str) -> None:
        out.append(Finding(rule, status, message, src))

    add("js-errors", FAIL if t.errors else PASS, "; ".join(t.errors[:2]) if t.errors else "no uncaught errors")
    if t.rendered_after_s is None:
        add("render", FAIL, f"nothing but a blank frame for {render_timeout_s:g} s")
    elif t.rendered_after_s > net.render_s:
        add("render", net.render_status, f"first frame after {t.rendered_after_s:g} s (> {net.render_s:g} s)")
    else:
        add("render", PASS, f"first frame after {t.rendered_after_s:g} s")
    calls = [c for c in t.record.get("calls", []) if c["api"] in CTA_LABELS]
    early = [c for c in calls if c["interactions"] == 0]
    first = [c for c in calls if c["interactions"] == 1]
    later = [c for c in calls if c["interactions"] >= 1]
    add(
        "cta-before-interaction",
        FAIL if early else PASS,
        f"{CTA_LABELS[early[0]['api']]} at {early[0]['t'] / 1000:.1f} s with no interaction" if early else "none",
    )
    add(
        "first-tap-store",
        net.first_tap_store if first else PASS,
        f"the first tap called {CTA_LABELS[first[0]['api']]}" if first else "the first tap did not open the store",
    )
    if not later:
        add("cta-call", UNCERTAIN, f"no CTA reached in {t.taps} taps (it may need a finished level)")
    else:
        api = later[0]["api"]
        if api in net.cta:
            status = PASS if net.verified and net.name != "mintegral" else UNCERTAIN
            add("cta-call", status, f"tap {later[0]['interactions']} called {CTA_LABELS[api]}")
        elif net.cta and net.verified:
            want = " or ".join(CTA_LABELS[a] for a in net.cta)
            add("cta-call", FAIL if net.name != "mintegral" else UNCERTAIN, f"called {CTA_LABELS[api]}, not {want}")
        else:
            add("cta-call", UNCERTAIN, f"called {CTA_LABELS[api]}; {net.label}'s expected call is not verified")
    redirects = t.navigations + [c.get("arg") or "" for c in calls if c["api"] == "window.open"]
    if redirects:
        add("redirect", FAIL if net.name == "meta" else WARN, f"navigates away: {', '.join(redirects[:3])}")
    if t.external:
        add("network-requests", net.external, f"{len(t.external)} external request(s): {', '.join(t.external[:3])}")
    else:
        add(
            "network-requests",
            PASS,
            "no request left the bundle" + (f" (stubbed: {', '.join(sorted(set(t.stubbed)))})" if t.stubbed else ""),
        )
    sound = [a for a in t.record.get("audio", []) if a["interactions"] == 0]
    add(
        "audio-before-tap",
        net.audio_before_tap if sound else PASS,
        f"{sound[0]['what']} before any interaction" if sound else "silent until the first interaction",
    )
    add("landscape", PASS if t.landscape_ok else net.landscape, "renders after a turn to landscape" if t.landscape_ok
        else "blank or errors after a turn to landscape")  # fmt: skip
    if t.console_errors:
        add("console", INFO, "; ".join(t.console_errors[:2]))
    return out
