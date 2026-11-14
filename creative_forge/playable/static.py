"""Static checks: what can be read from the file or zip without running it."""

from __future__ import annotations

import json
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from creative_forge.playable.rules import (
    CTA_LABELS,
    FAIL,
    INFO,
    MB,
    MIB,
    PASS,
    SOURCES,
    UNCERTAIN,
    WARN,
    Finding,
    Network,
    source_of,
)


class PlayableError(ValueError):
    pass


# A playable is a few MB (every network caps it at 5 MB or less). A zip from someone else is read into memory, never
# extracted here, but a decompression bomb or an absolute or ../ member name is refused before anything is read.
MAX_ZIP_MEMBERS = 2000
MAX_ZIP_BYTES = 100 * 1024 * 1024  # uncompressed, all members together


def read_zip(path: Path) -> dict[str, bytes]:
    """Every file of a zip by its member name, after checking names and sizes (``PlayableError`` otherwise).
    ``ZipFile.read`` stops at each member's declared size and checks its CRC, so the declared sizes bound memory."""
    try:
        with zipfile.ZipFile(path) as z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > MAX_ZIP_MEMBERS:
                raise PlayableError(f"{Path(path).name}: {len(infos)} files in the zip (at most {MAX_ZIP_MEMBERS})")
            total = sum(i.file_size for i in infos)
            if total > MAX_ZIP_BYTES:
                raise PlayableError(
                    f"{Path(path).name}: {total / 1e6:.0f} MB uncompressed (at most {MAX_ZIP_BYTES / 1e6:.0f} MB)"
                )
            for i in infos:
                name = i.filename
                parts = PurePosixPath(name).parts
                if (
                    name.startswith(("/", "\\"))
                    or "\\" in name
                    or "\x00" in name
                    or ".." in parts
                    or (parts and ":" in parts[0])
                ):
                    raise PlayableError(f"{Path(path).name}: unsafe file name in the zip: {name!r}")
            return {i.filename: z.read(i) for i in infos}
    except zipfile.BadZipFile as e:
        raise PlayableError(f"{Path(path).name}: not a readable zip: {e}") from None


@dataclass
class Bundle:
    path: Path
    kind: str  # "html" or "zip"
    size: int
    index: str  # path of the main HTML (inside the zip for a zip)
    html: str
    files: dict[str, bytes] = field(default_factory=dict)  # zip members (name -> bytes); empty for a single HTML

    @property
    def scripts(self) -> str:
        """The main HTML plus every JS file in the bundle, for API detection."""
        js = [b.decode("utf-8", "replace") for n, b in self.files.items() if n.lower().endswith(".js")]
        return "\n".join([self.html, *js])


def load(path: Path) -> Bundle:
    path = Path(path)
    if not path.is_file():
        raise PlayableError(f"{path} not found")
    size = path.stat().st_size
    if zipfile.is_zipfile(path):
        files = read_zip(path)
        htmls = [n for n in files if n.lower().endswith((".html", ".htm"))]
        index = "index.html" if "index.html" in files else (htmls[0] if len(htmls) == 1 else "")
        if not index:
            raise PlayableError(f"{path.name}: no index.html at the zip root and not exactly one HTML file inside")
        return Bundle(path, "zip", size, index, files[index].decode("utf-8", "replace"), files)
    text = path.read_bytes().decode("utf-8", "replace")
    if "<html" not in text.lower() and "<!doctype" not in text.lower():
        raise PlayableError(f"{path.name}: neither a zip nor an HTML file")
    return Bundle(path, "html", size, path.name, text)


# ------------------------------------------------------------------------------------------------------------ parsing
_BLOB = re.compile(r"data:[^\"')\s]{0,100};base64,[A-Za-z0-9+/=]{64,}")
_RESOURCE = re.compile(
    r"<(script|img|link|source|video|audio|iframe|embed|object|track)\b[^>]*?\b(src|href|data|poster)\s*=\s*"
    r"([\"'])(.*?)\3",
    re.I | re.S,
)
_CSS_URL = re.compile(r"url\(\s*([\"']?)([^\"')]+)\1\s*\)", re.I)
_SCRIPT_BLOCK = re.compile(r"<(script|style|svg)\b.*?</\1\s*>", re.I | re.S)
_VOID_SELF_CLOSED = re.compile(
    r"<(meta|link|br|img|input|hr|source|area|base|col|embed|param|track|wbr)\b[^>]*/>", re.I
)


