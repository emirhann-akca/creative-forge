"""Market trends from studio-pulse's radar in forge ideate's retrieval library."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml

from creative_forge.ideate.library import HashingEmbedder, LibraryIndex, load_library, market_trends_path
from creative_forge.paths import ENV_HOME, data_dir
from creative_forge.tags import Taxonomy

FIXTURE = Path(__file__).parent / "fixtures" / "market_trends.yaml"


def test_market_trends_join_the_library_and_are_retrieved(tmp_path, monkeypatch, good_video):
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "home"))
    monkeypatch.delenv("CREATIVE_FORGE_MARKET_TRENDS", raising=False)
    before = len(load_library())
    path = market_trends_path()
    assert path == data_dir() / "library" / "market_trends.yaml"
    path.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, path)
    entries = load_library()
    trends = [e for e in entries if e.kind == "market_trend"]
    assert [e.id for e in trends] == ["MT-2026-11-12-jam", "MT-2026-11-09-us-color-sort-weekly"]
    assert len(entries) == before + 2 and len(load_library(market_trends=False)) == before
    assert "[observed]" in trends[0].text and "\n" not in trends[0].text

    from creative_forge.ideate.tools import Toolbox
    from creative_forge.pricing import Pricing

    index = LibraryIndex(entries, HashingEmbedder(), tmp_path / "index.json")
    tools = Toolbox(index, [good_video], Pricing.load(), 2)
    hits = tools.search_creative_library("which jam mechanic games with buses are rising in the market", k=3)
    top = hits["results"][0]
    assert top["id"] == "MT-2026-11-12-jam" and top["kind"] == "market_trend", [h["id"] for h in hits["results"]]


def test_market_trends_path_can_point_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_HOME, str(tmp_path / "home"))
    other = tmp_path / "radar" / "market_trends.yaml"
    other.parent.mkdir()
    shutil.copyfile(FIXTURE, other)
    monkeypatch.setenv("CREATIVE_FORGE_MARKET_TRENDS", str(other))
    assert market_trends_path() == other
    assert sum(e.kind == "market_trend" for e in load_library()) == 2


def test_radar_mechanics_are_tag_values():
    tax = Taxonomy.load()
    doc = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    for e in doc["lessons"]:
        assert e["mechanic_shown"] in tax.dimensions["mechanic_shown"].values
        assert all(h in tax.dimensions["hook_type"].values for h in e["hooks"])
    for new in ("color-sort", "screw-pin", "jam"):
        assert new in tax.dimensions["mechanic_shown"].values
