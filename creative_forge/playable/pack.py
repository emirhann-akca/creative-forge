"""``forge playable pack``: one source playable, packaged the way each network takes it.

* Every local asset (scripts, stylesheets, images, fonts, sounds, CSS ``url()``) is inlined: scripts and styles as
  inline blocks, everything else as base64 ``data:`` URIs. External URLs are left alone (the check reports them).
* Single-HTML networks (AppLovin, Mintegral, Meta up to 2 MB) get one ``.html``; Unity gets a folder holding exactly
  one ``index.html``; Google and TikTok get a zip with ``index.html`` at its root.
* Google: ``<meta name="ad.orientation">`` and the ``exitapi.js`` script tag are added when missing.
* TikTok: ``config.json`` with ``playable_orientation``, and a ``mraid.js`` script tag is removed (TikTok says not to
  use MRAID).
* ``--cta-adapter`` adds ``window.forgeCTA(storeUrl)``, which calls the network's own exit API, so one source can call
  a single function and still pass on every network.

The packed file is then checked statically; packing never hides a failing rule.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import posixpath
import re
import zipfile
from pathlib import Path, PurePosixPath

from creative_forge.playable.rules import Network

EXITAPI_URL = "https://tpc.googlesyndication.com/pagead/gadgets/html5/api/exitapi.js"
_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "video/mp4",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".json": "application/json",
    ".wasm": "application/wasm",
}

ADAPTER = {
    "mraid.open": "if (window.mraid && mraid.open) { mraid.open(url); return; }",
    "FbPlayableAd.onCTAClick": "if (window.FbPlayableAd) { FbPlayableAd.onCTAClick(); return; }",
    "ExitApi.exit": "if (window.ExitApi) { ExitApi.exit(); return; }",
    "openAppStore": "if (window.openAppStore) { window.openAppStore(); return; }",
    "install": "if (window.install) { window.install(); return; }",
}


class PackError(ValueError):
    pass


def read_source(src: Path) -> tuple[str, dict[str, bytes], str]:
    """(main HTML, every file by posix path, the HTML's path) from an HTML file, a folder or a zip."""
    src = Path(src)
    files: dict[str, bytes] = {}
    if src.is_dir():
        for p in src.rglob("*"):
            if p.is_file():
                files[p.relative_to(src).as_posix()] = p.read_bytes()
        index = "index.html" if "index.html" in files else ""
    elif zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as z:
            files = {i.filename: z.read(i) for i in z.infolist() if not i.is_dir()}
        htmls = [n for n in files if n.lower().endswith((".html", ".htm"))]
        index = "index.html" if "index.html" in files else (htmls[0] if len(htmls) == 1 else "")
    elif src.is_file():
        for p in src.parent.rglob("*"):
            if p.is_file() and p.stat().st_size < 50_000_000:
                files[p.relative_to(src.parent).as_posix()] = p.read_bytes()
        index = src.name
    else:
        raise PackError(f"{src} not found")
    if not index:
        raise PackError(f"{src}: no index.html (or single HTML file) to pack")
    return files[index].decode("utf-8"), files, index


def _data_uri(name: str, data: bytes) -> str:
    ext = PurePosixPath(name).suffix.lower()
    mime = _MIME.get(ext) or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _resolve(base: str, ref: str, files: dict[str, bytes]) -> str | None:
    if re.match(r"^(https?:)?//|^(data|blob|javascript|about|#):?", ref, re.I) or ref.startswith("#"):
        return None
    clean = ref.split("?")[0].split("#")[0]
    path = posixpath.normpath(posixpath.join(posixpath.dirname(base), clean))
    return path if path in files else None


def _inline_css(css: str, css_path: str, files: dict[str, bytes]) -> str:
    def sub(m: re.Match[str]) -> str:
        path = _resolve(css_path, m.group(2), files)
        return f"url({_data_uri(path, files[path])})" if path else m.group(0)

    return re.sub(r"url\(\s*([\"']?)([^\"')]+)\1\s*\)", sub, css)


def inline(html: str, index: str, files: dict[str, bytes]) -> tuple[str, list[str]]:
    """Inline every local asset; returns the HTML and the references that could not be resolved."""
    missing: list[str] = []

    def script(m: re.Match[str]) -> str:
        ref = m.group(2)
        if ref.endswith("mraid.js"):
            return m.group(0)
        path = _resolve(index, ref, files)
        if path is None:
            if not re.match(r"^(https?:)?//", ref):
                missing.append(ref)
            return m.group(0)
        code = files[path].decode("utf-8").replace("</script", "<\\/script")
        return f"<script>{code}</script>"

    html = re.sub(r"<script\b[^>]*\bsrc\s*=\s*([\"'])(.*?)\1[^>]*>\s*</script>", script, html, flags=re.I | re.S)

    def stylesheet(m: re.Match[str]) -> str:
        tag = m.group(0)
        if not re.search(r"rel\s*=\s*[\"']?stylesheet", tag, re.I):
            return tag
        href = re.search(r"href\s*=\s*([\"'])(.*?)\1", tag, re.I)
        path = _resolve(index, href.group(2), files) if href else None
        if path is None:
            return tag
        return f"<style>{_inline_css(files[path].decode('utf-8'), path, files)}</style>"

    html = re.sub(r"<link\b[^>]*>", stylesheet, html, flags=re.I)

    html = _inline_attrs(html, index, files, missing)
    html = _inline_css(html, index, files)
    return html, sorted(set(missing))


def _inline_attrs(html: str, index: str, files: dict[str, bytes], missing: list[str]) -> str:
    def sub(m: re.Match[str]) -> str:
        ref = m.group(3)
        path = _resolve(index, ref, files)
        if path is None:
            if not re.match(r"^(https?:)?//|^(data|blob):|^#", ref, re.I):
                missing.append(ref)
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}{_data_uri(path, files[path])}{m.group(2)}"

    tags = r"img|source|audio|video|image|input|embed|track"
    pattern = rf"(<(?:{tags})\b[^>]*?\b(?:src|poster|xlink:href)\s*=\s*)([\"'])(.*?)\2"
    return re.sub(pattern, sub, html, flags=re.I | re.S)


