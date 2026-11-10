"""Orchestration: one job per concept, each in its own temp directory, run concurrently.

hook image -> hook video (lastFrame = real frame, tail trim, patches, 24->60 fps) -> assemble (crossfades, gameplay,
end card) -> mix (padded FX bus, ducked music) -> master (loudness/true-peak loop on encoded AAC) -> export (9:16,
4:5) -> QA -> blind judge -> report.
"""

from __future__ import annotations

import shutil
import tempfile
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image

from creative_forge.cache import GenerationCache
from creative_forge.config import Settings
from creative_forge.ffmpeg import extract_frame, require_tools
from creative_forge.generator import Generator
from creative_forge.imaging import disclaimer_overlay, prepare_first_frame
from creative_forge.ledger import BudgetGuard, CostLedger
from creative_forge.lint import LintIssue, lint
from creative_forge.pricing import Plan, Pricing, estimate
from creative_forge.providers.base import ImageEditRequest, JudgeRequest, MusicRequest, ProviderSet, VideoRequest
from creative_forge.qa.judge import aggregate, blind_codes, check_judge, judge_prompt
from creative_forge.qa.ocr import OcrBackend, check_image_text, detect_backend
from creative_forge.qa.result import CheckResult, Status, worst
from creative_forge.qa.runner import run_video_checks, write_sidecar
from creative_forge.qa.seam import check_seam_frames
from creative_forge.spec import AdSpec, Concept
from creative_forge.stages.audio import Placed, master, mix, mux, sfx_placements
from creative_forge.stages.export import export_4x5, export_9x16, size_4x5
from creative_forge.stages.video import assemble, prepare_gameplay, prepare_hook, render_end_card


@dataclass
class ExportResult:
    format: str
    path: Path
    checks: list[CheckResult]

    @property
    def status(self) -> Status:
        return worst(self.checks)


@dataclass
class ConceptResult:
    concept: str
    error: str | None = None
    exports: list[ExportResult] = field(default_factory=list)
    pre_checks: list[CheckResult] = field(default_factory=list)
    timeline: dict[str, float] = field(default_factory=dict)
    master: dict[str, Any] = field(default_factory=dict)
    contact_sheet: Path | None = None
    work_dir: Path | None = None
    seconds: float = 0.0

    @property
    def status(self) -> Status:
        if self.error:
            return Status.FAIL
        return worst(self.pre_checks + [c for e in self.exports for c in e.checks])


@dataclass
class RunResult:
    name: str
    out_dir: Path
    concepts: list[ConceptResult]
    ledger: CostLedger
    plan: Plan
    lint: list[LintIssue]
    judge_key: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0

    @property
    def status(self) -> Status:
        return worst([CheckResult("concept", c.status, None, "") for c in self.concepts])


def contact_sheet(video: Path, times: list[tuple[str, float]], out: Path, width: int = 216) -> Path:
    frames = []
    for _label, t in times:
        im = extract_frame(video, t)
        frames.append(im.resize((width, int(im.height * width / im.width))))
    sheet = Image.new("RGB", (width * len(frames) + 4 * (len(frames) - 1), frames[0].height), (16, 16, 16))
    for i, f in enumerate(frames):
        sheet.paste(f, (i * (width + 4), 0))
    sheet.save(out, quality=82)
    return out


