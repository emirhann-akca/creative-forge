"""``forge playable sample``: a small playable source to try the check / pack / publish loop with, per game.

A tap-to-sort board on a canvas (2D, no WebGL, no sound, no network): the first tap only starts it, three sorted
columns or the PLAY NOW button from the second tap on call ``window.forgeCTA(storeUrl)``, which ``forge playable pack
--cta-adapter`` maps to each network's exit API. With MRAID present it waits for ``ready`` and pauses while not
viewable. It is a stand-in to exercise the pipeline, not an ad: a real playable is built from the game itself.
"""

from __future__ import annotations

import html as _html
import json
from pathlib import Path

PAGE = """<!doctype html>
<html lang="{lang}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<style>html,body{{margin:0;height:100%;background:#1d3557;overflow:hidden;font-family:sans-serif}}
#c{{position:absolute;left:0;top:0;width:100%;height:100%;touch-action:none}}
#cta{{position:absolute;left:25%;top:84%;width:50%;height:9%;background:#e63946;color:#fff;border-radius:12px;
font:bold 20px sans-serif;display:flex;align-items:center;justify-content:center}}
#hint{{position:absolute;left:0;right:0;top:6%;text-align:center;color:#f1faee;font:bold 18px sans-serif}}</style>
</head><body>
<canvas id="c" width="360" height="640"></canvas>
<div id="hint">{hint}</div>
<div id="cta">{button}</div>
<script>
var STORE = {store};
var taps = 0, paused = false, sorted = 0, cols = [[0,1,2],[1,2,0],[2,0,1]], held = null;
var COLORS = ['#e9c46a', '#2a9d8f', '#f4a261'];
function size(){{ var c = document.getElementById('c'); c.width = innerWidth; c.height = innerHeight; draw(); }}
function draw(){{
  var c = document.getElementById('c'), g = c.getContext('2d'), w = c.width, h = c.height;
  g.fillStyle = '#1d3557'; g.fillRect(0, 0, w, h);
  for (var i = 0; i < 3; i++) {{
    var x = w * (0.2 + 0.3 * i), top = h * 0.25;
    g.fillStyle = '#f1faee'; g.fillRect(x - 34, top, 68, h * 0.42);
    for (var j = 0; j < cols[i].length; j++) {{
      g.fillStyle = COLORS[cols[i][j]];
      g.fillRect(x - 28, top + h * 0.42 - (j + 1) * 52, 56, 46);
    }}
  }}
}}
function done(){{ return cols.every(function(c){{ return c.length === 3 && c[0] === c[1] && c[1] === c[2]; }}); }}
function open(){{ if (window.forgeCTA) window.forgeCTA(STORE); }}
window.addEventListener('pointerdown', function(e){{
  taps += 1;
  if (paused || e.target.id === 'cta') return;
  var i = Math.max(0, Math.min(2, Math.floor(e.clientX / (innerWidth / 3))));
  if (held === null) {{ if (cols[i].length) held = cols[i].pop(); }}
  else {{ cols[i].push(held); held = null; }}
  draw();
  if (done() && taps > 1) open();
}}, true);
document.getElementById('cta').addEventListener('pointerdown', function(){{ if (taps < 2) return; open(); }});
function boot(){{ size(); window.addEventListener('resize', size); }}
if (window.mraid) {{
  mraid.addEventListener("viewableChange", function(v){{ paused = !v; }});
  if (mraid.getState() === "loading") mraid.addEventListener("ready", boot); else boot();
}} else {{ window.addEventListener('load', boot); }}
</script></body></html>
"""
TEXT = {
    "tr": {"hint": "Renkleri sırala!", "button": "OYNA"},
    "en": {"hint": "Sort the colours!", "button": "PLAY NOW"},
}


def sample_html(title: str, store_url: str, lang: str = "en") -> str:
    t = TEXT.get(lang, TEXT["en"])
    return PAGE.format(lang=lang, title=_html.escape(title), hint=_html.escape(t["hint"]),
                       button=_html.escape(t["button"]), store=json.dumps(store_url))  # fmt: skip


def write_sample(root: Path, name: str, *, title: str, store_url: str, app: str | None = None,
                 lang: str = "en") -> Path:  # fmt: skip
    """``<root>/playables/src/<name>/index.html`` (and the game's ``app.json``); refuses to overwrite."""
    from creative_forge.campaigns.store_url import parse_store_url
    from creative_forge.hub import write_app
    from creative_forge.make import slug

    parse_store_url(store_url)
    folder = Path(root) / "playables" / "src" / slug(name)
    if folder.exists():
        raise FileExistsError(f"{folder} exists")
    folder.mkdir(parents=True)
    (folder / "index.html").write_text(sample_html(title, store_url, lang), encoding="utf-8")
    if app:
        write_app(folder, app, title, store_url)
    return folder
