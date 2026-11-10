"""Human review gate: decisions, stale approvals, QA-failure refusal and overrides. No media needed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from creative_forge.cli import main
from creative_forge.review import ReviewError, decide, load_review, publish

T = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def check(name: str, status: str) -> dict:
    return {"check": name, "status": status, "value": None, "threshold": "", "unit": "", "message": ""}


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """A finished run: concept a passes; concept b's 9:16 export failed true_peak."""
    run = tmp_path / "out"
    run.mkdir()
    exports = {"t_a_9x16.mp4": "pass", "t_b_9x16.mp4": "fail"}
    for name in exports:
        (run / name).write_bytes(name.encode() * 100)
    report = {
        "name": "t",
        "concepts": [
            {
                "concept": "a",
                "pre_checks": [check("hook_landing", "pass")],
                "exports": [
                    {"format": "9:16", "file": "t_a_9x16.mp4", "status": "warn", "checks": [check("judge", "warn")]}
                ],
            },
            {
                "concept": "b",
                "pre_checks": [],
                "exports": [
                    {"format": "9:16", "file": "t_b_9x16.mp4", "status": "fail", "checks": [check("true_peak", "fail")]}
                ],
            },
        ],
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return run


def test_only_approved_exports_are_published_with_a_manifest(run_dir, tmp_path):
    decide(run_dir, "t_a_9x16.mp4", "approve", "hook reads well", "ayse", T)
    decide(run_dir, "t_b_9x16.mp4", "reject", "peak clips on the chime", "ayse", T)
    manifest = json.loads(publish(run_dir, tmp_path / "deliver", now=T).read_text(encoding="utf-8"))

    assert sorted(p.name for p in (tmp_path / "deliver").iterdir()) == ["manifest.json", "t_a_9x16.mp4"]
    (entry,) = manifest["exports"]
    assert entry["reviewer"] == "ayse" and entry["note"] == "hook reads well" and not entry["overridden"]
    assert manifest["not_published"] == [{"file": "t_b_9x16.mp4", "decision": "reject"}]
    assert manifest["override_reason"] is None


def test_failed_qa_blocks_publishing_until_overridden_with_a_reason(run_dir, tmp_path):
    decide(run_dir, "t_b_9x16.mp4", "approve", "", "ayse", T)
    with pytest.raises(ReviewError, match=r"t_b_9x16\.mp4 \(true_peak\)"):
        publish(run_dir, tmp_path / "deliver")
    assert not (tmp_path / "deliver").exists()  # refused means nothing copied

    with pytest.raises(ReviewError, match="needs a reason"):
        publish(run_dir, tmp_path / "deliver", override="  ")

    manifest = json.loads(
        publish(run_dir, tmp_path / "deliver", override="network re-encodes audio").read_text(encoding="utf-8")
    )
    assert manifest["override_reason"] == "network re-encodes audio"
    assert manifest["exports"][0]["overridden"] and manifest["exports"][0]["failed_checks"] == ["true_peak"]


def test_rerendered_file_voids_its_approval(run_dir, tmp_path):
    decide(run_dir, "t_a_9x16.mp4", "approve", "", "ayse", T)
    (run_dir / "t_a_9x16.mp4").write_bytes(b"re-rendered")
    with pytest.raises(ReviewError, match="changed since it was reviewed"):
        publish(run_dir, tmp_path / "deliver")


def test_decisions_are_validated(run_dir):
    with pytest.raises(ReviewError, match="needs a note"):
        decide(run_dir, "t_a_9x16.mp4", "reject", "", "ayse")
    with pytest.raises(ReviewError, match="not an export"):
        decide(run_dir, "../../etc/passwd", "approve", "", "ayse")
    with pytest.raises(ReviewError, match="decision must be"):
        decide(run_dir, "t_a_9x16.mp4", "maybe", "", "ayse")
    with pytest.raises(ReviewError, match="no export is approved"):
        publish(run_dir, run_dir / "deliver")


def test_hand_edited_report_cannot_point_outside_the_run(run_dir, tmp_path):
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    report["concepts"][0]["exports"][0]["file"] = "../secret.mp4"
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ReviewError, match="unsafe file name"):
        publish(run_dir, tmp_path / "deliver")


def test_review_and_publish_commands(run_dir, tmp_path, monkeypatch, capsys):
    answers = iter(["a", "looks good", "r", "true peak over"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    assert main(["review", str(run_dir), "--reviewer", "ayse"]) == 0
    assert {k: v["decision"] for k, v in load_review(run_dir)["decisions"].items()} == {
        "t_a_9x16.mp4": "approve",
        "t_b_9x16.mp4": "reject",
    }
    assert main(["publish", str(run_dir), "--to", str(tmp_path / "d")]) == 0
    assert main(["review", str(run_dir), "--approve", "t_b_9x16.mp4", "--reviewer", "ayse"]) == 0
    assert main(["publish", str(run_dir), "--to", str(tmp_path / "d2")]) == 1
    assert "failed QA checks" in capsys.readouterr().err