def strip_blobs(text: str) -> str:
    """Base64 payloads make up most of a playable; they are never code."""
    return _BLOB.sub("data:;base64,", text)


_SCRIPT_BODY = re.compile(r"(<script\b[^>]*>).*?(</script\s*>)", re.I | re.S)


def resources(html: str) -> list[tuple[str, str]]:
    """(tag, url) of everything the markup loads (tags and CSS ``url()``), outside inline scripts, whose strings
    only build markup at run time (the browser run sees those requests); data: and blob: URLs left out."""
    text = _SCRIPT_BODY.sub(r"\1\2", strip_blobs(html))
    out = [(m.group(1).lower(), m.group(4).strip()) for m in _RESOURCE.finditer(text)]
    out += [("css", m.group(2).strip()) for m in _CSS_URL.finditer(text)]
    skip = ("data:", "blob:", "#", "about:", "javascript:")
    return [(t, u) for t, u in out if u and not u.lower().startswith(skip) and not re.search(r"[\"'+]|\$\{", u)]


def is_external(url: str) -> bool:
    return url.lower().startswith(("http://", "https://", "//"))


def _api_used(code: str, api: str) -> bool:
    if api == "postMessage:download":
        return bool(re.search(r"postMessage\(\s*[\"']download[\"']", code))
    return bool(re.search(re.escape(api) + r"\s*\(", code))


def _mraid_used(code: str) -> bool:
    return bool(re.search(r"\bmraid\s*\.\s*(?!js\b)\w+", code))  # "mraid.js" is a file name, not a call


def _store_urls(code: str) -> list[str]:
    return re.findall(
        r"https://(?:play\.google\.com/store/apps/details\?id=[^\"'\s&]*|apps\.apple\.com/[^\"'\s]*)", code
    )


def _store_url_ok(url: str) -> bool:
    if "play.google.com" in url:
        pkg = url.split("id=", 1)[1] if "id=" in url else ""
        return bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+", pkg))
    return bool(re.search(r"/id\d{6,}", url))


