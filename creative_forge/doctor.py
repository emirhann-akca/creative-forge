"""``forge doctor``: everything a run depends on, in one report.

Each item is ``ok``, ``warn`` (works, but something is missing or degraded), ``fail`` (blocks ``forge demo`` /
``forge run``) or ``info``. The exit code is 1 when any item fails. Nothing secret is printed: for Vertex AI only the
presence and the ``type`` field of the Application Default Credentials file are reported, never its contents.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import requests

from creative_forge import __version__, paths
from creative_forge.config import ENV_COMFYUI_URL, ENV_PROJECT
from creative_forge.ffmpeg import (
    FFmpegError,
    aac_encoders,
    find_tools,
    inspect,
    managed_dir,
    probe_version,
)

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"


@dataclass
class Item:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


def _python() -> Item:
    return Item(
        "python",
        OK,
        f"Python {platform.python_version()} ({platform.machine()}), creative-forge {__version__}",
        {
            "executable": sys.executable,
            "platform": platform.platform(),
            "utf8_mode": bool(sys.flags.utf8_mode),
            "filesystem_encoding": sys.getfilesystemencoding(),
            "locale_encoding": _locale_encoding(),
        },
    )


def _locale_encoding() -> str:
    import locale

    return locale.getencoding() if hasattr(locale, "getencoding") else locale.getpreferredencoding(False)


def _ffmpeg() -> list[Item]:
    try:
        tools = find_tools()
    except FFmpegError as e:
        return [Item("ffmpeg", FAIL, str(e), {"managed_dir": str(managed_dir())})]
    details: dict[str, Any] = {"path": str(tools.ffmpeg), "source": tools.source, "managed_dir": str(managed_dir())}
    try:
        feats = inspect(tools.ffmpeg)
    except (FFmpegError, OSError) as e:
        return [Item("ffmpeg", FAIL, f"{tools.ffmpeg} does not run: {e}", details)]
    missing = feats.missing()
    usable_aac = aac_encoders() if not missing else []
    details |= {"version": feats.version, "missing": missing, "aac_encoders": usable_aac}
    if missing:
        item = Item("ffmpeg", FAIL, f"{feats.version}: lacks {', '.join(missing)}; run `forge setup ffmpeg`", details)
    elif not usable_aac:
        item = Item("ffmpeg", FAIL, f"{feats.version}: lists an AAC encoder but none of them opens", details)
    else:
        item = Item(
            "ffmpeg",
            OK,
            f"{feats.version} [{tools.source}] libx264, AAC={usable_aac[0]}, all required filters",
            details,
        )
    items = [item]
    try:
        pv = probe_version(tools.ffprobe)
        items.append(Item("ffprobe", OK, pv, {"path": str(tools.ffprobe)}))
    except (FFmpegError, OSError) as e:
        items.append(Item("ffprobe", FAIL, f"{tools.ffprobe} does not run: {e}", {"path": str(tools.ffprobe)}))
    return items


def _ocr() -> Item:
    from creative_forge.qa.ocr import available_backends, default_order

    available = available_backends()
    order = [n for n in default_order() if n in available]
    details = {"available": available, "auto_order": list(default_order())}
    if not order:
        return Item(
            "ocr",
            WARN,
            "no OCR backend: hook_text and first_frame_text checks will report skip; "
            "install creative-forge[ocr] (RapidOCR)",
            details,
        )
    return Item("ocr", OK, f"--ocr auto uses {order[0]} (available: {', '.join(available)})", details)


def _writable(directory: Path) -> str | None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".doctor-", dir=directory)
        os.close(fd)
        os.unlink(name)
        return None
    except OSError as e:
        return f"{type(e).__name__}: {e}"


def _dirs(cwd: Path) -> Item:
    locations = {"data": paths.data_dir(), "cache": paths.cache_dir(), "working directory": cwd}
    problems = {k: err for k, p in locations.items() if (err := _writable(p))}
    details = {k: str(p) for k, p in locations.items()} | {"not_writable": problems}
    if problems:
        return Item(
            "directories", FAIL, "not writable: " + "; ".join(f"{k} ({v})" for k, v in problems.items()), details
        )
    return Item("directories", OK, f"data {locations['data']} | cache {locations['cache']}", details)


def _comfyui(url: str) -> Item:
    try:
        r = requests.get(url.rstrip("/") + "/system_stats", timeout=3)
        r.raise_for_status()
        stats = r.json()
    except (requests.RequestException, ValueError) as e:
        return Item(
            "comfyui", WARN, f"{url} not reachable ({type(e).__name__}); only needed for image_provider: comfyui"
        )
    system = stats.get("system", {}) if isinstance(stats, dict) else {}
    devices = [d.get("name", "?") for d in stats.get("devices", [])] if isinstance(stats, dict) else []
    version = system.get("comfyui_version", "unknown version")
    return Item("comfyui", OK, f"{url} answers (ComfyUI {version})", {"url": url, "devices": devices})


def _adc_path() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", "")) / "gcloud" / "application_default_credentials.json"
    config = os.environ.get("CLOUDSDK_CONFIG")
    return (
        Path(config) / "application_default_credentials.json"
        if config
        else (Path.home() / ".config" / "gcloud" / "application_default_credentials.json")
    )


def _vertex() -> Item:
    """Presence only. The file holds a refresh token or a private key; neither is read out or printed."""
    explicit = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    path = Path(explicit) if explicit else _adc_path()
    project = bool(os.environ.get(ENV_PROJECT) or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    details: dict[str, Any] = {"credentials_file": str(path), "from_env": bool(explicit), "project_set": project}
    if not path.is_file():
        return Item(
            "vertex",
            INFO,
            "no Application Default Credentials (only needed for paid runs: gcloud auth application-default login)",
            details,
        )
    try:
        kind = json.loads(path.read_text(encoding="utf-8")).get("type", "unknown")
    except (OSError, ValueError, AttributeError):
        kind = "unreadable"
    details["type"] = kind
    project_note = "project set" if project else f"{ENV_PROJECT} not set"
    return Item("vertex", INFO, f"credentials present ({kind}); {project_note}", details)


def run_doctor(*, cwd: Path | None = None, comfyui_url: str | None = None) -> list[Item]:
    """ComfyUI is checked when a URL is given or ``CREATIVE_FORGE_COMFYUI_URL`` is set."""
    items = [_python(), *_ffmpeg(), _ocr(), _dirs(cwd or Path.cwd())]
    url = comfyui_url or os.environ.get(ENV_COMFYUI_URL)
    items.append(_comfyui(url) if url else Item("comfyui", INFO, f"not checked (set {ENV_COMFYUI_URL} or --comfyui)"))
    items.append(_vertex())
    return items


def failed(items: list[Item]) -> bool:
    return any(i.status == FAIL for i in items)


def to_json(items: list[Item]) -> str:
    # ASCII with \u escapes: survives any console code page and PowerShell's `>` redirection unchanged
    return json.dumps({"ok": not failed(items), "items": [asdict(i) for i in items]}, indent=2, ensure_ascii=True)


def format_text(items: list[Item]) -> str:
    w = max(len(i.name) for i in items)
    lines = [f"{i.name.ljust(w)}  {i.status.upper():4}  {i.summary}" for i in items]
    lines.append("")
    lines.append("blocking problems found" if failed(items) else "ready")
    return "\n".join(lines)
