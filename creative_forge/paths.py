"""Where creative-forge keeps its own files: the managed ffmpeg and the compiled OCR helper.

Run outputs and the generation cache are not here; they go where the spec and ``CREATIVE_FORGE_CACHE`` say.

Defaults come from ``platformdirs``:

* Windows: ``%LOCALAPPDATA%\\creative-forge`` (data) and ``%LOCALAPPDATA%\\creative-forge\\Cache`` (cache)
* macOS: ``~/Library/Application Support/creative-forge`` and ``~/Library/Caches/creative-forge``
* Linux: ``$XDG_DATA_HOME/creative-forge`` and ``$XDG_CACHE_HOME/creative-forge``

``CREATIVE_FORGE_HOME`` moves both under one directory (``<home>/data`` and ``<home>/cache``).
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import PlatformDirs

APP = "creative-forge"
ENV_HOME = "CREATIVE_FORGE_HOME"


def _dirs() -> PlatformDirs:
    return PlatformDirs(APP, appauthor=False, roaming=False)


def data_dir() -> Path:
    home = os.environ.get(ENV_HOME)
    return Path(home) / "data" if home else Path(_dirs().user_data_dir)


def cache_dir() -> Path:
    home = os.environ.get(ENV_HOME)
    return Path(home) / "cache" if home else Path(_dirs().user_cache_dir)
