"""Sound: mix (hook + gameplay + SFX bus, music ducked under it) and the loudness-matching master loop."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from creative_forge.ffmpeg import aac_bitrate, aac_encoder, probe, run
from creative_forge.loudness import measure
from creative_forge.spec import SFX, AudioSpec


@dataclass(frozen=True)
class Placed:
    path: Path
    at_s: float
    gain_db: float = 0.0
    pitch: float = 1.0


def mix(
    fx: list[Placed],
    music: Path | None,
    music_db: float,
    music_offset_s: float,
    total_s: float,
    a: AudioSpec,
    out: Path,
) -> Path:
    """FX bus (hook audio, gameplay audio, SFX) padded with silence to the full length, music faded and ducked
    under the bus by a sidechain compressor, light limiter.

    ``apad=whole_dur`` matters: without it the sidechain output ends with the last effect, and in production the
    music stopped (and the audio track ended) 1.6 s before the picture."""
    sr = a.sample_rate
    inputs: list[str] = []
    graph: list[str] = []
    n = 0
    for p in fx:
        if p.at_s >= total_s or not Path(p.path).exists():
            continue
        inputs += ["-i", str(p.path)]
        ms = round(max(0.0, p.at_s) * 1000)
        pitch = f"asetrate={sr}*{p.pitch:.4f},aresample={sr}," if abs(p.pitch - 1.0) > 0.002 else ""
        graph.append(
            f"[{n}:a]aformat=sample_rates={sr}:channel_layouts=stereo,{pitch}"
            f"adelay={ms}|{ms},volume={p.gain_db}dB[e{n}]"
        )
        n += 1
    if n:
        labels = "".join(f"[e{i}]" for i in range(n))
        graph.append(f"{labels}amix=inputs={n}:normalize=0,apad=whole_dur={total_s:.3f}[fx]")
    else:
        inputs += ["-f", "lavfi", "-t", f"{total_s:.3f}", "-i", f"anullsrc=r={sr}:cl=stereo"]
        graph.append(f"[{n}:a]anull[fx]")
        n += 1
    if music is not None:
        inputs += ["-ss", f"{music_offset_s:.3f}", "-i", str(music)]
        fade_out = max(0.1, total_s - 0.6)
        graph.append(
            f"[{n}:a]aformat=sample_rates={sr}:channel_layouts=stereo,volume={music_db}dB,"
            f"afade=t=in:st=0:d=0.4,afade=t=out:st={fade_out:.3f}:d=0.6,"
            f"apad=whole_dur={total_s:.3f},atrim=0:{total_s:.3f}[mus]"
        )
        graph.append("[fx]asplit=2[fx1][fx2]")
        graph.append(
            f"[mus][fx2]sidechaincompress=threshold={a.duck_threshold}:ratio={a.duck_ratio}:"
            f"attack={a.duck_attack_ms}:release={a.duck_release_ms}[ducked]"
        )
        graph.append("[fx1][ducked]amix=inputs=2:normalize=0[sum]")
    else:
        graph.append("[fx]anull[sum]")
    graph.append(f"[sum]alimiter=limit={a.mix_limiter},aresample={sr}[aout]")
    run(
        [
            *inputs,
            "-filter_complex",
            ";".join(graph),
            "-map",
            "[aout]",
            "-t",
            f"{total_s:.3f}",
            "-c:a",
            "pcm_s24le",
            str(out),
        ]
    )
    return out


def sfx_placements(sfx: list[SFX], resolve) -> list[Placed]:
    return [Placed(resolve(s.path), s.at_s, s.gain_db, s.pitch) for s in sfx]


@dataclass
class MasterIteration:
    gain_db: float
    hook_offset_db: float
    limiter: float
    integrated_lufs: float
    true_peak_dbtp: float
    hook_lufs: float
    gameplay_lufs: float

    @property
    def delta_lu(self) -> float:
        return self.hook_lufs - self.gameplay_lufs


@dataclass
class MasterResult:
    path: Path
    encoder: str
    converged: bool
    before_hook_lufs: float
    before_gameplay_lufs: float
    iterations: list[MasterIteration] = field(default_factory=list)

    @property
    def final(self) -> MasterIteration:
        return self.iterations[-1]

    def to_dict(self) -> dict:
        return {
            "encoder": self.encoder,
            "converged": self.converged,
            "before": {
                "hook_lufs": round(self.before_hook_lufs, 2),
                "gameplay_lufs": round(self.before_gameplay_lufs, 2),
            },
            "iterations": [{k: round(v, 3) for k, v in asdict(i).items()} for i in self.iterations],
        }


def _finite(x: float, fallback: float) -> float:
    return fallback if math.isinf(x) or math.isnan(x) else x


def master(
    src: Path, out: Path, hook_end: float, gameplay_window: tuple[float, float], a: AudioSpec, bitrate_kbps: int = 192
) -> MasterResult:
    """Loudness-match the hook to the gameplay, normalise the whole ad and limit true peak, measuring the ENCODED
    AAC each round.

    One pass is not enough: the limiter squashes transient gameplay SFX more than the smoother AI hook audio, so the
    hook/gameplay difference after limiting is not the difference you corrected before it. The loop measures after
    the limiter and after encoding, and corrects again (production: up to 6 rounds to reach <= 0.8 LU)."""
    enc = aac_encoder(a.encoder)
    bitrate_kbps = aac_bitrate(enc, bitrate_kbps)  # Media Foundation AAC takes 96/128/160/192 kbps only
    g0, g1 = gameplay_window
    hook0 = _finite(measure(src, 0.0, hook_end).integrated_lufs, -70.0)
    game0 = _finite(measure(src, g0, g1).integrated_lufs, -70.0)
    offset = game0 - hook0  # dB added to the hook (ramped over ramp_s around the cut)
    gain = 0.0
    limit = a.limiter_limit
    r = a.ramp_s
    res = MasterResult(out, enc, False, hook0, game0)
    prev: tuple[float, float] | None = None
    for _ in range(a.max_iterations):
        offset = max(-12.0, min(12.0, offset))
        env = (
            f"volume=volume='pow(10,(if(lt(t,{hook_end - r / 2:.3f}),{offset:.3f},"
            f"if(lt(t,{hook_end + r / 2:.3f}),{offset:.3f}*({hook_end + r / 2:.3f}-t)/{r:.3f},0)))/20)':eval=frame"
        )
        af = (
            f"{env},volume={gain:.2f}dB,aresample=192000,"
            f"alimiter=limit={limit:.4f}:attack=1:release=60:level=disabled,aresample={a.sample_rate}"
        )
        run(["-i", str(src), "-vn", "-af", af, "-c:a", enc, "-b:a", f"{bitrate_kbps}k", str(out)])
        whole = measure(out)
        hook = _finite(measure(out, 0.0, hook_end).integrated_lufs, -70.0)
        game = _finite(measure(out, g0, g1).integrated_lufs, -70.0)
        i = _finite(whole.integrated_lufs, -70.0)
        tp = whole.true_peak_dbtp
        res.iterations.append(MasterIteration(gain, offset, limit, i, tp, hook, game))
        loud_ok = abs(i - a.target_lufs) < a.tolerance_lu
        delta_ok = abs(hook - game) < a.hook_gameplay_max_delta_lu
        peak_ok = tp <= a.true_peak_max_dbtp
        if loud_ok and delta_ok and peak_ok:
            res.converged = True
            break
        # Secant step: behind a limiter, loudness rises by less than 1 dB per dB of gain, so a plain
        # "add the error" step (the original script) needs many rounds when the limiter is working hard.
        slope = 1.0
        if prev is not None and abs(gain - prev[0]) > 0.05:
            slope = min(1.0, max(0.25, (i - prev[1]) / (gain - prev[0])))
        prev = (gain, i)
        gain += (a.target_lufs - i) / slope
        offset -= hook - game
        if not peak_ok:  # encoder overshoot: pull the limiter ceiling down by the measured excess
            limit = max(0.1, limit * 10 ** ((a.true_peak_max_dbtp - tp - 0.1) / 20))
    return res


def mux(video: Path, audio: Path, out: Path, total_s: float) -> Path:
    """Explicit duration, never ``-shortest`` (it once cut the picture to a short audio track)."""
    run(
        [
            "-i",
            str(video),
            "-i",
            str(audio),
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c",
            "copy",
            "-t",
            f"{total_s:.3f}",
            "-movflags",
            "+faststart",
            str(out),
        ]
    )
    return out


def audio_duration(path: Path) -> float:
    return float(probe(path).audio_duration or 0.0)
