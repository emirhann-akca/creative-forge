"""HTTP hardening for every response of ``forge ui`` (and the desktop app, which serves the same app).

One ASGI middleware, outermost, so it also covers 4xx answers and ``/static``. In order:

1. Host allow-list: a request whose ``Host`` is not this machine is refused (DNS rebinding). ``hosts=None`` (a
   wildcard bind such as ``0.0.0.0``) accepts any Host, which is only safe together with step 2.
2. Access token, only when the UI listens on a non-loopback address: the URL printed at start carries
   ``?access=<token>``; the first request trades it for an HttpOnly, SameSite=Strict cookie and is redirected to the
   same URL without it. Any other request without that cookie (or an ``Authorization: Bearer`` header) gets 401, so
   another machine on the network cannot read a page, and with it the CSRF token, or approve an export.
3. Cross-site refusal on every state-changing method, whatever the route: an ``Origin``, when sent, must be this
   server's own and the browser must not mark the request cross-site (Fetch Metadata). The per-process CSRF token is
   checked by each route (``require_token``; plain HTML forms carry it as a field), so a request needs both.
4. Fetch Metadata resource isolation for reads: a cross-site request that is not a top-level navigation (an
   ``<img>``, ``<script>`` or ``fetch()`` from another site) is refused.
5. Security headers: a Content-Security-Policy with no inline script at all, no framing, nosniff, no Referer,
   same-origin opener and resource policies, and no-store on HTML (pages carry the token). A header a route set
   itself (the stricter policy of a run's ``report.html``) is left as it is.
"""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit

from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
ACCESS_PARAM = "access"
ACCESS_COOKIE = "forge_access"
OPEN_PATHS = frozenset({"/__app/health"})  # liveness only: no data, no token
CROSS_SITE = frozenset({"cross-site", "same-site"})

# style-src keeps 'unsafe-inline': templates use style attributes, which CSP cannot allow by hash. Scripts are the
# injection risk and come only from /static.
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
    "media-src 'self' blob:; font-src 'self' data:; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
# A run folder's report.html is a file on disk (it may come from someone else's run): no script, no requests.
REPORT_CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; frame-ancestors 'none'; sandbox"

HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-security-policy", CSP.encode("ascii")),
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"cross-origin-opener-policy", b"same-origin"),
    (b"cross-origin-resource-policy", b"same-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
]


def hostname(value: str) -> str:
    """Host header -> bare host name (no port, no IPv6 brackets), lower case."""
    value = value.strip()
    return (urlsplit(value if "//" in value else "//" + value).hostname or "").lower()


def same_origin(origin: str, host_header: str) -> bool:
    parts = urlsplit(origin)
    return parts.scheme in ("http", "https") and parts.netloc.lower() == host_header.lower()


class Guard:
    """The middleware described in the module docstring."""

    def __init__(
        self,
        app: Any,
        *,
        hosts: frozenset[str] | set[str] | None,
        access_token: str | None = None,
    ) -> None:
        if access_token is not None and len(access_token) < 16:
            raise ValueError("the access token must be at least 16 characters")
        self.app = app
        self.hosts = None if hosts is None else {h.lower().strip("[]") for h in hosts}
        self.access_token = access_token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        refusal = self.check(Request(scope))
        target = refusal if refusal is not None else self.app

        async def with_headers(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in HEADERS if k not in present]
                ctype = dict(headers).get(b"content-type", b"")
                if ctype.startswith(b"text/html") and b"cache-control" not in present:
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await target(scope, receive, with_headers)

    def check(self, request: Request) -> Response | None:
        host_header = request.headers.get("host", "")
        if self.hosts is not None and hostname(host_header) not in self.hosts:
            return PlainTextResponse(f"host {hostname(host_header)!r} is not allowed; start with --host to expose", 400)
        if self.access_token is not None and request.url.path not in OPEN_PATHS:
            refused = self._access(request)
            if refused is not None:
                return refused
        site = request.headers.get("sec-fetch-site")
        if request.method not in SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None and not same_origin(origin, host_header):
                return PlainTextResponse("cross-origin request refused", 403)
            if site in CROSS_SITE:
                return PlainTextResponse("cross-site request refused", 403)
            return None
        if site == "cross-site" and request.headers.get("sec-fetch-mode") != "navigate":
            return PlainTextResponse("cross-site request refused", 403)
        return None

    def _access(self, request: Request) -> Response | None:
        assert self.access_token is not None
        token = self.access_token.encode()
        offered = request.query_params.get(ACCESS_PARAM)
        if offered is not None:
            if not secrets.compare_digest(offered.encode(), token):
                return PlainTextResponse("wrong access token", 401)
            rest = [(k, v) for k, v in request.query_params.multi_items() if k != ACCESS_PARAM]
            # one leading slash: "//host" or "/\\host" would send the browser to another site
            path = "/" + request.url.path.lstrip("/\\")
            resp = RedirectResponse(path + ("?" + urlencode(rest) if rest else ""), status_code=303)
            resp.set_cookie(ACCESS_COOKIE, self.access_token, httponly=True, samesite="strict", path="/")
            return resp
        cookie = request.cookies.get(ACCESS_COOKIE, "")
        auth = request.headers.get("authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else ""
        for candidate in (cookie, bearer):
            if candidate and secrets.compare_digest(candidate.encode(), token):
                return None
        return PlainTextResponse(
            "this UI is reachable from the network and needs its access token: open the URL with ?access=... that "
            "forge ui printed when it started",
            401,
        )
