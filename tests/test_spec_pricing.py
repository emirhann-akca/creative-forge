from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from creative_forge.lint import lint
from creative_forge.pricing import Pricing, estimate
from creative_forge.spec import AdSpec, load_spec, timeline_for

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ads.yaml"


def minimal(**over) -> dict:
    d = {
        "name": "t",
        "concepts": [
            {
                "id": "c1",
                "gameplay": {"path": "g.mp4", "in_s": 1.0, "out_s": 11.0},
                "hook": {"video_prompt": "v", "image_prompt": "i, do not add any text", "duration_s": 8},
                "music": {"prompt": "m"},
            }
        ],
    }
    d.update(over)
    return d


def test_example_spec_is_valid():
    spec = load_spec(EXAMPLE)
    assert [c.id for c in spec.concepts] == ["dust-blower", "pressure-wash"]
    assert spec.base_dir == EXAMPLE.parent
    assert spec.resolve(Path("footage/x.mp4")) == EXAMPLE.parent / "footage/x.mp4"
    assert spec.concepts[0].hook.patches[0].box == (515, 40, 915, 335)


def test_defaults_are_production_values():
    s = AdSpec.model_validate(minimal())
    assert s.audio.target_lufs == -14.3 and s.audio.limiter_limit == 0.66 and s.audio.max_iterations == 6
    assert s.export.crf == 20 and s.export.maxrate_kbps == 3600 and s.export.max_file_mb == 30
    assert s.render.crossfade_s == 0.3 and s.render.hook_interpolate_fps == 60
    assert s.concepts[0].hook.effective_cut == 7.0  # 8 s minus the 1 s tail Veo drifts in
    assert s.concepts[0].formats == ["9:16", "4:5"]
    assert s.models.video == "veo-3.1-generate-001"


@pytest.mark.parametrize(
    "mutate, msg",
    [
        (lambda d: d["concepts"][0]["gameplay"].update(out_s=0.5), "out_s"),
        (lambda d: d["concepts"].append(dict(d["concepts"][0])), "duplicate"),
        (lambda d: d["concepts"][0]["hook"].update(samples=1, pick=1), "pick"),
        (lambda d: d["concepts"][0]["hook"].update(cut_s=9.0), "cut_s"),
        (lambda d: d["concepts"][0]["hook"].update(duration_s=5), "duration_s"),
        (lambda d: d["concepts"][0].update(music={"prompt": "a", "path": "b.wav"}), "exactly one"),
        (lambda d: d["concepts"][0].update(formats=["9:16", "9:16"]), "unique"),
        (lambda d: d["concepts"][0].update(formats=["1:1"]), "formats"),
        (lambda d: d["concepts"][0].update(id="Bad Id"), "id"),
        (lambda d: d.update(render={"width": 1081}), "even"),
        (lambda d: d.update(render={"width": 1920, "height": 1080}), "portrait"),
        (lambda d: d.update(typo_key=1), "typo_key"),
        (lambda d: d["concepts"][0]["hook"].update(patches=[{"box": [0, 0, 5000, 10], "from_s": 1}]), "patch"),
        # names become output file names, model ids become REST URL path segments
        (lambda d: d.update(name="../../elsewhere"), "name"),
        (lambda d: d.update(models={"video": "veo/../../other:predict"}), "video"),
        # caught at load time, not after the paid generations
        (lambda d: d.update(export={"preset": "slwo"}), "preset"),
        (lambda d: d["concepts"][0]["hook"].update(text_regions=[[50, 10, 20, 30]]), "text_regions"),
        (lambda d: d["concepts"][0]["gameplay"].update(crop=[0, 150, 0, 2144]), "crop"),
    ],
)
def test_invalid_specs_are_rejected(mutate, msg):
    d = minimal()
    mutate(d)
    with pytest.raises(ValidationError, match=msg):
        AdSpec.model_validate(d)


