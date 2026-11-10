from __future__ import annotations

import statistics

import pytest
from PIL import Image, ImageDraw

from creative_forge.ffmpeg import run
from creative_forge.imaging import font
from creative_forge.providers.base import JudgeRequest
from creative_forge.providers.fake import FakeJudgeProvider
from creative_forge.qa.judge import aggregate, blind_codes, check_judge
from creative_forge.qa.ocr import OcrToken, check_hook_text, classify, detect_backend, levenshtein
from creative_forge.qa.result import Status


def test_levenshtein():
    assert levenshtein("MILK", "MILK") == 0
    assert levenshtein("MIILAK", "MILK") == 2
    assert levenshtein("", "ABC") == 3


def test_classify_garbled_vs_unexpected():
    toks = [
        OcrToken("f1", "MIILAK", 0.9),  # the production example
        OcrToken("f1", "PLAY NOW", 0.9),  # known
        OcrToken("f2", "Not", 0.9),  # 3 letters: too short to call garbled
        OcrToken("f2", "Bananaz", 0.1),  # low confidence: ignored
        OcrToken("f3", "QWERTY", 0.9),  # nothing like the game's words
    ]
    found = {(f.word, f.kind) for f in classify(toks, ["MILK", "PLAY NOW"])}
    assert found == {("MIILAK", "garbled"), ("Not", "unexpected"), ("QWERTY", "unexpected")}


def test_classify_ignores_overlay_regions():
    toks = [OcrToken("f", "footage", 1.0, (0.3, 0.6, 0.7, 0.65))]
    assert classify(toks, [], ignore=[(0.2, 0.55, 0.8, 0.7)]) == []
    assert len(classify(toks, [])) == 1


def test_ocr_without_backend_is_an_explicit_skip(tmp_path):
    r = check_hook_text(tmp_path / "x.mp4", 0, 3, 2, [], None)
    assert r.status is Status.SKIP and "no OCR backend" in r.message


@pytest.mark.skipif(detect_backend() is None, reason="no OCR backend (macOS Vision or tesseract) on this machine")
def test_garbled_text_in_hook_is_caught(tmp_path):
    """Render the production defect ('MILK' copied by Veo as 'MIILAK') into a clip and read it back."""
    img = Image.new("RGB", (360, 640), (40, 120, 60))
    ImageDraw.Draw(img).text((70, 260), "MIILAK", font=font(64), fill="white")
    png = tmp_path / "f.png"
    img.save(png)
    clip = tmp_path / "hook.mp4"
    run(["-loop", "1", "-i", str(png), "-t", "1.5", "-r", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(clip)])
    r = check_hook_text(clip, 0, 1.5, 2, ["MILK", "PLAY NOW"], detect_backend())
    assert r.status is Status.FAIL
    assert any(f["word"] == "MIILAK" for f in r.details["findings"])


def test_judge_noise_is_flagged():
    """Production: the same video scored 8 in one run and 4 in the next."""
    agg = aggregate([{"scores": {"overall": 8}}, {"scores": {"overall": 4}}])
    assert agg.mean["overall"] == 6
    assert agg.stdev["overall"] == pytest.approx(statistics.stdev([8, 4]))
    assert agg.unstable == ["overall"]
    r = check_judge(agg)
    assert r.status is Status.WARN and "unstable" in r.message


def test_single_judge_run_is_not_trusted():
    r = check_judge(aggregate([{"scores": {"overall": 9}}]))
    assert r.status is Status.WARN and "single" in r.message


def test_stable_runs_pass_and_floor_fails():
    runs = [{"scores": {"overall": 7, "hook_stop_power": 6}, "flaws": [{"t": "00:03", "flaw": "x"}]}] * 3
    agg = aggregate(runs)
    assert check_judge(agg).status is Status.PASS
    assert len(agg.flaws) == 3
    assert check_judge(agg, min_overall=8).status is Status.FAIL


def test_failed_runs_are_reported():
    agg = aggregate([{"error": "quota"}, {"error": "quota"}])
    r = check_judge(agg)
    assert agg.runs == 0 and r.status is Status.FAIL and "quota" in r.message


def test_blind_codes_shuffle_deterministically(tmp_path):
    vids = [tmp_path / f"{n}.mp4" for n in "abcdef"]
    a, b = blind_codes(vids, 1), blind_codes(vids, 1)
    assert a == b and list(a) == [f"V0{i}" for i in range(1, 7)]
    assert sorted(a.values()) == sorted(vids)
    assert list(a.values()) != vids  # order is not the input order


def test_fake_judge_is_deterministic_per_run(good_video):
    j = FakeJudgeProvider()
    r0 = j.score(JudgeRequest(good_video, "p", "m", run_index=0))
    assert r0 == j.score(JudgeRequest(good_video, "p", "m", run_index=0))
    assert set(r0["scores"]) >= {"overall", "hook_stop_power"}