class Pipeline:
    def __init__(
        self,
        spec: AdSpec,
        providers: ProviderSet,
        settings: Settings | None = None,
        *,
        budget_usd: float | None = None,
        jobs: int = 2,
        pricing: Pricing | None = None,
        keep_work: bool = False,
        ocr: str = "auto",
        judge_runs: int | None = None,
        judge_seed: int = 20261018,
        log: Callable[[str], None] = print,
    ) -> None:
        require_tools()
        self.spec = spec
        self.settings = settings or Settings.from_env()
        self.pricing = pricing or Pricing.load()
        self.ledger = CostLedger()
        self.guard = BudgetGuard(self.ledger, budget_usd)
        self.jobs = max(1, jobs)
        self.keep_work = keep_work
        self.judge_runs = spec.qa.judge_runs if judge_runs is None else judge_runs
        self.judge_seed = judge_seed
        self.log = log
        self.out_dir = spec.output_path
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.work_root = self.out_dir / ".work"
        self.work_root.mkdir(exist_ok=True)
        cache_dir = self.settings.cache_dir
        cache_dir = cache_dir if cache_dir.is_absolute() else spec.base_dir / cache_dir
        self.gen = Generator(
            providers, GenerationCache(cache_dir), self.ledger, self.guard, self.pricing, self.work_root / "scratch"
        )
        self._ocr_pref = ocr
        self._ocr: OcrBackend | None = None
        self._ocr_ready = False

    @property
    def ocr(self) -> OcrBackend | None:
        if not self._ocr_ready:
            self._ocr = None if self._ocr_pref == "off" else detect_backend(self._ocr_pref)
            self._ocr_ready = True
        return self._ocr

    # ---------------------------------------------------------------------------------------------------- one concept
    def run_concept(self, c: Concept) -> ConceptResult:
        spec, r, a, e, h = self.spec, self.spec.render, self.spec.audio, self.spec.export, c.hook
        res = ConceptResult(c.id)
        t0 = time.time()
        work = Path(tempfile.mkdtemp(prefix=f"{c.id}-", dir=self.work_root))  # private per job: no shared temp files
        res.work_dir = work
        try:
            src = spec.resolve(c.gameplay.path)
            if src is None or not src.exists():
                raise FileNotFoundError(f"gameplay clip not found: {src}")
            self.log(f"[{c.id}] gameplay segment")
            gp = prepare_gameplay(src, c.gameplay, r, work)

            self.log(f"[{c.id}] hook first frame")
            first = prepare_first_frame(gp.real_frame, h.text_regions, h.effects, work / "first_frame_base.png")
            if h.image_prompt:
                refs = tuple(spec.resolve(p) for p in h.reference_images)  # type: ignore[misc]
                first = self.gen.image_edit(
                    ImageEditRequest(h.image_prompt, first, spec.models.image_edit, refs, "9:16"), c.id
                )
            res.pre_checks.append(check_image_text(first, spec.qa.known_text, self.ocr, "first_frame_text"))

            self.log(f"[{c.id}] hook video ({h.samples} x {h.duration_s}s)")
            clips = self.gen.video(
                VideoRequest(
                    prompt=h.video_prompt,
                    first_frame=first,
                    model=spec.models.video,
                    last_frame=gp.real_frame,
                    duration_s=h.duration_s,
                    generate_audio=h.audio,
                    sample_count=h.samples,
                    negative_prompt=h.negative_prompt,
                ),
                c.id,
            )
            veo = clips[h.pick]
            landing = extract_frame(veo, max(0.0, h.effective_cut - 1 / 24))
            q = spec.qa
            res.pre_checks.append(
                check_seam_frames(
                    "hook_landing",
                    landing,
                    Image.open(gp.real_frame),
                    q.landing_mean_warn,
                    q.landing_mean_fail,
                    q.landing_block_warn,
                    q.landing_block_fail,
                )
            )

            self.log(f"[{c.id}] hook trim + interpolation")
            hook = prepare_hook(veo, gp.real_frame, h, r, work)
            end = render_end_card(gp.video, c.end_card, r, work, spec.resolve)
            self.log(f"[{c.id}] assemble")
            assembled, tl = assemble(hook, gp, end, r, h.disclaimer, work)
            res.timeline = {k: round(v, 3) for k, v in tl.items()}
            overlay_box = None
            if h.disclaimer:
                bb = disclaimer_overlay(h.disclaimer, r.width, r.height).getbbox()
                overlay_box = [bb[0] / r.width, bb[1] / r.height, bb[2] / r.width, bb[3] / r.height] if bb else None

            music = None
            if c.music is not None:
                if c.music.path is not None:
                    music = spec.resolve(c.music.path)
                else:
                    music = self.gen.music(
                        MusicRequest(
                            c.music.prompt or "",
                            spec.models.music,
                            c.music.negative_prompt,
                            c.music.seed,
                            tl["total"] + c.music.start_offset_s,
                        ),
                        c.id,
                    )
            fx: list[Placed] = []
            if hook.audio:
                fx.append(Placed(hook.audio, 0.0))
            if gp.audio:
                fx.append(Placed(gp.audio, tl["gameplay_start"]))
            fx += sfx_placements(c.sfx, spec.resolve)
            self.log(f"[{c.id}] mix + master")
            mixed = mix(
                fx,
                music,
                c.music.volume_db if c.music else 0.0,
                c.music.start_offset_s if c.music else 0.0,
                tl["total"],
                a,
                work / "mix.wav",
            )
            m = master(
                mixed,
                work / "master.m4a",
                tl["hook_end"],
                (tl["gameplay_start"] + a.gameplay_skip_s, tl["gameplay_end"]),
                a,
                e.audio_bitrate_kbps,
            )
            res.master = m.to_dict()
            muxed = mux(assembled, m.path, work / "master.mp4", tl["total"])

            for fmt in c.formats:
                out = self.out_dir / f"{spec.name}_{c.id}_{fmt.replace(':', 'x')}.mp4"
                self.log(f"[{c.id}] export {fmt}")
                if fmt == "9:16":
                    export_9x16(muxed, out, tl["total"], e)
                    size = (r.width, r.height)
                else:
                    export_4x5(muxed, out, tl["total"], r.width, e)
                    size = size_4x5(r.width)
                write_sidecar(
                    out,
                    {
                        "concept": c.id,
                        "format": fmt,
                        "size": list(size),
                        "timeline": res.timeline,
                        "overlay_text": [h.disclaimer] if h.disclaimer else [],
                        "ocr_ignore": [overlay_box] if overlay_box else [],
                    },
                )
                derived = fmt != "9:16" and "9:16" in c.formats
                res.exports.append(
                    ExportResult(
                        fmt,
                        out,
                        run_video_checks(
                            out, spec.qa, a, e, expect_format=fmt, expect_size=size, ocr=self.ocr, derived=derived
                        ),
                    )
                )

            sheet_src = next((x.path for x in res.exports if x.format == "9:16"), res.exports[0].path)
            x = tl["crossfade"]
            res.contact_sheet = contact_sheet(
                sheet_src,
                [
                    ("hook start", 0.1),
                    ("before cut", max(0.0, tl["hook_end"] - 1 / r.fps)),
                    ("after cut", tl["gameplay_start"] + x + 1 / r.fps),
                    ("gameplay", (tl["gameplay_start"] + tl["gameplay_end"]) / 2),
                    ("end card", tl["end_card_start"] + (tl["total"] - tl["end_card_start"]) * 0.7),
                ],
                self.out_dir / f"{spec.name}_{c.id}_sheet.jpg",
            )
            if not self.keep_work:
                shutil.rmtree(work, ignore_errors=True)
                res.work_dir = None
        except Exception as ex:  # one failed concept must not take the batch down; the report shows it
            res.error = f"{type(ex).__name__}: {ex}"
            self.log(f"[{c.id}] FAILED: {res.error}\n{traceback.format_exc(limit=3)}")
        res.seconds = round(time.time() - t0, 1)
        return res

    # ---------------------------------------------------------------------------------------------------- judge
    def judge(self, results: list[ConceptResult]) -> dict[str, str]:
        targets = {}
        for cr in results:
            ex = next((x for x in cr.exports if x.format == "9:16"), cr.exports[0] if cr.exports else None)
            if ex is not None and not cr.error:
                targets[ex.path] = (cr, ex)
        if not targets or self.judge_runs <= 0:
            return {}
        codes = blind_codes(list(targets), self.judge_seed)
        raw: dict[Path, list[dict[str, Any]]] = {p: [] for p in targets}
        prompt = judge_prompt()
        with tempfile.TemporaryDirectory(prefix="judge-", dir=self.work_root) as td:
            for run_index in range(self.judge_runs):
                for code, path in codes.items():
                    blind = Path(td) / f"{code}.mp4"
                    if not blind.exists():
                        shutil.copy2(path, blind)
                    try:
                        raw[path].append(
                            self.gen.judge(
                                JudgeRequest(blind, prompt, self.spec.models.judge, 5, run_index),
                                targets[path][0].concept,
                            )
                        )
                    except Exception as ex:
                        raw[path].append({"error": f"{type(ex).__name__}: {ex}"})
        q = self.spec.qa
        for path, (_cr, ex) in targets.items():
            agg = aggregate(raw[path], q.judge_unstable_stdev)
            ex.checks.append(check_judge(agg, 2, q.judge_unstable_stdev, q.judge_min_overall))
        return {code: targets[p][0].concept for code, p in codes.items()}

    # ---------------------------------------------------------------------------------------------------- batch
    def run(self, only: list[str] | None = None) -> RunResult:
        t0 = time.time()
        concepts = [c for c in self.spec.concepts if not only or c.id in only]
        plan = estimate(self.spec, self.pricing, self.judge_runs)
        issues = lint(self.spec)
        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            results = list(pool.map(self.run_concept, concepts))
        key = self.judge(results)
        self.ledger.write(self.out_dir / "cost_ledger.json")
        return RunResult(
            self.spec.name, self.out_dir, results, self.ledger, plan, issues, key, round(time.time() - t0, 1)
        )
