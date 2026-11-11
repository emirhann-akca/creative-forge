"""Variant matrix (hooks x segments x end cards x languages x formats) and `forge segments`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from conftest import need
from pydantic import ValidationError

from creative_forge.cli import main
from creative_forge.config import Settings
from creative_forge.ffmpeg import probe, run
from creative_forge.naming import parse
from creative_forge.pricing import Pricing, estimate
from creative_forge.spec import AdSpec, load_spec, timeline_for

HOOK = {
    "video_prompt": "the board gets cleaned",
    "image_prompt": "Add a hand holding a brush, entering from the bottom right corner. Do not add any text.",
    "duration_s": 6,
    "cut_s": 2.6,
    "text_regions": [[0, 0, 10, 10]],
}


def matrix_spec(**over) -> dict:
    d = {
        "name": "mx",
        "providers": "fake",
        "qa": {"judge_runs": 0},
        "concepts": [
            {
                "id": "c1",
                "gameplay": {"path": "g.mp4", "in_s": 0, "out_s": 3},
                "hook": HOOK,
                "music": {"prompt": "loop"},
                "tags": {"hook_type": "satisfying-clean", "mechanic_shown": ["goods-sort"], "emotion": ["calm"]},
                "variants": {
                    "hooks": [
                        {"id": "brush"},
                        {"id": "blower", "video_prompt": "a leaf blower clears it", "tags": {"hook_type": "real-hand"}},
                        {"id": "fail", "image_prompt": None, "tags": {"hook_type": "fail-state"}},
                    ],
                    "segments": [{"id": f"lvl{i}", "in_s": 3 + i, "out_s": 9 + i} for i in range(4)],
                    "end_cards": [{"id": "try"}, {"id": "fill", "headline": "Fill every order!"}],
                },
            }
        ],
    }
    d.update(over)
    return d


def test_48_exports_cost_3_paid_hook_generations():
    spec = AdSpec.model_validate(matrix_spec())
    c = spec.concepts[0]
    assert [h for h, _ in c.hook_variants()] == ["brush", "blower", "fail"]
    assert len(spec.matrix(c)) == 3 * 4 * 2  # x 1 language
    plan = estimate(spec, Pricing.load())
    assert (plan.renders, plan.exports) == (24, 48)
    assert plan.count("hook video") == 3 and plan.count("hook image") == 2 and plan.count("music") == 1
    table = plan.table()
    assert "48 export(s) from 24 render(s)" in table and "3 hook video(s), 2 image edit(s)" in table
    # 3 x 6 s Veo with audio + 2 image edits + 1 track: the 48 exports add nothing to the paid total
    assert plan.total_priced == pytest.approx(3 * 6 * 0.40 + 2 * 0.134 + 0.08)


def test_variants_merge_over_the_base_and_keep_their_own_tags():
    spec = AdSpec.model_validate(matrix_spec())
    c = spec.concepts[0]
    hooks = dict(c.hook_variants())
    assert hooks["blower"].video_prompt == "a leaf blower clears it" and hooks["blower"].cut_s == 2.6
    assert hooks["fail"].image_prompt is None and hooks["brush"].image_prompt == HOOK["image_prompt"]
    segs = dict(c.segment_variants())
    assert segs["lvl2"].path == Path("g.mp4") and (segs["lvl2"].in_s, segs["lvl2"].out_s) == (5, 11)
    assert c.hook_tags("fail")["hook_type"] == "fail-state" and c.hook_tags("brush")["hook_type"] == "satisfying-clean"
    tl = timeline_for(spec, c, hooks["brush"], segs["lvl0"], c.end_card)
    assert tl["total"] == pytest.approx(2.6 + 3 + 6 + 2.6 - 0.6)


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda v: v["hooks"].append({"id": "brush"}), "duplicate id 'brush'"),
        (lambda v: v["hooks"].append({"id": "Bad_Id"}), "must be lowercase"),
        (lambda v: v["hooks"].append({"id": "x", "video_promt": "typo"}), "video_promt"),
        (lambda v: v["segments"].append({"id": "short", "in_s": 3, "out_s": 3.5}), "shorter than 1 s"),
        (lambda v: v["end_cards"].append({"id": "x", "headline": {"en": "Go"}}) or v.update(languages=["en", "tr"]),
         "no text for \\['tr'\\]"),
        (lambda v: v.update(languages=["en", "en"]), "unique"),
    ],
)  # fmt: skip
def test_invalid_variants_are_rejected_at_load(mutate, msg):
    d = matrix_spec()
    mutate(d["concepts"][0]["variants"])
    with pytest.raises(ValidationError, match=msg):
        AdSpec.model_validate(d)


def test_plan_command_shows_the_matrix_and_lints_each_hook(tmp_path, capsys):
    d = matrix_spec()
    d["concepts"][0]["variants"]["hooks"].append({"id": "late", "cut_s": 5.8})  # inside Veo's drifting last second
    p = tmp_path / "mx.yaml"
    p.write_text(yaml.safe_dump(d), encoding="utf-8")
    assert main(["plan", str(p)]) == 0
    out = capsys.readouterr().out
    assert "64 export(s) from 32 render(s)" in out and "c1/late: [tail-drift]" in out
    assert "c1/blower" in out  # cost lines are per hook


def _clip(path: Path, color: str, seconds: float) -> Path:
    run([
        "-f", "lavfi", "-i", f"testsrc2=s=180x320:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"color=c={color}@0.35:s=180x320:r=30:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=660:sample_rate=48000:duration={seconds}",
        "-filter_complex", "[0:v][1:v]overlay,format=yuv420p[v]", "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-t", str(seconds), str(path),
    ])  # fmt: skip
    return path


@pytest.mark.slow
def test_fake_run_of_a_2x2x2_matrix_generates_each_hook_once(tmp_path):
    from creative_forge.pipeline import Pipeline
    from creative_forge.providers.fake import fake_providers

    _clip(tmp_path / "g.mp4", "blue", 12)
    d = matrix_spec(output_dir="out", render={"width": 180, "height": 320, "preset": "veryfast"})
    d["export"] = {"preset": "veryfast"}
    d["qa"] = {"judge_runs": 0, "duration_s": [4, 60]}
    c = d["concepts"][0]
    c["formats"] = ["9:16"]
    c["hook"] = {**HOOK, "duration_s": 4, "cut_s": 2.2}
    c["hook"]["disclaimer"] = {"en": "Not gameplay", "tr": "Oyun görüntüsü değil"}
    c["end_card"] = {"duration_s": 1.0, "headline": {"en": "Try it!", "tr": "Dene!"}}
    c["end_card"]["button"] = {"en": "PLAY", "tr": "OYNA"}
    c["variants"] = {
        "hooks": [{"id": "brush"}, {"id": "blower", "video_prompt": "a leaf blower clears it"}],
        "segments": [{"id": "a", "in_s": 4, "out_s": 6}, {"id": "b", "in_s": 7, "out_s": 10}],
        "languages": ["en", "tr"],
    }
    p = tmp_path / "mx.yaml"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    spec = load_spec(p)
    providers = fake_providers()
    settings = Settings(project=None, cache_dir=tmp_path / "cache")
    rr = Pipeline(spec, providers, settings, jobs=4, ocr="off", log=lambda _: None).run()
    assert [r.error for r in rr.concepts] == [None] * 8, [r.error for r in rr.concepts]
    # 8 renders, but each hook was generated once and the music once; the two hooks share their image prompt and
    # first frame, so the second image edit is a cache hit
    assert (providers.image_edit.calls, providers.video.calls, providers.music.calls) == (1, 2, 1)
    edits = [e for e in rr.ledger.entries if e.stage == "image_edit"]
    assert [e.cached for e in edits].count(True) == 1 and len(edits) == 2
    exports = [e for r in rr.concepts for e in r.exports]
    names = {e.path.name for e in exports}
    assert len(names) == 8 and all(parse(n[:-4]).concept == "c1" for n in names)
    assert {(e.creative.hook, e.creative.segment, e.creative.language) for e in exports} == {
        (h, s, lang) for h in ("brush", "blower") for s in ("a", "b") for lang in ("en", "tr")
    }
    by = {(e.creative.hook, e.creative.segment, e.creative.language): e for e in exports}
    # segment b is 1 s longer than a; both keep picture and sound in step
    da, db = probe(by[("brush", "a", "en")].path).duration, probe(by[("brush", "b", "en")].path).duration
    assert db - da == pytest.approx(1.0, abs=0.1)
    tr = by[("blower", "b", "tr")].path
    side = json.loads((tr.parent / (tr.name + ".forge.json")).read_text(encoding="utf-8"))
    assert side["overlay_text"] == ["Oyun görüntüsü değil"] and side["ids"]["language"] == "tr"
    assert all(x.status.value != "fail" for e in exports for x in e.checks if x.check in ("av_duration", "seam")), [
        (x.check, x.status.value, x.message) for e in exports for x in e.checks if x.status.value == "fail"
    ]

    # a third language costs no new generation: every hook and the music come from the cache
    c["variants"]["languages"] = ["en", "tr", "de"]
    c["end_card"]["headline"]["de"], c["end_card"]["button"]["de"] = "Probier es!", "SPIELEN"
    c["hook"]["disclaimer"]["de"] = "Kein Gameplay"
    p.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
    again = fake_providers()
    rr2 = Pipeline(load_spec(p), again, settings, jobs=4, ocr="off", log=lambda _: None).run()
    assert len(rr2.concepts) == 12 and (again.image_edit.calls, again.video.calls, again.music.calls) == (0, 0, 0)
    gen = [e for e in rr2.ledger.entries if e.stage in ("image_edit", "video", "music")]
    assert len(gen) == 5 and all(e.cached for e in gen)


def test_join_gameplay_keeps_the_landing_frame_and_both_lengths(tmp_path):
    from creative_forge.spec import GameplaySpec, RenderSpec
    from creative_forge.stages.video import join_gameplay, prepare_gameplay

    src = _clip(tmp_path / "src.mp4", "red", 6)
    r = RenderSpec(width=180, height=320, preset="veryfast")
    (tmp_path / "l").mkdir()
    (tmp_path / "x").mkdir()
    landing = prepare_gameplay(src, GameplaySpec(path=src, in_s=0, out_s=2), r, tmp_path / "l")
    extra = prepare_gameplay(src, GameplaySpec(path=src, in_s=3, out_s=5.5, use_audio=False), r, tmp_path / "x")
    joined = join_gameplay(landing, extra, r, tmp_path / "j")
    info = probe(joined.video)
    assert joined.real_frame == landing.real_frame
    assert info.video_duration == pytest.approx(4.5, abs=0.05) and joined.duration == pytest.approx(4.5, abs=0.05)
    assert joined.audio is not None and probe(joined.audio).duration == pytest.approx(4.5, abs=0.05)


@pytest.fixture(scope="module")
def cuts_video(tmp_path_factory) -> Path:
    """Four shots with hard cuts at frames 60, 105 and 165 (30 fps): 2 s, 1.5 s, 2 s, 2.5 s."""
    out = tmp_path_factory.mktemp("seg") / "capture.mp4"
    run([
        "-f", "lavfi", "-i", "testsrc2=s=180x320:r=30:d=2",
        "-f", "lavfi", "-i", "color=c=red:s=180x320:r=30:d=1.5",
        "-f", "lavfi", "-i", "smptebars=s=180x320:r=30:d=2",
        "-f", "lavfi", "-i", "mandelbrot=s=180x320:r=30,trim=duration=2.5",
        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0,format=yuv420p[v]", "-map", "[v]",
        "-c:v", "libx264", "-preset", "veryfast", "-g", "15", str(out),
    ])  # fmt: skip
    return out


@pytest.mark.parametrize("detector", ["content", "adaptive"])
def test_scene_detection_finds_synthetic_cuts_within_one_frame(cuts_video, detector):
    need("scenedetect")
    from creative_forge.segments import detect_scenes, suggest

    scenes, fps = detect_scenes(cuts_video, detector=detector)
    assert fps == pytest.approx(30)
    cuts = [s.start_frame for s in scenes[1:]]
    assert len(cuts) == 3, scenes
    for found, true in zip(cuts, (60, 105, 165), strict=True):
        assert abs(found - true) <= 1, (cuts, detector)
    picks = suggest(scenes, min_len_s=1.5, margin_s=0.2)
    assert [(p.in_s, p.out_s) for p in picks] == [(0.2, 1.8), (3.7, 5.3), (5.7, 7.8)]


def test_segments_command_prints_a_spec_ready_block(cuts_video, capsys):
    need("scenedetect")
    assert main(["segments", str(cuts_video), "--min-len", "1.5"]) == 0
    out = capsys.readouterr().out
    block = yaml.safe_load(out[out.index("variants:") :])
    segs = block["variants"]["segments"]
    assert [s["id"] for s in segs] == ["s01", "s02", "s03"] and segs[1]["in_s"] == 3.7
    d = matrix_spec()
    d["concepts"][0]["variants"]["segments"] = segs
    assert len(AdSpec.model_validate(d).concepts[0].segment_variants()) == 3
    assert main(["segments", str(cuts_video), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data["cuts_s"]) == 3 and abs(data["cuts_s"][0] - 2.0) <= 1 / 30 + 1e-6
