"""Picture stages: gameplay segment, hook clip, end card, assembly."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

from creative_forge.ffmpeg import extract_frame, last_frame, pipe_writer, probe, run
from creative_forge.imaging import disclaimer_overlay, end_card_frames
from creative_forge.spec import EndCardSpec, GameplaySpec, HookSpec, RenderSpec

MCI = "minterpolate=fps={fps}:mi_mode=mci:mc_mode=aobmc:me_mode=bidir:vsbmc=1"


def atempo_chain(speed: float) -> list[str]:
    """``atempo`` accepts 0.5-2.0 per instance; chain instances for anything outside that range."""
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    if abs(s - 1.0) > 1e-6:
        parts.append(f"atempo={s:.6f}")
    return parts


def cover_filter(w: int, h: int) -> str:
    return f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},setsar=1"


@dataclass(frozen=True)
class GameplayClip:
    video: Path
    audio: Path | None
    real_frame: Path  # the first frame exactly as the viewer sees it; used as Veo's lastFrame
    duration: float


def prepare_gameplay(src: Path, g: GameplaySpec, r: RenderSpec, work: Path) -> GameplayClip:
    """Cut, crop, scale and retime the real gameplay. The real frame for Veo is taken from THIS output, so the hook
    lands on exactly the pixels that follow it (same crop, same scaling, no second colour pass)."""
    span = g.out_s - g.in_s
    vf = []
    if g.crop:
        x, y, cw, ch = g.crop
        vf.append(f"crop={cw}:{ch}:{x}:{y}")
    vf += [cover_filter(r.width, r.height), f"setpts=PTS/{g.speed}", f"fps={r.fps}", "format=yuv420p"]
    out = work / "gameplay.mp4"
    run(
        [
            "-ss",
            f"{g.in_s:.3f}",
            "-t",
            f"{span:.3f}",
            "-i",
            str(src),
            "-vf",
            ",".join(vf),
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            str(r.intermediate_crf),
            "-preset",
            r.preset,
            str(out),
        ]
    )
    duration = float(probe(out).video_duration or g.output_duration)
    audio = None
    if g.use_audio and probe(src).has_audio:
        audio = work / "gameplay.wav"
        af = [
            *atempo_chain(g.speed),
            "aformat=sample_rates=48000:channel_layouts=stereo",
            f"volume={g.audio_gain_db}dB",
        ]
        run(
            [
                "-ss",
                f"{g.in_s:.3f}",
                "-t",
                f"{span:.3f}",
                "-i",
                str(src),
                "-vn",
                "-af",
                ",".join(af),
                "-t",
                f"{duration:.3f}",
                "-c:a",
                "pcm_s16le",
                str(audio),
            ]
        )
    real = work / "real_frame.png"
    extract_frame(out, 0.0).save(real)
    return GameplayClip(out, audio, real, duration)


@dataclass(frozen=True)
class HookClip:
    video: Path
    audio: Path | None
    duration: float


def _patch_png(real_frame: Path, box: tuple[int, int, int, int], out: Path) -> Path:
    """Region of the real frame with feathered edges (covers UI that Veo invented late in the clip)."""
    x0, y0, x1, y1 = box
    part = Image.open(real_frame).convert("RGB").crop(box).convert("RGBA")
    m = Image.new("L", part.size, 0)
    inset = max(2, min(10, (x1 - x0) // 8, (y1 - y0) // 8))
    ImageDraw.Draw(m).rectangle((inset, inset, part.width - inset - 1, part.height - inset - 1), fill=255)
    part.putalpha(m.filter(ImageFilter.GaussianBlur(inset / 2)))
    part.save(out)
    return out


def prepare_hook(veo: Path, real_frame: Path, h: HookSpec, r: RenderSpec, work: Path) -> HookClip:
    """Trim Veo's tail, cover hallucinated UI with real pixels, then 24 -> 60 fps motion-compensated interpolation
    and an exact decimation to the output rate. A plain ``fps`` conversion repeats frames and judders."""
    cut = h.effective_cut
    inputs = ["-t", f"{cut:.3f}", "-i", str(veo)]
    graph = [f"[0:v]scale={r.width}:{r.height}:flags=lanczos,setsar=1[b0]"]
    last = "[b0]"
    for i, p in enumerate(h.patches):
        png = _patch_png(real_frame, p.box, work / f"patch_{i}.png")
        inputs += ["-loop", "1", "-i", str(png)]
        graph.append(f"[{i + 1}:v]format=rgba,fade=t=in:st={p.from_s:.3f}:d={p.fade_s:.3f}:alpha=1[p{i}]")
        graph.append(f"{last}[p{i}]overlay={p.box[0]}:{p.box[1]}:shortest=1[b{i + 1}]")
        last = f"[b{i + 1}]"
    graph.append(f"{last}{MCI.format(fps=r.hook_interpolate_fps)},fps={r.fps},format=yuv420p[v]")
    out = work / "hook.mp4"
    run(
        [
            *inputs,
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            str(r.intermediate_crf),
            "-preset",
            r.preset,
            "-t",
            f"{cut:.3f}",
            str(out),
        ]
    )
    audio = None
    if probe(veo).has_audio:
        audio = work / "hook.wav"
        fade_at = max(0.0, cut - 0.25)
        run(
            [
                "-t",
                f"{cut:.3f}",
                "-i",
                str(veo),
                "-vn",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-af",
                f"loudnorm=I=-16:TP=-3:LRA=11,aresample=48000,afade=t=out:st={fade_at:.3f}:d=0.25,"
                f"volume={h.audio_gain_db}dB",
                "-t",
                f"{cut:.3f}",
                "-c:a",
                "pcm_s16le",
                str(audio),
            ]
        )
    return HookClip(out, audio, float(probe(out).video_duration or cut))


def render_end_card(gameplay: Path, e: EndCardSpec, r: RenderSpec, work: Path, resolve) -> Path:
    if e.image is not None:
        bg, blur = Image.open(resolve(e.image)), False
    else:
        bg, blur = last_frame(gameplay), True
    out = work / "end_card.mp4"
    proc = pipe_writer(out, r.width, r.height, r.fps, crf=r.intermediate_crf, preset=r.preset)
    assert proc.stdin is not None
    for frame in end_card_frames(
        bg,
        r.width,
        r.height,
        r.fps,
        e.duration_s,
        e.headline,
        e.button,
        logo=resolve(e.logo),
        fnt=resolve(e.font),
        blur=blur,
    ):
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"end card encode failed: {proc.stderr.read().decode()[-500:] if proc.stderr else ''}")
    return out


def assemble(
    hook: HookClip, gameplay: GameplayClip, end_card: Path, r: RenderSpec, disclaimer: str | None, work: Path
) -> tuple[Path, dict[str, float]]:
    """hook --crossfade--> real gameplay --crossfade--> end card. Durations come from the rendered clips."""
    x = r.crossfade_s
    hd, gd = hook.duration, gameplay.duration
    ed = float(probe(end_card).video_duration or 0.0)
    timeline = {
        "hook_end": hd - x,
        "crossfade": x,
        "gameplay_start": hd - x,
        "gameplay_end": hd - x + gd - x,
        "end_card_start": hd - x + gd - x,
        "total": hd + gd + ed - 2 * x,
    }
    norm = f"fps={r.fps},format=yuv420p,setsar=1"
    inputs = ["-i", str(hook.video), "-i", str(gameplay.video), "-i", str(end_card)]
    graph = [
        f"[0:v]{norm}[h]",
        f"[1:v]{norm}[g]",
        f"[2:v]{norm}[e]",
        f"[h][g]xfade=transition=fade:duration={x:.3f}:offset={hd - x:.3f}[hg]",
        f"[hg][e]xfade=transition=fade:duration={x:.3f}:offset={timeline['end_card_start']:.3f}[v0]",
    ]
    out_label = "[v0]"
    if disclaimer:
        png = work / "disclaimer.png"
        disclaimer_overlay(disclaimer, r.width, r.height).save(png)
        inputs += ["-loop", "1", "-i", str(png)]
        graph.append(f"[3:v]format=rgba[d];[v0][d]overlay=0:0:shortest=1:enable='lt(t,{timeline['hook_end']:.3f})'[v1]")
        out_label = "[v1]"
    out = work / "assembled.mp4"
    run(
        [
            *inputs,
            "-filter_complex",
            ";".join(graph),
            "-map",
            out_label,
            "-an",
            "-c:v",
            "libx264",
            "-crf",
            str(r.intermediate_crf),
            "-preset",
            r.preset,
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(r.fps),
            "-t",
            f"{timeline['total']:.3f}",
            str(out),
        ]
    )
    return out, timeline
