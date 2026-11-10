"""Garbled-text detection in the AI hook.

Veo copies text it sees in the first frame and garbles it ("MILK" became "MIILAK"). The pipeline strips text from
the first frame, and this check reads the hook back: any word close to (but not equal to) a word the game really
shows is reported as garbled; any other word in an AI shot is reported as unexpected.

Backends (``--ocr auto`` tries them in this order):

* macOS: Apple Vision (compiled once from ``ocr_vision.swift`` with ``swiftc``), then RapidOCR, then tesseract;
* Windows and Linux: RapidOCR (``creative-forge[ocr]``: PP-OCR models on onnxruntime, shipped inside the wheel,
  no download at run time), then tesseract.

Every result records the backend that read the frames. With no backend, the check returns an explicit ``skip`` with
the reason; it never passes silently.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from PIL import Image

from creative_forge import fsutil
from creative_forge.ffmpeg import run
from creative_forge.paths import cache_dir as tool_cache_dir
from creative_forge.qa.result import CheckResult, Status, skipped

BACKENDS = ("vision", "rapidocr", "tesseract")
NO_BACKEND = (
    "no OCR backend available: install creative-forge[ocr] (RapidOCR) or tesseract; on macOS, Vision needs swiftc"
)

NormBox = tuple[float, float, float, float]  # x0, y0, x1, y1 as fractions of the frame, top-left origin


@dataclass(frozen=True)
class OcrToken:
    file: str
    text: str
    confidence: float
    box: NormBox | None = None


@dataclass(frozen=True)
class TextFinding:
    file: str
    word: str
    kind: str  # "garbled" or "unexpected"
    nearest: str | None
    distance: int | None


class OcrBackend:
    name = "base"

    def read_folder(self, folder: Path) -> list[OcrToken]:
        raise NotImplementedError


class VisionOcr(OcrBackend):
    name = "macos-vision"

    def __init__(self, binary: Path) -> None:
        self.binary = binary

    @classmethod
    def build(cls, cache_dir: Path | None = None) -> VisionOcr | None:
        if sys.platform != "darwin" or shutil.which("swiftc") is None:
            return None
        src = resources.files("creative_forge.qa").joinpath("ocr_vision.swift").read_bytes()
        digest = hashlib.sha256(src).hexdigest()[:12]
        cache_dir = cache_dir or tool_cache_dir()
        binary = cache_dir / f"ocr_vision-{digest}"
        if not binary.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as td:
                swift = Path(td) / "ocr_vision.swift"
                swift.write_bytes(src)
                tmp_bin = Path(td) / "ocr_vision"
                p = subprocess.run(
                    ["swiftc", "-O", str(swift), "-o", str(tmp_bin)], capture_output=True, encoding="utf-8"
                )
                if p.returncode != 0:
                    return None
                staged = cache_dir / f".{binary.name}.{os.getpid()}"
                shutil.copy2(tmp_bin, staged)  # the temp dir may sit on another volume; rename only within one
                fsutil.replace(staged, binary)
        return cls(binary)

    def read_folder(self, folder: Path) -> list[OcrToken]:
        p = subprocess.run(
            [str(self.binary), str(folder)], capture_output=True, encoding="utf-8", errors="replace", check=True
        )
        out = []
        for line in p.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                box = tuple(float(v) for v in parts[3].split(",")) if len(parts) >= 4 else None
                out.append(OcrToken(parts[0], parts[2], float(parts[1]), box))  # type: ignore[arg-type]
        return out


class TesseractOcr(OcrBackend):
    name = "tesseract"

    @classmethod
    def build(cls) -> TesseractOcr | None:
        return cls() if shutil.which("tesseract") else None

    def read_folder(self, folder: Path) -> list[OcrToken]:
        out = []
        for png in sorted(folder.glob("*.png")):
            w, h = Image.open(png).size
            p = subprocess.run(
                ["tesseract", str(png), "stdout", "--psm", "11", "tsv"],
                capture_output=True,
                encoding="utf-8",  # tesseract writes UTF-8 whatever the console code page is
                errors="replace",
            )
            for line in p.stdout.splitlines()[1:]:
                cols = line.split("\t")
                if len(cols) == 12 and cols[11].strip() and float(cols[10]) >= 0:
                    left, top, bw, bh = (int(cols[i]) for i in (6, 7, 8, 9))
                    box = (left / w, top / h, (left + bw) / w, (top + bh) / h)
                    out.append(OcrToken(png.name, cols[11].strip(), float(cols[10]) / 100.0, box))
        return out


class RapidOcr(OcrBackend):
    """RapidOCR 3.x on onnxruntime. Same models, same answers on Windows, macOS and Linux."""

    name = "rapidocr"

    def __init__(self, engine: object) -> None:
        self._engine = engine
        self._lock = threading.Lock()  # one engine shared by the concept threads; calls are serialized

    @classmethod
    def build(cls) -> RapidOcr | None:
        try:
            from rapidocr import RapidOCR
        except ImportError:
            return None
        return cls(RapidOCR(params={"Global.log_level": "error"}))

    def read_folder(self, folder: Path) -> list[OcrToken]:
        out = []
        for png in sorted(folder.glob("*.png")):
            with Image.open(png) as im:  # opened through Pillow: cv2.imread fails on non-ASCII Windows paths
                rgb = im.convert("RGB")
            w, h = rgb.size
            with self._lock:
                r = self._engine(rgb)  # type: ignore[operator]
            if not r.txts:
                continue
            for box, text, score in zip(r.boxes, r.txts, r.scores, strict=True):
                xs, ys = [float(p[0]) for p in box], [float(p[1]) for p in box]
                norm = (min(xs) / w, min(ys) / h, max(xs) / w, max(ys) / h)
                out.append(OcrToken(png.name, str(text), float(score), norm))
        return out


def default_order(platform: str | None = None) -> tuple[str, ...]:
    """Vision first on macOS (best on stylized game fonts in our tests); RapidOCR first everywhere else."""
    return BACKENDS if (platform or sys.platform) == "darwin" else ("rapidocr", "tesseract")


def build_backend(name: str) -> OcrBackend | None:
    if name == "vision":
        return VisionOcr.build()
    if name == "rapidocr":
        return RapidOcr.build()
    if name == "tesseract":
        return TesseractOcr.build()
    raise ValueError(f"unknown OCR backend {name!r} (choose from auto, {', '.join(BACKENDS)}, off)")


def detect_backend(prefer: str = "auto") -> OcrBackend | None:
    """``auto``: the first available backend in ``default_order()``. A named backend: that one or ``None``."""
    if prefer == "off":
        return None
    for name in default_order() if prefer == "auto" else (prefer,):
        b = build_backend(name)
        if b is not None:
            return b
    return None


def available_backends() -> list[str]:
    """Names of the backends that can be built here (builds each once; Vision may compile its helper)."""
    return [n for n in BACKENDS if build_backend(n) is not None]


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _inside(box: NormBox | None, regions: list[NormBox]) -> bool:
    if box is None:
        return False
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return any(r[0] <= cx <= r[2] and r[1] <= cy <= r[3] for r in regions)


def classify(
    tokens: list[OcrToken],
    known_text: list[str],
    ignore: list[NormBox] | None = None,
    min_len: int = 3,
    min_garbled_len: int = 4,
    min_conf: float = 0.3,
) -> list[TextFinding]:
    """``ignore``: regions holding our own overlays (e.g. the "Not gameplay footage" label)."""
    known = sorted({w.upper() for phrase in known_text for w in re.findall(r"[A-Za-z]+", phrase)})
    findings = []
    for t in tokens:
        if t.confidence < min_conf or _inside(t.box, ignore or []):
            continue
        for raw in re.findall(r"[A-Za-z]+", t.text):
            w = raw.upper()
            if len(w) < min_len or w in known:
                continue
            if known and len(w) >= min_garbled_len:
                nearest = min(known, key=lambda k: levenshtein(w, k))
                dist = levenshtein(w, nearest)
                if dist <= max(1, len(nearest) // 2):
                    findings.append(TextFinding(t.file, raw, "garbled", nearest, dist))
                    continue
            findings.append(TextFinding(t.file, raw, "unexpected", None, None))
    return findings


def _result(name: str, backend: OcrBackend, findings: list[TextFinding], scope: str) -> CheckResult:
    garbled = [f for f in findings if f.kind == "garbled"]
    unexpected = [f for f in findings if f.kind == "unexpected"]
    status = Status.FAIL if garbled else (Status.WARN if unexpected else Status.PASS)
    msg = "; ".join(
        [f"{f.word!r} ~ {f.nearest!r} in {f.file}" for f in garbled[:5]]
        + [f"unexpected {f.word!r} in {f.file}" for f in unexpected[:5]]
    )
    return CheckResult(
        name,
        status,
        len(garbled),
        f"no garbled text in {scope}",
        "garbled word(s)",
        msg or f"read with {backend.name}",
        {"backend": backend.name, "findings": [f.__dict__ for f in findings[:50]]},
    )


def _no_backend(name: str) -> CheckResult:
    r = skipped(name, NO_BACKEND)
    r.details["backend"] = None
    return r


def check_hook_text(
    video: Path,
    start: float,
    end: float,
    fps: float,
    known_text: list[str],
    backend: OcrBackend | None,
    ignore: list[NormBox] | None = None,
) -> CheckResult:
    if backend is None:
        return _no_backend("hook_text")
    if end - start < 0.1:
        return skipped("hook_text", "empty hook span")
    with tempfile.TemporaryDirectory(prefix="forge-ocr-") as td:
        run(
            [
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{end - start:.3f}",
                "-i",
                str(video),
                "-vf",
                f"fps={fps:g}",
                str(Path(td) / "f_%04d.png"),
            ]
        )
        findings = classify(backend.read_folder(Path(td)), known_text, ignore)
    return _result("hook_text", backend, findings, f"the hook ({start:.1f}-{end:.1f} s)")


def check_image_text(
    image: Path, known_text: list[str], backend: OcrBackend | None, name: str, label: str | None = None
) -> CheckResult:
    if backend is None:
        return _no_backend(name)
    with tempfile.TemporaryDirectory(prefix="forge-ocr-") as td:
        shutil.copy2(image, Path(td) / "frame.png")
        findings = classify(backend.read_folder(Path(td)), known_text)
    return _result(name, backend, findings, label or image.name)
