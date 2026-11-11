"""Price table and cost estimation (no network: this only reads YAML)."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from creative_forge.spec import AdSpec, HookSpec


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
    renders: int = 0  # (concept, hook, segment, end card, language) combinations
    exports: int = 0  # renders x formats

    @property
    def total_priced(self) -> float:
        return round(sum(line.usd for line in self.lines if line.usd is not None), 4)

    def count(self, kind: str) -> int:
        return sum(1 for line in self.lines if line.kind == kind)

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
        if self.exports:
            out.append(
                f"{self.exports} export(s) from {self.renders} render(s); paid generations, each at most once (cache): "
                f"{self.count('hook video')} hook video(s), {self.count('hook image')} image edit(s), "
                f"{self.count('music')} music track(s), {self.count('judge')} judge line(s)"
            )
        if self.pricing:
            out.append(f"prices as of {self.pricing.as_of}; verify on Google Cloud's pricing page")
        return "\n".join(out)


def estimate(spec: AdSpec, pricing: Pricing, judge_runs: int | None = None) -> Plan:
    """Upper-bound cost of a run: every generation is assumed to miss the cache within the run's own reuse.

    A variant matrix pays per *hook*, not per export: every hook lands on the concept's landing frame, so one hook
    generation serves every segment, end card, language and format it is combined with (the cache key does not
    change). Music is one track per concept. The judge watches one export per render (the 9:16 master)."""
    plan = Plan(pricing=pricing)
    runs = spec.qa.judge_runs if judge_runs is None else judge_runs
    for c in spec.concepts:
        hooks = c.hook_variants()
        for hid, h in hooks:
            who = c.id if len(hooks) == 1 and hid == "main" else f"{c.id}/{hid}"
            if h.image_prompt and spec.image_provider == "comfyui":
                plan.lines.append(PlanLine(who, "hook image", spec.image_edit_model, "1 local SDXL edit", 0.0))
            elif h.image_prompt:
                plan.lines.append(PlanLine(who, "hook image", spec.models.image_edit, "1 image edit", pricing.image(1)))
            plan.lines.append(_video_line(spec, pricing, who, h))
        if c.music and c.music.prompt:
            plan.lines.append(PlanLine(c.id, "music", spec.models.music, "1 track", pricing.music(1)))
        renders = len(spec.matrix(c))
        plan.renders += renders
        plan.exports += renders * len(c.formats)
        if runs:  # the judge watches the 9:16 master only; 4:5 is derived from it
            detail = f"{runs} blind run(s)" + (f" x {renders} renders" if renders > 1 else "")
            price = pricing.judge(runs * renders)
            plan.lines.append(PlanLine(c.id, "judge", spec.models.judge, detail, price))
    return plan


def _video_line(spec: AdSpec, pricing: Pricing, who: str, h: HookSpec) -> PlanLine:
    detail = f"{h.samples} x {h.duration_s}s {'with' if h.audio else 'no'} audio"
    return PlanLine(who, "hook video", spec.models.video, detail, pricing.video(h.duration_s, h.samples, h.audio))
