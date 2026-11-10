"""Pillow/numpy image work: text stripping, programmatic effects, overlays and end-card frames.

Sizes are authored for a 1080 px wide frame and scaled with ``k = width / 1080``.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from creative_forge.spec import Box, Effect


def font(size: int, path: Path | None = None) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if path is not None:
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size=size)


def smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def out_back(t: float, s: float = 1.9) -> float:
    t = max(0.0, min(1.0, t)) - 1
    return t * t * ((s + 1) * t + s) + 1


# ------------------------------------------------------------------------------------------------ first-frame prep
def strip_text(img: Image.Image, regions: list[Box]) -> Image.Image:
    """Blur text regions out of the first frame. Veo copies visible text and garbles it ("MILK" -> "MIILAK")."""
    out = img.convert("RGB").copy()
    for x0, y0, x1, y1 in regions:
        pad = max(8, (y1 - y0) // 2)
        bx0, by0 = max(0, x0 - pad), max(0, y0 - pad)
        bx1, by1 = min(out.width, x1 + pad), min(out.height, y1 + pad)
        area = out.crop((bx0, by0, bx1, by1))
        blurred = area.filter(ImageFilter.GaussianBlur(max(6, min(x1 - x0, y1 - y0) // 2)))
        mask = Image.new("L", area.size, 0)
        ImageDraw.Draw(mask).rectangle((x0 - bx0, y0 - by0, x1 - bx0, y1 - by0), fill=255)
        mask = mask.filter(ImageFilter.GaussianBlur(pad / 3))
        out.paste(blurred, (bx0, by0), mask)
    return out


def apply_effect(img: Image.Image, effect: Effect) -> Image.Image:
    """Effects are painted on the REAL frame. Asking the image model to cover the whole frame in dust/mud made it
    redraw the products underneath, so the board changed when the effect was cleared."""
    a = np.asarray(img.convert("RGB"), dtype=np.float32)
    h, w = a.shape[:2]
    rng = np.random.default_rng(effect.seed)
    s = effect.strength
    lum = a.mean(axis=2, keepdims=True)
    noise = (
        np.asarray(
            Image.fromarray((rng.random((max(1, h // 8), max(1, w // 8))) * 255).astype(np.uint8)).resize(
                (w, h), Image.BICUBIC
            ),
            dtype=np.float32,
        )[..., None]
        / 255.0
    )
    if effect.type == "dust":
        grey = lum * 0.55 + 105
        a = a * (1 - 0.85 * s) + grey * (0.85 * s)
        a += (noise - 0.5) * 50 * s
        specks = (rng.random((h, w, 1)) > 0.995) * 60 * s
        a += specks
    elif effect.type == "mud":
        mud = np.array([88, 58, 30], np.float32) * (0.75 + 0.5 * noise)
        a = a * (1 - 0.92 * s) + mud * (0.92 * s)
    elif effect.type == "frost":
        ice = np.array([215, 232, 248], np.float32)
        a = a * (1 - 0.7 * s) + ice * (0.7 * s) + (noise - 0.5) * 30 * s
    elif effect.type == "darken":
        a = a * (1 - 0.6 * s)
    out = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    if effect.type in ("dust", "frost"):
        out = out.filter(ImageFilter.GaussianBlur(1.2 * s))
    return out


def prepare_first_frame(real_frame: Path, regions: list[Box], effects: list[Effect], out: Path) -> Path:
    img = Image.open(real_frame).convert("RGB")
    img = strip_text(img, regions)
    for e in effects:
        img = apply_effect(img, e)
    img.save(out)
    return out


def draw_hand(img: Image.Image, progress: float = 0.0) -> Image.Image:
    """A simple stand-in "hand holding a tool" entering from the bottom-right corner (used by the fake image
    provider). It enters from an edge and stays clear of the centre, as the hook prompt rules require."""
    out = img.convert("RGB").copy()
    w, h = out.size
    k = w / 1080
    d = ImageDraw.Draw(out)
    ox, oy = int(w * (0.70 - 0.1 * progress)), int(h * (0.80 - 0.05 * progress))
    d.rounded_rectangle((ox, oy, ox + int(420 * k), oy + int(170 * k)), int(60 * k), fill=(224, 172, 140))
    for i in range(4):
        fx = ox + int((40 + i * 90) * k)
        d.rounded_rectangle(
            (fx, oy - int(70 * k), fx + int(70 * k), oy + int(40 * k)), int(30 * k), fill=(214, 160, 128)
        )
    d.rectangle((ox - int(260 * k), oy + int(50 * k), ox + int(20 * k), oy + int(110 * k)), fill=(30, 30, 34))
    return out


# ------------------------------------------------------------------------------------------------ overlays
def headline(text: str, size: int, fnt: Path | None = None, fill=(255, 255, 255), stroke=(46, 16, 74)) -> Image.Image:
    """Bold headline with a thick dark outline and a soft drop shadow."""
    f = font(size, fnt)
    sw = max(3, size // 9)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    x0, y0, x1, y1 = probe.textbbox((0, 0), text, font=f, stroke_width=sw)
    im = Image.new("RGBA", (x1 - x0 + 40, y1 - y0 + 40), (0, 0, 0, 0))
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text(
        (20 - x0, 20 - y0 + size * 0.07),
        text,
        font=f,
        fill=(20, 6, 34, 140),
        stroke_width=sw,
        stroke_fill=(20, 6, 34, 140),
    )
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(max(2, size // 20))))
    ImageDraw.Draw(im).text((20 - x0, 20 - y0), text, font=f, fill=fill, stroke_width=sw, stroke_fill=stroke)
    return im


def button(text: str, k: float, fnt: Path | None = None) -> Image.Image:
    """Port of the production call-to-action button: green pill, darker base edge, gloss strip, outlined label."""
    gw, gh = int(600 * k), int(160 * k)
    im = Image.new("RGBA", (gw + int(20 * k), gh + int(30 * k)), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pad = int(10 * k)
    d.rounded_rectangle((pad, int(22 * k), gw + pad, gh + int(22 * k)), gh // 2, fill=(18, 92, 20, 255))
    d.rounded_rectangle(
        (pad, int(8 * k), gw + pad, gh + int(8 * k)),
        gh // 2,
        fill=(40, 150, 40, 255),
        outline=(255, 255, 255, 255),
        width=max(2, int(8 * k)),
    )
    inner = Image.new("RGBA", (gw - int(24 * k), gh - int(30 * k)))
    gd = ImageDraw.Draw(inner)
    for y in range(inner.height):
        u = y / max(1, inner.height)
        gd.line([(0, y), (inner.width, y)], fill=(int(150 - 90 * u), int(245 - 55 * u), int(90 - 50 * u), 255))
    m = Image.new("L", inner.size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, inner.width - 1, inner.height - 1), inner.height // 2, fill=255)
    inner.putalpha(m)
    im.alpha_composite(inner, (int(22 * k), int(23 * k)))
    gloss = Image.new("RGBA", (gw - int(120 * k), max(2, int(34 * k))), (255, 255, 255, 0))
    ImageDraw.Draw(gloss).rounded_rectangle(
        (0, 0, gloss.width - 1, gloss.height - 1), gloss.height // 2, fill=(255, 255, 255, 90)
    )
    im.alpha_composite(gloss, (int(70 * k), int(34 * k)))
    label = headline(text, max(10, int(78 * k)), fnt, stroke=(20, 80, 20))
    im.alpha_composite(label, ((im.width - label.width) // 2, int(8 * k) + (gh - label.height) // 2))
    return im


def disclaimer_overlay(
    text: str, width: int, height: int, cy_ratio: float = 1205 / 1920, fnt: Path | None = None
) -> Image.Image:
    """Full-frame transparent overlay with a small "Not gameplay footage" label (shown only during the hook)."""
    k = width / 1080
    f = font(max(10, int(32 * k)), fnt)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    x0, y0, x1, y1 = probe.textbbox((0, 0), text, font=f)
    box = Image.new("RGBA", (x1 - x0 + int(40 * k), y1 - y0 + int(24 * k)), (0, 0, 0, 0))
    d = ImageDraw.Draw(box)
    d.rounded_rectangle((0, 0, box.width - 1, box.height - 1), int(16 * k), fill=(0, 0, 0, 105))
    d.text((int(20 * k) - x0, int(12 * k) - y0), text, font=f, fill=(255, 255, 255, 235))
    full = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    full.alpha_composite(box, ((width - box.width) // 2, int(height * cy_ratio - box.height / 2)))
    return full


def place(base: Image.Image, im: Image.Image, cx: float, cy: float, scale: float = 1.0, alpha: float = 1.0) -> None:
    if scale <= 0.01 or alpha <= 0.01:
        return
    if abs(scale - 1) > 1e-3:
        im = im.resize((max(1, int(im.width * scale)), max(1, int(im.height * scale))), Image.BICUBIC)
    if alpha < 0.999:
        a = im.getchannel("A").point(lambda v: int(v * alpha))
        im = im.copy()
        im.putalpha(a)
    base.alpha_composite(im, (int(cx - im.width / 2), int(cy - im.height / 2)))


def end_card_frames(
    background: Image.Image,
    width: int,
    height: int,
    fps: int,
    duration_s: float,
    headline_text: str,
    button_text: str,
    logo: Path | None = None,
    fnt: Path | None = None,
    blur: bool = True,
) -> Iterator[Image.Image]:
    """End card: blurred last gameplay frame under a purple veil, optional logo, headline, pulsing button."""
    k = width / 1080
    base = background.convert("RGB").resize((width, height), Image.LANCZOS)
    if blur:
        base = base.filter(ImageFilter.GaussianBlur(26 * k))
    base = base.convert("RGBA")
    base.alpha_composite(Image.new("RGBA", (width, height), (38, 18, 66, 150)))
    logo_im = None
    if logo is not None:
        logo_im = Image.open(logo).convert("RGBA")
        lw = int(560 * k)
        logo_im = logo_im.resize((lw, max(1, int(logo_im.height * lw / logo_im.width))), Image.LANCZOS)
    head = headline(headline_text, max(12, int(84 * k)), fnt, fill=(255, 245, 210))
    if head.width > width * 0.92:
        head = head.resize((int(width * 0.92), int(head.height * width * 0.92 / head.width)), Image.LANCZOS)
    btn = button(button_text, k, fnt)
    for j in range(round(duration_s * fps)):
        t = j / fps
        z = base.copy()
        if logo_im is not None:
            place(z, logo_im, width / 2, height * 465 / 1920, out_back(t / 0.35) * (1 + 0.02 * math.sin(t * 3)))
        place(z, head, width / 2, height * 985 / 1920, out_back((t - 0.25) / 0.3))
        pulse = 1 + 0.06 * max(0.0, math.sin((t - 0.8) * 7.0)) if t > 0.8 else out_back((t - 0.45) / 0.3)
        place(z, btn, width / 2, height * 1150 / 1920, pulse)
        yield z.convert("RGB")
