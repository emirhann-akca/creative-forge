"""``forge demo``: the full pipeline offline, on synthetic gameplay, with fake providers. No keys, no cost."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml
from PIL import Image, ImageDraw

from creative_forge.config import Settings
from creative_forge.ffmpeg import run
from creative_forge.imaging import font
from creative_forge.pipeline import Pipeline, RunResult
from creative_forge.providers.fake import fake_providers
from creative_forge.spec import load_spec

W, H = 360, 640
LABEL_BOX = (110, 14, 250, 50)  # where the synthetic "LEVEL 7" HUD label is drawn


def synthetic_gameplay(out: Path, seconds: float = 12.0) -> Path:
    """Moving test pattern (testsrc2) with a HUD label and click-like "SFX" on a quiet bed, at 60 fps."""
    hud = out.parent / "hud.png"
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 64), fill=(40, 24, 70, 230))
    d.text((LABEL_BOX[0] + 8, LABEL_BOX[1] + 2), "LEVEL 7", font=font(28), fill=(255, 255, 255, 255))
    im.save(hud)
    sfx = "0.25*sin(2*PI*1200*t)*exp(-12*mod(t,0.5))+0.08*sin(2*PI*110*t)+0.04*sin(2*PI*330*t)"
    run(
        [
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=s={W}x{H}:r=60:d={seconds}",
            "-i",
            str(hud),
            "-f",
            "lavfi",
            "-i",
            f"aevalsrc='{sfx}|{sfx}':s=48000:d={seconds}",
            "-filter_complex",
            "[0:v][1:v]overlay=0:0,format=yuv420p[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-crf",
            "18",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-t",
            f"{seconds}",
            str(out),
        ]
    )
    return out


def demo_spec(out_dir: Path, gameplay: Path) -> dict:
    hook_rules = (
        "Keep every shelf, product and the basket bar exactly in the same position and shape. Do not add any text."
    )
    return {
        "version": 1,
        "name": "demo",
        "providers": "fake",
        "output_dir": str(out_dir),
        "render": {"width": W, "height": H, "fps": 30, "preset": "veryfast"},
        "export": {"preset": "veryfast"},
        "qa": {"duration_s": [8, 60], "known_text": ["LEVEL", "PLAY NOW", "Try it yourself"], "judge_runs": 2},
        "concepts": [
            {
                "id": "dust-blower",
                "gameplay": {"path": str(gameplay), "in_s": 1.0, "out_s": 9.0},
                "hook": {
                    "image_prompt": "Edit this exact image. " + hook_rules + " Add a realistic adult hand holding the "
                    "nozzle of a small leaf blower, entering from the bottom right corner.",
                    "video_prompt": "The hand sweeps the leaf blower across the board; the dust blows away shelf by "
                    "shelf until the board is clean and looks exactly like the final frame.",
                    "duration_s": 6,
                    "cut_s": 4.4,
                    "text_regions": [list(LABEL_BOX)],
                    "effects": [{"type": "dust", "strength": 0.8}],
                    "patches": [{"box": [0, 0, W, 64], "from_s": 3.6}],
                },
                "end_card": {"headline": "Try it yourself!", "button": "PLAY NOW"},
                "music": {"prompt": "Upbeat playful puzzle game loop, bright marimba, 120 bpm", "volume_db": -19},
                "formats": ["9:16", "4:5"],
                "tags": {
                    "hook_type": "satisfying-clean",
                    "mechanic_shown": ["goods-sort"],
                    "emotion": ["satisfaction"],
                },
            },
            {
                "id": "mud-wash",
                "gameplay": {"path": str(gameplay), "in_s": 2.0, "out_s": 10.0, "speed": 1.1},
                "hook": {
                    "image_prompt": "Edit this exact image. " + hook_rules + " Add a realistic adult hand holding a "
                    "pressure washer lance, entering from the bottom right corner.",
                    "video_prompt": "Satisfying pressure washing: the jet clears the mud stripe by stripe until the "
                    "board is spotless and looks exactly like the final frame.",
                    "duration_s": 8,
                    "cut_s": 4.8,
                    "text_regions": [list(LABEL_BOX)],
                    "effects": [{"type": "mud", "strength": 0.9}],
                },
                "end_card": {"headline": "Fill every order!", "button": "PLAY NOW"},
                "music": {"prompt": "Calm lo-fi groove for a cozy puzzle game", "volume_db": -20},
                "formats": ["9:16", "4:5"],
                "tags": {
                    "hook_type": "real-hand",
                    "mechanic_shown": ["goods-sort"],
                    "emotion": ["satisfaction", "calm"],
                },
            },
        ],
    }


def run_demo(
    out_dir: Path, *, jobs: int = 2, keep_work: bool = False, ocr: str = "auto", log: Callable[[str], None] = print
) -> RunResult:
    out_dir = Path(out_dir).resolve()
    inputs = out_dir / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    gameplay = inputs / "gameplay.mp4"
    if not gameplay.exists():
        log("synthesizing gameplay (testsrc2 + HUD + click SFX)")
        synthetic_gameplay(gameplay)
    spec_path = out_dir / "demo.yaml"
    spec_path.write_text(yaml.safe_dump(demo_spec(out_dir, gameplay), sort_keys=False), encoding="utf-8")
    spec = load_spec(spec_path)
    settings = Settings(project=None, cache_dir=out_dir / ".cache")
    return Pipeline(spec, fake_providers(), settings, jobs=jobs, keep_work=keep_work, ocr=ocr, log=log).run()
