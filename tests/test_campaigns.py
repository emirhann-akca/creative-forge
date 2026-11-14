"""`forge campaign`: store URL and creative gates, the plan, idempotency, the audit chain, activation guards, sync and
recommendations, all against the local mock network (contract tests). No live API is called."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
import requests
from campaign_fixtures import config, deliver, ids

from creative_forge.campaigns.audit import AuditLog
from creative_forge.campaigns.config import CampaignConfigError, parse_config
from creative_forge.campaigns.creatives import CreativeGateError, load_creatives
from creative_forge.campaigns.engine import CampaignEngine, confirmation_phrase
from creative_forge.campaigns.mock_server import MockNetwork, MockServer
from creative_forge.campaigns.networks import SafetyError
from creative_forge.campaigns.networks.google import GoogleAdsAdapter
from creative_forge.campaigns.networks.meta import MetaAdapter
from creative_forge.campaigns.plan import build_plan, preview_text
from creative_forge.campaigns.state import CampaignState
from creative_forge.campaigns.store_url import StoreUrlError, parse_store_url
from creative_forge.cli import main
from creative_forge.perf.cells import CellStore
from creative_forge.perf.store import PerfStore

TODAY = date(2026, 11, 14)
START = date(2026, 11, 15)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    deliver(tmp_path / "deliver")
    return tmp_path


@pytest.fixture
def mock(work: Path):
    net = MockNetwork(work / "mock.json")
    server = MockServer(net)
    server.start()
    yield net, server.url
    server.stop()


def cfg_of(work: Path, **changes):
    return parse_config(config(**changes), work)


def engine(work: Path, url: str | None, **kw) -> CampaignEngine:
    return CampaignEngine(CampaignState(work / "state"), mock_url=url, perf_db=work / "perf.sqlite", today=TODAY,
                          sleep=lambda _s: None, **kw)  # fmt: skip


class NoNetwork(requests.Session):
    def request(self, *a, **kw):  # type: ignore[override]
        raise AssertionError(f"unexpected network call: {a[:2]}")


# ---------------------------------------------------------------------------------------------------- store URLs
@pytest.mark.parametrize(
    "url,platform,app_id",
    [
        ("https://play.google.com/store/apps/details?id=com.studio.sort_game", "android", "com.studio.sort_game"),
        ("https://play.google.com/store/apps/details?id=com.studio.game&hl=tr", "android", "com.studio.game"),
        ("https://apps.apple.com/tr/app/sort-game/id1234567890", "ios", "1234567890"),
        ("https://apps.apple.com/app/id987654321", "ios", "987654321"),
    ],
)
def test_store_url_accepts_real_listing_formats(url, platform, app_id):
    s = parse_store_url(url)
    assert (s.platform, s.app_id) == (platform, app_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://play.google.com/store/apps/details?id=DOLDUR",
        "https://play.google.com/store/apps/details?id=com.studio.TODO",
        "https://play.google.com/store/apps/details?id=com.example.game",
        "https://apps.apple.com/app/idXXXXXXXX",
        "https://apps.apple.com/app/id0000000000",
        "https://play.google.com/store/apps/details?id=game",
        "https://play.google.com/store/apps/details",
        "http://play.google.com/store/apps/details?id=com.studio.game",
        "https://example.com/app",
        "https://store.example.org/details?id=com.studio.game",
        "",
    ],
)
def test_store_url_refuses_placeholders_and_non_listings(url):
    with pytest.raises(StoreUrlError):
        parse_store_url(url)


def test_a_placeholder_store_url_stops_the_plan_before_anything_else(work):
    bad = cfg_of(work, store_url="https://play.google.com/store/apps/details?id=com.studio.DOLDUR")
    with pytest.raises(StoreUrlError, match="placeholder"):
        build_plan(bad, TODAY)
    other_platform = cfg_of(work, platform="ios")
    with pytest.raises(StoreUrlError, match="ios"):
        build_plan(other_platform, TODAY)


# ------------------------------------------------------------------------------------------------------- config
def test_config_refuses_budgets_above_the_limits(work):
    with pytest.raises(CampaignConfigError, match=r"max_daily"):
        cfg_of(work, budget={"daily": 60, "total_cap": 400})
    with pytest.raises(CampaignConfigError, match=r"max_total"):
        cfg_of(work, budget={"daily": 40, "total_cap": 900})
    with pytest.raises(CampaignConfigError, match=r"below one day"):
        cfg_of(work, budget={"daily": 40, "total_cap": 20})
    with pytest.raises(CampaignConfigError, match=r"limits"):
        parse_config({k: v for k, v in config().items() if k != "limits"}, work)


def test_the_schedule_follows_from_the_caps(work):
    cfg = cfg_of(work, budget={"daily": 30, "total_cap": 100, "start": "2026-11-20"})
    assert cfg.days == 3 and cfg.end_date() == date(2026, 11, 22)
    assert cfg_of(work, budget={"daily": 40, "total_cap": 400}).start_date(TODAY) == START  # default: tomorrow


# ---------------------------------------------------------------------------------------------- creatives gate
def test_only_approved_qa_clean_unchanged_exports_reach_a_campaign(work):
    folder = deliver(work / "mixed", ("ok", "override", "failed", "noreviewer", "changed", "missing", "qafail"))
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    by = {e["ad_name"].split(".")[2]: e for e in manifest["exports"]}
    by["override"]["overridden"] = True
    by["failed"]["failed_checks"] = ["true_peak"]
    by["noreviewer"]["reviewer"] = ""
    by["qafail"]["qa_status"] = "fail"
    (folder / by["changed"]["file"]).write_bytes(b"re-rendered after review")
    (folder / by["missing"]["file"]).unlink()
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    ok, excluded = load_creatives(folder)
    assert [c.concept for c in ok] == ["ok"]
    reasons = {e.file.split(".")[2]: e.reason for e in excluded}
    assert "override" in reasons["override"] and "true_peak" not in reasons["override"]
    assert "--override" in reasons["failed"] and "reviewer" in reasons["noreviewer"]
    assert "SHA-256" in reasons["changed"] and "missing" in reasons["missing"] and "fail" in reasons["qafail"]


def test_a_folder_without_a_publish_manifest_is_refused(tmp_path):
    with pytest.raises(CreativeGateError, match="forge publish"):
        load_creatives(tmp_path)


# --------------------------------------------------------------------------------------------------------- plan
def test_the_plan_is_offline_paused_and_keyed(work):
    cfg = cfg_of(work)
    plan = build_plan(cfg, TODAY)
    again = build_plan(cfg, TODAY)
    state = CampaignState(work / "state")
    d = plan.to_dict(state)
    assert [n["network"] for n in d["networks"]] == ["meta", "tiktok", "google"]
    keys = [o["key"] for n in d["networks"] for o in n["ops"]]
    assert keys == [o.key for n in again.networks for o in n.ops] and len(set(keys)) == len(keys)
    for n in d["networks"]:
        for o in n["ops"]:
            assert f"[cf:{o['key'][:10]}]" in o["name"] or o["kind"] in ("criteria",)
            assert o["state"] == "create"
    meta = d["networks"][0]["ops"]
    assert meta[0]["payload"]["status"] == "PAUSED" and meta[0]["payload"]["objective"] == "OUTCOME_APP_PROMOTION"
    assert meta[0]["payload"]["daily_budget"] == 4000 and meta[0]["payload"]["spend_cap"] == 40000
    assert all(o["payload"]["status"] == "PAUSED" for o in meta if o["kind"] in ("campaign", "adset", "ad"))
    tt = d["networks"][1]["ops"]
    assert tt[0]["payload"]["operation_status"] == "DISABLE" and tt[0]["payload"]["budget_mode"] == "BUDGET_MODE_TOTAL"
    assert tt[1]["payload"]["schedule_end_time"] == "2026-11-24 23:59:59"
    g = d["networks"][2]["ops"]
    camp = g[1]["payload"]["operations"][0]["create"]
    assert camp["status"] == "PAUSED" and camp["appCampaignSetting"]["appId"] == "com.forgetest.sortgame"
    text = preview_text(plan, state)
    assert "33 to create" in text and "nothing can spend before" in text


# -------------------------------------------------------------------------------------------------------- audit
def test_the_audit_chain_detects_an_edited_deleted_or_reordered_line(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    for i in range(4):
        log.append("event", n=i, access_token="secret-value")
    assert log.verify().ok and log.verify().records == 4
    assert "secret-value" not in log.path.read_text(encoding="utf-8")
    lines = log.path.read_text(encoding="utf-8").splitlines()
    edited = json.loads(lines[1]) | {"n": 99}
    for broken in (
        [lines[0], json.dumps(edited), *lines[2:]],
        [lines[0], *lines[2:]],
        [lines[1], lines[0], *lines[2:]],
    ):
        log.path.write_text("\n".join(broken) + "\n", encoding="utf-8")
        assert not log.verify().ok


# ----------------------------------------------------------------------------------------------- create (mock)
def test_create_makes_everything_paused_on_all_three_networks(work, mock):
    net, url = mock
    with engine(work, url) as eng:
        rep = eng.create(cfg_of(work))
    assert rep.ok and {n.network: n.created for n in rep.networks} == {"meta": 11, "tiktok": 11, "google": 11}
    created = {(network, o["_kind"]): o.get("_created_status") for network in ("meta", "tiktok", "google")
               for o in net.objects(network)}  # fmt: skip
    assert created[("meta", "campaign")] == created[("meta", "adset")] == created[("meta", "ad")] == "PAUSED"
    assert created[("tiktok", "campaign")] == created[("tiktok", "adgroup")] == created[("tiktok", "ad")] == "DISABLE"
    assert (
        created[("google", "campaign")]
        == created[("google", "ad_group")]
        == created[("google", "ad_group_ad")]
        == "PAUSED"
    )
    recs = CampaignState(work / "state").campaigns()
    assert {r.network for r in recs} == {"meta", "tiktok", "google"} and all(r.status == "PAUSED" for r in recs)
    assert all(r.cell_id for r in recs)  # a draft test cell per campaign, not started
    assert all(not CellStore(work / "perf.sqlite").get(r.cell_id).locked for r in recs)
    audit = AuditLog(work / "state" / "audit.jsonl")
    assert audit.verify().ok
    text = audit.path.read_text(encoding="utf-8")
    assert "mock-token" not in text and sum(1 for r in audit.records() if r["kind"] == "api_call") >= 33


def test_a_second_create_creates_nothing(work, mock):
    net, url = mock
    with engine(work, url) as eng:
        eng.create(cfg_of(work))
        before = {n: len(net.objects(n)) for n in ("meta", "tiktok", "google")}
        rep = eng.create(cfg_of(work))
    assert all(n.created == 0 and n.existing == 11 for n in rep.networks)
    assert {n: len(net.objects(n)) for n in ("meta", "tiktok", "google")} == before


@pytest.mark.parametrize(
    "network,path,mode",
    [
        ("meta", "/adsets", "500_after"),
        ("meta", "/campaigns", "drop_after"),
        ("tiktok", "adgroup/create", "500_after"),
        ("google", "campaigns:mutate", "500_after"),
        ("google", "adGroupAds:mutate", "drop_after"),
    ],
)
def test_an_answer_lost_after_the_network_created_the_object_never_duplicates_it(work, mock, network, path, mode):
    net, url = mock
    net.fault(network, path, mode)
    cfg = cfg_of(work)
    with engine(work, url, timeout_s=10) as eng:
        first = eng.create(cfg, [network])
        assert not first.ok and "may exist" in first.networks[0].stopped
        second = eng.create(cfg, [network])
    assert second.ok and second.networks[0].reconciled == 1
    kinds = [o["_kind"] for o in net.objects(network)]
    assert len(kinds) == {"meta": 11, "tiktok": 11, "google": 12}[network] and kinds.count("campaign") == 1
    assert kinds.count({"meta": "adset", "tiktok": "adgroup", "google": "ad_group"}[network]) == 1


def test_a_throttled_create_is_retried_and_created_once(work, mock):
    net, url = mock
    net.fault("meta", "/campaigns", "429", times=2)
    net.fault("tiktok", "campaign/create", "429")
    net.fault("google", "campaignBudgets:mutate", "429")
    with engine(work, url) as eng:
        rep = eng.create(cfg_of(work))
    assert rep.ok
    assert len(net.objects("meta", "campaign")) == len(net.objects("tiktok", "campaign")) == 1
    assert len(net.objects("google", "campaign_budget")) == 1


def test_a_currency_mismatch_stops_before_any_write(work, mock):
    net, url = mock
    net.set_account("meta", "act_1001", currency="TRY")
    with engine(work, url) as eng:
        rep = eng.create(cfg_of(work), ["meta"])
    assert "TRY" in rep.networks[0].stopped and not net.objects("meta")


def test_live_mode_is_refused_without_allow_live_and_makes_no_call(work):
    cfg = config()
    cfg["networks"]["meta"]["mode"] = "live"
    eng = CampaignEngine(CampaignState(work / "state"), perf_db=work / "p.sqlite", today=TODAY, session=NoNetwork())
    with pytest.raises(SafetyError, match="live"):
        eng.create(parse_config(cfg, work))
    assert not (work / "state" / "campaigns.sqlite").exists() or not CampaignState(work / "state").campaigns()


def test_sandbox_mode_refuses_accounts_that_are_not_sandboxes(work, mock, monkeypatch):
    net, url = mock
    monkeypatch.setenv("CREATIVE_FORGE_META_ADS_TOKEN", "mock-token")
    monkeypatch.setattr(MetaAdapter, "sandbox_base", f"{url}/meta")
    monkeypatch.setattr(GoogleAdsAdapter, "sandbox_base", f"{url}/google")
    secrets = {"token": "mock-token", "developer_token": "dev"}
    monkeypatch.setattr(GoogleAdsAdapter, "load_secrets", classmethod(lambda cls, mode: secrets))
    net.set_account("meta", "act_1001", funding_source="6001")
    net.set_account("google", "1234567890", test_account=False)
    c = config()
    c["networks"]["meta"]["mode"] = c["networks"]["google"]["mode"] = "sandbox"
    with engine(work, url) as eng:
        rep = eng.create(parse_config(c, work), ["meta", "google"])
    stops = {n.network: n.stopped for n in rep.networks}
    assert "payment method" in stops["meta"] and "test account" in stops["google"]
    assert not net.objects("meta") and not [o for o in net.objects("google") if o["_kind"] != "customer"]
    net.set_account("meta", "act_1001", funding_source="")
    net.set_account("google", "1234567890", test_account=True)
    with engine(work, url) as eng:
        assert eng.create(parse_config(c, work), ["meta", "google"]).ok


def test_a_campaign_read_back_active_after_create_is_paused_at_once(work, mock, monkeypatch):
    net, url = mock
    real = MetaAdapter.read
    monkeypatch.setattr(MetaAdapter, "read", lambda self, rec: real(self, rec) | {"status": "ACTIVE"})
    with engine(work, url) as eng, pytest.raises(SafetyError, match="read back as ACTIVE"):
        eng.create(cfg_of(work), ["meta"])
    assert net.objects("meta", "campaign")[0]["status"] == "PAUSED"
    assert any(r["kind"] == "safety_pause" for r in AuditLog(work / "state" / "audit.jsonl").records())


def test_manual_networks_get_an_upload_pack_and_no_api_call(work, mock):
    net, url = mock
    c = config()
    c["networks"] = {"meta": c["networks"]["meta"], "applovin": {}, "unity": {}, "mintegral": {}}
    with engine(work, url) as eng:
        rep = eng.create(parse_config(c, work))
    assert {p.name for p in rep.packs} == {"applovin", "unity", "mintegral"}
    pack = json.loads((rep.packs[0] / "pack.json").read_text(encoding="utf-8"))
    assert pack["status_at_creation"] == "PAUSED" and len(pack["files"]) == 3 and "paused" in pack["why_manual"]
    assert {c for c, _m, _p in net.calls} == {"meta"}


# ------------------------------------------------------------------------------------------ activate, pause, sync
def _created(work, url, **cfg_changes):
    eng = engine(work, url)
    cfg = cfg_of(work, **cfg_changes)
    eng.create(cfg)
    return eng, cfg, {r.network: r for r in eng.state.campaigns()}


def test_activation_needs_the_typed_phrase_the_caps_and_an_unchanged_budget(work, mock):
    net, url = mock
    eng, cfg, recs = _created(work, url)
    meta = recs["meta"]
    assert confirmation_phrase(meta) == "YAYINA AL 40.00 USD"
    for wrong in ("", "YAYINA AL", "YAYINA AL 4000 USD", "YAYINA AL 40.00 TRY", "evet"):
        with pytest.raises(SafetyError, match="type exactly"):
            eng.activate(meta.ref, wrong, cfg)
    tight = cfg_of(work, limits={"max_daily": 45, "max_total": 300}, budget={"daily": 30, "total_cap": 300})
    with pytest.raises(SafetyError, match="limits"):
        eng.activate(meta.ref, "YAYINA AL 40.00 USD", tight)
    camp = net.objects("meta", "campaign")[0]
    camp["daily_budget"] = "9000"  # someone raised the budget in Ads Manager
    with pytest.raises(SafetyError, match="changed outside forge"):
        eng.activate(meta.ref, "yayına al 40,00 usd", cfg)
    camp["daily_budget"] = "4000"
    assert all(o["status"] == "PAUSED" for o in net.objects("meta") if o["_kind"] in ("campaign", "adset", "ad"))
    rec = eng.activate(meta.ref, "yayına al 40,00 usd", cfg)
    assert rec.status == "ACTIVE"
    assert all(o["status"] == "ACTIVE" for o in net.objects("meta") if o["_kind"] in ("campaign", "adset", "ad"))
    assert CellStore(work / "perf.sqlite").get(rec.cell_id).locked
    with pytest.raises(SafetyError, match="already active"):
        eng.activate(meta.ref, "YAYINA AL 40.00 USD", cfg)
    kinds = [r["kind"] for r in eng.audit.records()]
    assert kinds.count("activate_refused") == 1 and kinds.count("activate_confirmed") == 1
    eng.close()


def test_nothing_is_delivered_while_paused_and_pause_stops_delivery(work, mock):
    net, url = mock
    eng, cfg, recs = _created(work, url)
    assert net.advance(3, START) == 0
    eng.activate(recs["tiktok"].ref, "ACTIVATE 40.00 USD", cfg)
    assert net.advance(2, START) > 0 and {r["network"] for r in net.data["delivery"]} == {"tiktok"}
    eng.pause(recs["tiktok"].ref)
    n = len(net.data["delivery"])
    net.advance(2)
    assert len(net.data["delivery"]) == n
    eng.close()


def test_sync_imports_delivery_and_recommends_without_acting(work, mock):
    net, url = mock
    net.set_truth("spring.alpha", 6.5)
    net.set_truth("spring.beta", 10.0)
    net.set_truth("spring.gamma", 3.5)
    eng, cfg, recs = _created(work, url, control="alpha")
    for r in recs.values():
        eng.activate(r.ref, confirmation_phrase(r), cfg)
    net.advance(10, START)
    eng.today = date(2026, 11, 24)
    results = {s.campaign.network: s for s in eng.sync(cfg=cfg)}
    store = PerfStore(work / "perf.sqlite")
    for network, level in (("meta", "ad"), ("tiktok", "ad"), ("google", "video_asset")):
        rows = store.rows("SELECT DISTINCT creative_id FROM perf_rows WHERE network = ? AND level = ?",
                          (network, level))  # fmt: skip
        assert {r["creative_id"] for r in rows} == {ids(c).creative_id for c in ("alpha", "beta", "gamma")}, network
        actions = {
            r.target.split(".")[2]: r.action for r in results[network].recommendations if r.target.startswith("cf1")
        }
        assert actions == {"beta": "scale", "gamma": "pause_ad"}, network
        assert not any(r.applied for r in results[network].recommendations)
    assert all(o["status"] == "ACTIVE" for o in net.objects("meta", "ad"))
    assert net.objects("meta", "campaign")[0]["daily_budget"] == "4000"  # scale is never applied
    eng.close()


def test_auto_pause_pauses_killed_ads_only_when_enabled_and_never_scales(work, mock):
    net, url = mock
    net.set_truth("spring.alpha", 6.5)
    net.set_truth("spring.beta", 10.0)
    net.set_truth("spring.gamma", 3.5)
    eng, cfg, recs = _created(work, url, control="alpha", automation={"auto_pause": True})
    for r in recs.values():
        eng.activate(r.ref, confirmation_phrase(r), cfg)
    net.advance(10, START)
    eng.today = date(2026, 11, 24)
    results = {s.campaign.network: s for s in eng.sync(cfg=cfg)}
    status = {o["name"].split(".")[2]: o["status"] for o in net.objects("meta", "ad")}
    assert status == {"alpha": "ACTIVE", "beta": "ACTIVE", "gamma": "PAUSED"}
    assert {o["ad_name"].split(".")[2]: o["operation_status"] for o in net.objects("tiktok", "ad")}[
        "gamma"
    ] == "DISABLE"
    google = [r for r in results["google"].recommendations if r.action == "pause_ad"]
    assert google and not google[0].applied and "cannot be paused on its own" in google[0].reason
    assert net.objects("meta", "campaign")[0]["daily_budget"] == "4000"
    assert any(r["kind"] == "auto_pause" for r in eng.audit.records())
    eng.close()


def test_a_campaign_at_its_cap_is_flagged_and_paused_only_with_auto_pause(work, mock):
    net, url = mock
    eng, cfg, recs = _created(work, url, budget={"daily": 40, "total_cap": 80, "start": "2026-11-15"})
    eng.activate(recs["google"].ref, confirmation_phrase(recs["google"]), cfg)
    net.advance(3, START)  # Google has no total budget: the mock keeps spending until the end date (2 days)
    eng.today = date(2026, 11, 17)
    s = eng.sync(recs["google"].ref, cfg)[0]
    assert any(r.action in ("pause_campaign", "alert") for r in s.recommendations)
    assert net.objects("google", "campaign")[0]["status"] == "ENABLED"
    eng.close()


# ----------------------------------------------------------------------------------------------- mock contract
def test_the_mock_enforces_auth_required_fields_and_duplicates(mock):
    _, url = mock
    r = requests.post(f"{url}/meta/v27.0/act_1/campaigns", data={"name": "x", "objective": "OUTCOME_APP_PROMOTION",
                      "status": "PAUSED"}, headers={"Authorization": "Bearer mock-token"}, timeout=5)  # fmt: skip
    assert r.status_code == 400 and "special_ad_categories" in r.json()["error"]["message"]
    r = requests.post(f"{url}/tiktok/open_api/v1.3/campaign/create/", json={}, timeout=5)
    assert r.json()["code"] == 40105
    h = {"Authorization": "Bearer mock-token", "developer-token": "d"}
    base = f"{url}/google/v26/customers/1234567890"
    budget = {"operations": [{"create": {"name": "b", "amountMicros": "1000000", "explicitlyShared": False}}]}
    b = requests.post(f"{base}/campaignBudgets:mutate", json=budget, headers=h, timeout=5).json()
    body = {"operations": [{"create": {"name": "same", "campaignBudget": b["results"][0]["resourceName"],
            "advertisingChannelType": "MULTI_CHANNEL", "advertisingChannelSubType": "APP_CAMPAIGN",
            "appCampaignSetting": {"appId": "com.a.b", "appStore": "GOOGLE_APP_STORE"}}}]}  # fmt: skip
    first = requests.post(f"{base}/campaigns:mutate", json=body, headers=h, timeout=5)
    assert first.status_code == 200
    assert requests.post(f"{base}/campaigns:mutate", json=body, headers=h, timeout=5).status_code == 400
    search = requests.post(f"{base}/googleAds:search", headers=h, timeout=5, json={
        "query": "SELECT campaign.status FROM campaign WHERE campaign.name = 'same'"}).json()  # fmt: skip
    assert search["results"][0]["campaign"]["status"] == "ENABLED"  # the documented default when status is left out


# ------------------------------------------------------------------------------------------------------- CLI
def test_cli_plan_create_status_activate_pause_and_audit(work, mock, capsys):
    net, url = mock
    path = work / "campaign.yaml"
    import yaml

    path.write_text(yaml.safe_dump(config()), encoding="utf-8")
    common = ["--state", str(work / "state"), "--db", str(work / "perf.sqlite"), "--mock-url", url]
    assert main(["campaign", "plan", str(path), *common]) == 0
    out = capsys.readouterr().out
    assert "dry run: nothing was sent" in out and not net.calls
    assert main(["campaign", "plan", str(path), "--json", *common]) == 0
    assert json.loads(capsys.readouterr().out)["networks"][0]["ops"][0]["payload"]["status"] == "PAUSED"
    assert main(["campaign", "create", str(path), "--network", "meta", *common]) == 0
    assert "PAUSED" in capsys.readouterr().out
    ref = CampaignState(work / "state").campaigns()[0].ref
    assert main(["campaign", "activate", ref, "--confirm", "YAYINA AL 4.00 USD", *common]) == 2
    assert "type exactly" in capsys.readouterr().err
    assert main(["campaign", "activate", ref, "--confirm", "YAYINA AL 40.00 USD", *common]) == 0
    assert main(["campaign", "status", *common]) == 0
    assert "ACTIVE" in capsys.readouterr().out
    assert main(["campaign", "pause", ref, *common]) == 0
    assert main(["campaign", "sync", *common]) == 0
    assert main(["campaign", "audit", *common]) == 0
    assert "chain intact" in capsys.readouterr().out
    bad = config(store_url="https://play.google.com/store/apps/details?id=com.studio.DOLDUR")
    path.write_text(yaml.safe_dump(bad), encoding="utf-8")
    assert main(["campaign", "create", str(path), *common]) == 2
    assert "placeholder" in capsys.readouterr().err


def test_the_shipped_example_parses_and_is_refused_until_its_store_url_is_filled():
    from creative_forge.campaigns.config import load_config

    cfg = load_config(Path(__file__).resolve().parents[1] / "examples" / "campaign.yaml")
    assert [n for n, net in cfg.api_networks() if net.mode == "mock"] == ["meta", "tiktok", "google"]
    assert cfg.manual_networks() == ["applovin"] and cfg.days == 10
    with pytest.raises(StoreUrlError, match="DOLDUR"):
        build_plan(cfg, TODAY)


def test_create_again_after_activation_leaves_the_live_campaign_alone(work, mock):
    net, url = mock
    eng, cfg, recs = _created(work, url)
    eng.activate(recs["meta"].ref, "YAYINA AL 40.00 USD", cfg)
    rep = eng.create(cfg)
    assert rep.ok and all(n.created == 0 for n in rep.networks)
    assert net.objects("meta", "campaign")[0]["status"] == "ACTIVE"
    assert eng.state.campaign(recs["meta"].ref).status == "ACTIVE"
    eng.close()


def test_live_mode_needs_the_switch_to_create_and_activate_but_not_to_pause_or_sync(work, mock, monkeypatch):
    net, url = mock
    monkeypatch.setattr(MetaAdapter, "live_base", f"{url}/meta")  # the "live" host is the mock in this test
    monkeypatch.setenv("CREATIVE_FORGE_META_ADS_TOKEN", "mock-token")
    c = config()
    c["networks"] = {"meta": c["networks"]["meta"] | {"mode": "live"}}
    cfg = parse_config(c, work)
    with engine(work, url) as eng, pytest.raises(SafetyError, match="live"):
        eng.create(cfg)
    assert not net.objects("meta")
    with engine(work, url, allow_live=True) as eng:
        assert eng.create(cfg).ok
    ref = CampaignState(work / "state").campaigns()[0].ref
    with engine(work, url) as eng:
        with pytest.raises(SafetyError, match="live"):
            eng.activate(ref, "YAYINA AL 40.00 USD", cfg)
        assert eng.sync(ref)[0].info["status"] == "PAUSED"
        assert eng.pause(ref).status == "PAUSED"
