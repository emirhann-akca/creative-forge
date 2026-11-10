"""Content-addressed cache for paid generations.

The key is a SHA-256 over the canonical JSON of the request, with every input file replaced by the hash of its
bytes. Same prompt + same input pixels + same parameters = same key = no second payment.

Entries are written to a temporary directory and renamed into place, so concurrent jobs never see half an entry.
A long-running operation (Veo) stores its operation name in a ``pending`` file the moment it is accepted: a rerun
after a crash or timeout resumes polling that operation instead of submitting (and paying for) a new one.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _canonical(obj: Any) -> Any:
    if isinstance(obj, Path):
        return {"file_sha256": file_sha256(obj)}
    if isinstance(obj, dict):
        return {str(k): _canonical(v) for k, v in sorted(obj.items())}
    if isinstance(obj, list | tuple):
        return [_canonical(v) for v in obj]
    return obj


def content_key(kind: str, payload: dict[str, Any]) -> str:
    blob = json.dumps({"kind": kind, "payload": _canonical(payload)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class GenerationCache:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def entry_dir(self, kind: str, key: str) -> Path:
        return self.root / kind / key[:2] / key

    def get(self, kind: str, key: str) -> list[Path] | None:
        d = self.entry_dir(kind, key)
        meta = d / "meta.json"
        if not meta.exists():
            return None
        files = json.loads(meta.read_text(encoding="utf-8"))["files"]
        paths = [d / f for f in files]
        return paths if all(p.exists() for p in paths) else None

    def put(self, kind: str, key: str, files: list[Path], meta: dict[str, Any]) -> list[Path]:
        final = self.entry_dir(kind, key)
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=final.parent))
        names = []
        for i, f in enumerate(files):
            name = f"out_{i}{Path(f).suffix}"
            shutil.copy2(f, tmp / name)
            names.append(name)
        pending = final / "pending.json"
        if pending.exists():
            shutil.copy2(pending, tmp / "pending.json")
        (tmp / "meta.json").write_text(json.dumps({**meta, "files": names}, indent=2), encoding="utf-8")
        if final.exists():
            shutil.rmtree(final)
        os.replace(tmp, final)
        return [final / n for n in names]

    # Long-running operations -------------------------------------------------------------------------------------
    def pending(self, kind: str, key: str) -> dict[str, Any] | None:
        p = self.entry_dir(kind, key) / "pending.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def set_pending(self, kind: str, key: str, data: dict[str, Any]) -> None:
        d = self.entry_dir(kind, key)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".pending-{os.getpid()}.json"
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, d / "pending.json")
