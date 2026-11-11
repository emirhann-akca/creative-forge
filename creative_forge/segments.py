"""``forge segments``: suggest gameplay segment in/out points from a capture with PySceneDetect.

A screen recording of a play session is mostly continuous, with hard cuts where a level ends, a menu opens or the
recording was edited. Scene detection finds those cuts; each scene long enough becomes a suggested segment, trimmed by
a small margin so no frame of a transition (a fade, a menu sliding in) ends up at either end. The suggestions are
printed as a ready-to-paste ``variants.segments`` block; choosing among them is a person's job.

PySceneDetect is BSD-3-Clause (``scenedetect`` 0.7.3 on PyPI, licence expression checked on 2026-11-11) and runs on
the CPU with OpenCV. It is an optional extra: ``pip install "creative-forge[segments]"``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path


class SegmentsUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Scene:
    start_s: float
    end_s: float
    start_frame: int
    end_frame: int  # exclusive

    @property
    def duration(self) -> float:
        return self.end_s - self.start_s


@dataclass(frozen=True)
class Suggestion:
    id: str
    in_s: float
    out_s: float
    scene: int


def detect_scenes(
    video: Path, *, detector: str = "content", threshold: float | None = None, min_scene_s: float = 0.5
) -> tuple[list[Scene], float]:
    """Scenes (start inclusive, end exclusive) and the video's frame rate."""
    try:
        from scenedetect import AdaptiveDetector, ContentDetector, SceneManager, open_video
    except ImportError as e:
        raise SegmentsUnavailable('PySceneDetect is not installed: pip install "creative-forge[segments]"') from e
    v = open_video(str(video))
    fps = float(v.frame_rate)
    min_len = max(1, round(min_scene_s * fps))
    if detector == "adaptive":
        det = AdaptiveDetector(min_scene_len=min_len, **({"adaptive_threshold": threshold} if threshold else {}))
    elif detector == "content":
        det = ContentDetector(min_scene_len=min_len, **({"threshold": threshold} if threshold else {}))
    else:
        raise ValueError("detector must be 'content' or 'adaptive'")
    sm = SceneManager()
    sm.add_detector(det)
    sm.detect_scenes(video=v)
    scenes = [
        Scene(round(a.get_seconds(), 3), round(b.get_seconds(), 3), a.get_frames(), b.get_frames())
        for a, b in sm.get_scene_list()
    ]
    if not scenes:  # no cut at all: the whole capture is one scene
        n = int(v.duration.get_frames()) if v.duration is not None else 0
        scenes = [Scene(0.0, round(n / fps, 3), 0, n)]
    return scenes, fps


def suggest(
    scenes: list[Scene], *, min_len_s: float = 3.0, max_len_s: float = 10.0, margin_s: float = 0.2
) -> list[Suggestion]:
    """One segment per scene that is long enough after trimming ``margin_s`` at both ends, capped at ``max_len_s``
    (taken from the start of the scene, where the state the hook may land on is freshest)."""
    out = []
    for i, sc in enumerate(scenes):
        a, b = sc.start_s + margin_s, sc.end_s - margin_s
        if b - a < min_len_s:
            continue
        b = min(b, a + max_len_s)
        out.append(Suggestion(f"s{len(out) + 1:02d}", round(a, 2), round(b, 2), i))
    return out


def as_yaml(video: Path, suggestions: list[Suggestion]) -> str:
    lines = ["variants:", "  segments:"]
    for s in suggestions:
        lines.append(f"    - {{id: {s.id}, path: {Path(video).as_posix()}, in_s: {s.in_s}, out_s: {s.out_s}}}")
    if not suggestions:
        lines.append("    []  # no scene is long enough; lower --min-len")
    return "\n".join(lines)


def to_dict(video: Path, fps: float, scenes: list[Scene], suggestions: list[Suggestion]) -> dict:
    return {
        "video": str(video),
        "fps": fps,
        "cuts_s": [s.start_s for s in scenes[1:]],
        "scenes": [asdict(s) for s in scenes],
        "suggestions": [asdict(s) for s in suggestions],
    }
