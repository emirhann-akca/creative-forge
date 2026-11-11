"""Provider interfaces. A provider turns one request into files; it knows nothing about caching or budgets
(the ``Generator`` wraps every call with cache lookup, budget reservation and ledger entries)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ImageEditRequest:
    prompt: str
    base_image: Path
    model: str
    reference_images: tuple[Path, ...] = ()
    aspect_ratio: str = "9:16"
    mask_box: tuple[int, int, int, int] | None = None  # hook.edit_region; providers without a mask input ignore it

    def cache_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": self.prompt,
            "base_image": self.base_image,
            "model": self.model,
            "reference_images": list(self.reference_images),
            "aspect_ratio": self.aspect_ratio,
        }
        if self.mask_box is not None:
            payload["mask_box"] = list(self.mask_box)
        return payload


@dataclass(frozen=True)
class VideoRequest:
    prompt: str
    first_frame: Path
    model: str
    last_frame: Path | None = None
    duration_s: int = 8
    generate_audio: bool = True
    sample_count: int = 1
    aspect_ratio: str = "9:16"
    resolution: str = "1080p"
    negative_prompt: str = ""
    reference_images: tuple[Path, ...] = ()

    def cache_payload(self) -> dict[str, Any]:
        return {
            "prompt": self.prompt,
            "first_frame": self.first_frame,
            "model": self.model,
            "last_frame": self.last_frame,
            "duration_s": self.duration_s,
            "generate_audio": self.generate_audio,
            "sample_count": self.sample_count,
            "aspect_ratio": self.aspect_ratio,
            "resolution": self.resolution,
            "negative_prompt": self.negative_prompt,
            "reference_images": list(self.reference_images),
        }


@dataclass(frozen=True)
class MusicRequest:
    prompt: str
    model: str
    negative_prompt: str | None = None
    seed: int | None = None
    duration_hint_s: float = 60.0

    def cache_payload(self) -> dict[str, Any]:
        return {"prompt": self.prompt, "model": self.model, "negative_prompt": self.negative_prompt, "seed": self.seed}


@dataclass(frozen=True)
class JudgeRequest:
    video: Path
    prompt: str
    model: str
    fps: int = 5
    run_index: int = 0


@dataclass
class ProviderResult:
    files: list[Path]
    info: dict[str, Any] = field(default_factory=dict)


class Provider(ABC):  # noqa: B024 - shared attributes only; each kind below declares its abstract method
    name: str = "provider"
    billable: bool = True
    local: bool = False  # runs on this machine (e.g. ComfyUI): no list price, nothing to simulate

    def cache_params(self) -> dict[str, Any]:
        """Provider-side settings that change the output and so belong in the cache key."""
        return {}


class ImageEditProvider(Provider):
    @abstractmethod
    def edit(self, req: ImageEditRequest, out_dir: Path) -> ProviderResult: ...


class VideoProvider(Provider):
    @abstractmethod
    def generate(
        self, req: VideoRequest, out_dir: Path, *, operation: str | None = None, on_submitted: Any = None
    ) -> ProviderResult:
        """Generate ``sample_count`` clips. ``operation`` resumes a previously submitted long-running operation;
        ``on_submitted(name)`` is called as soon as a new operation is accepted, before any polling."""


class MusicProvider(Provider):
    @abstractmethod
    def compose(self, req: MusicRequest, out_dir: Path) -> ProviderResult: ...


class JudgeProvider(Provider):
    @abstractmethod
    def score(self, req: JudgeRequest) -> dict[str, Any]:
        """Return the parsed judge JSON (``{"scores": {...}, "flaws": [...]}``) or ``{"error": "..."}``."""


@dataclass
class ProviderSet:
    image_edit: ImageEditProvider
    video: VideoProvider
    music: MusicProvider
    judge: JudgeProvider
    routing: list[dict[str, Any]] = field(default_factory=list)  # local-vs-cloud decisions, copied into the ledger