def test_timeline_math():
    s = AdSpec.model_validate(minimal())
    t = timeline_for(s, s.concepts[0])
    # hook 7.0 + gameplay 10.0 + end card 2.6 - two 0.3 s crossfades
    assert t["total"] == pytest.approx(19.0)
    assert t["gameplay_start"] == pytest.approx(6.7)
    assert t["end_card_start"] == pytest.approx(16.4)


def test_load_spec_rejects_non_mapping(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("- just a list\n", encoding="utf-8")
    with pytest.raises(ValueError, match="mapping"):
        load_spec(p)


def test_bundled_pricing_is_october_2026():
    p = Pricing.load()
    assert p.as_of == "2026-10"
    assert (p.video_per_second_audio, p.video_per_second_silent) == (0.40, 0.20)
    assert p.image_per_image == 0.134 and p.music_per_track == 0.08
    assert p.judge_per_call is None
    assert "Verify" in p.note


def test_cost_estimate():
    s = AdSpec.model_validate(minimal())
    s.concepts[0].hook.samples = 2
    plan = estimate(s, Pricing.load())
    by_kind = {ln.kind: ln.usd for ln in plan.lines}
    assert by_kind["hook image"] == pytest.approx(0.134)
    assert by_kind["hook video"] == pytest.approx(2 * 8 * 0.40)
    assert by_kind["music"] == pytest.approx(0.08)
    assert by_kind["judge"] is None  # unpriced, listed separately
    assert plan.total_priced == pytest.approx(0.134 + 6.4 + 0.08)
    table = plan.table()
    assert "unpriced" in table and "verify" in table


def test_silent_video_and_override_pricing(tmp_path):
    d = yaml.safe_load(resources.files("creative_forge.data").joinpath("pricing.yaml").read_text(encoding="utf-8"))
    d["judge"]["per_call"] = 0.05
    f = tmp_path / "pricing.local.yaml"
    f.write_text(yaml.safe_dump(d), encoding="utf-8")
    p = Pricing.load(f)
    assert p.judge(2) == pytest.approx(0.10)
    assert p.video(8, 1, audio=False) == pytest.approx(1.6)


def test_lint_rules():
    d = minimal()
    h = d["concepts"][0]["hook"]
    h["image_prompt"] = "Cover the entire board in thick mud. Add a hand holding a pressure washer."
    h["cut_s"] = 7.6
    rules = {i.rule for i in lint(AdSpec.model_validate(d))}
    assert {"hand-enters-from-edge", "no-new-text", "effects-programmatic", "tail-drift", "strip-text"} <= rules

    h["image_prompt"] = "Add a hand holding a leaf blower, entering from the bottom right corner. Do not add any text."
    h["cut_s"] = 4.5
    h["text_regions"] = [[0, 0, 10, 10]]
    d["concepts"][0]["tags"] = {"hook_type": "real-hand", "mechanic_shown": ["goods-sort"], "emotion": ["calm"]}
    assert lint(AdSpec.model_validate(d)) == []

    h["cut_s"] = 5.6  # inside the last 3 s without patches
    assert [i.rule for i in lint(AdSpec.model_validate(d))] == ["tail-ui"]


@pytest.mark.parametrize(
    "prompt, flagged",
    [
        ("Add a hand holding a blower, entering from the bottom right corner. Do not add any text.", False),
        ("A hand with a wand enters from the left edge of the frame. No text.", False),
        # verbatim from the one real `forge ideate` run: "from ... corner" satisfied the old regex
        ("A human hand wiping dust from the top-right corner of a supermarket shelf. No text.", True),
    ],
)
def test_hand_must_enter_from_an_edge(prompt, flagged):
    d = minimal()
    d["concepts"][0]["hook"]["image_prompt"] = prompt
    rules = {i.rule for i in lint(AdSpec.model_validate(d))}
    assert ("hand-enters-from-edge" in rules) is flagged
