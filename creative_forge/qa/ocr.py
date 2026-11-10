"""Garbled-text detection in the AI hook.

Veo copies text it sees in the first frame and garbles it ("MILK" became "MIILAK"). The pipeline strips text from
the first frame, and this check reads the hook back: any word close to (but not equal to) a word the game really
shows is reported as garbled; any other word in an AI shot is reported as unexpected.

Backends, in order: macOS Vision (compiled once from ``ocr_vision.swift``), then ``tesseract``. With neither, the
check returns an explicit ``skip`` result with the reason; it never passes silently.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from PIL import Image

from creative_forge.ffmpeg import run
from creative_forge.qa.result import CheckResult, Status, skipped

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
        cache_dir = cache_dir or Path.home() / ".cache" / "creative-forge"
        binary = cache_dir / f"ocr_vision-{digest}"
        if not binary.exists():
            cache_dir.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory() as td:
                swift = Path(td) / "ocr_vision.swift"
                swift.write_bytes(src)
                tmp_bin = Path(td) / "ocr_vision"
                p = subprocess.run(["swiftc", "-O", str(swift), "-o", str(tmp_bin)], capture_output=True, text=True)
                if p.returncode != 0:
                    return None
                shutil.move(str(tmp_bin), binary)
        return cls(binary)

    def read_folder(self, folder: Path) -> list[OcrToken]:
        p = subprocess.run([str(self.binary), str(folder)], capture_output=True, text=True, check=True)
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
            p = subprocess.run(["tesseract", str(png), "stdout", "--psm", "11", "tsv"], capture_output=True, text=True)
            for line in p.stdout.splitlines()[1:]:
                cols = line.split("\t")
                if len(cols) == 12 and cols[11].strip() and float(cols[10]) >= 0:
                    left, top, bw, bh = (int(cols[i]) for i in (6, 7, 8, 9))
                    box = (left / w, top / h, (left + bw) / w, (top + bh) / h)
                    out.append(OcrToken(png.name, cols[11].strip(), float(cols[10]) / 100.0, box))
        return out


def detect_backend(prefer: str = "auto") -> OcrBackend | None:
    if prefer in ("auto", "vision"):
        b = VisionOcr.build()
        if b or prefer == "vision":
            return b
    if prefer in ("auto", "tesseract"):
        return TesseractOcr.build()
    return None


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
        return skipped("hook_text", "no OCR backend available: install tesseract, or run on macOS with swiftc")
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


def check_image_text(image: Path, known_text: list[str], backend: OcrBackend | None, name: str) -> CheckResult:
    if backend is None:
        return skipped(name, "no OCR backend available: install tesseract, or run on macOS with swiftc")
    with tempfile.TemporaryDirectory(prefix="forge-ocr-") as td:
        shutil.copy2(image, Path(td) / "frame.png")
        findings = classify(backend.read_folder(Path(td)), known_text)
    return _result(name, backend, findings, image.name)
