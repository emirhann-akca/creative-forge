"""Synthetic media fixtures, generated once per session with ffmpeg (small frames keep the suite fast)."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from creative_forge.ffmpeg import ENV_FFMPEG_DIR, FFmpegError, find_tools, run
from creative_forge.paths import ENV_HOME

_ISOLATED_HOME: str | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Tests never write into the real user data and cache folders (doctor's write probe, the compiled Vision helper):
    CREATIVE_FORGE_HOME points at a temporary folder. The ffmpeg this machine resolves (managed build or PATH) is pinned
    first with CREATIVE_FORGE_FFMPEG_DIR, so moving the home does not hide it. Runs before test modules are imported."""
    global _ISOLATED_HOME
    try:
        tools = find_tools()
        if tools.ffprobe.parent == tools.ffmpeg.parent:
            os.environ.setdefault(ENV_FFMPEG_DIR, str(tools.ffmpeg.parent))
    except FFmpegError:
        pass  # the tests that need ffmpeg fail with the resolver's own message
    _ISOLATED_HOME = tempfile.mkdtemp(prefix="forge-test-home-")
    os.environ[ENV_HOME] = _ISOLATED_HOME


def pytest_unconfigure(config: pytest.Config) -> None:
    if _ISOLATED_HOME:
        shutil.rmtree(_ISOLATED_HOME, ignore_errors=True)


W, H = 180, 320


def tone(amplitude: float, freq: int = 997) -> str:
    expr = f"{amplitude}*sin(2*PI*{freq}*t)"
    return f"aevalsrc='{expr}|{expr}':s=48000"


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("media")


@pytest.fixture(scope="session")
def tone_minus20(media: Path) -> Path:
    """Stereo 997 Hz sine with 0.1 peak amplitude: -20 dBFS peak, which BS.1770 reads as -20 LUFS."""
    out = media / "tone_-20.wav"
    run(["-f", "lavfi", "-i", tone(0.1) + ":d=5", str(out)])
    return out


@pytest.fixture(scope="session")
def good_video(media: Path) -> Path:
    """6 s of moving picture with continuous audio normalized near -14.3 LUFS."""
    out = media / "good.mp4"
    run([
        "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=6",
        "-f", "lavfi", "-i", tone(0.3, 440) + ":d=6",
        "-af", "loudnorm=I=-14.3:TP=-3:LRA=7:linear=true", "-ar", "48000",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-b:a", "160k", "-t", "6", str(out),
    ])  # fmt: skip
    return out


@pytest.fixture(scope="session")
def short_audio_video(media: Path) -> Path:
    """The production defect: picture 5.0 s, audio 3.4 s (muxed without -shortest)."""
    out = media / "short_audio.mp4"
    run([
        "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=5",
        "-f", "lavfi", "-i", tone(0.2, 440) + ":d=3.4",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", str(out),
    ])  # fmt: skip
    return out


@pytest.fixture(scope="session")
def frozen_intro_video(media: Path) -> Path:
    """First second frozen (cloned frame) and first 0.6 s silent."""
    out = media / "frozen_intro.mp4"
    run([
        "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=4",
        "-f", "lavfi", "-i", "aevalsrc='if(lt(t,0.6),0,0.2*sin(2*PI*440*t))':s=48000:d=5",
        "-vf", "tpad=start_duration=1:start_mode=clone",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-t", "5", str(out),
    ])  # fmt: skip
    return out


@pytest.fixture(scope="session")
def clip24(media: Path) -> Path:
    out = media / "clip24.mp4"
    run(["-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=24:d=3", "-c:v", "libx264", "-crf", "16", str(out)])
    return out


@pytest.fixture(scope="session")
def dup30(media: Path, clip24: Path) -> Path:
    """24 -> 30 fps by frame repetition (what a plain fps filter does)."""
    out = media / "dup30.mp4"
    run(["-i", str(clip24), "-vf", "fps=30", "-c:v", "libx264", "-crf", "16", str(out)])
    return out


@pytest.fixture(scope="session")
def mci30(media: Path, clip24: Path) -> Path:
    """24 -> 60 fps motion-compensated, then an exact 2:1 drop to 30 (what the pipeline does)."""
    out = media / "mci30.mp4"
    run([
        "-i", str(clip24),
        "-vf", "minterpolate=fps=60:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1,fps=30",
        "-c:v", "libx264", "-crf", "16", str(out),
    ])  # fmt: skip
    return out


@pytest.fixture(scope="session")
def loudness_jump_video(media: Path) -> Path:
    """Quiet 'hook' (0.02 amplitude) for 3 s, then loud 'gameplay' (0.3): about a 23 LU jump."""
    out = media / "jump.mp4"
    expr = "if(lt(t,3),0.02,0.3)*sin(2*PI*440*t)"
    run([
        "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=8",
        "-f", "lavfi", "-i", f"aevalsrc='{expr}|{expr}':s=48000:d=8",
        "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-t", "8", str(out),
    ])  # fmt: skip
    return out


@pytest.fixture(scope="session")
def hard_cut_video(media: Path) -> Path:
    """2 s of flat red, then the test pattern: a seam no crossfade can hide."""
    out = media / "hard_cut.mp4"
    run([
        "-f", "lavfi", "-i", f"color=c=red:s={W}x{H}:r=30:d=2",
        "-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r=30:d=3",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,format=yuv420p[v]", "-map", "[v]",
        "-c:v", "libx264", "-preset", "veryfast", str(out),
    ])  # fmt: skip
    return out


@pytest.fixture(scope="session")
def mix_source(media: Path) -> tuple[Path, float, float, float]:
    """A master-stage input: smooth, quiet 'AI hook' noise for 4 s, then transient 'gameplay' clicks over a bed.
    The clicks are what the limiter squashes harder than the hook. Returns (wav, hook_end, g0, g1)."""
    out = media / "mix_source.wav"
    hook = "0.08*sin(2*PI*330*t)*(0.6+0.4*sin(2*PI*0.5*t))"
    game = "0.45*sin(2*PI*1200*t)*exp(-25*mod(t,0.25))+0.05*sin(2*PI*110*t)"
    expr = f"if(lt(t,4),{hook},{game})"
    run(["-f", "lavfi", "-i", f"aevalsrc='{expr}|{expr}':s=48000:d=12", "-c:a", "pcm_s24le", str(out)])
    return out, 4.0, 4.6, 11.5


def need(module: str):
    """Import an optional extra's module, or skip. CI sets CREATIVE_FORGE_REQUIRE_EXTRAS=1 so a missing extra fails
    the job instead of quietly skipping its tests."""
    if os.environ.get("CREATIVE_FORGE_REQUIRE_EXTRAS") == "1":
        import importlib

        return importlib.import_module(module)
    return pytest.importorskip(module)
