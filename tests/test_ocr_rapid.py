"""RapidOCR, the OCR backend on Windows and Linux, run for real on Pillow-rendered frames.

The production defect: Veo copied "MILK" from the first frame as "MIILAK". With "MILK" known, a frame reading
"MIILAK" must be reported as garbled and a frame reading "MILK" must not. CI sets CREATIVE_FORGE_REQUIRE_OCR=rapidocr
so a missing backend fails the job instead of skipping these tests."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFilter

from creative_forge.ffmpeg import run
from creative_forge.imaging import font
from creative_forge.qa.ocr import RapidOcr, check_hook_text, classify, default_order, detect_backend
from creative_forge.qa.result import Status

if os.environ.get("CREATIVE_FORGE_REQUIRE_OCR") != "rapidocr":
    pytest.importorskip("rapidocr", reason="install creative-forge[ocr] for the RapidOCR backend")

KNOWN = ["MILK", "PLAY NOW", "LEVEL"]


def render(word: str, style: str) -> Image.Image:
    """Four looks: plain, game-like (white with an outline on blue), small, rotated and blurred."""
    if style == "small":
        im = Image.new("RGB", (360, 180), (245, 245, 240))
        ImageDraw.Draw(im).text((60, 60), word, font=font(36), fill=(20, 20, 20))
        return im
    bg = (40, 90, 200) if style == "game" else (245, 245, 240)
    im = Image.new("RGB", (720, 360), bg)
    d = ImageDraw.Draw(im)
    if style == "game":
        d.text((120, 110), word, font=font(110), fill="white", stroke_width=6, stroke_fill=(10, 20, 60))
    else:
        d.text((120, 110), word, font=font(110), fill=(20, 20, 20))
    if style == "rot_blur":
        im = im.rotate(8, resample=Image.BICUBIC, fillcolor=bg).filter(ImageFilter.GaussianBlur(1.2))
    return im


@pytest.fixture(scope="module")
def engine() -> RapidOcr:
    b = RapidOcr.build()
    assert b is not None, "rapidocr is not importable"
    return b


@pytest.mark.parametrize("style", ["plain", "game", "small", "rot_blur"])
def test_miilak_is_garbled_and_milk_is_clean(engine, tmp_path, style):
    folder = tmp_path / "Çalışma Ş" / "İğ"  # non-ASCII path: cv2.imread would fail here on Windows
    folder.mkdir(parents=True)
    render("MIILAK", style).save(folder / "f_0001.png")
    render("MILK", style).save(folder / "f_0002.png")
    tokens = engine.read_folder(folder)
    read = {t.file: t.text.replace(" ", "") for t in tokens}
    assert read.get("f_0001.png") == "MIILAK" and read.get("f_0002.png") == "MILK", read
    assert all(t.box and 0 <= t.box[0] < t.box[2] <= 1 and 0 <= t.box[1] < t.box[3] <= 1 for t in tokens)

    findings = classify(tokens, KNOWN)
    garbled = [f for f in findings if f.kind == "garbled"]
    assert [(f.file, f.word, f.nearest) for f in garbled] == [("f_0001.png", "MIILAK", "MILK")]
    assert not [f for f in findings if f.file == "f_0002.png"]


def test_hook_text_check_fails_on_a_garbled_hook_and_names_the_backend(engine, tmp_path):
    img = Image.new("RGB", (360, 640), (40, 120, 60))
    ImageDraw.Draw(img).text((50, 260), "MIILAK", font=font(64), fill="white")
    png = tmp_path / "f.png"
    img.save(png)
    clip = tmp_path / "hook.mp4"
    run(["-loop", "1", "-i", str(png), "-t", "1.5", "-r", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(clip)])
    r = check_hook_text(clip, 0, 1.5, 2, KNOWN, engine)
    assert r.status is Status.FAIL and r.details["backend"] == "rapidocr"
    assert any(f["word"] == "MIILAK" and f["nearest"] == "MILK" for f in r.details["findings"])


def test_backend_order_per_platform():
    assert default_order("win32") == ("rapidocr", "tesseract")
    assert default_order("linux") == ("rapidocr", "tesseract")
    assert default_order("darwin")[0] == "vision" and "rapidocr" in default_order("darwin")
    assert detect_backend("rapidocr").name == "rapidocr"
    assert detect_backend("off") is None
    if sys.platform != "darwin":
        assert detect_backend("auto").name == "rapidocr"


def test_cli_accepts_rapidocr(tmp_path, good_video, capsys):
    from creative_forge.cli import main

    out = tmp_path / "qa.json"
    main(["qa", str(good_video), "--ocr", "rapidocr", "--json", str(out)])
    assert out.exists() and Path(out).stat().st_size > 0
