"""Synthetic playables: one good build per network, and flaws that each break exactly one rule."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

STORE = "https://play.google.com/store/apps/details?id=com.example.puzzle"
TIKTOK_SDK = "https://sf16-example.ibytedtos.com/obj/static/playable/sdk/playable-sdk.js"  # an illustrative host
CTA = {
    "applovin": f"mraid.open('{STORE}')",
    "unity": f"mraid.open('{STORE}')",
    "liftoff": f"mraid.open('{STORE}')",
    "moloco": f"mraid.open('{STORE}')",
    "meta": "FbPlayableAd.onCTAClick()",
    "google": "ExitApi.exit()",
    "tiktok": "window.openAppStore()",
    "mintegral": "window.install()",
}
HEAD = {
    "applovin": '<script src="mraid.js"></script>',
    "google": '<meta name="ad.orientation" content="portrait">'
    '<script type="text/javascript" src="https://tpc.googlesyndication.com/pagead/gadgets/html5/api/exitapi.js">'
    "</script>",
    "tiktok": f'<script src="{TIKTOK_SDK}"></script>',
}
MRAID_START = """
function boot(){ draw(); }
if (window.mraid) {
  mraid.addEventListener("viewableChange", function(v){ paused = !v; });
  if (mraid.getState() === "loading") mraid.addEventListener("ready", boot); else boot();
} else { boot(); }
"""
PLAIN_START = "window.addEventListener('load', draw);"


def html(network: str, **flaw: object) -> str:
    """A small canvas game: tap 1 starts it, the PLAY NOW button calls the network's CTA from tap 2 on."""
    mraid = network in ("applovin", "unity", "liftoff", "moloco")
    start = MRAID_START if mraid and not flaw.get("no_ready") else PLAIN_START
    if flaw.get("no_viewable"):
        start = start.replace('mraid.addEventListener("viewableChange", function(v){ paused = !v; });', "")
    if flaw.get("no_mraid"):
        start = PLAIN_START
    cta = str(flaw.get("cta", CTA[network]))
    head = str(flaw.get("head", HEAD.get(network, "")))
    extra_head = str(flaw.get("extra_head", ""))
    body_extra = str(flaw.get("body", ""))
    script_extra = str(flaw.get("script", ""))
    gate = "taps < 1" if flaw.get("first_tap_cta") else "taps < 2"
    button = "#ffffff" if flaw.get("blank") else "#e63946"
    draw = (
        ""
        if flaw.get("blank")
        else "g.fillStyle='#f1faee'; g.fillRect(40,100,280,300); g.fillStyle='#457b9d'; g.fillRect(80,140,80,80);"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{head}{extra_head}
<title>Fixture</title>
<style>html,body{{margin:0;height:100%;background:{"#ffffff" if flaw.get("blank") else "#1d3557"};overflow:hidden}}
#c{{position:absolute;left:0;top:0;width:100%;height:100%}}
#cta{{position:absolute;left:25%;top:84%;width:50%;height:9%;background:{button};color:#fff;
font:bold 20px sans-serif;display:flex;align-items:center;justify-content:center}}</style>
</head><body>
<canvas id="c" width="360" height="640"></canvas>
<div id="cta">PLAY NOW</div>{body_extra}
<script>
var taps = 0, paused = false;
function draw(){{ var g = document.getElementById('c').getContext('2d'); {draw} }}
window.addEventListener('pointerdown', function(){{ taps += 1; }}, true);
document.getElementById('cta').addEventListener('pointerdown', function(e){{ if ({gate}) return; {cta}; }});
{start}
{script_extra}
</script></body></html>"""


def write(tmp: Path, network: str, *, kind: str | None = None, name: str | None = None, files: dict | None = None,
          config: object = None, **flaw: object) -> Path:  # fmt: skip
    """``kind``: "html" or "zip" (default: what the network takes). ``files``: extra zip members."""
    zip_networks = {"google", "tiktok"}
    kind = kind or ("zip" if network in zip_networks else "html")
    page = html(network, **flaw)
    if kind == "html":
        p = tmp / (name or ("index.html" if network == "unity" else f"{network}.html"))
        p.write_text(page, encoding="utf-8")
        return p
    p = tmp / (name or f"{network}.zip")
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(str(flaw.get("index", "index.html")), page)
        if network == "tiktok" and config != "absent":
            z.writestr("config.json", json.dumps(config if config is not None else {"playable_orientation": 0}))
        for n, data in (files or {}).items():
            z.writestr(n, data)
    return p
