"""Append-only, hash-chained audit log (JSON Lines) of every ad-network call and every decision.

Each line is one record with ``seq``, ``at``, ``prev`` (the previous record's hash) and ``hash`` = SHA-256 of the
record's canonical JSON without ``hash``. Editing, reordering or deleting any line breaks the chain from that point,
and ``verify()`` names the first broken record. The file is only ever opened for appending; a lock file keeps two
processes (the CLI and ``forge ui``) from writing the same ``prev`` twice.

Records never hold a secret: request headers are not logged at all, and bodies pass through the client's redactor
before they get here. Uploaded files are logged by name, size and SHA-256, not by content.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
SECRET_KEYS = frozenset({"access_token", "authorization", "access-token", "developer-token", "api_key", "secret"})


def canonical(record: dict[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def record_hash(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def scrub(value: Any) -> Any:
    """Drop secret-named keys anywhere in a JSON-like value (a second line of defence after the redactor)."""
    if isinstance(value, dict):
        return {k: ("***" if str(k).lower() in SECRET_KEYS else scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


@dataclass(frozen=True)
class AuditCheck:
    ok: bool
    records: int
    problem: str = ""


@contextlib.contextmanager
def _file_lock(path: Path, timeout_s: float = 10.0) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:  # a lock left by a crashed process is stale after 60 s
                if time.time() - lock.stat().st_mtime > 60:
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"audit log {path} is locked by another process") from None
            time.sleep(0.02)
    try:
        yield
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            lock.unlink()


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def _tail(self) -> tuple[int, str]:
        if not self.path.is_file() or self.path.stat().st_size == 0:
            return 0, GENESIS
        with open(self.path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            chunk = f.read()
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        last = json.loads(lines[-1].decode("utf-8"))
        return int(last["seq"]), str(last["hash"])

    def append(self, kind: str, /, **fields: Any) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, _file_lock(self.path):
            seq, prev = self._tail()
            rec: dict[str, Any] = {
                "seq": seq + 1,
                "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
                "kind": kind,
                **scrub(fields),
                "prev": prev,
            }
            rec = json.loads(canonical(rec))  # what is hashed is exactly what is written and read back
            rec["hash"] = record_hash(rec)
            with open(self.path, "a", encoding="utf-8", newline="\n") as f:
                f.write(canonical(rec) + "\n")
                f.flush()
                os.fsync(f.fileno())
            return rec

    def records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def verify(self) -> AuditCheck:
        if not self.path.is_file():
            return AuditCheck(True, 0)
        prev, n = GENESIS, 0
        with open(self.path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    return AuditCheck(False, n, f"line {lineno}: not JSON")
                n += 1
                if rec.get("seq") != n:
                    return AuditCheck(False, n, f"line {lineno}: seq {rec.get('seq')} where {n} was expected")
                if rec.get("prev") != prev:
                    return AuditCheck(False, n, f"line {lineno}: prev does not match the hash of record {n - 1}")
                if rec.get("hash") != record_hash(rec):
                    return AuditCheck(False, n, f"line {lineno}: hash does not match the record (edited)")
                prev = rec["hash"]
        return AuditCheck(True, n)
