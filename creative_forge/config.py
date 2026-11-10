"""Runtime settings that come from the environment, never from source code.

The Google Cloud project is required only for real (Vertex AI) runs. Nothing here has a default project:
a run with ``providers: vertex`` fails fast with a clear message when the variable is missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_PROJECT = "CREATIVE_FORGE_PROJECT"
ENV_VIDEO_LOCATION = "CREATIVE_FORGE_VIDEO_LOCATION"
ENV_GENAI_LOCATION = "CREATIVE_FORGE_GENAI_LOCATION"
ENV_CACHE = "CREATIVE_FORGE_CACHE"
ENV_COMFYUI_URL = "CREATIVE_FORGE_COMFYUI_URL"


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    project: str | None
    video_location: str = "us-central1"
    genai_location: str = "global"
    cache_dir: Path = Path(".forge-cache")
    comfyui_url: str = "http://127.0.0.1:8188"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        project = env.get(ENV_PROJECT) or env.get("GOOGLE_CLOUD_PROJECT") or None
        return cls(
            project=project,
            video_location=env.get(ENV_VIDEO_LOCATION, "us-central1"),
            genai_location=env.get(ENV_GENAI_LOCATION, "global"),
            cache_dir=Path(env.get(ENV_CACHE, ".forge-cache")),
            comfyui_url=env.get(ENV_COMFYUI_URL, "http://127.0.0.1:8188"),
        )

    def require_project(self) -> str:
        if not self.project:
            raise ConfigError(
                f"No Google Cloud project configured. Set {ENV_PROJECT} (or GOOGLE_CLOUD_PROJECT) "
                "and authenticate with `gcloud auth application-default login`."
            )
        return self.project