def _head_insert(html: str, snippet: str) -> str:
    m = re.search(r"<head[^>]*>", html, re.I)
    if m:
        return html[: m.end()] + snippet + html[m.end() :]
    return snippet + html


def adapter_script(net: Network) -> str:
    calls = "".join(ADAPTER[a] for a in net.cta if a in ADAPTER) or ADAPTER["mraid.open"]
    return "<script>window.forgeCTA=function(url){" + calls + "};</script>"


def pack(
    src: Path, net: Network, out_dir: Path, *, orientation: str = "portrait", tiktok_orientation: int = 0,
    cta_adapter: bool = False,
) -> tuple[Path, list[str]]:  # fmt: skip
    """Returns the packed file and notes about what was changed or could not be resolved."""
    html, files, index = read_source(src)
    html, missing = inline(html, index, files)
    notes = [f"not inlined (missing): {m}" for m in missing]
    if cta_adapter:
        html = _head_insert(html, adapter_script(net))
        notes.append("added window.forgeCTA(url) for " + (", ".join(net.cta) or "mraid.open"))
    if net.name == "google":
        if not re.search(r"<meta[^>]+name\s*=\s*[\"']ad\.orientation", html, re.I):
            html = _head_insert(html, f'<meta name="ad.orientation" content="{orientation}">')
            notes.append(f"added <meta name=ad.orientation content={orientation}>")
        if "exitapi.js" not in html:
            html = _head_insert(html, f'<script type="text/javascript" src="{EXITAPI_URL}"></script>')
            notes.append("added the exitapi.js script tag")
    if net.mraid == "forbidden":
        html, n = re.subn(r"<script[^>]+src\s*=\s*[\"'][^\"']*mraid\.js[\"'][^>]*>\s*</script>", "", html, flags=re.I)
        if n:
            notes.append("removed the mraid.js script tag (TikTok: do not use MRAID)")
    stem = Path(src).stem if Path(src).is_file() else Path(src).name
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = html.encode("utf-8")
    if net.packaging == "zip" or (net.name == "meta" and net.html_max and len(data) > net.html_max):
        out = out_dir / f"{stem}.{net.name}.zip"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("index.html", data)
            if net.name == "tiktok":
                z.writestr("config.json", json.dumps({"playable_orientation": tiktok_orientation}))
                notes.append(f"config.json playable_orientation={tiktok_orientation}")
        return out, notes
    if net.name == "unity":
        folder = out_dir / f"{stem}.unity"
        folder.mkdir(exist_ok=True)
        out = folder / "index.html"
    else:
        out = out_dir / f"{stem}.{net.name}.html"
    out.write_bytes(data)
    return out, notes
