"""CLI commands and one full `forge demo` run (fake providers, synthetic gameplay)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from creative_forge.cli import main
from creative_forge.ffmpeg import probe
from creative_forge.naming import parse
from creative_forge.qa.result import Status

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ads.yaml"


def test_plan_prints_estimate(capsys):
    assert main(["plan", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "veo-3.1-generate-001" in out and "total (upper bound" in out and "2026-10" in out


def test_dry_run_makes_no_calls_and_reports_problems(capsys, monkeypatch):
    monkeypatch.delenv("CREATIVE_FORGE_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    import creative_forge.providers as providers

    monkeypatch.setattr(providers, "build_providers", lambda *a, **k: pytest.fail("dry run built providers"))
    assert main(["run", str(EXAMPLE), "--dry-run"]) == 1  # example footage is not shipped
    out = capsys.readouterr().out
    assert "gameplay file missing" in out and "CREATIVE_FORGE_PROJECT" in out
    assert "dry run: no generation calls were made" in out


def test_unknown_only_id_is_an_error_not_an_empty_run(tmp_path, capsys):
    spec = tmp_path / "s.yaml"
    spec.write_text(
        "name: t\nproviders: fake\noutput_dir: out\nconcepts:\n"
        "  - {id: c1, gameplay: {path: g.mp4, in_s: 0, out_s: 10}, hook: {video_prompt: v}}\n",
        encoding="utf-8",
    )
    assert main(["run", str(spec), "--only", "c2"]) == 2
    assert "no concept with id c2" in capsys.readouterr().err


def test_qa_command_on_a_defective_file(short_audio_video, tmp_path, capsys):
    report = tmp_path / "qa.json"
    code = main(["qa", str(short_audio_video), "--ocr", "off", "--json", str(report)])
    assert code == 1
    data = json.loads(report.read_text(encoding="utf-8"))
    checks = {c["check"]: c for c in next(iter(data.values()))}
    assert checks["av_duration"]["status"] == "fail"
    assert checks["seam"]["status"] == "skip"  # no timeline given


@pytest.mark.slow
def test_demo_end_to_end(tmp_path):
    from creative_forge.demo import run_demo
    from creative_forge.report import write_report

    rr = run_demo(tmp_path / "demo", keep_work=True, log=lambda _: None)
    assert [c.error for c in rr.concepts] == [None, None]
    assert rr.status is not Status.FAIL, json.dumps(
        [(c.concept, [(x.check, x.status.value, x.message) for e in c.exports for x in e.checks]) for c in rr.concepts]
    )
    html, js = write_report(rr)
    assert "Cost ledger" in html.read_text(encoding="utf-8") and "data:image/jpeg;base64," in html.read_text(
        encoding="utf-8"
    )

    exports = [e for c in rr.concepts for e in c.exports]
    assert {e.format for e in exports} == {"9:16", "4:5"} and len(exports) == 4
    for e in exports:
        info = probe(e.path)
        assert abs(info.video_duration - info.audio_duration) < 0.1
        side = json.loads((e.path.parent / (e.path.name + ".forge.json")).read_text(encoding="utf-8"))
        ids = parse(side["ad_name"])  # the file name is the ad name, and it round-trips
        assert (
            e.path.name == side["ad_name"] + ".mp4" and ids.creative_id == side["creative_id"] == e.creative.creative_id
        )
        assert side["tags"]["format"] == ids.format and side["tags"]["ai_generated"] == "yes"
        assert side["tags"]["hook_type"] in ("satisfying-clean", "real-hand")
        assert [m["role"] for m in side["models"]] == ["image_edit", "video", "music"]
    by_check = {x.check: x for x in exports[0].checks}
    for name in ("loudness", "true_peak", "hook_gameplay_loudness", "av_duration", "cadence", "seam"):
        assert by_check[name].status is Status.PASS, by_check[name]
    assert by_check["judge"].status in (Status.PASS, Status.WARN)

    # per-job temp dirs: one private directory per concept
    work = [c.work_dir for c in rr.concepts]
    assert work[0] != work[1] and all(w and w.exists() for w in work)

    # free run, but the ledger shows what Vertex AI would have charged
    assert rr.ledger.spent == 0 and sum(e.list_usd or 0 for e in rr.ledger.entries) > 5

    # a second run hits the cache for every paid-type generation
    again = run_demo(tmp_path / "demo", log=lambda _: None)
    gen = [e for e in again.ledger.entries if e.stage in ("image_edit", "video", "music")]
    assert gen and all(e.cached for e in gen)
    # ... and keeps every creative ID: a re-render is the same creative
    assert sorted(x.creative.ad_name for c in again.concepts for x in c.exports) == sorted(
        x.creative.ad_name for x in exports
    )
    assert json.loads(js.read_text(encoding="utf-8"))["concepts"][0]["exports"][0]["format"] == "9:16"
