"""Audio/video stages: the fixes for the production defects, verified on synthetic media."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from creative_forge.ffmpeg import encoders, extract_frame, probe, run
from creative_forge.loudness import measure
from creative_forge.spec import AudioSpec, ExportSpec, RenderSpec
from creative_forge.stages.audio import Placed, master, mix, mux
from creative_forge.stages.export import export_4x5, export_9x16, maxrate_for, size_4x5
from creative_forge.stages.video import atempo_chain

ENCODERS = ["aac"] + (["aac_at"] if "aac_at" in encoders() else [])


@pytest.mark.parametrize("encoder", ENCODERS)
def test_master_loop_matches_hook_to_gameplay_after_limiter(mix_source, tmp_path, encoder):
    src, hook_end, g0, g1 = mix_source
    before_hook = measure(src, 0, hook_end).integrated_lufs
    before_game = measure(src, g0, g1).integrated_lufs
    assert abs(before_hook - before_game) > 5  # the kind of gap production saw (+5 to -9 LU)

    a = AudioSpec(encoder=encoder)
    res = master(src, tmp_path / "m.m4a", hook_end, (g0, g1), a)
    final = res.final
    assert res.converged, res.to_dict()
    assert abs(final.integrated_lufs - a.target_lufs) < a.tolerance_lu
    assert abs(final.hook_lufs - final.gameplay_lufs) < a.hook_gameplay_max_delta_lu
    # true peak measured on the ENCODED file, independently of the loop's own numbers
    assert measure(res.path).true_peak_dbtp <= a.true_peak_max_dbtp + 0.05


def test_single_pass_is_not_enough(mix_source, tmp_path):
    """One correction round leaves a hook/gameplay gap: the limiter squashes the transient gameplay more."""
    src, hook_end, g0, g1 = mix_source
    res = master(src, tmp_path / "m1.m4a", hook_end, (g0, g1), AudioSpec(max_iterations=1))
    first = res.iterations[0]
    assert not res.converged
    assert abs(first.integrated_lufs - AudioSpec().target_lufs) > 0.3 or abs(first.delta_lu) > 0.8


def test_mix_pads_fx_bus_to_full_length(tmp_path):
    """Defect: the sidechain output ended with the last effect, so audio stopped 1.6 s before the picture."""
    fx = tmp_path / "fx.wav"
    music = tmp_path / "music.wav"
    run(["-f", "lavfi", "-i", "aevalsrc='0.3*sin(2*PI*800*t)|0.3*sin(2*PI*800*t)':s=48000:d=2", str(fx)])
    run(["-f", "lavfi", "-i", "aevalsrc='0.1*sin(2*PI*220*t)|0.1*sin(2*PI*220*t)':s=48000:d=20", str(music)])
    out = mix([Placed(fx, 1.0)], music, -19, 0.0, 10.0, AudioSpec(), tmp_path / "mix.wav")
    assert probe(out).audio_duration == pytest.approx(10.0, abs=0.02)
    # music is still playing in the final second (it would be cut if the bus were not padded)
    assert measure(out, 8.5, 9.4).integrated_lufs > -40


def test_mix_without_any_sources_is_silence_of_full_length(tmp_path):
    out = mix([], None, 0, 0, 3.0, AudioSpec(), tmp_path / "silent.wav")
    assert probe(out).audio_duration == pytest.approx(3.0, abs=0.02)


def test_mux_keeps_picture_length_with_short_audio(tmp_path, good_video):
    """Defect: `-shortest` cut the picture to a shorter audio track. Explicit -t keeps the picture."""
    audio = tmp_path / "short.m4a"
    run(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3", "-c:a", "aac", str(audio)])
    video_only = tmp_path / "v.mp4"
    run(["-i", str(good_video), "-an", "-c", "copy", str(video_only)])
    out = mux(video_only, audio, tmp_path / "muxed.mp4", 6.0)
    assert probe(out).video_duration == pytest.approx(6.0, abs=0.05)


def test_maxrate_keeps_60s_under_30mb():
    e = ExportSpec()
    assert maxrate_for(60, e) == 3600  # production: crf 20 + maxrate 3.6M for a 60 s ad
    assert maxrate_for(120, e) < 3600  # longer ads get a lower ceiling
    assert (maxrate_for(120, e) + e.audio_bitrate_kbps) * 120 / 8000 < e.max_file_mb


def test_4x5_centres_full_frame_with_blurred_sides(tmp_path, good_video):
    """4:5 must not crop gameplay: the whole 9:16 frame is in the middle column."""
    e = ExportSpec(preset="veryfast")
    out = export_4x5(good_video, tmp_path / "f45.mp4", 6.0, 180, e)
    info = probe(out)
    w, h = size_4x5(180)
    assert (info.width, info.height) == (w, h)
    assert info.audio_duration == pytest.approx(info.video_duration, abs=0.05)
    fg_w = round(h * 9 / 16) // 2 * 2
    x0 = (w - fg_w) // 2
    frame = np.asarray(extract_frame(out, 2.0), np.float32)
    ref = np.asarray(extract_frame(good_video, 2.0).resize((fg_w, h), Image.LANCZOS), np.float32)
    centre = frame[:, x0 : x0 + fg_w]
    assert np.abs(centre - ref).mean() < 12  # the full source frame, not a crop of it
    side = frame[:, : max(4, x0 - 2)]
    assert side.std() < np.asarray(ref).std()  # blurred background is smoother than the picture


def test_9x16_export_keeps_size_and_av_length(tmp_path, good_video):
    out = export_9x16(good_video, tmp_path / "f916.mp4", 6.0, ExportSpec(preset="veryfast"))
    info = probe(out)
    assert (info.width, info.height) == (180, 320)
    assert abs(info.video_duration - info.audio_duration) < 0.05


def test_atempo_chain_limits():
    assert atempo_chain(1.0) == []
    assert atempo_chain(1.1) == ["atempo=1.100000"]
    assert atempo_chain(3.0)[0] == "atempo=2.0"
    assert atempo_chain(0.3)[0] == "atempo=0.5"


def test_hook_prep_interpolates_without_repeats(tmp_path, clip24):
    """24 fps Veo output -> mci -> output fps: no repeated frames (plain fps conversion repeats 1 in 5)."""
    from creative_forge.qa.media_checks import check_cadence
    from creative_forge.qa.result import Status
    from creative_forge.spec import HookSpec
    from creative_forge.stages.video import prepare_hook

    real = tmp_path / "real.png"
    extract_frame(clip24, 2.9).save(real)
    h = HookSpec(video_prompt="x", duration_s=4, cut_s=2.5, patches=[{"box": [0, 0, 60, 40], "from_s": 1.0}])
    r = RenderSpec(width=180, height=320, fps=30, preset="veryfast")
    clip = prepare_hook(clip24, real, h, r, tmp_path)
    info = probe(clip.video)
    assert info.fps == pytest.approx(30, abs=0.01)
    assert clip.duration == pytest.approx(2.5, abs=0.05)
    assert check_cadence(info, 0, 2.5, 0.02, 0.08).status is Status.PASS
    assert clip.audio is None  # the fixture has no audio track
    # the patch shows the real frame's pixels after from_s
    late = np.asarray(extract_frame(clip.video, 2.3), np.float32)[5:35, 5:55]
    ref = np.asarray(Image.open(real), np.float32)[5:35, 5:55]
    assert np.abs(late - ref).mean() < 15


def test_per_job_temp_dirs_do_not_collide(tmp_path, good_video):
    """Defect: parallel jobs sharing temp files corrupted outputs. Two concurrent exports of different sources
    into private dirs produce two distinct, valid files."""
    from concurrent.futures import ThreadPoolExecutor

    from creative_forge.ffmpeg import probe as pr

    other = tmp_path / "other.mp4"
    run(["-i", str(good_video), "-vf", "hflip", "-c:a", "copy", str(other)])

    def job(src: Path, name: str) -> Path:
        d = tmp_path / name
        d.mkdir()
        return export_9x16(src, d / "out.mp4", 6.0, ExportSpec(preset="veryfast"))

    with ThreadPoolExecutor(2) as pool:
        a, b = pool.map(job, [good_video, other], ["job-a", "job-b"])
    assert a != b and pr(a).video_duration == pytest.approx(6.0, abs=0.05)
    fa = np.asarray(extract_frame(a, 1.0), np.float32)
    fb = np.asarray(extract_frame(b, 1.0), np.float32)
    assert np.abs(fa - fb[:, ::-1]).mean() < 10  # b is the mirror of a, not a copy of it
