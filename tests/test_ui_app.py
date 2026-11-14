"""The ads app (`forge ui` / `forge app`): Turkish and English pages, empty states, the gallery with performance
overlays, approval through review.py, test cells (sufficiency, verdicts, locked rules), the CSV upload, settings that
never show a secret, and the shared design system files."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from creative_forge.naming import CreativeIds
from creative_forge.perf.cells import (
    DECIDED,
    KEEP,
    KILL,
    KILL_EARLY,
    PENDING,
    SCALE,
    SCREENED,
    WAITING,
    Arm,
    CellError,
    CellStore,
    LockedError,
    Rule,
    evaluate,
    judge,
)
from creative_forge.perf.store import ImportSummary, PerfRow, PerfStore
from creative_forge.review import load_review
from creative_forge.ui.app import create_app
from creative_forge.ui.i18n import T, fmt_num, fmt_pct

TOKEN = "tok-123"
BASE = "http://127.0.0.1:8765"
HERE = Path(__file__).resolve().parent
UI = HERE.parent / "creative_forge" / "ui"
PAGES = ["/", "/creatives", "/concepts", "/tests", "/tests/new", "/perf", "/playables", "/campaigns", "/settings"]


def ids(concept: str, hook: str = "main", fmt: str = "9x16", lang: str = "en") -> CreativeIds:
    return CreativeIds("spring", concept, hook, "main", "main", fmt, lang)


def check(name: str, status: str) -> dict:
    return {"check": name, "status": status, "value": 1.5, "threshold": "<= 2", "unit": "s", "message": ""}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """One run with three exports carrying real creative IDs: two of concept a (hooks main / oops), one of b that
    failed QA."""
    run = tmp_path / "out" / "spring"
    run.mkdir(parents=True)
    exports = [(ids("a"), "pass", "near-miss"), (ids("a", "oops"), "warn", "fail-state"), (ids("b"), "fail", "reveal")]
    concepts = []
    for cid, status, hook_type in exports:
        (run / f"{cid.ad_name}.mp4").write_bytes(cid.creative_id.encode() * 50)
        tags = {"hook_type": hook_type, "mechanic_shown": ["goods-sort"], "format": "9x16"}
        concepts.append(
            {
                "concept": cid.concept,
                "variant": {"hook": cid.hook, "segment": "main", "end_card": "main", "language": "en"},
                "hypothesis": f"hypothesis of {cid.concept}" if cid.concept == "a" else None,
                "timeline": {"total": 12.0},
                "pre_checks": [check("first_frame_text", "pass")],
                "exports": [
                    {
                        "format": "9:16",
                        "file": f"{cid.ad_name}.mp4",
                        "creative_id": cid.creative_id,
                        "ad_name": cid.ad_name,
                        "tags": tags,
                        "status": status,
                        "checks": [check("true_peak", status)],
                    }
                ],
            }
        )
    (run / "report.json").write_text(json.dumps({"name": "spring", "concepts": concepts}), encoding="utf-8")
    return tmp_path / "out"


A, OOPS, B = ids("a"), ids("a", "oops"), ids("b")


def rows(cid: CreativeIds, impressions: float, installs: float, days: int = 5, network: str = "meta") -> list:
    start = date.today() - timedelta(days=days)
    day = [(start + timedelta(days=d)).isoformat() for d in range(days)]
    n, i = impressions / days, installs / days
    key = f"{cid.creative_id}-ad"
    return [
        PerfRow(network, "act_1", "ad", d, key, cid.ad_name, impressions=n, clicks=n / 100, spend=i * 1.5, installs=i)
        for d in day
    ]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    store = PerfStore(tmp_path / "perf.sqlite")
    store.upsert(rows(A, 60000, 300) + rows(OOPS, 52000, 330) + rows(B, 900, 4), ImportSummary("meta", "act_1", "ad"))
    return store.path


def client_for(root: Path, db: Path, tmp_path: Path, **kw) -> TestClient:
    app = create_app(root, token=TOKEN, perf_db=db, poster_cache=tmp_path / "posters", **kw)
    return TestClient(app, base_url=BASE)


@pytest.fixture
def client(root: Path, db: Path, tmp_path: Path) -> TestClient:
    return client_for(root, db, tmp_path)


# ------------------------------------------------------------------------------------------------------- language
def test_every_page_renders_in_turkish_by_default_and_in_english(client):
    for path in PAGES:  # no cookie yet: Turkish
        tr = client.get(path)
        assert tr.status_code == 200, path
        assert '<html lang="tr">' in tr.text and "Genel bakış" in tr.text and "Ayarlar" in tr.text, path
    for path in PAGES:
        en = client.get(path + "?lang=en")
        assert en.status_code == 200, path
        assert '<html lang="en">' in en.text and "Overview" in en.text and "Settings" in en.text, path


def test_turkish_pages_carry_no_english_ui_words(client):
    english = ["Overview", "Creatives", "Settings", "Approve", "Reject", "Waiting for review", "No data", "Import",
               "Performance", "Concepts", "Campaigns", "Run the demo", "insufficient data"]  # fmt: skip
    for path in [*PAGES, f"/runs/spring/exports/{A.ad_name}.mp4", "/runs/spring"]:
        text = re.sub(r"<script.*?</script>", "", client.get(path).text, flags=re.S)
        visible = re.sub(r"<[^>]+>", " ", text)
        for word in english:
            assert word not in visible, f"{path}: {word!r}"


def test_the_language_choice_is_remembered(client):
    assert client.get("/?lang=en").cookies.get("forge_lang") == "en"
    assert '<html lang="en">' in client.get("/creatives").text
    client.get("/?lang=tr")
    assert '<html lang="tr">' in client.get("/creatives").text


def test_both_dictionaries_have_the_same_keys_and_every_template_key_exists():
    assert set(T["tr"]) == set(T["en"])
    for k, v in T["tr"].items():
        assert type(v) is type(T["en"][k]), k
        if isinstance(v, str):
            assert v.count("%s") == T["en"][k].count("%s"), k
    used: set[str] = set()
    families: set[str] = set()
    for f in (UI / "templates").glob("*.html"):
        text = f.read_text(encoding="utf-8")
        used |= set(re.findall(r"\bt\.([a-z0-9_]+)", text))
        families |= set(re.findall(r't\["([a-z0-9_]+)" ~', text))
    used |= set(re.findall(r"\bt\.([a-z0-9_]+)", (UI / "app.py").read_text(encoding="utf-8")))
    missing = sorted(k for k in used - {"get", "items", "keys"} if k not in T["tr"])  # dict methods are not keys
    assert not missing, missing
    for prefix in families:
        assert any(k.startswith(prefix) for k in T["tr"]), prefix
    for status in ("pass", "warn", "fail", "skip"):
        assert f"qa_{status}" in T["tr"]
    for v in ("winner", "loser", "insufficient_data", "no_clear_difference"):
        assert f"vd_{v}" in T["tr"]
    for v in (PENDING, KILL_EARLY, SCALE, KILL, KEEP):
        assert f"oc_{v}" in T["tr"]


def test_turkish_number_and_percent_formats():
    assert fmt_num(1234.5, "tr", 2) == "1.234,50" and fmt_num(1234.5, "en", 2) == "1,234.50"
    assert fmt_num(None, "tr") == "—" and fmt_num("360x640", "tr") == "360x640"
    assert fmt_pct(26.94, "tr", 1, True) == "+%26,9" and fmt_pct(-53.3, "en", 1, True) == "−53.3%"


# ---------------------------------------------------------------------------------------------------- empty states
def test_empty_states_offer_a_next_action(tmp_path):
    (tmp_path / "out").mkdir()
    c = client_for(tmp_path / "out", tmp_path / "none.sqlite", tmp_path)
    home = c.get("/").text
    assert "Henüz koşu yok" in home and "Demo verisiyle dene" in home and 'hx-post="/demo"' in home
    assert "Performans verisi içe aktar" in home and 'href="/settings#import"' in home
    assert "Demo verisiyle dene" in c.get("/creatives").text
    perf = c.get("/perf").text
    assert "Henüz performans verisi yok" in perf and "Performans verisi içe aktar" in perf
    assert "Henüz test yok" in c.get("/tests").text
    assert "Seçilecek kreatif yok" in c.get("/tests/new").text
    assert "Henüz denetim sonucu yok" in c.get("/playables").text
    assert "Henüz konsept yok" in c.get("/concepts").text
    assert not (tmp_path / "none.sqlite").exists()  # reading pages never creates the store


# ---------------------------------------------------------------------------------------------- gallery and detail
def test_gallery_shows_performance_confidence_qa_review_and_tags(client):
    page = client.get("/creatives").text
    for cid in (A, OOPS, B):
        assert f"/runs/spring/poster/{cid.ad_name}.mp4" in page and f"/runs/spring/media/{cid.ad_name}.mp4" in page
    assert "IPM 5,0" in page and "IPM 6,3" in page  # 300 / 60,000 and 330 / 52,000 per mille, Turkish decimals
    assert "kazanan" in page  # 6.35 vs the rest, non-overlapping intervals
    assert "örneklem yetersiz" in page  # b: 900 impressions
    assert "QA · kaldı" in page and "Onay bekliyor" in page and "fail-state" in page and "goods-sort" in page
    assert "IPM 6.3" in client.get("/creatives?lang=en").text


def test_gallery_filters(client):
    only_failed = client.get("/creatives?qa=fail").text
    assert B.ad_name in only_failed and A.ad_name not in only_failed
    by_hook = client.get("/creatives?hook=fail-state").text
    assert OOPS.ad_name in by_hook and A.ad_name not in by_hook
    nothing = client.get("/creatives?review=approve").text
    assert "Bu süzgeçle kreatif yok" in nothing


def test_detail_shows_lineage_checks_and_performance(client):
    page = client.get(f"/runs/spring/exports/{A.ad_name}.mp4").text
    assert "hypothesis of a" in page and A.creative_id in page and A.ad_name in page
    assert "first_frame_text" in page and "true_peak" in page and "<b>meta</b>" in page
    assert client.get("/runs/spring/exports/nope.mp4").status_code == 404
    assert client.get("/runs/../exports/x.mp4").status_code == 404


def test_approval_from_the_detail_view_is_bound_to_the_file_hash(client, root):
    run = root / "spring"
    file = f"{A.ad_name}.mp4"
    r = client.post(
        "/runs/spring/review",
        data={"file": file, "decision": "approve", "note": "net", "reviewer": "ayşe", "view": "detail"},
        headers={"X-Forge-Token": TOKEN},
    )
    assert r.status_code == 200 and 'id="review"' in r.text and "Kaydedildi" in r.text and "Onaylı" in r.text
    d = load_review(run)["decisions"][file]
    assert d["decision"] == "approve" and d["reviewer"] == "ayşe" and len(d["sha256"]) == 64
    (run / file).write_bytes(b"re-rendered")  # the decision no longer applies to these bytes
    assert "Onay geçersiz" in client.get("/creatives").text
    assert "Onay geçersiz" in client.get(f"/runs/spring/exports/{file}").text


def test_review_posts_need_the_token_and_a_loopback_host(client, root):
    data = {"file": f"{A.ad_name}.mp4", "decision": "approve", "view": "detail"}
    assert client.post("/runs/spring/review", data=data).status_code == 403
    assert client.post("/runs/spring/review", data=data, headers={"X-Forge-Token": "guess"}).status_code == 403
    evil = TestClient(client.app, base_url="http://evil.example:8765")
    assert evil.post("/runs/spring/review", data={**data, "token": TOKEN}).status_code == 400
    assert load_review(root / "spring")["decisions"] == {}
    bad = client.post("/runs/spring/review", data={**data, "decision": "reject", "note": " "},
                      headers={"X-Forge-Token": TOKEN})  # fmt: skip
    assert bad.status_code == 422 and "Ret için not gerekli" in bad.text and 'id="review"' in bad.text


def test_poster_frames(tmp_path, good_video):
    run = tmp_path / "out" / "r"
    run.mkdir(parents=True)
    cid = ids("p")
    (run / f"{cid.ad_name}.mp4").write_bytes(good_video.read_bytes())
    (run / "bad.mp4").write_bytes(b"not a video")
    exports = [{"format": "9:16", "file": f"{cid.ad_name}.mp4", "status": "pass", "checks": []},
               {"format": "9:16", "file": "bad.mp4", "status": "pass", "checks": []}]  # fmt: skip
    report = {"concepts": [{"concept": "p", "pre_checks": [], "timeline": {"total": 6}, "exports": exports}]}
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    c = client_for(tmp_path / "out", tmp_path / "none.sqlite", tmp_path)
    r = c.get(f"/runs/r/poster/{cid.ad_name}.mp4")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert len(list((tmp_path / "posters").glob("*.jpg"))) == 1
    assert c.get(f"/runs/r/poster/{cid.ad_name}.mp4").content == r.content  # cached
    assert c.get("/runs/r/poster/bad.mp4").status_code == 404
    assert c.get("/runs/r/poster/report.json").status_code == 404


def test_concept_board_groups_variants(client):
    page = client.get("/concepts").text
    assert "hypothesis of a" in page and "Hipotez yazılmamış" in page  # b has none
    assert page.count('class="variant"') == 3  # a/main, a/oops, b/main
    assert "near-miss" in page and "fail-state" in page and "goods-sort" in page


# ---------------------------------------------------------------------------------------------- test cell logic
def arm(cid: str, installs: float, impressions: float) -> Arm:
    return Arm(cid, None, impressions, installs, installs * 1.5, installs, impressions, 1000.0)


def test_below_the_first_screen_there_is_no_decision_whatever_the_numbers_say():
    rule = Rule()
    o = judge(arm("v", 30, 1000), arm("c", 300, 60000), rule)  # IPM 30 vs 5: huge, but only 30 installs
    assert (o.stage, o.verdict, o.installs_missing, o.next_threshold) == (WAITING, PENDING, 20, 50)
    o = judge(arm("v", 120, 20000), arm("c", 40, 8000), rule)  # the control is the one behind
    assert o.verdict == PENDING and o.installs_missing == 10


def test_first_screen_drops_only_a_clearly_worse_variant():
    rule = Rule()
    early = judge(arm("v", 70, 30000), arm("c", 300, 60000), rule)  # IPM 2.3 vs 5.0, intervals apart
    assert (early.stage, early.verdict) == (DECIDED, KILL_EARLY)
    better = judge(arm("v", 120, 15000), arm("c", 300, 60000), rule)  # 8.0 vs 5.0 but below 200 installs
    assert (better.stage, better.verdict) == (SCREENED, PENDING)
    assert (better.installs_missing, better.next_threshold) == (80, 200)


def test_verdicts_at_the_decision_threshold():
    rule = Rule()
    assert judge(arm("v", 330, 52000), arm("c", 300, 60000), rule).verdict == SCALE  # +27 %, P ~ 1
    assert judge(arm("v", 210, 70000), arm("c", 300, 60000), rule).verdict == KILL  # -40 %
    keep = judge(arm("v", 310, 60000), arm("c", 300, 60000), rule)  # +3 %
    assert keep.verdict == KEEP and keep.progress == 1.0
    strict = judge(arm("v", 330, 52000), arm("c", 300, 60000), Rule(scale_pct=40))
    assert strict.verdict == KEEP  # the lift is real but below the pre-registered bar


def test_rule_validation_and_locking(tmp_path):
    store = CellStore(tmp_path / "perf.sqlite")
    assert store.list() == [] and not (tmp_path / "perf.sqlite").exists()
    for bad in (
        dict(control="c1", variants=["c1"]),
        dict(control="c1", variants=["v1", "v2", "v3", "v4", "v5"]),
        dict(control="", variants=["v1"]),
        dict(control="c1", variants=[]),
    ):
        with pytest.raises(CellError):
            store.create("t", "meta", rule=Rule(), **bad)
    with pytest.raises(CellError, match="screen_installs"):
        store.create("t", "meta", "c1", ["v1"], Rule(screen_installs=300, decide_installs=200))
    cell = store.create("hook test", "meta", "c1", ["v1", "v2"], Rule(text="scale at +20 %"))
    assert cell.status == "draft" and not cell.locked
    store.update_rule(cell.id, Rule(scale_pct=25, text="scale at +25 %"))
    started = store.start(cell.id, date(2026, 11, 1))
    assert started.status == "running" and started.locked and started.rule.scale_pct == 25 and not started.tampered
    with pytest.raises(LockedError):
        store.update_rule(cell.id, Rule(scale_pct=5))
    with pytest.raises(LockedError):
        store.delete(cell.id)
    with pytest.raises(LockedError):
        store.start(cell.id)
    # editing the stored rule behind the app's back is visible
    con = sqlite3.connect(tmp_path / "perf.sqlite")
    con.execute("UPDATE test_cells SET rule = ? WHERE id = ?", (Rule(scale_pct=5).canonical(), cell.id))
    con.commit()
    con.close()
    assert store.get(cell.id).tampered
    assert store.close(cell.id, date(2026, 11, 20)).status == "closed"


def test_evaluate_reads_one_network_from_the_start_day(db, tmp_path):
    store = CellStore(db)
    cell = store.create("a vs oops", "meta", A.creative_id, [OOPS.creative_id, B.creative_id], Rule())
    draft = evaluate(cell, db)
    assert not draft.has_data and all(o.verdict == PENDING for o in draft.outcomes)
    started = store.start(cell.id, date.today() - timedelta(days=30))
    res = evaluate(started, db)
    assert res.control.installs == pytest.approx(300) and res.outcomes[0].verdict == SCALE
    assert res.outcomes[1].verdict == PENDING and res.outcomes[1].installs_missing == 46  # 4 installs of 50
    assert res.ready and res.decided == 1
    other = store.start(store.create("tiktok", "tiktok", A.creative_id, [OOPS.creative_id], Rule()).id)
    assert not evaluate(other, db).has_data  # meta rows never count on another network
    late = store.start(store.create("late", "meta", A.creative_id, [OOPS.creative_id], Rule()).id, date.today())
    assert evaluate(late, db).control.installs < 300  # rows before the start day are not counted


# ------------------------------------------------------------------------------------------------- test routes
def test_creating_and_starting_a_test_from_the_form(client, db):
    form = {"name": "Kanca testi", "network": "meta", "metric": "ipm", "control": A.creative_id,
            "variants": [OOPS.creative_id, B.creative_id], "scale_pct": "20", "kill_pct": "20",
            "screen_installs": "50", "decide_installs": "200", "confidence": "95", "text": "", "start": "1",
            "start_on": (date.today() - timedelta(days=30)).isoformat(), "token": TOKEN}  # fmt: skip
    r = client.post("/tests", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tests/1"
    cell = CellStore(db).get(1)
    assert cell.locked and cell.variants == (OOPS.creative_id, B.creative_id)
    assert "ölçekle" in cell.rule.text and "%20" in cell.rule.text  # written from the thresholds, in Turkish
    page = client.get("/tests/1").text
    assert "ölçekle" in page and "henüz karar yok: 46 kurulum daha" in page and "kural kilitli" not in page
    assert "kilitlendi" in page and "Testi kapat" in page
    listing = client.get("/tests").text
    assert "Kanca testi" in listing and "kural kilitli" in listing
    assert "karar hazır" in client.get("/").text  # the overview lists it as ready to decide
    assert client.post("/tests/1/delete", data={"token": TOKEN}).status_code == 409  # started tests stay
    assert client.post("/tests/1/close", data={"token": TOKEN}, follow_redirects=False).status_code == 303
    assert CellStore(db).get(1).status == "closed"


def test_invalid_tests_are_refused_with_the_form_kept(client, db):
    form = {"name": "x", "network": "meta", "control": A.creative_id, "variants": [A.creative_id],
            "scale_pct": "20", "kill_pct": "20", "screen_installs": "50", "decide_installs": "200",
            "confidence": "95", "token": TOKEN}  # fmt: skip
    r = client.post("/tests", data=form)
    assert r.status_code == 422 and "Test kaydedilmedi" in r.text and "control cannot also be a variant" in r.text
    assert CellStore(db).list() == []


def test_test_posts_need_the_token(client, db):
    form = {"name": "x", "network": "meta", "control": A.creative_id, "variants": [OOPS.creative_id]}
    assert client.post("/tests", data=form).status_code == 403
    cell = CellStore(db).create("x", "meta", A.creative_id, [OOPS.creative_id], Rule())
    for action in ("start", "close", "delete"):
        assert client.post(f"/tests/{cell.id}/{action}", data={}).status_code == 403
    assert not CellStore(db).get(cell.id).locked
    r = client.post(f"/tests/{cell.id}/delete", data={"token": TOKEN}, follow_redirects=False)
    assert r.status_code == 303 and CellStore(db).get(cell.id) is None


# ---------------------------------------------------------------------------------------------------- settings
def test_settings_report_credentials_by_name_and_never_by_value(client, monkeypatch):
    meta_secret, comfy_secret = "EAAB-sekret-meta-9f8e7d", "comfy-proxy-sekret-31"
    monkeypatch.setenv("CREATIVE_FORGE_META_TOKEN", meta_secret)
    monkeypatch.delenv("CREATIVE_FORGE_APPLOVIN_KEY", raising=False)
    monkeypatch.setenv("CREATIVE_FORGE_COMFYUI_TOKEN", comfy_secret)
    for lang in ("tr", "en"):
        page = client.get(f"/settings?lang={lang}").text
        assert meta_secret not in page and comfy_secret not in page
        assert "CREATIVE_FORGE_META_TOKEN" in page and "CREATIVE_FORGE_APPLOVIN_KEY" in page
    tr = client.get("/settings?lang=tr").text
    meta_row = tr[tr.index("<b>Meta</b>") : tr.index("<b>AppLovin</b>")]
    applovin_row = tr[tr.index("<b>AppLovin</b>") : tr.index("<b>TikTok</b>")]
    assert "bağlı" in meta_row and "eksik" in applovin_row
    for path in PAGES:
        assert meta_secret not in client.get(path).text


def test_csv_upload_imports_through_the_same_code_as_the_cli(tmp_path, root):
    db = tmp_path / "fresh.sqlite"
    c = client_for(root, db, tmp_path)
    csv = f"Date,Ad name,Impressions,Installs\n2026-11-10,{A.ad_name},1000,9\n2026-11-10,someone else,500,1\n"
    files = {"file": ("tiktok-ekim.csv", csv.encode("utf-8"), "text/csv")}
    assert c.post("/settings/import", data={"network": "tiktok"}, files=files).status_code == 403
    assert not db.exists()
    r = c.post("/settings/import", data={"network": "tiktok", "token": TOKEN}, files=files)
    assert r.status_code == 200 and "2 satır içe aktarıldı" in r.text and "tiktok-ekim.csv" in r.text
    got = PerfStore(db).rows("SELECT creative_id, source FROM perf_rows ORDER BY impressions DESC")
    assert got[0]["creative_id"] == A.creative_id and got[0]["source"] == "csv:tiktok-ekim.csv"
    assert got[1]["creative_id"] is None
    bad = c.post("/settings/import", data={"network": "tiktok", "token": TOKEN},
                 files={"file": ("x.csv", b"foo,bar\n1,2\n", "text/csv")})  # fmt: skip
    assert bad.status_code == 422 and "CSV okunamadı" in bad.text and "no column for" in bad.text
    assert PerfStore(db).count() == 2
    nofile = c.post("/settings/import", data={"network": "tiktok", "token": TOKEN})
    assert nofile.status_code == 422 and "Bir CSV dosyası seç" in nofile.text
    imports = c.get("/perf").text
    assert "TikTok" in imports and "CSV" in imports


def test_campaigns_page_starts_empty_and_writes_nothing_on_a_get(client, tmp_path):
    page = client.get("/campaigns").text
    assert (
        "Kampanya yapılandırması yok" in page and "Henüz kampanya yok" in page
    )  # tests/test_campaigns_ui.py: the rest


def test_playables_page_reads_check_output(client, root):
    (root / "playables").mkdir()
    result = [{"file": "/x/mini.html", "network": "tiktok", "status": "fail",
               "findings": [{"rule": "config-json", "status": "fail", "message": "no config.json", "source": "",
                             "checked": "2026-11-11"}], "trace": {}}]  # fmt: skip
    (root / "playables" / "mini.check.json").write_text(json.dumps(result), encoding="utf-8")
    (root / "playables" / "other.json").write_text('{"not": "a check"}', encoding="utf-8")
    page = client.get("/playables").text
    assert "mini.check.json" in page and "config-json" in page and "other.json" not in page
    assert "AppLovin" in page and "Mintegral" in page  # the rules table


# ---------------------------------------------------------------------------------------------- design system
def test_the_shared_design_system_is_served_and_pages_make_no_external_requests(client):
    css = (UI / "static" / "ui.css").read_text(encoding="utf-8")
    js = (UI / "static" / "ui.js").read_text(encoding="utf-8")
    assert css.startswith("/* studio-ui ") and js.startswith("/* studio-ui ")  # pinned: test_studio_ui_pin
    assert client.get("/static/ui.css").text == css and client.get("/static/ui.js").text == js
    for path in PAGES:
        page = client.get(path).text
        assert '/static/ui.css"' in page and '/static/ui.js"' in page and 'id="ui-commands"' in page
        assert not re.search(r'<(script|link|img|video|iframe)[^>]+(src|href)="(https?:)?//', page), path
        assert "data-theme-toggle" in page and "data-cmdk" in page


def test_command_palette_lists_pages_runs_and_tests(client, db):
    CellStore(db).create("palet testi", "meta", A.creative_id, [OOPS.creative_id], Rule())
    page = client.get("/").text
    data = json.loads(re.search(r'id="ui-commands"[^>]*>(.*?)</script>', page, re.S).group(1))
    hrefs = {c["href"] for c in data}
    assert {"/", "/creatives", "/tests", "/settings", "/runs/spring", "/tests/1"} <= hrefs
    assert any(c["label"] == "palet testi" for c in data)
