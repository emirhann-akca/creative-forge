"""The ads app in a real browser, for what a test client cannot see: the headers a browser derives from the page.

Under ``Referrer-Policy: no-referrer`` Chromium sends ``Origin: null`` with a plain ``<form>`` POST, so the guard
refused every form that is not htmx (new test, CSV upload, every Kampanya button) with "cross-origin request refused".
These tests submit such forms from a served page, check that a failed htmx action says so instead of doing nothing,
and that a decision updates the page header. Playwright's Chromium in CI (CREATIVE_FORGE_REQUIRE_EXTRAS=1 makes a
missing browser a failure); locally CREATIVE_FORGE_BROWSER_CHANNEL=chrome drives an installed Chrome.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from conftest import need

from creative_forge.naming import CreativeIds
from creative_forge.review import load_review
from creative_forge.ui.app import create_app

TOKEN = "tok-browser"
CID = CreativeIds("spring", "a", "main", "main", "main", "9x16", "en")


def _run(root: Path) -> None:
    """One run with one export (the bytes need not be a video: nothing here plays it)."""
    run = root / "spring"
    run.mkdir(parents=True)
    (run / f"{CID.ad_name}.mp4").write_bytes(b"not a video" * 50)
    export = {"format": "9:16", "file": f"{CID.ad_name}.mp4", "creative_id": CID.creative_id, "ad_name": CID.ad_name,
              "tags": {"hook_type": "reveal"}, "status": "pass", "checks": []}  # fmt: skip
    concept = {"concept": "a", "variant": {"hook": "main"}, "timeline": {"total": 12.0}, "exports": [export]}
    (run / "report.json").write_text(json.dumps({"name": "spring", "concepts": [concept]}), encoding="utf-8")


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "out"
    _run(root)
    app = create_app(root, token=TOKEN, perf_db=tmp_path / "perf.sqlite", poster_cache=tmp_path / "posters",
                     campaign_state=tmp_path / "state", demo_cmd=[sys.executable, "-c", "pass"])  # fmt: skip
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("the UI did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{sock.getsockname()[1]}", root
    server.should_exit = True
    thread.join(10)
    sock.close()


@pytest.fixture
def page() -> Iterator:
    sync_api = need("playwright.sync_api")
    channel = os.environ.get("CREATIVE_FORGE_BROWSER_CHANNEL") or None
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel=channel, headless=True)
        except sync_api.Error as e:
            if os.environ.get("CREATIVE_FORGE_REQUIRE_EXTRAS") == "1":
                raise
            pytest.skip(f"no Chromium for the browser tests: {str(e).splitlines()[0]}")
        tab = browser.new_page()
        tab.set_default_timeout(15000)
        yield tab
        browser.close()


def test_a_plain_form_post_from_the_page_is_accepted(served, page, tmp_path):
    base, _ = served
    csv = tmp_path / "unity-ekim.csv"
    rows = f"Day,Ad name,Impr.,Clicks,Cost,Installs\n2026-11-10,{CID.ad_name},1000,10,5.00,8\n"
    csv.write_text(rows, encoding="utf-8")
    page.goto(base + "/settings")
    page.set_input_files("#csv-file", str(csv))
    page.select_option("#csv-network", "unity")
    with page.expect_navigation():
        page.click(".import-form button[type=submit]")
    assert "cross-origin request refused" not in page.content()
    assert "/settings?m=" in page.url and page.url.endswith("#import")
    assert "1 satır içe aktarıldı (unity-ekim.csv)" in page.inner_text("#import")
    # the test form too: a plain POST that answers with a redirect to the new test
    page.goto(base + "/tests/new")
    page.fill("#name", "Tarayıcı testi")
    page.check(f'input[name=control][value="{CID.creative_id}"]')
    with page.expect_navigation():
        page.click('button[name=start][value="0"]')
    assert "Kontrol" in page.content() and "En az bir varyant seç." in page.inner_text("main")


def test_a_decision_updates_the_card_and_the_header(served, page):
    base, root = served
    page.goto(f"{base}/runs/spring/exports/{CID.ad_name}.mp4")
    assert page.inner_text("#hdr-review") == "Onay bekliyor"
    page.click("#review button[value=approve]")
    page.wait_for_selector("#review >> text=Kaydedildi")
    assert page.inner_text("#hdr-review") == "Onaylı"
    assert load_review(root / "spring")["decisions"][f"{CID.ad_name}.mp4"]["decision"] == "approve"


def test_a_failed_htmx_action_says_so(served, page):
    base, root = served
    page.goto(f"{base}/runs/spring/exports/{CID.ad_name}.mp4")
    # the page of an app that has since restarted: its token no longer matches
    page.evaluate("document.body.setAttribute('hx-headers', JSON.stringify({'X-Forge-Token': 'from-before'}))")
    page.evaluate("document.querySelectorAll('input[name=token]').forEach(function (i) { i.value = 'from-before'; })")
    page.click("#review button[value=approve]")
    assert "sayfa eskimiş" in page.inner_text("#forge-toast")
    assert load_review(root / "spring")["decisions"] == {}
    # an app that is no longer running
    page.route("**/review", lambda route: route.abort())
    page.click("#forge-toast button.ghost")
    page.click("#review button[value=approve]")
    assert "uygulamaya ulaşılamadı" in page.inner_text("#forge-toast")
