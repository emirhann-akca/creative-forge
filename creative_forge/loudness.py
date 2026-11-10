"""EBU R128 / ITU-R BS.1770 measurement through ffmpeg's ``ebur128`` filter (true peak with ``peak=true``)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from creative_forge.ffmpeg import stderr_of

_I = re.compile(r"I:\s+(-?[\d.]+|-?inf) LUFS")
_LRA = re.compile(r"LRA:\s+(-?[\d.]+) LU")
_PEAK = re.compile(r"Peak:\s+(-?[\d.]+|-?inf) dBFS")


@dataclass(frozen=True)
class Loudness:
    integrated_lufs: float  # -inf for silence
    true_peak_dbtp: float  # -inf for silence
    lra_lu: float


def _num(s: str) -> float:
    return -math.inf if "inf" in s else float(s)


def measure(
    path: str | Path, start: float | None = None, end: float | None = None, pre_filter: str | None = None
) -> Loudness:
    """Integrated loudness, true peak and loudness range of ``path`` (optionally only ``[start, end)``)."""
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if start is not None and end is not None:
        args += ["-t", f"{max(0.01, end - start):.3f}"]
    elif end is not None:
        args += ["-t", f"{end:.3f}"]
    af = (pre_filter + "," if pre_filter else "") + "ebur128=peak=true:framelog=quiet"
    err = stderr_of([*args, "-i", str(path), "-vn", "-af", af, "-f", "null", "-"])
    i = _I.findall(err)
    p = _PEAK.findall(err)
    lra = _LRA.findall(err)
    if not i:
        raise RuntimeError(f"ebur128 produced no summary for {path}")
    return Loudness(_num(i[-1]), _num(p[-1]) if p else -math.inf, float(lra[-1]) if lra else 0.0)
