"""Ad specification: one YAML file declares every concept of an ad batch.

Every default below is the value the October 2026 production scripts used, unless the comment says otherwise.
Unknown keys are rejected (``extra="forbid"``) so a typo in a spec fails before anything is generated or paid for.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

FormatName = Literal["9:16", "4:5"]
Box = tuple[int, int, int, int]
# Passed to ffmpeg as-is. A typo in export.preset used to fail only at export, after the paid generations.
X264Preset = Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]
# Model IDs end up in REST URL paths (.../models/{id}:method), so they must be plain identifiers.
ModelId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9._@-]{0,127}$")]
# Names and ids become output file names and ad-name components: no separators, no dots.
SafeName = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]
# Lowercase BCP 47-style language tag ("en", "tr", "pt-br"); part of every ad name.
Lang = Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")]
# Hand-set creative tags (dimension -> value or values), checked against the tag taxonomy by `forge plan`.
Tags = dict[str, str | list[str]]
# Text shown in the ad: one string for every language, or one per language ({en: "...", tr: "..."}).
Localized = str | dict[str, str]
# Ids of hook / segment / end-card variants; ad-name components (no dots, no underscores).
VariantId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")]


def localized(text: Localized | None, lang: str) -> str | None:
    """The text for ``lang`` (a plain string applies to every language)."""
    if text is None or isinstance(text, str):
        return text
    return text[lang]


def _missing_langs(text: Localized | None, langs: list[str]) -> list[str]:
    return [lng for lng in langs if isinstance(text, dict) and lng not in text]


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


class ComfyUISpec(_Model):
    """Local SDXL edit through ComfyUI (``image_provider: comfyui``). Sampler values are SDXL-base defaults, not
    production values: the October 2026 ads used Gemini for this step."""

    checkpoint: Annotated[str, Field(pattern=r"^[\w.-]+\.(safetensors|ckpt)$")] = "sd_xl_base_1.0.safetensors"
    steps: int = Field(28, ge=1, le=150)
    cfg: float = Field(6.0, gt=0, le=30)
    sampler: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")] = "dpmpp_2m"
    scheduler: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")] = "karras"
    seed: int = Field(7, ge=0)
    # None: 1.0 for a masked edit (new content inside hook.edit_region; at 0.9 SDXL base barely changed the region
    # in the local test), 0.45 for a whole-frame img2img pass.
    denoise: float | None = Field(None, gt=0, le=1)
    negative_prompt: str = "text, letters, watermark, logo, blurry, deformed hand, extra fingers, photo frame"
    timeout_s: float = Field(900, gt=0)


ModelFile = Annotated[str, Field(pattern=r"^[\w.+-]+\.(safetensors|ckpt|gguf|pt|pth|bin)$")]

# Per-workflow defaults. File names are the ones in Comfy-Org's repackaged Wan repositories on Hugging Face
# (Wan_2.1_ComfyUI_repackaged, Wan_2.2_ComfyUI_Repackaged; apache-2.0, listed 2026-11-11). Sampler values are starting
# points to tune on the worker, not measured: no GPU run has been made with them yet.
WAN_WORKFLOWS: dict[str, dict[str, Any]] = {
    "wan21_flf2v": {
        "diffusion_model": "wan2.1_flf2v_720p_14B_fp16.safetensors",
        "vae": "wan_2.1_vae.safetensors",
        "clip_vision": "clip_vision_h.safetensors",
        "width": 720,
        "height": 1280,
        "fps": 16,
        "steps": 20,
        "cfg": 5.0,
        "multiple": 16,
        "last_frame": True,
    },
    "wan22_ti2v_5b": {
        "diffusion_model": "wan2.2_ti2v_5B_fp16.safetensors",
        "vae": "wan2.2_vae.safetensors",
        "clip_vision": None,
        "width": 704,
        "height": 1280,
        "fps": 24,
        "steps": 30,
        "cfg": 5.0,
        "multiple": 32,
        "last_frame": False,
    },
}


class ComfyUIVideoSpec(_Model):
    """Hook video on a ComfyUI worker (``video_provider: comfyui``) with Wan. ``wan21_flf2v`` lands on the real
    gameplay frame (first frame + last frame, the same recipe as Veo's ``lastFrame``); ``wan22_ti2v_5b`` is plain
    image-to-video for 8 GB cards and relies on the crossfade, patches and the landing check instead."""

    workflow: Literal["wan21_flf2v", "wan22_ti2v_5b"] = "wan21_flf2v"
    diffusion_model: ModelFile | None = None  # None: the workflow's default file
    text_encoder: ModelFile = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
    vae: ModelFile | None = None
    clip_vision: ModelFile | None = None
    width: int | None = Field(None, ge=64, le=2048)
    height: int | None = Field(None, ge=64, le=2048)
    fps: int | None = Field(None, ge=8, le=60)
    steps: int | None = Field(None, ge=1, le=100)
    cfg: float | None = Field(None, gt=0, le=20)
    shift: float = Field(8.0, gt=0, le=50)
    sampler: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")] = "uni_pc"
    scheduler: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")] = "simple"
    seed: int = Field(7, ge=0)
    timeout_s: float = Field(3600, gt=0)  # a 14B clip with offloading can take tens of minutes
    # Only with this set may the run use the cloud video model (`models.video`, budget-guarded) when the worker is
    # unreachable or lacks a node or a model file. The ledger records which one ran.
    cloud_fallback: bool = False

    def setting(self, name: str) -> Any:
        value = getattr(self, name)
        return WAN_WORKFLOWS[self.workflow][name] if value is None else value

    def model_files(self) -> dict[str, str]:
        files = {
            "diffusion_model": self.setting("diffusion_model"),
            "text_encoder": self.text_encoder,
            "vae": self.setting("vae"),
        }
        if WAN_WORKFLOWS[self.workflow]["clip_vision"]:
            files["clip_vision"] = self.setting("clip_vision")
        return files

    @model_validator(mode="after")
    def _sizes(self) -> ComfyUIVideoSpec:
        m = WAN_WORKFLOWS[self.workflow]["multiple"]
        for name in ("width", "height"):
            if self.setting(name) % m:
                raise ValueError(f"comfyui_video.{name} must be a multiple of {m} for {self.workflow}")
        return self


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
    encoder: Literal["auto", "aac_at", "aac_mf", "aac"] = "auto"  # auto: aac_at, then aac_mf, then aac
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
    # The only region the image model may change (x0, y0, x1, y1, output pixels). Everything outside it is restored
    # from the real frame after the edit, whatever the model did there. ComfyUI also uses it as the inpaint mask.
    edit_region: Box | None = None
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
    disclaimer: Localized | None = "Not gameplay footage"
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
        if self.edit_region is not None:
            _corners(self.edit_region, "hook.edit_region")
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
    headline: Localized = "Try it yourself!"
    button: Localized = "PLAY NOW"
    logo: Path | None = None
    font: Path | None = None
    image: Path | None = None  # background image; default is the blurred last gameplay frame


class Variants(_Model):
    """A matrix: every hook x segment x end card x language becomes one render, exported in every format.

    * ``hooks``: overrides merged over the concept's ``hook`` (plus their own ``tags``). Every hook lands on the
      concept's ``gameplay`` first frame, so a hook is generated (and paid for) once, whatever it is combined with.
    * ``segments``: more real gameplay played after the landing segment (a hard cut), merged over ``gameplay``, so
      ``{id: lvl21, in_s: 30, out_s: 38}`` reuses its path and crop.
    * ``end_cards``: overrides merged over ``end_card``.
    * ``languages``: end-card text and disclaimer per language (``headline: {en: ..., tr: ...}``).
    """

    hooks: list[dict[str, Any]] = Field(default_factory=list)
    segments: list[dict[str, Any]] = Field(default_factory=list)
    end_cards: list[dict[str, Any]] = Field(default_factory=list)
    languages: list[Lang] = Field(default_factory=list)


def _resolve(kind: str, model: type[BaseModel], base: BaseModel, items: list[dict[str, Any]]) -> list[tuple[str, Any]]:
    out, seen = [], set()
    for i, raw in enumerate(items):
        raw = dict(raw)
        vid = raw.pop("id", None)
        raw.pop("tags", None)
        if not isinstance(vid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", vid):
            raise ValueError(f"variants.{kind}[{i}].id must be lowercase a-z, 0-9 and '-' (got {vid!r})")
        if vid in seen:
            raise ValueError(f"variants.{kind}: duplicate id {vid!r}")
        seen.add(vid)
        try:
            out.append((vid, model.model_validate({**base.model_dump(exclude_unset=True), **raw})))
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(x) for x in first["loc"])
            raise ValueError(f"variants.{kind}[{vid}]: {where}: {first['msg']}") from None
    return out


class Concept(_Model):
    id: SafeName
    gameplay: GameplaySpec  # the landing segment: every hook lands on its first frame
    hook: HookSpec
    end_card: EndCardSpec = Field(default_factory=EndCardSpec)
    music: MusicSpec | None = None
    sfx: list[SFX] = Field(default_factory=list)
    formats: list[FormatName] = Field(default_factory=lambda: ["9:16", "4:5"], min_length=1)
    tags: Tags = Field(default_factory=dict)  # e.g. {hook_type: satisfying-clean, emotion: [satisfaction]}
    # What this concept is expected to show, written before it is tested ("a fail-state hook beats the control on
    # IPM"). Not sent to any model; it goes into report.json and the concept board of `forge ui`.
    hypothesis: str | None = Field(None, max_length=500)
    variants: Variants | None = None
    _hooks: list[tuple[str, HookSpec]] = PrivateAttr(default_factory=list)
    _segments: list[tuple[str, GameplaySpec | None]] = PrivateAttr(default_factory=list)
    _end_cards: list[tuple[str, EndCardSpec]] = PrivateAttr(default_factory=list)

    @field_validator("formats")
    @classmethod
    def _unique_formats(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("formats must be unique")
        return v

    @model_validator(mode="after")
    def _expand(self) -> Concept:
        v = self.variants or Variants()
        self._hooks = _resolve("hooks", HookSpec, self.hook, v.hooks) or [("main", self.hook)]
        self._segments = _resolve("segments", GameplaySpec, self.gameplay, v.segments) or [("main", None)]
        self._end_cards = _resolve("end_cards", EndCardSpec, self.end_card, v.end_cards) or [("main", self.end_card)]
        if len(set(v.languages)) != len(v.languages):
            raise ValueError("variants.languages must be unique")
        return self

    def hook_variants(self) -> list[tuple[str, HookSpec]]:
        return list(self._hooks)

    def segment_variants(self) -> list[tuple[str, GameplaySpec | None]]:
        """``None``: no extra segment (the landing segment is the whole gameplay part)."""
        return list(self._segments)

    def end_card_variants(self) -> list[tuple[str, EndCardSpec]]:
        return list(self._end_cards)

    def hook_tags(self, hook_id: str) -> Tags:
        """The concept's tags, overridden per dimension by the hook variant's own ``tags``."""
        for raw in self.variants.hooks if self.variants else []:
            if raw.get("id") == hook_id:
                return {**self.tags, **(raw.get("tags") or {})}
        return dict(self.tags)


# ISO 3166-1 alpha-2 country codes, or EU / EEA / UK. Model licences with a territory clause are checked against it.
Region = Annotated[str, Field(pattern=r"^([A-Za-z]{2}|EU|EEA|eu|eea)$")]


class Targets(_Model):
    """Where the ads will run and who runs them; what licence conditions are checked against (``forge plan``)."""

    # Empty means "may serve anywhere": a licence that excludes a territory is then refused.
    regions: list[Region] = Field(default_factory=list)
    # Some open-weights licences (LTX) need a paid licence above a revenue cap. None: not declared (warns).
    annual_revenue_usd: float | None = Field(None, ge=0)


class AdSpec(_Model):
    version: Literal[1] = 1
    name: SafeName
    providers: Literal["vertex", "fake"] = "vertex"
    image_provider: Literal["comfyui"] | None = None  # None: same as `providers`
    comfyui: ComfyUISpec = Field(default_factory=ComfyUISpec)
    video_provider: Literal["comfyui"] | None = None  # None: same as `providers` (Veo)
    comfyui_video: ComfyUIVideoSpec = Field(default_factory=ComfyUIVideoSpec)
    output_dir: Path = Path("out")
    models: Models = Field(default_factory=Models)
    render: RenderSpec = Field(default_factory=RenderSpec)
    export: ExportSpec = Field(default_factory=ExportSpec)
    audio: AudioSpec = Field(default_factory=AudioSpec)
    qa: QASpec = Field(default_factory=QASpec)
    targets: Targets = Field(default_factory=Targets)
    language: Lang = "en"  # language of the end-card text; part of every ad name
    taxonomy: Path | None = None  # tag taxonomy YAML; None: the bundled one (data/tag_taxonomy.yaml)
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
            if c.gameplay.output_duration <= 2 * x + self.audio.gameplay_skip_s + 0.5:
                raise ValueError(f"{c.id}: gameplay segment is too short for two crossfades")
            for sid, seg in c.segment_variants():
                if seg is not None and seg.output_duration < 1.0:
                    raise ValueError(f"{c.id}: segment {sid} is shorter than 1 s")
            langs = self.languages_for(c)
            for hid, h in c.hook_variants():
                where = c.id if hid == "main" else f"{c.id}/{hid}"
                if h.effective_cut <= x + 0.1:
                    raise ValueError(f"{where}: hook cut must be longer than the crossfade")
                boxes = [("patch box", p.box) for p in h.patches] + [("edit_region", h.edit_region)]
                for what, box in boxes:
                    if box is not None and (box[2] > self.render.width or box[3] > self.render.height):
                        raise ValueError(f"{where}: {what} outside the {self.render.width}x{self.render.height} frame")
                if _missing_langs(h.disclaimer, langs):
                    raise ValueError(f"{where}: disclaimer has no text for {_missing_langs(h.disclaimer, langs)}")
            for eid, e in c.end_card_variants():
                for name, text in (("headline", e.headline), ("button", e.button)):
                    if _missing_langs(text, langs):
                        raise ValueError(f"{c.id}: end card {eid} {name} has no text for {_missing_langs(text, langs)}")
        return self

    def languages_for(self, c: Concept) -> list[str]:
        return list(c.variants.languages) if c.variants and c.variants.languages else [self.language]

    def matrix(self, c: Concept) -> list[tuple[str, HookSpec, str, GameplaySpec | None, str, EndCardSpec, str]]:
        """Every (hook, segment, end card, language) render of a concept, in a stable order."""
        return [
            (hid, h, sid, seg, eid, e, lang)
            for hid, h in c.hook_variants()
            for sid, seg in c.segment_variants()
            for eid, e in c.end_card_variants()
            for lang in self.languages_for(c)
        ]

    @property
    def image_edit_model(self) -> str:
        """What the hook image step runs: a Vertex model ID, or the local checkpoint file."""
        return self.comfyui.checkpoint if self.image_provider == "comfyui" else self.models.image_edit

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


def timeline_for(
    spec: AdSpec,
    concept: Concept,
    hook: HookSpec | None = None,
    segment: GameplaySpec | None = None,
    end_card: EndCardSpec | None = None,
) -> dict[str, float]:
    """Planned output timeline of one ad (seconds). Crossfades overlap neighbours, so they are subtracted once each.
    An extra segment follows the landing segment with a hard cut, so its length simply adds."""
    x = spec.render.crossfade_s
    hook_s = (hook or concept.hook).effective_cut
    gameplay = concept.gameplay.output_duration + (segment.output_duration if segment else 0.0)
    end = (end_card or concept.end_card).duration_s
    gameplay_start = hook_s - x
    end_card_start = gameplay_start + gameplay - x
    return {
        "hook_end": gameplay_start,
        "crossfade": x,
        "gameplay_start": gameplay_start,
        "gameplay_end": end_card_start,
        "end_card_start": end_card_start,
        "total": end_card_start + end,
    }
