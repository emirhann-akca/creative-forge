"""Ad specification: one YAML file declares every concept of an ad batch.

Every default below is the value the October 2026 production scripts used, unless the comment says otherwise.
Unknown keys are rejected (``extra="forbid"``) so a typo in a spec fails before anything is generated or paid for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FormatName = Literal["9:16", "4:5"]
Box = tuple[int, int, int, int]
# Passed to ffmpeg as-is. A typo in export.preset used to fail only at export, after the paid generations.
X264Preset = Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
# Model IDs end up in REST URL paths (.../models/{id}:method), so they must be plain identifiers.
ModelId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._@-]{0,127}$")]
# Names and ids become output file names: no separators, no "..".
SafeName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]

# Negative prompt sent with every Veo request in production.
DEFAULT_VEO_NEGATIVE = (
    "text, letters, words, captions, logo, watermark, user interface, extra limbs, extra fingers, deformed face, "
    "melting, morphing characters, human, realistic photo, blurry"
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _corners(box: Box, what: str) -> Box:
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0:
        raise ValueError(f"{what} must be (x0, y0, x1, y1) with x1 > x0 >= 0 and y1 > y0 >= 0")
    return box


class Models(_Model):
    """Model IDs. They are configuration, not code: check them against the Vertex AI model garden."""

    image_edit: ModelId = "gemini-3-pro-image-preview"
    video: ModelId = "veo-3.1-generate-001"
    music: ModelId = "lyria-3-pro-preview"
    judge: ModelId = "gemini-3.1-pro-preview"


class RenderSpec(_Model):
    width: int = Field(1080, ge=64)
    height: int = Field(1920, ge=64)
    fps: int = Field(30, ge=12, le=60)
    crossfade_s: float = Field(0.3, ge=0.05, le=2.0)
    # Veo returns 24 fps. Motion-compensated interpolation to 60 fps, then an exact 2:1 drop to 30 fps.
    hook_interpolate_fps: int = Field(60, ge=24, le=120)
    intermediate_crf: int = Field(12, ge=0, le=30)
    preset: X264Preset = "medium"

    @field_validator("width", "height")
    @classmethod
    def _even(cls, v: int) -> int:
        if v % 2:
            raise ValueError("must be even (yuv420p)")
        return v


class ExportSpec(_Model):
    crf: int = Field(20, ge=0, le=40)
    maxrate_kbps: int = Field(3600, ge=200)
    preset: X264Preset = "slow"
    audio_bitrate_kbps: int = Field(192, ge=64, le=320)
    max_file_mb: float = Field(30.0, gt=0)


class AudioSpec(_Model):
    target_lufs: float = -14.3
    tolerance_lu: float = Field(0.3, gt=0)
    true_peak_max_dbtp: float = -2.0
    hook_gameplay_max_delta_lu: float = Field(0.8, gt=0)
    limiter_limit: float = Field(0.66, gt=0.05, le=1.0)
    ramp_s: float = Field(0.4, gt=0)
    max_iterations: int = Field(6, ge=1, le=20)
    gameplay_skip_s: float = Field(0.6, ge=0)
    encoder: Literal["auto", "aac_at", "aac"] = "auto"
    sample_rate: int = 48000
    # Mix stage (sidechain ducking of music under hook/gameplay sound)
    duck_threshold: float = 0.05
    duck_ratio: float = 4.0
    duck_attack_ms: float = 5.0
    duck_release_ms: float = 250.0
    mix_limiter: float = 0.95


class QASpec(_Model):
    duration_s: tuple[float, float] = (15.0, 60.0)
    av_mismatch_warn_s: float = 0.1
    av_mismatch_fail_s: float = 0.5
    loudness_warn_lu: float = 1.0
    loudness_fail_lu: float = 2.0
    true_peak_fail_dbtp: float = -1.0
    hook_gameplay_fail_lu: float = 2.0
    # Seam thresholds are heuristic defaults (mean absolute difference on a 0-255 scale); tune per game.
    # landing_*: Veo's frame at the cut vs the real frame it was asked to land on (no motion involved).
    landing_mean_warn: float = 8.0
    landing_mean_fail: float = 16.0
    landing_block_warn: float = 40.0
    landing_block_fail: float = 70.0
    # seam_*: delivered video, last hook frame vs first clean gameplay frame. That span includes ~0.35 s of real
    # gameplay motion, so only the global mean is graded by default; the worst block is reported, not graded.
    seam_mean_warn: float = 12.0
    seam_mean_fail: float = 24.0
    seam_block_warn: float | None = None
    seam_block_fail: float | None = None
    intro_window_s: float = 1.0
    freeze_min_s: float = 0.5
    freeze_noise_db: float = -60.0
    silence_min_s: float = 0.5
    silence_noise_db: float = -50.0
    duplicate_warn_ratio: float = 0.02
    duplicate_fail_ratio: float = 0.08
    ocr_fps: float = 2.0
    known_text: list[str] = Field(default_factory=list)
    judge_runs: int = Field(2, ge=0, le=10)
    judge_unstable_stdev: float = 1.0
    judge_min_overall: float | None = None

    @model_validator(mode="after")
    def _ordered(self) -> QASpec:
        lo, hi = self.duration_s
        if not 0 < lo < hi:
            raise ValueError("qa.duration_s must be (min, max) with 0 < min < max")
        return self


class GameplaySpec(_Model):
    path: Path
    in_s: float = Field(ge=0)
    out_s: float
    speed: float = Field(1.0, ge=0.25, le=4.0)
    crop: Box | None = None  # x, y, w, h in source pixels (e.g. drop a status bar), applied before scaling
    use_audio: bool = True
    audio_gain_db: float = 0.0

    @model_validator(mode="after")
    def _span(self) -> GameplaySpec:
        if self.out_s <= self.in_s:
            raise ValueError("gameplay.out_s must be greater than gameplay.in_s")
        if self.crop is not None:
            x, y, w, h = self.crop
            _corners((x, y, x + w, y + h), "gameplay.crop (x, y, w, h)")
        return self

    @property
    def output_duration(self) -> float:
        return (self.out_s - self.in_s) / self.speed


class Effect(_Model):
    """A programmatic effect applied to the REAL frame (Gemini redraws products when asked to do this itself)."""

    type: Literal["dust", "mud", "frost", "darken"]
    strength: float = Field(0.8, ge=0, le=1)
    seed: int = 7


class Patch(_Model):
    """Cover a region where Veo invented UI with the same region of the real frame, from ``from_s`` on."""

    box: Box  # x0, y0, x1, y1 in output pixels
    from_s: float = Field(ge=0)
    fade_s: float = Field(0.15, gt=0)

    @model_validator(mode="after")
    def _box(self) -> Patch:
        _corners(self.box, "patch.box")
        return self


class HookSpec(_Model):
    image_prompt: str | None = None  # None: the prepared real frame is the first frame (no image model call)
    reference_images: list[Path] = Field(default_factory=list, max_length=3)
    video_prompt: str
    negative_prompt: str = DEFAULT_VEO_NEGATIVE
    duration_s: Literal[4, 6, 8] = 8
    audio: bool = True
    samples: int = Field(1, ge=1, le=4)
    pick: int = Field(0, ge=0)
    cut_s: float | None = None
    tail_trim_s: float = Field(1.0, ge=0)  # Veo drifts in the last ~1 s
    text_regions: list[Box] = Field(default_factory=list)  # x0, y0, x1, y1 in output pixels, blurred out
    effects: list[Effect] = Field(default_factory=list)
    patches: list[Patch] = Field(default_factory=list)
    disclaimer: str | None = "Not gameplay footage"
    audio_gain_db: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> HookSpec:
        if self.pick >= self.samples:
            raise ValueError("hook.pick must be smaller than hook.samples")
        if self.cut_s is not None and not 0.5 <= self.cut_s <= self.duration_s:
            raise ValueError(f"hook.cut_s must be within [0.5, {self.duration_s}]")
        if self.cut_s is None and self.duration_s - self.tail_trim_s < 0.5:
            raise ValueError("hook.tail_trim_s leaves less than 0.5 s of hook")
        for box in self.text_regions:
            _corners(box, "hook.text_regions entry")
        return self

    @property
    def effective_cut(self) -> float:
        return self.cut_s if self.cut_s is not None else self.duration_s - self.tail_trim_s


class SFX(_Model):
    path: Path
    at_s: float = Field(ge=0)
    gain_db: float = 0.0
    pitch: float = Field(1.0, gt=0.25, lt=4.0)


class MusicSpec(_Model):
    prompt: str | None = None
    negative_prompt: str | None = None
    path: Path | None = None  # an existing, licensed track instead of a generated one
    seed: int | None = None
    volume_db: float = -19.0
    start_offset_s: float = Field(0.0, ge=0)

    @model_validator(mode="after")
    def _one_source(self) -> MusicSpec:
        if (self.prompt is None) == (self.path is None):
            raise ValueError("music needs exactly one of `prompt` (generate) or `path` (existing track)")
        return self


class EndCardSpec(_Model):
    duration_s: float = Field(2.6, ge=0.5, le=10)
    headline: str = "Try it yourself!"
    button: str = "PLAY NOW"
    logo: Path | None = None
    font: Path | None = None
    image: Path | None = None  # background image; default is the blurred last gameplay frame


class Concept(_Model):
    id: SafeName
    gameplay: GameplaySpec
    hook: HookSpec
    end_card: EndCardSpec = Field(default_factory=EndCardSpec)
    music: MusicSpec | None = None
    sfx: list[SFX] = Field(default_factory=list)
    formats: list[FormatName] = Field(default_factory=lambda: ["9:16", "4:5"], min_length=1)

    @field_validator("formats")
    @classmethod
    def _unique_formats(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("formats must be unique")
        return v


class AdSpec(_Model):
    version: Literal[1] = 1
    name: SafeName
    providers: Literal["vertex", "fake"] = "vertex"
    output_dir: Path = Path("out")
    models: Models = Field(default_factory=Models)
    render: RenderSpec = Field(default_factory=RenderSpec)
    export: ExportSpec = Field(default_factory=ExportSpec)
    audio: AudioSpec = Field(default_factory=AudioSpec)
    qa: QASpec = Field(default_factory=QASpec)
    concepts: list[Concept] = Field(min_length=1)
    base_dir: Path = Field(default=Path("."), exclude=True)

    @model_validator(mode="after")
    def _check(self) -> AdSpec:
        ids = [c.id for c in self.concepts]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate concept ids: {dupes}")
        if self.render.width >= self.render.height:
            raise ValueError("render must be portrait (the master is 9:16; 4:5 is derived from it)")
        x = self.render.crossfade_s
        for c in self.concepts:
            if c.hook.effective_cut <= x + 0.1:
                raise ValueError(f"{c.id}: hook cut must be longer than the crossfade")
            if c.gameplay.output_duration <= 2 * x + self.audio.gameplay_skip_s + 0.5:
                raise ValueError(f"{c.id}: gameplay segment is too short for two crossfades")
            for p in c.hook.patches:
                if p.box[2] > self.render.width or p.box[3] > self.render.height:
                    raise ValueError(f"{c.id}: patch box outside the {self.render.width}x{self.render.height} frame")
        return self

    def resolve(self, p: Path | None) -> Path | None:
        if p is None:
            return None
        p = Path(p).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def output_path(self) -> Path:
        return self.resolve(self.output_dir)  # type: ignore[return-value]


def load_spec(path: str | Path) -> AdSpec:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")
    spec = AdSpec.model_validate(data)
    spec.base_dir = path.resolve().parent
    return spec


def timeline_for(spec: AdSpec, concept: Concept) -> dict[str, float]:
    """Output timeline of one ad (seconds). Crossfades overlap neighbours, so they are subtracted once each."""
    x = spec.render.crossfade_s
    hook = concept.hook.effective_cut
    gameplay = concept.gameplay.output_duration
    end = concept.end_card.duration_s
    gameplay_start = hook - x
    end_card_start = gameplay_start + gameplay - x
    return {
        "hook_end": gameplay_start,
        "crossfade": x,
        "gameplay_start": gameplay_start,
        "gameplay_end": end_card_start,
        "end_card_start": end_card_start,
        "total": end_card_start + end,
    }