# ------------------------------------------------------------------------------------------------------------- checks
def check(bundle: Bundle, net: Network) -> list[Finding]:
    src = source_of(net)
    out: list[Finding] = []

    def add(rule: str, status: str, message: str, source: str | None = None) -> None:
        if not net.verified and status in (FAIL, WARN) and rule.startswith(("packaging", "size", "cta", "mraid")):
            status, message = UNCERTAIN, f"{message} (the {net.label} spec page could not be read; not verified)"
        out.append(Finding(rule, status, message, src if source is None else source))

    code = strip_blobs(bundle.scripts)
    # packaging and size
    if not net.verified:
        add("packaging", UNCERTAIN, f"{bundle.kind}; the {net.label} playable spec is unverified (page not readable)")
    elif net.packaging == "html" and bundle.kind != "html":
        add("packaging", FAIL, f"{net.label} takes a single HTML file; this is a {bundle.kind}")
    elif net.packaging == "zip" and bundle.kind != "zip":
        add("packaging", FAIL, f"{net.label} takes a zip; this is a single {bundle.kind} file")
    else:
        add("packaging", PASS, f"{bundle.kind} ({net.packaging.replace('_', ' ')} accepted)")
    limit = net.html_max if bundle.kind == "html" else net.zip_max
    if limit:
        mb = f"{limit / MB:g} MB"
        binary = limit // MB * MIB
        soft = net.zip_soft_max if bundle.kind == "zip" and net.zip_soft_max else binary
        if bundle.size <= limit:
            add("size", PASS, f"{bundle.size / MB:.2f} MB <= {mb}")
        elif bundle.size <= max(soft, binary):
            add(
                "size",
                UNCERTAIN,
                f"{bundle.size:,} bytes: over {mb} if MB means 10^6, within it if 2^20 or the other figure",
            )
        else:
            add("size", FAIL, f"{bundle.size / MB:.2f} MB > {mb}")
    elif net.verified:
        add("size", INFO, f"{bundle.size / MB:.2f} MB; no size limit read for {net.label}")
    if bundle.kind == "zip":
        if net.zip_max_files and len(bundle.files) > net.zip_max_files:
            add("zip-files", FAIL, f"{len(bundle.files)} files > {net.zip_max_files}")
        if net.file_types:
            bad = sorted({PurePosixPath(n).suffix.lower() or n for n in bundle.files} - set(net.file_types))
            add("file-types", FAIL if bad else PASS, f"not allowed: {', '.join(bad)}" if bad else "only allowed types")
        if net.packaging == "zip" and bundle.index != "index.html":
            add(
                "index", FAIL if net.verified else UNCERTAIN, f"main HTML is {bundle.index}, not index.html at the root"
            )
    # TikTok config.json
    if net.name == "tiktok":
        raw = bundle.files.get("config.json")
        if raw is None:
            add("config-json", FAIL, "no config.json at the zip root", SOURCES["tiktok-pangle"])
        else:
            try:
                cfg = json.loads(raw.decode("utf-8"))
                ori = cfg.get("playable_orientation") if isinstance(cfg, dict) else None
                ok = ori in (0, 1, 2)
                add(
                    "config-json",
                    PASS if ok else FAIL,
                    f"playable_orientation = {ori!r}" + ("" if ok else " (must be 0, 1 or 2)"),
                    SOURCES["tiktok-pangle"],
                )
            except ValueError as e:
                add("config-json", FAIL, f"config.json is not JSON: {e}", SOURCES["tiktok-pangle"])
    # MRAID
    mraid_tag = bool(re.search(r"<script[^>]+src\s*=\s*[\"'][^\"']*mraid\.js", bundle.html, re.I))
    used = _mraid_used(code)
    if net.mraid == "forbidden":
        if mraid_tag:
            add("mraid", FAIL, "loads mraid.js; TikTok says not to use the MRAID format")
        elif used:
            add("mraid", UNCERTAIN, "references mraid.* (feature detection?); TikTok says not to use the MRAID format")
        else:
            add("mraid", PASS, "no MRAID")
    elif net.mraid in ("required", "injected"):
        if not used:
            add("mraid", FAIL, f"no mraid.* call; {net.label} playables use MRAID {net.mraid_version}")
        elif net.mraid == "injected" and mraid_tag:
            add(
                "mraid",
                UNCERTAIN,
                f"loads mraid.js, while {net.label} injects MRAID {net.mraid_version} itself; whether the tag is "
                "harmless there is not stated",
            )
        else:
            add("mraid", PASS, f"uses MRAID ({net.mraid_version} expected)")
        if not re.search(r"addEventListener\(\s*[\"']ready[\"']", code):
            add("mraid-ready", WARN, "no mraid 'ready' listener: start after ready")
        else:
            add("mraid-ready", PASS, "waits for mraid 'ready'")
    if net.name == "unity":
        has = re.search(r"[\"']viewableChange[\"']", code)
        add("viewable", PASS if has else WARN, "listens to viewableChange" if has else "no viewableChange listener: "
            "start when viewable and pause when hidden")  # fmt: skip
        add(
            "index-name",
            PASS if bundle.index == "index.html" else WARN,
            "index.html"
            if bundle.index == "index.html"
            else f"named {bundle.index}; Unity asks for index.html (`forge playable pack --network unity` writes one)",
        )
        lines = bundle.html.count("\n") + 1
        if lines > 400 and len(bundle.html) / lines < 120:
            add("minified", WARN, f"{lines} lines: Unity asks for a minified index.html")
    # CTA call
    if net.cta:
        found = [api for api in net.cta if _api_used(code, api)]
        expected = " or ".join(CTA_LABELS[a] for a in net.cta)
        if found:
            add("cta", PASS if net.verified and net.name != "mintegral" else UNCERTAIN, f"calls {CTA_LABELS[found[0]]}")
        else:
            add("cta", FAIL if net.name != "mintegral" else UNCERTAIN, f"never calls {expected}")
    else:
        apis = [CTA_LABELS[a] for a in CTA_LABELS if _api_used(code, a)]
        add(
            "cta",
            UNCERTAIN,
            f"{net.label}'s CTA call is not verified; the code calls {', '.join(apis) or 'nothing known'}",
        )
    if net.sdk_script:
        tag = re.search(rf"<script[^>]+src\s*=\s*[\"'][^\"']*{re.escape(net.sdk_script)}[\"']", bundle.html, re.I)
        add(
            "sdk-script",
            PASS if tag else FAIL,
            f"loads {net.sdk_script}" if tag else f"no <script src> for {net.sdk_script}",
        )
    if net.name == "google":
        meta = re.search(r"<meta[^>]+name\s*=\s*[\"']ad\.orientation[\"']", bundle.html, re.I)
        add(
            "ad-orientation",
            PASS if meta else FAIL,
            "has ad.orientation meta" if meta else 'no <meta name="ad.orientation">',
        )
        outside = _SCRIPT_BLOCK.sub("", strip_blobs(bundle.html))
        closed = _VOID_SELF_CLOSED.findall(outside)
        add(
            "self-closing",
            UNCERTAIN if closed else PASS,
            f"self-closed tags ({', '.join(sorted({t.lower() for t in closed}))}); Google lists self-closing tags "
            "as unsupported, scope unclear"
            if closed
            else "no self-closed tags outside scripts and SVG",
        )
    # resources: external and missing
    res = resources(bundle.html)
    external = [u for _, u in res if is_external(u)]
    sdk = [u for u in external if net.sdk_script and u.lower().split("?")[0].endswith(net.sdk_script)]
    allowed = [
        u for u in external if (urlsplit(u if "//" in u[:8] else "https:" + u).hostname or "") in net.allowed_hosts
    ]
    bad = [u for u in external if u not in sdk and u not in allowed]
    if bad:
        add("external", net.external, f"loads from outside the bundle: {', '.join(sorted(set(bad))[:5])}")
    else:
        add("external", PASS, "no external resources in the markup" + (" (only allowed hosts)" if allowed else ""))
    local = [u for _, u in res if not is_external(u)]
    if bundle.kind == "html":
        missing = [u for u in local if not u.endswith("mraid.js")]
        if missing:
            add(
                "inlined",
                FAIL,
                f"references files that a single HTML cannot carry: {', '.join(sorted(set(missing))[:5])}",
            )
        else:
            add("inlined", PASS, "every asset is inlined")
    else:
        base = PurePosixPath(bundle.index).parent
        names = set(bundle.files)
        missing = [
            u
            for u in local
            if not u.endswith("mraid.js") and posixpath.normpath(str(base / u.split("?")[0].split("#")[0])) not in names
        ]
        if missing:
            add("inlined", FAIL, f"references files missing from the zip: {', '.join(sorted(set(missing))[:5])}")
    # code-level hints, decided by the dynamic run
    if re.search(r"\bXMLHttpRequest\b|\bfetch\s*\(", code):
        add(
            "xhr",
            UNCERTAIN if net.name == "unity" else INFO,
            "uses fetch/XMLHttpRequest (data: URLs are fine; the browser run shows whether anything leaves the page)",
        )
    if re.search(r"window\.open\s*\(|location\.(href|assign|replace)\b|window\.location\s*=", code):
        add(
            "redirect",
            UNCERTAIN if net.name == "meta" else INFO,
            "code can redirect (window.open / location); Meta allows no JavaScript redirects. The browser run shows "
            "whether it does",
        )
    if net.name == "applovin" and re.search(r"getContext\(\s*[\"'](webgl2?|experimental-webgl)", code):
        lost = "webglcontextlost" in code
        add("webgl-context-loss", PASS if lost else WARN, "handles webglcontextlost" if lost else "WebGL without a "
            "webglcontextlost fallback")  # fmt: skip
    if net.needs_store_url:
        urls = _store_urls(code)
        broken = sorted({u for u in urls if not _store_url_ok(u)})
        if broken:
            add("store-url", FAIL, f"placeholder store link(s): {', '.join(broken)}; the CTA would open a dead page",
                SOURCES["mraid3"])  # fmt: skip
        elif not urls:
            add("store-url", WARN, "no store link found for mraid.open(url)", SOURCES["mraid3"])
        else:
            add("store-url", PASS, f"{len(urls)} store link(s) look valid", SOURCES["mraid3"])
    return out
