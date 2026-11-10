"""File operations that hold up on Windows.

On Windows a rename or delete fails while any process has the file open without ``FILE_SHARE_DELETE``: a video
player, the search indexer or an antivirus scanner that opens every new mp4 for a moment. The error is a
``PermissionError`` (sharing violation or access denied) that clears once the other handle is closed, so these
helpers retry with a short backoff there. On POSIX a ``PermissionError`` is permanent and is raised at once.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

T = TypeVar("T")

IS_WINDOWS = os.name == "nt"
ATTEMPTS = 12  # 10 ms doubling up to 0.5 s: about 3.5 s in total before giving up


def _retry(fn: Callable[[], T], retry: bool | None, attempts: int) -> T:
    retry = IS_WINDOWS if retry is None else retry
    delay = 0.01
    for i in range(attempts):
        try:
            return fn()
        except PermissionError:
            if not retry or i == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
    raise AssertionError("unreachable")


def replace(src: str | Path, dst: str | Path, *, retry: bool | None = None, attempts: int = ATTEMPTS) -> None:
    """``os.replace`` (atomic on one volume), retried while another process holds ``dst`` open on Windows."""
    _retry(lambda: os.replace(src, dst), retry, attempts)


def _clear_readonly(func: Callable[..., object], path: str, _exc: object) -> None:
    os.chmod(path, stat.S_IWRITE)  # Windows refuses to delete read-only files; git checkouts and zips make them
    func(path)


def rmtree(path: str | Path, *, retry: bool | None = None, attempts: int = ATTEMPTS) -> None:
    """``shutil.rmtree`` that clears read-only bits and retries on a sharing violation. A missing path is fine."""

    def go() -> None:
        if not Path(path).exists():
            return
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_clear_readonly)
        else:  # onerror is deprecated from 3.12 on
            shutil.rmtree(path, onerror=_clear_readonly)

    _retry(go, retry, attempts)
