"""Provider construction."""

from __future__ import annotations

from creative_forge.config import Settings
from creative_forge.providers.base import ProviderSet


def build_providers(kind: str, settings: Settings) -> ProviderSet:
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
