"""The Kampanya page of `forge ui`: config starter, dry-run preview, the one primary button, the typed "Yayına al"
confirmation with the budget shown, pause, sync, and the end-to-end demo button. Mock network only."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml
from campaign_fixtures import config, deliver
from fastapi.testclient import TestClient

from creative_forge.campaigns.mock_server import MockNetwork
from creative_forge.campaigns.state import CampaignState
from creative_forge.ui.app import create_app

TOKEN = "tok-camp"
BASE = "http://127.0.0.1:8765"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "out"
    deliver(r / "deliver")
    return r


def client(root: Path, tmp_path: Path, **kw) -> TestClient:
    app = create_app(root, token=TOKEN, perf_db=tmp_path / "perf.sqlite", poster_cache=tmp_path / "posters",
                     campaign_state=tmp_path / "state", **kw)  # fmt: skip
    return TestClient(app, base_url=BASE)


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", "", html, flags=re.S))


def write_config(root: Path, **changes) -> Path:
    (root / "campaigns").mkdir(exist_ok=True)
    c = config(**changes)
    c["creatives"] = "../deliver"
    p = root / "campaigns" / "spring.yaml"
    p.write_text(yaml.safe_dump(c), encoding="utf-8")
    return p


def test_without_a_config_the_page_offers_the_starter_and_refuses_a_placeholder(root, tmp_path):
    c = client(root, tmp_path)
    page = c.get("/campaigns").text
    assert "Kampanya yapılandırması yok" in page and 'action="/campaigns/starter"' in page and "deliver" in page
    bad = c.post("/campaigns/starter", data={"token": TOKEN, "deliver": "deliver", "daily": "20", "total_cap": "200",
                 "max_daily": "30", "max_total": "300", "countries": "TR",
                 "store_url": "https://play.google.com/store/apps/details?id=DOLDUR"})  # fmt: skip
    assert bad.status_code == 422 and "placeholder" in bad.text
    over = c.post("/campaigns/starter", data={"token": TOKEN, "deliver": "deliver", "daily": "50", "total_cap": "200",
                  "max_daily": "30", "max_total": "300", "countries": "TR",
                  "store_url": "https://play.google.com/store/apps/details?id=com.forgetest.sortgame"})  # fmt: skip
    assert over.status_code == 422 and "max_daily" in over.text
    ok = c.post("/campaigns/starter", data={"token": TOKEN, "deliver": "deliver", "daily": "20", "total_cap": "200",
                "max_daily": "30", "max_total": "300", "countries": "TR",
                "store_url": "https://play.google.com/store/apps/details?id=com.forgetest.sortgame"},
                follow_redirects=False)  # fmt: skip
    assert ok.status_code == 303 and "/campaigns?config=" in ok.headers["location"]
    written = list((root / "campaigns").glob("*.yaml"))
    assert len(written) == 1 and "mode: mock" in written[0].read_text(encoding="utf-8")
    assert "Kampanyayı hazırla (duraklatılmış)" in c.get(ok.headers["location"]).text


def test_the_preview_the_primary_button_and_the_typed_activation(root, tmp_path):
    write_config(root)
    c = client(root, tmp_path)
    page = c.get("/campaigns").text
    assert "Kampanyayı hazırla (duraklatılmış)" in page and "Önizleme (kuru koşu)" in page
    assert "OUTCOME_APP_PROMOTION" in page and "BUDGET_MODE_TOTAL" in page and "APP_CAMPAIGN" in page
    assert page.count("+ yeni") == 33 and "Henüz kampanya yok" in page
    assert c.post("/campaigns/prepare", data={"config": "spring.yaml"}).status_code == 403  # no token
    assert not (tmp_path / "state" / "campaigns.sqlite").exists()
    done = c.post("/campaigns/prepare", data={"token": TOKEN, "config": "spring.yaml"})
    assert done.status_code == 200 and "33 yeni nesne" in done.text and "Hepsi duraklatılmış" in done.text
    assert done.text.count("= var") == 33 and "duraklatılmış" in done.text
    assert "YAYINA AL 40.00 USD" in done.text and "günlük 40,00 USD, toplam tavan 400,00 USD" in done.text
    mock = MockNetwork(tmp_path / "state" / "mock_network.json")
    assert {o["status"] for o in mock.objects("meta") if o["_kind"] in ("campaign", "adset", "ad")} == {"PAUSED"}
    recs = {r.network: r for r in CampaignState(tmp_path / "state").campaigns()}
    meta = recs["meta"].ref
    wrong = c.post("/campaigns/activate", data={"token": TOKEN, "config": "spring.yaml", "ref": meta, "phrase": "evet"})
    assert wrong.status_code == 409 and "İşlem tamamlanmadı" in wrong.text and "Onay ifadesi tutmadı" in wrong.text
    assert "type exactly" in wrong.text  # the engine's own words, folded under "Teknik ayrıntı"
    assert CampaignState(tmp_path / "state").campaign(meta).status == "PAUSED"
    on = c.post("/campaigns/activate", data={"token": TOKEN, "config": "spring.yaml", "ref": meta,
                "phrase": "YAYINA AL 40.00 USD"})  # fmt: skip
    assert on.status_code == 200 and "yayında" in on.text
    assert CampaignState(tmp_path / "state").campaign(meta).status == "ACTIVE"
    synced = c.post("/campaigns/sync", data={"token": TOKEN, "config": "spring.yaml"})
    assert synced.status_code == 200 and "3 kampanya ağlardan eşitlendi" in synced.text
    off = c.post("/campaigns/pause", data={"token": TOKEN, "config": "spring.yaml", "ref": meta})
    assert off.status_code == 200 and "duraklatıldı" in off.text
    assert CampaignState(tmp_path / "state").campaign(meta).status == "PAUSED"
    assert "zincir sağlam" in off.text


def test_a_placeholder_store_url_shows_the_refusal_and_no_button(root, tmp_path):
    write_config(root, store_url="https://play.google.com/store/apps/details?id=com.studio.TODO")
    page = client(root, tmp_path).get("/campaigns").text
    assert "Plan reddedildi" in page and "placeholder" in page and "Kampanyayı hazırla (duraklatılmış)" not in page


def test_a_live_network_needs_its_own_tick_box(root, tmp_path):
    c = config()
    c["networks"]["meta"]["mode"] = "live"
    (root / "campaigns").mkdir()
    c["creatives"] = "../deliver"
    (root / "campaigns" / "live.yaml").write_text(yaml.safe_dump(c), encoding="utf-8")
    cl = client(root, tmp_path)
    page = cl.get("/campaigns").text
    assert "CANLI hesap" in page and 'name="live" value="1" required' in page
    refused = cl.post("/campaigns/prepare", data={"token": TOKEN, "config": "live.yaml"})
    assert refused.status_code == 409 and "live mode" in refused.text


def test_turkish_and_english_pages_and_the_demo_button(root, tmp_path):
    write_config(root)
    c = client(root, tmp_path, campaign_demo_cmd=[sys.executable, "-c", "print('demo ok')"])
    tr = visible(c.get("/campaigns").text)
    for word in ("Campaigns", "Prepare", "Preview", "Switch on", "Pause", "Overview", "Performance"):
        assert word not in tr, word
    en = c.get("/campaigns?lang=en").text
    assert "Prepare the campaign (paused)" in en and "Preview (dry run)" in en
    started = c.post("/campaigns/demo", data={"token": TOKEN})
    assert started.status_code == 200 and "campaign-demo" in started.text
    c.app.state.campaign_demo.wait(30)
    assert "demo ok" in c.get("/campaigns/demo/status").text
    assert "Denemenin kreatiflerini gör" not in c.get("/campaigns/demo/status").text  # nothing rendered yet
    (root / "campaign-demo" / "render").mkdir(parents=True)
    (root / "campaign-demo" / "render" / "report.json").write_text("{}", encoding="utf-8")
    assert 'href="/creatives?run=campaign-demo%2Frender"' in c.get("/campaigns/demo/status").text
    after_restart = client(root, tmp_path / "again").get("/campaigns").text  # a new process: the demo is idle
    assert "Önceki denemenin çıktısı duruyor." in after_restart and "campaign-demo%2Frender" in after_restart


def test_starter_errors_are_turkish_and_the_form_keeps_what_was_chosen(root, tmp_path):
    deliver(root / "second" / "deliver")
    c = client(root, tmp_path)
    form = {"token": TOKEN, "deliver": "second/deliver", "daily": "50", "total_cap": "400", "max_daily": "30",
            "max_total": "300", "countries": "TUR",
            "store_url": "https://play.google.com/store/apps/details?id=DOLDUR"}  # fmt: skip
    page = visible(c.post("/campaigns/starter", data=form).text)
    assert "Bağlantıda yer tutucu var (doldur)" in page
    real = "https://play.google.com/store/apps/details?id=com.forgetest.sortgame"
    over = c.post("/campaigns/starter", data={**form, "store_url": real})
    assert over.status_code == 422 and "Ülkeler iki harfli ISO kodlarıyla yazılmalı" in over.text
    assert '<option value="second/deliver" selected>' in over.text
    over = c.post("/campaigns/starter", data={**form, "countries": "TR", "store_url": real})
    assert "Günlük bütçe (50), günlük üst sınırın (30) üstünde." in over.text
    assert "Toplam tavan (400), toplam üst sınırın (300) üstünde." in over.text


def test_each_campaign_action_redirects_so_a_reload_never_repeats_it(root, tmp_path):
    write_config(root)
    c = client(root, tmp_path)
    made = c.post("/campaigns/prepare", data={"token": TOKEN, "config": "spring.yaml"}, follow_redirects=False)
    assert made.status_code == 303 and made.headers["location"].startswith("/campaigns?config=spring.yaml&m=")
    assert "33 yeni nesne" in c.get(made.headers["location"]).text
    assert "33 yeni nesne" not in c.get(made.headers["location"]).text  # a reload shows the page, not the result again
    meta = {r.network: r for r in CampaignState(tmp_path / "state").campaigns()}["meta"]
    on = c.post("/campaigns/activate", data={"token": TOKEN, "config": "spring.yaml", "ref": meta.ref,
                "phrase": "yayına al 40,00 usd"}, follow_redirects=False)  # fmt: skip
    assert on.status_code == 303 and on.headers["location"].endswith("#status")
    shown = c.get(on.headers["location"]).text
    status = shown[shown.index('id="status"') :]
    assert "yayında.</b>" in status and "yayında.</b>" not in shown[: shown.index('id="status"')]  # next to the table
    again = c.post("/campaigns/activate", data={"token": TOKEN, "config": "spring.yaml", "ref": meta.ref,
                   "phrase": "YAYINA AL 40.00 USD"})  # fmt: skip
    assert again.status_code == 409 and "zaten yayında" in again.text
    for action in ("pause", "sync"):
        r = c.post(f"/campaigns/{action}", data={"token": TOKEN, "config": "spring.yaml", "ref": meta.ref},
                   follow_redirects=False)  # fmt: skip
        assert r.status_code == 303 and r.headers["location"].endswith("#status"), action
    assert CampaignState(tmp_path / "state").campaign(meta.ref).status == "PAUSED"
