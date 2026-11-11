"""Model licence registry and the guard in `forge plan` / `forge run`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from creative_forge.cli import main
from creative_forge.licenses import (
    OK,
    OVERRIDDEN,
    REFUSE,
    SERVICE,
    WARN,
    LicenseRefused,
    ModelUse,
    Registry,
    check,
    evaluate,
    parse_overrides,
)
from creative_forge.spec import AdSpec

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "ads.yaml"


def spec(**over) -> AdSpec:
    d = {
        "name": "t",
        "providers": "fake",
        "concepts": [
            {
                "id": "c1",
                "gameplay": {"path": "missing.mp4", "in_s": 0, "out_s": 10},
                "hook": {"video_prompt": "v", "image_prompt": "add a hand entering from the left edge; no text"},
                "music": {"prompt": "m"},
            }
        ],
    }
    d.update(over)
    return AdSpec.model_validate(d)


def test_registry_entries_are_sourced_and_dated():
    reg = Registry.load()
    assert reg.as_of == "2026-11-11"
    ids = [e.id for e in reg.entries]
    assert len(ids) == len(set(ids))
    for e in reg.entries:
        assert e.source.startswith("https://"), e.id
        assert e.match and all(p == p.lower() for p in e.match), e.id
        if e.commercial != "service":  # hosted APIs have no weights licence to read
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", e.checked or ""), e.id
            assert e.weights_license, e.id
    # every family the research names is in the registry
    for name in (
        "veo-3.1-generate-001",
        "gemini-3-pro-image-preview",
        "lyria-3-pro-preview",
        "wan2.1_flf2v_720p_14B_fp16.safetensors",
        "wan2.1_i2v_480p_14B_fp16.safetensors",
        "wan2.2_ti2v_5B_fp16.safetensors",
        "wan2.2_i2v_high_noise_14B_fp16.safetensors",
        "qwen_image_edit_2509_fp8_e4m3fn.safetensors",
        "sd_xl_base_1.0.safetensors",
        "ltx-video-2b-v0.9.5.safetensors",
        "flux1-kontext-dev.safetensors",
        "flux2-dev.safetensors",
        "hunyuan_video_t2v_720p_bf16.safetensors",
        "musicgen-small",
        "xtts-v2",
        "f5-tts",
        "tr_TR-dfki-medium",
        "bria-rmbg",
        "birefnet-general",
    ):
        assert reg.lookup(name) is not None, name


@pytest.mark.parametrize(
    "model, status, words",
    [
        ("flux1-kontext-dev.safetensors", REFUSE, "revenue-generating"),
        ("flux2-dev.safetensors", REFUSE, "Non-Commercial"),
        ("musicgen-medium", REFUSE, "cc-by-nc-4.0"),
        ("xtts-v2", REFUSE, "coqui-public-model-license"),
        ("f5-tts", REFUSE, "cc-by-nc-4.0"),
        ("tr_TR-dfki-medium", REFUSE, "BY-NC-SA"),
        ("bria-rmbg", REFUSE, "paid agreement"),
        ("rmbg-1.4", REFUSE, "paid agreement"),
        ("birefnet-general", OK, "pin"),
        ("wan2.1_flf2v_720p_14B_fp8_e4m3fn.safetensors", OK, "Chinese"),
        ("qwen-image-edit-2511", OK, "2511"),
        ("ltx-2-19b-dev.safetensors", WARN, "$10,000,000"),
        ("cogvideox-5b-i2v", WARN, "registration"),
        ("veo-3.1-generate-001", SERVICE, "terms"),
        ("my-own-finetune.safetensors", WARN, "not in the licence registry"),
    ],
)
def test_each_model_gets_a_named_verdict(model, status, words):
    f = evaluate(spec(), ModelUse("x", model, "comfyui"), Registry.load())
    assert f.status == status and words in f.reason, f


@pytest.mark.parametrize(
    "targets, status",
    [
        ({}, REFUSE),  # no regions declared: the ads may serve in the EU
        ({"regions": ["TR", "US"]}, WARN),
        ({"regions": ["TR", "DE"]}, REFUSE),  # Germany is in the EU
        ({"regions": ["GB"]}, REFUSE),  # GB is the UK
        ({"regions": ["EEA"]}, REFUSE),
        ({"regions": ["KR"]}, REFUSE),
    ],
)
def test_hunyuan_territory_clause_uses_the_target_regions(targets, status):
    use = ModelUse("video", "hunyuan_video_i2v_720p_bf16.safetensors", "comfyui")
    f = evaluate(spec(targets=targets), use, Registry.load())
    assert f.status == status, f.reason
    if status == REFUSE:
        assert "excludes" in f.reason


def test_ltx_revenue_cap_warns_below_and_refuses_at_the_cap():
    use = ModelUse("video", "ltx-video-2b-v0.9.5.safetensors", "comfyui")
    assert evaluate(spec(), use, Registry.load()).status == WARN
    assert "not declared" in evaluate(spec(), use, Registry.load()).reason
    assert evaluate(spec(targets={"annual_revenue_usd": 250_000}), use, Registry.load()).status == WARN
    refused = evaluate(spec(targets={"annual_revenue_usd": 12_000_000}), use, Registry.load())
    assert refused.status == REFUSE and "paid licence" in refused.reason


def test_spec_models_are_collected_by_role():
    s = spec(image_provider="comfyui", comfyui={"checkpoint": "flux1-kontext-dev.safetensors"})
    report = check(s)
    roles = {f.role: f for f in report.findings}
    assert set(roles) == {"image_edit", "video", "music", "judge"}
    assert roles["image_edit"].status == REFUSE and roles["video"].status == SERVICE
    with pytest.raises(LicenseRefused, match="flux1-kontext-dev"):
        report.raise_if_refused()
    # no judge runs and no music prompt: those models are not called, so not checked
    quiet = spec(qa={"judge_runs": 0})
    quiet_roles = {f.role for f in check(quiet).findings}
    assert "judge" not in quiet_roles


def test_override_needs_a_reason_and_is_per_model():
    with pytest.raises(ValueError, match="MODEL=REASON"):
        parse_overrides(["flux1-kontext-dev.safetensors"])
    with pytest.raises(ValueError, match="MODEL=REASON"):
        parse_overrides(["flux1-kontext-dev.safetensors=  "])
    s = spec(image_provider="comfyui", comfyui={"checkpoint": "flux1-kontext-dev.safetensors"})
    report = check(s, parse_overrides(["flux1-kontext-dev.safetensors=internal mood board, never shipped"]))
    assert not report.refused
    (f,) = report.overridden
    assert f.status == OVERRIDDEN and f.override_reason == "internal mood board, never shipped"
    # an override for another model changes nothing
    assert check(s, {"musicgen-small": "x"}).refused


def test_plan_exits_nonzero_and_names_the_reason(tmp_path, capsys):
    p = tmp_path / "s.yaml"
    p.write_text(
        "name: t\nproviders: fake\nimage_provider: comfyui\ncomfyui: {checkpoint: flux2-dev.safetensors}\n"
        "concepts:\n  - {id: c1, gameplay: {path: g.mp4, in_s: 0, out_s: 10}, "
        "hook: {video_prompt: v, image_prompt: 'a hand enters from the left edge; no text'}}\n",
        encoding="utf-8",
    )
    assert main(["plan", str(p)]) == 1
    captured = capsys.readouterr()
    assert "flux2-dev.safetensors" in captured.err and "Non-Commercial" in captured.err
    assert "LICENSE-FLUX-DEV" in captured.err and "--license-override" in captured.err
    # run refuses before building any provider
    assert main(["run", str(p), "--dry-run"]) == 4
    # an explicit override with a reason lets the plan through
    assert main(["plan", str(p), "--license-override", "flux2-dev.safetensors=licensed from BFL, contract 42"]) == 0
    assert "overridden" in capsys.readouterr().out


def test_example_spec_passes_the_guard(capsys):
    assert main(["plan", str(EXAMPLE)]) == 0
    out = capsys.readouterr().out
    assert "model licences:" in out and "service" in out


def test_pipeline_refuses_before_any_generation_and_records_overrides(tmp_path):
    from creative_forge.config import Settings
    from creative_forge.pipeline import Pipeline
    from creative_forge.providers.fake import fake_providers

    s = spec(models={"music": "musicgen-small"}, output_dir=str(tmp_path / "out"))
    providers = fake_providers()
    settings = Settings(project=None, cache_dir=tmp_path / "cache")
    with pytest.raises(LicenseRefused, match="musicgen-small"):
        Pipeline(s, providers, settings, log=lambda _: None).run()
    assert providers.video.calls == 0 and providers.music.calls == 0

    pipe = Pipeline(s, providers, settings, log=lambda _: None, license_overrides={"musicgen-small": "test fixture"})
    rr = pipe.run()  # the gameplay clip is missing, so the concept fails fast, but the run is recorded
    assert rr.concepts[0].error and "gameplay clip not found" in rr.concepts[0].error
    ledger = json.loads((tmp_path / "out" / "cost_ledger.json").read_text(encoding="utf-8"))
    (rec,) = ledger["license_overrides"]
    assert rec["model"] == "musicgen-small" and rec["override_reason"] == "test fixture"
    assert "cc-by-nc-4.0" in rec["refused_because"] and rec["source"].startswith("https://huggingface.co/")


def test_licenses_command(capsys):
    assert main(["licenses"]) == 0
    assert "flux1-kontext-dev" in capsys.readouterr().out
    assert main(["licenses", "wan2.2_ti2v_5B_fp16.safetensors"]) == 0
    out = capsys.readouterr().out
    assert "wan2.2-ti2v-5b" in out and "apache-2.0" in out and "huggingface.co/Wan-AI" in out
    assert main(["licenses", "nope.safetensors"]) == 1
