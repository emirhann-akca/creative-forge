"""Every QA check against synthetic media with a known answer."""

from __future__ import annotations

import pytest
from PIL import Image

from creative_forge.ffmpeg import probe
from creative_forge.loudness import measure
from creative_forge.qa.audio_checks import check_hook_gameplay, check_loudness, check_true_peak
from creative_forge.qa.media_checks import (
    check_av_duration,
    check_cadence,
    check_duration,
    check_file_size,
    check_format,
    check_intro_freeze,
    check_intro_silence,
)
from creative_forge.qa.result import Status, worst
from creative_forge.qa.seam import check_seam, check_seam_frames, seam_diff


def test_ebur128_reads_known_tone(tone_minus20):
    m = measure(tone_minus20)
    assert m.integrated_lufs == pytest.approx(-20.0, abs=0.3)
    assert m.true_peak_dbtp == pytest.approx(-20.0, abs=0.3)


def test_loudness_check_against_target(tone_minus20):
    info = probe(tone_minus20)
    assert check_loudness(info, -20.0, 1.0, 2.0).status is Status.PASS
    r = check_loudness(info, -14.3, 1.0, 2.0)
    assert r.status is Status.FAIL
    assert r.value == pytest.approx(-20.0, abs=0.3)


def test_true_peak_thresholds(tone_minus20, good_video):
    assert check_true_peak(probe(tone_minus20), -2.0, -1.0).status is Status.PASS
    assert check_true_peak(probe(tone_minus20), -25.0, -22.0).status is Status.FAIL
    assert check_true_peak(probe(tone_minus20), -21.0, -19.0).status is Status.WARN


def test_av_duration_mismatch_is_caught(short_audio_video):
    r = check_av_duration(probe(short_audio_video), 0.1, 0.5)
    assert r.status is Status.FAIL
    assert r.value == pytest.approx(1.6, abs=0.1)


def test_av_duration_ok(good_video):
    assert check_av_duration(probe(good_video), 0.1, 0.5).status is Status.PASS


def test_duration_bounds(good_video):
    info = probe(good_video)
    assert check_duration(info, 5, 10).status is Status.PASS
    assert check_duration(info, 15, 60).status is Status.FAIL


def test_format_checks(good_video):
    info = probe(good_video)
    assert check_format(info, "9:16").status is Status.PASS
    assert check_format(info, "4:5").status is Status.FAIL
    assert check_format(info, "9:16", (1080, 1920)).status is Status.FAIL
    assert check_format(info).status is Status.PASS  # any standard aspect


def test_file_size_cap(good_video):
    info = probe(good_video)
    assert check_file_size(info, 30).status is Status.PASS
    assert check_file_size(info, 0.001).status is Status.FAIL


def test_frozen_intro_is_caught(frozen_intro_video):
    r = check_intro_freeze(probe(frozen_intro_video), 1.0, 0.4, -60)
    assert r.status is Status.FAIL
    assert r.value == pytest.approx(1.0, abs=0.1)


def test_silent_intro_is_caught(frozen_intro_video):
    r = check_intro_silence(probe(frozen_intro_video), 1.0, 0.4, -50)
    assert r.status is Status.FAIL
    assert r.value == pytest.approx(0.6, abs=0.05)


def test_moving_intro_passes(good_video):
    info = probe(good_video)
    assert check_intro_freeze(info, 1.0, 0.4, -60).status is Status.PASS
    assert check_intro_silence(info, 1.0, 0.4, -50).status is Status.PASS


def test_cadence_flags_repeated_frames(dup30):
    r = check_cadence(probe(dup30), 0.0, 3.0, 0.02, 0.08)
    assert r.status is Status.FAIL
    assert r.value == pytest.approx(0.2, abs=0.03)  # one repeat every 5 frames


def test_cadence_passes_motion_interpolation(mci30):
    assert check_cadence(probe(mci30), 0.0, 3.0, 0.02, 0.08).status is Status.PASS


def test_hook_gameplay_delta_is_caught(loudness_jump_video):
    info = probe(loudness_jump_video)
    r = check_hook_gameplay(info, 3.0, 3.6, 8.0, 0.8, 2.0)
    assert r.status is Status.FAIL
    assert r.value < -15  # hook much quieter than gameplay
    assert check_hook_gameplay(info, None, None, None, 0.8, 2.0).status is Status.SKIP


def test_seam_diff_identical_and_different():
    a = Image.new("RGB", (90, 160), (200, 40, 40))
    assert seam_diff(a, a.copy()).mean == pytest.approx(0.0)
    b = Image.new("RGB", (90, 160), (40, 200, 40))
    r = check_seam_frames("seam", a, b, 8, 16, 40, 70)
    assert r.status is Status.FAIL
    small = a.copy()
    small.paste((255, 255, 255), (0, 0, 6, 6))  # a small invented UI element: global mean low, worst block high
    r2 = check_seam_frames("landing", a, small, 8, 16, 40, 70)
    assert r2.value < 8 and r2.details["worst_block"] > 70 and r2.status is Status.FAIL


def test_seam_on_video(hard_cut_video, good_video):
    assert check_seam(probe(hard_cut_video), 1.8, 0.3, 12, 24).status is Status.FAIL  # red | pattern at 2.0 s
    assert check_seam(probe(good_video), None, 0.3, 12, 24).status is Status.SKIP


def test_worst_ignores_skips():
    from creative_forge.qa.result import CheckResult, skipped

    assert worst([CheckResult("a", Status.PASS, 1.0, ""), skipped("b", "x")]) is Status.PASS
    assert worst([skipped("b", "x")]) is Status.SKIP
    assert worst([CheckResult("a", Status.WARN, 1.0, ""), CheckResult("c", Status.FAIL, 1.0, "")]) is Status.FAIL
