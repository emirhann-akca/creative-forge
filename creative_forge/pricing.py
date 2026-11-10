"""Price table and cost estimation (no network: this only reads YAML)."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from creative_forge.spec import AdSpec


@dataclass(frozen=True)
class Pricing:
    as_of: str
    currency: str
    video_per_second_audio: float
    video_per_second_silent: float
    image_per_image: float
    music_per_track: float
    judge_per_call: float | None
    note: str = ""

    @classmethod
    def load(cls, path: str | Path | None = None) -> Pricing:
        if path is None:
            text = resources.files("creative_forge.data").joinpath("pricing.yaml").read_text(encoding="utf-8")
        else:
            text = Path(path).read_text(encoding="utf-8")
        d = yaml.safe_load(text)
        return cls(
            as_of=str(d["as_of"]),
            currency=d.get("currency", "USD"),
            video_per_second_audio=float(d["video"]["per_second_with_audio"]),
            video_per_second_silent=float(d["video"]["per_second_silent"]),
            image_per_image=float(d["image_edit"]["per_image"]),
            music_per_track=float(d["music"]["per_track"]),
            judge_per_call=None if d.get("judge", {}).get("per_call") is None else float(d["judge"]["per_call"]),
            note=d.get("note", ""),
        )

    def video(self, seconds: float, samples: int, audio: bool) -> float:
        rate = self.video_per_second_audio if audio else self.video_per_second_silent
        return round(rate * seconds * samples, 4)

    def image(self, images: int = 1) -> float:
        return round(self.image_per_image * images, 4)

    def music(self, tracks: int = 1) -> float:
        return round(self.music_per_track * tracks, 4)

    def judge(self, calls: int = 1) -> float | None:
        return None if self.judge_per_call is None else round(self.judge_per_call * calls, 4)


@dataclass
class PlanLine:
    concept: str
    kind: str
    model: str
    detail: str
    usd: float | None


@dataclass
class Plan:
    lines: list[PlanLine] = field(default_factory=list)
    pricing: Pricing | None = None

    @property
    def total_priced(self) -> float:
        return round(sum(line.usd for line in self.lines if line.usd is not None), 4)

    @property
    def unpriced(self) -> list[PlanLine]:
        return [line for line in self.lines if line.usd is None]

    def table(self) -> str:
        rows = [("concept", "step", "model", "detail", "USD")]
        for ln in self.lines:
            rows.append((ln.concept, ln.kind, ln.model, ln.detail, "unpriced" if ln.usd is None else f"{ln.usd:.2f}"))
        widths = [max(len(r[i]) for r in rows) for i in range(5)]
        out = ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)) for r in rows]
        out.insert(1, "  ".join("-" * w for w in widths))
        total = f"total (upper bound, before cache hits): ${self.total_priced:.2f}"
        if self.unpriced:
            total += f" + {len(self.unpriced)} unpriced call(s)"
        out.append(total)
        if self.pricing:
            out.append(f"prices as of {self.pricing.as_of}; verify on Google Cloud's pricing page")
        return "\n".join(out)


def estimate(spec: AdSpec, pricing: Pricing, judge_runs: int | None = None) -> Plan:
    """Upper-bound cost of a run: every generation is assumed to miss the cache."""
    plan = Plan(pricing=pricing)
    runs = spec.qa.judge_runs if judge_runs is None else judge_runs
    for c in spec.concepts:
        h = c.hook
        if h.image_prompt:
            plan.lines.append(PlanLine(c.id, "hook image", spec.models.image_edit, "1 image edit", pricing.image(1)))
        plan.lines.append(
            PlanLine(
                c.id,
                "hook video",
                spec.models.video,
                f"{h.samples} x {h.duration_s}s {'with' if h.audio else 'no'} audio",
                pricing.video(h.duration_s, h.samples, h.audio),
            )
        )
        if c.music and c.music.prompt:
            plan.lines.append(PlanLine(c.id, "music", spec.models.music, "1 track", pricing.music(1)))
        if runs:  # the judge watches the 9:16 master only; 4:5 is derived from it
            plan.lines.append(PlanLine(c.id, "judge", spec.models.judge, f"{runs} blind run(s)", pricing.judge(runs)))
    return plan
