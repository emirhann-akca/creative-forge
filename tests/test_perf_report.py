"""`forge perf report`: per creative and per tag with intervals, verdict thresholds, lessons that ideate retrieves."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from creative_forge.cli import main
from creative_forge.ideate.library import HashingEmbedder, LibraryIndex, load_library
from creative_forge.paths import ENV_HOME
from creative_forge.perf.commands import creatives_from
from creative_forge.perf.lessons import lessons_from, lessons_path, write_lessons
from creative_forge.perf.meta import to_row
from creative_forge.perf.report import build_report
from creative_forge.perf.stats import INSUFFICIENT, LOSER, NO_DIFFERENCE, WINNER, compare, prob_better, wilson
from creative_forge.perf.store import ImportSummary, PerfRow, PerfStore

FIX = Path(__file__).parent / "fixtures" / "perf"


@pytest.fixture
def store(tmp_path: Path) -> PerfStore:
    """Two days of Meta ad-level rows (fail-state hooks clearly out-install satisfying-clean ones, one tiny ad, one
    ad that is not ours), two AppLovin creative sets, and the publish manifest's tags."""
    s = PerfStore(tmp_path / "perf.sqlite")
    rows = []
    for page in ("meta_insights_page1.json", "meta_insights_page2.json"):
        rows += [to_row(d, "act_1") for d in json.loads((FIX / page).read_text(encoding="utf-8"))["data"]]
    s.upsert(rows, ImportSummary("meta", "act_1", "ad"))
    al = []
    for page in ("applovin_report_page1.json", "applovin_report_page2.json"):
        for d in json.loads((FIX / page).read_text(encoding="utf-8"))["results"]:
            al.append(
                PerfRow(
                    "applovin", "default", "creative_set", d["day"], d["creative_set_id"], d["creative_set"],
                    impressions=float(d["impressions"]), clicks=float(d["clicks"]), spend=float(d["cost"]),
                    installs=float(d["conversions"]),
                )
            )  # fmt: skip
    s.upsert(al, ImportSummary("applovin", "default", "creative_set"))
    s.register(creatives_from(FIX / "manifest.json"), "manifest.json")
    return s


def test_wilson_and_bayes_reference_values():
    lo, hi = wilson(5, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.2366, 0.7634)
    assert round(wilson(0, 10)[1], 4) == 0.2775  # a zero count still gets an upper bound
    assert wilson(0, 0) == (0.0, 1.0)
    assert prob_better(600, 100000, 300, 100000) > 0.999
    assert 0.45 < prob_better(50, 10000, 50, 10000) < 0.55
    assert compare(9, 600, 2400, 400000, 10000).verdict == INSUFFICIENT  # IPM 15 on 600 impressions is not a win
    assert compare(1300, 220000, 2438, 200600, 10000).verdict == LOSER
    assert compare(100, 20000, 101, 20000, 10000).verdict == NO_DIFFERENCE


def test_report_per_creative_matches_hand_computed_values(store):
    rep = build_report(store, network="meta", min_impressions=10000)
    meta = rep["networks"]["meta"]
    by_id = {c["creative_id"]: c for c in meta["creatives"]}
    a = by_id["e252faa195"]
    assert (a["impressions"], a["installs"], a["ipm"], a["ctr"], a["cpi"]) == (120000, 720, 6.0, 1.5, 0.8333)
    assert (a["hook_rate"], a["completion"], a["hook_rate_2s"]) == (25.0, 5.0, None)
    assert a["tags"]["hook_type"] == "satisfying-clean" and a["registered"]
    tiny = by_id["cd1924d01f"]
    assert tiny["ipm"] == 30.0 and tiny["comparison"]["verdict"] == INSUFFICIENT  # highest IPM, 600 impressions
    assert by_id["fc972ce8b2"]["comparison"]["verdict"] == WINNER
    assert meta["unmatched"] == {"names": 1, "impressions": 20000}
    assert len(meta["creatives"]) == 5


