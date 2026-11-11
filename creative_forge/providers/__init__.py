"""Provider construction."""

from __future__ import annotations

from collections.abc import Callable

from creative_forge.config import ConfigError, Settings
from creative_forge.providers.base import ProviderSet
from creative_forge.spec import ComfyUISpec, ComfyUIVideoSpec


def build_providers(
    kind: str,
    settings: Settings,
    *,
    image: str | None = None,
    comfyui: ComfyUISpec | None = None,
    video: str | None = None,
    comfyui_video: ComfyUIVideoSpec | None = None,
    log: Callable[[str], None] | None = None,
) -> ProviderSet:
    """``kind`` picks every provider; ``image="comfyui"`` swaps in the local ComfyUI image edit, and
    ``video="comfyui"`` routes the hook video to a ComfyUI worker (Wan), falling back to ``kind``'s video provider
    only when ``comfyui_video.cloud_fallback`` is set. The decision is recorded in ``ProviderSet.routing``."""
    providers = _build(kind, settings)
    if image == "comfyui":
        from creative_forge.providers.comfyui import ComfyUIImageEditProvider

        providers.image_edit = ComfyUIImageEditProvider(settings.comfyui_url, comfyui or ComfyUISpec())
    if video == "comfyui":
        from creative_forge.providers.comfyui import ComfyUIError
        from creative_forge.providers.comfyui_video import route_video

        try:
            providers.video, record = route_video(
                settings.comfyui_url, comfyui_video or ComfyUIVideoSpec(), providers.video, log=log
            )
        except ComfyUIError as e:
            raise ConfigError(str(e)) from None
        providers.routing.append(record)
    return providers


def _build(kind: str, settings: Settings) -> ProviderSet:
    if kind == "fake":
        from creative_forge.providers.fake import fake_providers

        return fake_providers()
    if kind == "vertex":
        from creative_forge.providers.vertex import (
            VertexClient,
            VertexGeminiImageProvider,
            VertexGeminiJudgeProvider,
            VertexLyriaProvider,
            VertexVeoProvider,
        )

        client = VertexClient(settings.require_project())
        return ProviderSet(
            image_edit=VertexGeminiImageProvider(client, settings.genai_location),
            video=VertexVeoProvider(client, settings.video_location),
            music=VertexLyriaProvider(client, settings.genai_location),
            judge=VertexGeminiJudgeProvider(client, settings.genai_location),
        )
    raise ValueError(f"unknown provider kind {kind!r} (expected 'vertex' or 'fake')")
