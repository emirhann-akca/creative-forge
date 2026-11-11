"""`forge playable check|pack`: every rule against a good and a bad synthetic playable.

Static rules run everywhere. The browser rules need Playwright and a Chromium (CI installs one; locally
CREATIVE_FORGE_BROWSER_CHANNEL=chrome drives an installed Chrome); without them they skip, unless CI's
CREATIVE_FORGE_REQUIRE_EXTRAS=1 makes that a failure. The browser is muted.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from conftest import need
from playable_fixtures import STORE, write

from creative_forge.cli import main
from creative_forge.playable.commands import check_file
from creative_forge.playable.rules import FAIL, NETWORKS, PASS, SOURCES, UNCERTAIN, WARN
from creative_forge.playable.static import check, load


def rules(path: Path, network: str) -> dict[str, str]:
    return {f.rule: f.status for f in check(load(path), NETWORKS[network])}


def test_every_rule_names_its_source_and_check_date(tmp_path):
    for name, net in NETWORKS.items():
        for f in check(load(write(tmp_path, name)), net):
            assert f.checked == "2026-11-11", f
            if net.verified:
                assert f.source.startswith("https://"), (name, f)


@pytest.mark.parametrize("network", ["applovin", "unity", "meta", "google", "tiktok"])
def test_good_builds_pass_every_static_rule(tmp_path, network):
    bad = {r: s for r, s in rules(write(tmp_path, network), network).items() if s not in (PASS, "info")}
    assert bad == {}, bad


# (network, rule, good build kwargs, bad build kwargs, status of the bad build)
CASES = [
    ("applovin", "packaging", {}, {"kind": "zip"}, FAIL),
    ("google", "packaging", {}, {"kind": "html"}, FAIL),
    ("applovin", "size", {}, {"body": "<!--" + "x" * 5_300_000 + "-->"}, FAIL),
    ("meta", "size", {}, {"body": "<!--" + "x" * 2_060_000 + "-->"}, UNCERTAIN),  # 10^6 or 2^20 bytes per MB?
    ("meta", "size", {}, {"body": "<!--" + "x" * 2_200_000 + "-->"}, FAIL),
    ("meta", "zip-files", {"kind": "zip"}, {"kind": "zip", "files": {f"a{i}.png": b"x" for i in range(101)}}, FAIL),
    ("google", "file-types", {}, {"files": {"sound.mp3": b"ID3"}}, FAIL),
    ("google", "index", {}, {"index": "game/index.html"}, FAIL),
    ("tiktok", "config-json", {}, {"config": "absent"}, FAIL),
    ("tiktok", "config-json", {}, {"config": {"playable_orientation": 5}}, FAIL),
    ("tiktok", "mraid", {}, {"extra_head": '<script src="mraid.js"></script>'}, FAIL),
    ("tiktok", "mraid", {}, {"script": "if (window.mraid) mraid.getState();"}, UNCERTAIN),
    ("applovin", "mraid", {}, {"no_mraid": True, "cta": "window.open('x')"}, FAIL),
    ("unity", "mraid", {}, {"extra_head": '<script src="mraid.js"></script>'}, UNCERTAIN),
    ("applovin", "mraid-ready", {}, {"no_ready": True}, WARN),
    ("unity", "viewable", {}, {"no_viewable": True}, WARN),
    ("unity", "index-name", {"name": "index.html"}, {"name": "game.html"}, WARN),
    ("meta", "cta", {}, {"cta": "console.log('cta')"}, FAIL),
    ("google", "cta", {}, {"cta": "console.log('cta')"}, FAIL),
    ("google", "sdk-script", {}, {"head": '<meta name="ad.orientation" content="portrait">'}, FAIL),
    ("tiktok", "sdk-script", {}, {"head": ""}, FAIL),
    ("google", "ad-orientation", {}, {"head": '<script src="https://tpc.googlesyndication.com/x/exitapi.js"></script>'},
     FAIL),
    ("google", "self-closing", {}, {"extra_head": '<meta name="author" content="x" />'}, UNCERTAIN),
    ("unity", "external", {}, {"extra_head": '<script src="https://cdn.example.com/lib.js"></script>'}, FAIL),
    ("meta", "external", {}, {"extra_head": '<link rel="stylesheet" href="https://cdn.example.com/a.css">'}, WARN),
    ("google", "external", {"extra_head": '<link rel="stylesheet" href="https://fonts.googleapis.com/css?f=A">'},
     {"extra_head": '<link rel="stylesheet" href="https://cdn.example.com/a.css">'}, FAIL),
    ("applovin", "inlined", {}, {"body": '<img src="hero.png">'}, FAIL),
    ("google", "inlined", {"files": {"hero.png": b"png"}, "body": '<img src="hero.png">'},
     {"body": '<img src="hero.png">'}, FAIL),
    ("applovin", "store-url", {}, {"cta": "mraid.open('https://play.google.com/store/apps/details?id=TODO')"}, FAIL),
    ("applovin", "webgl-context-loss", {"script": "c.getContext('webgl'); c.addEventListener('webglcontextlost', f);"},
     {"script": "document.createElement('canvas').getContext('webgl');"}, WARN),
    ("unity", "xhr", {}, {"script": "fetch('data:,x');"}, UNCERTAIN),
    ("meta", "redirect", {}, {"script": "function out(){ window.location = 'https://example.com'; }"}, UNCERTAIN),
]  # fmt: skip


@pytest.mark.parametrize("network, rule, good, bad, status", CASES, ids=[f"{c[0]}-{c[1]}-{c[4]}" for c in CASES])
def test_each_static_rule_has_a_good_and_a_bad_fixture(tmp_path, network, rule, good, bad, status):
    (tmp_path / "g").mkdir()
    (tmp_path / "b").mkdir()
    ok = rules(write(tmp_path / "g", network, **good), network)
    assert ok.get(rule, PASS) == PASS, (rule, ok)
    found = rules(write(tmp_path / "b", network, **bad), network)
    assert found.get(rule) == status, (rule, found)


def test_unverified_networks_never_fail_on_their_own_rules(tmp_path):
    for name in ("liftoff", "moloco"):
        found = rules(write(tmp_path, name, kind="zip", name=f"{name}.zip"), name)
        assert found["packaging"] == UNCERTAIN and found["cta"] == UNCERTAIN
        assert FAIL not in found.values()
    mint = rules(write(tmp_path, "mintegral"), "mintegral")
    assert mint["cta"] == UNCERTAIN and mint["packaging"] == PASS
    assert rules(write(tmp_path, "mintegral", kind="zip", name="m.zip"), "mintegral")["packaging"] == FAIL


def test_ironsource_is_reported_as_sunset(tmp_path, capsys):
    r = check_file(write(tmp_path, "unity"), "ironsource", dynamic=False)
    assert r.status == FAIL and "2026-04-30" in r.findings[0].message and r.findings[0].source == SOURCES["ironsource"]


# ---------------------------------------------------------------------------------------------------- browser runs
def run(path: Path, network: str) -> dict[str, str]:
    need("playwright")
    from creative_forge.playable.dynamic import BrowserUnavailable

    try:
        r = check_file(path, network, render_timeout_s=4.0, settle_s=0.3, max_taps=6)
    except BrowserUnavailable as e:
        if os.environ.get("CREATIVE_FORGE_REQUIRE_EXTRAS") == "1":
            raise
        pytest.skip(str(e))
    return {f.rule: f.status for f in r.findings} | {"__trace__": json.dumps(r.trace)}


@pytest.mark.parametrize("network", ["applovin", "unity", "meta", "google", "tiktok"])
def test_good_builds_pass_in_the_browser(tmp_path, network):
    name = "Oyun Ş deneme.html" if network in ("applovin", "meta") else None  # served under a quoted URL
    found = run(write(tmp_path, network, name=name), network)
    trace = json.loads(found.pop("__trace__"))
    assert {r: s for r, s in found.items() if s not in (PASS, "info")} == {}, (found, trace["record"].get("calls"))
    calls = trace["record"]["calls"]
    assert calls and calls[0]["interactions"] >= 2  # reached by tapping, never before the second tap
    if network == "applovin":
        assert calls[0]["arg"] == STORE


BROWSER_CASES = [
    ("applovin", "js-errors", {"script": "undefinedFunction();"}, FAIL),
    ("unity", "render", {"blank": True}, FAIL),
    ("applovin", "cta-before-interaction", {"script": f"setTimeout(function(){{ mraid.open('{STORE}'); }}, 100);"},
     FAIL),
    ("applovin", "first-tap-store", {"first_tap_cta": True,
     "script": "window.addEventListener('pointerdown', function(){ if (taps === 1) mraid.open('" + STORE + "'); });"},
     FAIL),
    ("meta", "cta-call", {"cta": f"window.open('{STORE}')"}, FAIL),
    ("meta", "redirect", {"cta": f"window.open('{STORE}')"}, FAIL),
    ("unity", "network-requests", {"script": "fetch('https://tracker.example.com/ping').catch(function(){});"}, FAIL),
    ("google", "audio-before-tap", {"script": "try { var a = new AudioContext(); var o = a.createOscillator(); "
     "o.connect(a.destination); o.start(); } catch (e) {}"}, FAIL),
    ("applovin", "landscape", {"script": "window.addEventListener('resize', function(){ if (innerWidth > innerHeight) "
     "{ document.body.innerHTML = ''; document.body.style.background = '#fff'; throw new Error('portrait only'); } "
     "});"},
     FAIL),
]  # fmt: skip


@pytest.mark.parametrize("network, rule, bad, status", BROWSER_CASES, ids=[f"{c[0]}-{c[1]}" for c in BROWSER_CASES])
def test_each_browser_rule_catches_its_flaw(tmp_path, network, rule, bad, status):
    found = run(write(tmp_path, network, **bad), network)
    trace = found.pop("__trace__")
    assert found.get(rule) == status, (rule, found, trace[:600])


def test_cli_check_and_json(tmp_path, capsys):
    good, bad = write(tmp_path, "meta", name="good.html"), write(tmp_path, "meta", name="bad.html", cta="1")
    out = tmp_path / "r.json"
    code = main(["playable", "check", str(good), str(bad), "--network", "meta", "--network", "google",
                 "--static-only", "--json", str(out)])  # fmt: skip
    assert code == 1
    text = capsys.readouterr().out
    assert "PASS      meta      good.html" in text and "FAIL      google    good.html" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert len(data) == 4 and all(
        f["source"] and f["checked"] for r in data for f in r["findings"] if r["network"] != "x"
    )


def test_pack_inlines_assets_and_builds_each_network_format(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    (src / "game.js").write_text("window.forgeReady = true;", encoding="utf-8")
    (src / "style.css").write_text("#cta{background:url(img/bg.png)}", encoding="utf-8")
    (src / "img").mkdir()
    (src / "img" / "bg.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (src / "index.html").write_text(
        '<!doctype html><html><head><meta charset="utf-8"><script src="mraid.js"></script>'
        '<link rel="stylesheet" href="style.css"></head><body><img src="img/bg.png"><div id="cta">PLAY</div>'
        '<script src="game.js"></script><script>document.getElementById("cta").onclick=function(){forgeCTA("'
        + STORE
        + '")}; if (window.mraid) mraid.addEventListener("ready", function(){}); mraid.addEventListener'
        '("viewableChange", function(){});</script></body></html>',
        encoding="utf-8",
    )
    out = tmp_path / "packed"
    assert main(["playable", "pack", str(src), "--network", "unity", "--out", str(out), "--cta-adapter"]) == 0
    unity = (out / "src.unity" / "index.html").read_text(encoding="utf-8")
    assert (
        "window.forgeReady = true;" in unity and "data:image/png;base64," in unity and 'href="style.css"' not in unity
    )
    assert "window.forgeCTA=function(url){if (window.mraid && mraid.open)" in unity

    main(["playable", "pack", str(src), "--network", "google", "--out", str(out), "--cta-adapter"])
    with zipfile.ZipFile(out / "src.google.zip") as z:
        assert z.namelist() == ["index.html"]
        g = z.read("index.html").decode("utf-8")
    assert 'name="ad.orientation"' in g and "exitapi.js" in g and "ExitApi.exit()" in g

    main(["playable", "pack", str(src), "--network", "tiktok", "--out", str(out), "--tiktok-orientation", "1"])
    with zipfile.ZipFile(out / "src.tiktok.zip") as z:
        assert sorted(z.namelist()) == ["config.json", "index.html"]
        assert json.loads(z.read("config.json")) == {"playable_orientation": 1}
        assert "mraid.js" not in z.read("index.html").decode("utf-8")
    printed = capsys.readouterr().out
    assert "removed the mraid.js script tag" in printed and "static check:" in printed