def test_tag_verdicts_need_both_sides_above_the_threshold(store):
    rep = build_report(store, network="meta", min_impressions=10000)
    tags = {(t["dimension"], t["value"]): t for t in rep["networks"]["meta"]["tags"]}
    fail = tags[("hook_type", "fail-state")]
    # (690 + 520 + 9) x 2 installs over (55,000 + 45,000 + 300) x 2 impressions, against (360 + 290) x 2 over 220,000
    assert fail["creatives"] == 3 and fail["impressions"] == 200600
    assert fail["comparison"]["value"] == round(2438 / 200600 * 1000, 4)
    assert fail["comparison"]["rest"] == round(1300 / 220000 * 1000, 4)
    assert fail["comparison"]["verdict"] == WINNER and fail["comparison"]["p_better"] > 0.999
    assert tags[("hook_type", "satisfying-clean")]["comparison"]["verdict"] == LOSER
    assert tags[("length_bucket", "s00-15")]["comparison"]["verdict"] == INSUFFICIENT  # only the tiny ad
    strict = build_report(store, network="meta", min_impressions=1_000_000)
    assert {t["comparison"]["verdict"] for t in strict["networks"]["meta"]["tags"]} == {INSUFFICIENT}
    assert lessons_from(strict) == []


def test_networks_are_never_pooled_and_hook_rate_ignores_rows_without_it(store):
    rep = build_report(store, min_impressions=10000)
    assert set(rep["networks"]) == {"meta", "applovin"}
    al = rep["networks"]["applovin"]
    assert {c["creative_id"] for c in al["creatives"]} == {"36d3704762", "1d4e68cf56"}
    assert all(c["hook_rate"] is None for c in al["creatives"])  # AppLovin reports no 3-second views
    ctr = build_report(store, metric="ctr", min_impressions=10000)
    assert ctr["networks"]["meta"]["creatives"][0]["comparison"]["metric"] == "ctr"
    with pytest.raises(ValueError, match="metric"):
        build_report(store, metric="roas")


def test_a_winning_tag_becomes_a_lesson_that_ideate_retrieves(store, tmp_path, monkeypatch, good_video):
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "home"))
    rep = build_report(store, network="meta", min_impressions=10000)
    path, n = write_lessons(rep)
    assert path == lessons_path() and n >= 2
    ids = [e.id for e in load_library() if e.kind == "perf_lesson"]
    assert "perf-meta-ipm-hook_type-fail-state" in ids and "perf-meta-ipm-hook_type-satisfying-clean" in ids
    assert len(load_library(perf_lessons=False)) == len(load_library()) - n

    from creative_forge.ideate.tools import Toolbox
    from creative_forge.pricing import Pricing

    index = LibraryIndex(load_library(), HashingEmbedder(), tmp_path / "index.json")
    tools = Toolbox(index, [good_video], Pricing.load(), 2)
    hits = tools.search_creative_library("which hook type wins on installs for a goods sort puzzle", k=3)["results"]
    assert hits[0]["id"] == "perf-meta-ipm-hook_type-fail-state", [h["id"] for h in hits]
    assert hits[0]["kind"] == "perf_lesson" and "12.15" in hits[0]["text"] and "P(better" in hits[0]["text"]

    # a new report for the same network and metric replaces those lessons instead of piling up
    write_lessons(rep)
    assert [e.id for e in load_library() if e.kind == "perf_lesson"] == ids


def test_cli_report_writes_html_json_and_lessons(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "home"))
    out = tmp_path / "rep" / "perf.html"
    args = ["perf", "report", "--db", str(store.path), "--out", str(out), "--write-lessons", "--network", "meta"]
    assert main(args) == 0
    printed = capsys.readouterr().out
    assert "tag hook_type=fail-state: ipm 12.15 vs 5.91 for the rest -> winner" in printed
    assert "lessons:" in printed and lessons_path().is_file()
    page = out.read_text(encoding="utf-8")
    assert "<title>Creative Performance</title>" in page and "fc972ce8b2" in page and "insufficient data" in page
    assert json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))["metric"] == "ipm"


def test_ui_performance_page(store, tmp_path):
    from creative_forge.ui.app import create_app

    (tmp_path / "out").mkdir()
    base = "http://127.0.0.1:8765"
    client = TestClient(create_app(tmp_path / "out", token="t", perf_db=store.path), base_url=base)
    page = client.get("/perf?lang=en")
    assert page.status_code == 200 and "Per creative" in page.text and "<h2>meta</h2>" in page.text
    assert "cf1.puzzle-ads.oops.wrong-move.lvl7.tryit.9x16.en.fc972ce8b2" in page.text and "winner" in page.text
    assert "kazanan" in client.get("/perf?lang=tr").text  # Turkish by default, the same verdicts
    assert client.get("/perf?metric=nope").status_code == 422
    empty_app = create_app(tmp_path / "out", token="t", perf_db=tmp_path / "none.sqlite")
    empty = TestClient(empty_app, base_url=base).get("/perf?lang=en")
    assert "No performance data yet" in empty.text and not (tmp_path / "none.sqlite").exists()
