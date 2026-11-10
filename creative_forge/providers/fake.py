"""Offline providers that synthesize media with ffmpeg/Pillow, so the whole pipeline runs with no keys and no cost.

They honour the same contracts as the Vertex providers: the fake "Veo" clip starts on the first frame, dissolves onto
``last_frame`` (landing at 55 % of the clip) and is 24 fps with audio, so downstream stages (interpolation, seam
check, loudness matching) do real work on it.
"""

from __future__ import annotations

import hashlib
import random
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from creative_forge.ffmpeg import run
from creative_forge.imaging import draw_hand
from creative_forge.providers.base import (
    ImageEditProvider,
    ImageEditRequest,
    JudgeProvider,
    JudgeRequest,
    MusicProvider,
    MusicRequest,
    ProviderResult,
    ProviderSet,
    VideoProvider,
    VideoRequest,
)
from creative_forge.qa.judge import CRITERIA


class _Counter:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def bump(self) -> int:
        with self._lock:
            self.calls += 1
            return self.calls


class FakeImageEditProvider(ImageEditProvider, _Counter):
    name = "fake-image"

    def __init__(self, billable: bool = False) -> None:
        _Counter.__init__(self)
        self.billable = billable

    def edit(self, req: ImageEditRequest, out_dir: Path) -> ProviderResult:
        self.bump()
        out = out_dir / "image_0.png"
        draw_hand(Image.open(req.base_image)).save(out)
        return ProviderResult([out], {"model_text": ["fake edit: hand with tool enters from the bottom-right corner"]})


class FakeVideoProvider(VideoProvider, _Counter):
    name = "fake-video"
    COLORS = ("white", "yellow", "cyan", "orange")

    def __init__(self, billable: bool = False, fps: int = 24) -> None:
        _Counter.__init__(self)
        self.billable = billable
        self.fps = fps

    def generate(
        self,
        req: VideoRequest,
        out_dir: Path,
        *,
        operation: str | None = None,
        on_submitted: Callable[[str], None] | None = None,
    ) -> ProviderResult:
        n = self.bump()
        if operation is None:
            operation = f"fake-operation-{n}"
            if on_submitted:
                on_submitted(operation)
        first = Image.open(req.first_frame)
        w, h = first.size
        w, h = w - w % 2, h - h % 2
        last = req.last_frame or req.first_frame
        d, r = float(req.duration_s), self.fps
        sq = max(8, w // 12)
        files = []
        for i in range(req.sample_count):
            out = out_dir / f"veo_{i}.mp4"
            inputs = [
                "-loop",
                "1",
                "-framerate",
                str(r),
                "-t",
                f"{d}",
                "-i",
                str(req.first_frame),
                "-loop",
                "1",
                "-framerate",
                str(r),
                "-t",
                f"{d}",
                "-i",
                str(last),
                "-f",
                "lavfi",
                "-i",
                f"color=c={self.COLORS[i % 4]}:s={sq}x{sq}:r={r}:d={d}",
            ]
            graph = (
                f"[0:v]scale={w}:{h},setsar=1,format=yuv420p[a];[1:v]scale={w}:{h},setsar=1,format=yuv420p[b];"
                f"[a][b]xfade=transition=fade:duration={d * 0.4:.3f}:offset={d * 0.15:.3f},trim=duration={d}[x];"
                f"[2:v]format=rgba,fade=t=out:st={d * 0.3:.3f}:d={d * 0.2:.3f}:alpha=1[s];"
                f"[x][s]overlay=x='mod(t*{w / 3:.1f}\\,{w - sq})':y='{h * 0.35:.0f}+{h * 0.1:.0f}*sin(t*2)'"
                f":shortest=1,format=yuv420p[v]"
            )
            maps = ["-map", "[v]"]
            if req.generate_audio:
                inputs += [
                    "-f",
                    "lavfi",
                    "-i",
                    f"anoisesrc=d={d}:c=pink:r=48000:a=0.05:seed={n * 10 + i}",
                    "-f",
                    "lavfi",
                    "-i",
                    f"sine=frequency=880:sample_rate=48000:duration={d}",
                ]
                graph += (
                    f";[3:a]highpass=f=300,volume='0.4+0.6*sin(PI*t/{d})':eval=frame[n];"
                    f"[4:a]volume='if(gt(t,{d * 0.7:.2f}),0.6*exp(-4*(t-{d * 0.7:.2f})),0)':eval=frame[c];"
                    f"[n][c]amix=inputs=2:normalize=0,aformat=channel_layouts=stereo[aout]"
                )
                maps += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
            run(
                [
                    *inputs,
                    "-filter_complex",
                    graph,
                    *maps,
                    "-c:v",
                    "libx264",
                    "-crf",
                    "18",
                    "-preset",
                    "veryfast",
                    "-r",
                    str(r),
                    "-t",
                    f"{d}",
                    str(out),
                ]
            )
            files.append(out)
        return ProviderResult(files, {"operation": operation, "rai_filtered": 0})


class FakeMusicProvider(MusicProvider, _Counter):
    name = "fake-music"

    def __init__(self, billable: bool = False) -> None:
        _Counter.__init__(self)
        self.billable = billable

    def compose(self, req: MusicRequest, out_dir: Path) -> ProviderResult:
        self.bump()
        out = out_dir / "music.wav"
        d = req.duration_hint_s
        chord = "0.12*(sin(2*PI*220*t)+sin(2*PI*277.18*t)+sin(2*PI*329.63*t))/3*(0.55+0.45*abs(sin(2*PI*1.0*t)))"
        run(["-f", "lavfi", "-i", f"aevalsrc='{chord}|{chord}':s=48000:d={d}", "-c:a", "pcm_s16le", str(out)])
        return ProviderResult([out], {"mime": "audio/wav"})


class FakeJudgeProvider(JudgeProvider, _Counter):
    """Deterministic per (video, run): a stable base score plus run-to-run jitter, like the real judge."""

    name = "fake-judge"

    def __init__(self, billable: bool = False, base: float = 7.0, jitter: int = 1) -> None:
        _Counter.__init__(self)
        self.billable = billable
        self.base = base
        self.jitter = jitter

    def score(self, req: JudgeRequest) -> dict[str, Any]:
        self.bump()
        digest = hashlib.sha256(Path(req.video).read_bytes()[:1_000_000]).hexdigest()
        rng = random.Random(f"{digest}:{req.run_index}")
        scores = {c: max(1, min(10, round(self.base + rng.randint(-self.jitter, self.jitter)))) for c in CRITERIA}
        return {
            "scores": scores,
            "hook_description": "synthetic",
            "strongest_point": "synthetic",
            "weakest_point": "synthetic",
            "flaws": [],
        }


def fake_providers() -> ProviderSet:
    return ProviderSet(FakeImageEditProvider(), FakeVideoProvider(), FakeMusicProvider(), FakeJudgeProvider())
