"""Creative IDs, the ad-name format, and the tag taxonomy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from creative_forge.cli import main
from creative_forge.lint import errors, lint
from creative_forge.naming import AdNameError, CreativeIds, find, format_ratio, format_token, parse
from creative_forge.review import decide, publish
from creative_forge.spec import AdSpec
from creative_forge.tags import Taxonomy, export_tags, flatten, length_bucket

IDS = CreativeIds("puzzle-ads", "dust-blower", "blower", "lvl21", "fill", "9x16", "tr")


def test_creative_id_is_deterministic_and_stable():
    # a literal: changing the hashing scheme would silently orphan every ad already running under the old names
    assert IDS.creative_id == CreativeIds(*IDS.components).creative_id == "91c97d00dc"
    assert IDS.ad_name == "cf1.puzzle-ads.dust-blower.blower.lvl21.fill.9x16.tr.91c97d00dc"
    other = CreativeIds("puzzle-ads", "dust-blower", "blower", "lvl21", "fill", "4x5", "tr")
    assert other.creative_id != IDS.creative_id and other.variant == IDS.variant


@pytest.mark.parametrize(
    "ids",
    [
        IDS,
        CreativeIds("t", "c1", "main", "main", "main", "4x5", "en"),
        CreativeIds("my_spec", "a_b-c", "h-1", "seg_2", "e", "9x16", "pt-br"),
        CreativeIds("0", "9", "x", "y", "z", "4x5", "fil"),
    ],
)
def test_parse_round_trips(ids):
    assert parse(ids.ad_name) == ids
    assert parse(ids.ad_name.upper()) == ids  # a network that upper-cases names
    assert find(f"{ids.ad_name}.mp4") == ids
    assert find(f"{ids.ad_name} - Copy (2)") == ids
    assert find(f"UA test | {ids.ad_name} | week 45") == ids


@pytest.mark.parametrize(
    "name",
    [
        "cf1.puzzle-ads.dust-blower.blower.lvl21.fill.9x16.tr.91c97d00dd",  # checksum off by one
        "cf1.puzzle-ads.dust-blower.washer.lvl21.fill.9x16.tr.91c97d00dc",  # a component edited by hand
        "cf2.puzzle-ads.dust-blower.blower.lvl21.fill.9x16.tr.91c97d00dc",
        "cf1.puzzle-ads.dust-blower.blower.lvl21.fill.1x1.tr.91c97d00dc",
        "puzzle-ads_dust-blower_9x16",
        "",
    ],
)
def test_parse_rejects_anything_else(name):
    with pytest.raises(AdNameError):
        parse(name)
    assert find(name) is None


def test_components_are_validated():
    with pytest.raises(AdNameError, match="hook"):
        CreativeIds("t", "c", "has.dot", "main", "main", "9x16", "en")
    with pytest.raises(AdNameError, match="language"):
        CreativeIds("t", "c", "main", "main", "main", "9x16", "EN-us")
    assert format_token("9:16") == "9x16" and format_ratio("4x5") == "4:5"


def test_taxonomy_and_derived_tags():
    tax = Taxonomy.load()
    assert set(tax.derived) == {"format", "length_bucket", "ai_generated"}
    assert "fail-state" in tax.dimensions["hook_type"].values
    assert [length_bucket(s) for s in (6, 15, 15.4, 22, 30.4, 44, 58, 75)] == [
        "s00-15",
        "s00-15",
        "s00-15",
        "s16-30",
        "s16-30",
        "s31-45",
        "s46-60",
        "s61plus",
    ]
    tags = export_tags({"hook_type": "fail-state", "emotion": ["humor", "surprise"]}, "4x5", 27.3)
    assert tags == {
        "emotion": ["humor", "surprise"],
        "hook_type": "fail-state",
        "format": "4x5",
        "length_bucket": "s16-30",
        "ai_generated": "yes",
    }
    assert ("emotion", "surprise") in flatten(tags) and ("format", "4x5") in flatten(tags)


def spec_with(tags: dict) -> AdSpec:
    return AdSpec.model_validate(
        {
            "name": "t",
            "concepts": [
                {
                    "id": "c1",
                    "gameplay": {"path": "g.mp4", "in_s": 0, "out_s": 10},
                    "hook": {"video_prompt": "v", "text_regions": [[0, 0, 10, 10]], "cut_s": 4.0},
                    "tags": tags,
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "tags, rule, words",
    [
        ({"hook_typ": "fail-state"}, "unknown-tag", "unknown tag dimension 'hook_typ'"),
        ({"hook_type": "failstate"}, "unknown-tag", "hook_type='failstate' is not in the taxonomy"),
        ({"hook_type": ["fail-state", "reveal"]}, "unknown-tag", "takes one value"),
        ({"emotion": ["calm", "angry"]}, "unknown-tag", "emotion='angry'"),
        ({"format": "9x16"}, "derived-tag", "derived per export"),
    ],
)
def test_unknown_tags_fail_lint(tags, rule, words):
    issues = errors(lint(spec_with(tags)))
    assert any(i.rule == rule and words in i.message for i in issues), issues


def test_known_tags_pass_and_missing_ones_only_warn():
    good = spec_with({"hook_type": "fail-state", "mechanic_shown": ["goods-sort"], "emotion": ["humor"]})
    assert lint(good) == []
    issues = lint(spec_with({"hook_type": "fail-state"}))
    assert [(i.rule, i.severity) for i in issues] == [("untagged", "warn")]
    assert "mechanic_shown, emotion" in issues[0].message


def test_custom_taxonomy_from_the_spec(tmp_path):
    (tmp_path / "tags.yaml").write_text(
        "dimensions:\n  hook_type: {values: [unboxing]}\n  format: {derived: true, values: [9x16, 4x5]}\n",
        encoding="utf-8",
    )
    s = spec_with({"hook_type": "unboxing"})
    s.taxonomy = Path("tags.yaml")
    s.base_dir = tmp_path
    assert errors(lint(s)) == []
    s2 = spec_with({"hook_type": "fail-state"})
    s2.taxonomy, s2.base_dir = Path("tags.yaml"), tmp_path
    assert errors(lint(s2))


def test_plan_fails_on_unknown_tags(tmp_path, capsys):
    p = tmp_path / "s.yaml"
    p.write_text(
        "name: t\nproviders: fake\nconcepts:\n  - {id: c1, gameplay: {path: g.mp4, in_s: 0, out_s: 10}, "
        "hook: {video_prompt: v}, tags: {hook_type: viral}}\n",
        encoding="utf-8",
    )
    assert main(["plan", str(p)]) == 1
    assert "ERROR c1: [unknown-tag]" in capsys.readouterr().out
    assert main(["run", str(p), "--dry-run"]) == 2


def test_manifest_carries_ids_tags_and_models(tmp_path):
    run = tmp_path / "out"
    run.mkdir()
    name = IDS.ad_name + ".mp4"
    (run / name).write_bytes(b"x" * 100)
    tags = export_tags({"hook_type": "satisfying-clean"}, "9x16", 24.0)
    models = [{"role": "video", "model": "veo-3.1-generate-001", "provider": "vertex-veo", "license": None}]
    (run / (name + ".forge.json")).write_text(
        json.dumps({"creative_id": IDS.creative_id, "ad_name": IDS.ad_name, "tags": tags, "models": models}),
        encoding="utf-8",
    )
    report = {
        "name": "t",
        "concepts": [
            {
                "concept": "dust-blower",
                "pre_checks": [],
                "exports": [
                    {
                        "format": "9:16",
                        "file": name,
                        "creative_id": IDS.creative_id,
                        "ad_name": IDS.ad_name,
                        "tags": tags,
                        "status": "pass",
                        "checks": [],
                    }
                ],
            }
        ],
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    decide(run, name, "approve", "", "ci")
    manifest = json.loads(publish(run, tmp_path / "deliver").read_text(encoding="utf-8"))
    (entry,) = manifest["exports"]
    assert entry["creative_id"] == IDS.creative_id and entry["ad_name"] == IDS.ad_name
    assert entry["tags"]["length_bucket"] == "s16-30" and entry["models"] == models
    assert parse(entry["ad_name"]) == IDS
