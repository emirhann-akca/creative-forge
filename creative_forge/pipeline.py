"""Orchestration: one job per concept, each in its own temp directory, run concurrently.

hook image -> hook video (lastFrame = real frame, tail trim, patches, 24->60 fps) -> assemble (crossfades, gameplay,
end card) -> mix (padded FX bus, ducked music) -> master (loudness/true-peak loop on encoded AAC) -> export (9:16,
4:5) -> QA -> blind judge -> report.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
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
from creative_forge.imaging import disclaimer_overlay, prepare_first_frame, restore_outside
from creative_forge.ledger import BudgetGuard, CostLedger
from creative_forge.licenses import check as check_licenses
from creative_forge.licenses import describe as describe_license
from creative_forge.licenses import override_records
from creative_forge.lint import LintIssue, lint
from creative_forge.lint import errors as lint_errors
from creative_forge.naming import CreativeIds, format_token
from creative_forge.pricing import Plan, Pricing, estimate
from creative_forge.providers.base import ImageEditRequest, JudgeRequest, MusicRequest, ProviderSet, VideoRequest
from creative_forge.qa.judge import aggregate, blind_codes, check_judge, judge_prompt
from creative_forge.qa.ocr import OcrBackend, check_image_text, detect_backend
from creative_forge.qa.result import CheckResult, Status, worst
from creative_forge.qa.runner import run_video_checks, write_sidecar
from creative_forge.qa.seam import check_seam_frames
from creative_forge.spec import AdSpec, Concept, GameplaySpec, HookSpec, localized, timeline_for
from creative_forge.stages.audio import Placed, master, mix, mux, sfx_placements
from creative_forge.stages.export import export_4x5, export_9x16, size_4x5
from creative_forge.stages.video import (
    GameplayClip,
    HookClip,
    assemble,
    join_gameplay,
    prepare_gameplay,
    prepare_hook,
    render_end_card,
)
from creative_forge.tags import export_tags


@dataclass
class ExportResult:
    format: str
    path: Path
    checks: list[CheckResult]
    creative: CreativeIds | None = None
    tags: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> Status:
        return worst(self.checks)


@dataclass
class HookAssets:
    clip: HookClip
    pre_checks: list[CheckResult]


class Memo:
    """Compute each key once across threads; a failure is remembered and re-raised for every user of the key."""

    def __init__(self) -> None:
        self._values: dict[tuple, Any] = {}
        self._locks: dict[tuple, threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: tuple, fn: Callable[[], Any]) -> Any:
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if key not in self._values:
                try:
                    self._values[key] = (True, fn())
                except Exception as e:
                    self._values[key] = (False, e)
            ok, value = self._values[key]
        if not ok:
            raise value
        return value


@dataclass
class ConceptResult:
    concept: str
    error: str | None = None
    variant: dict[str, str] = field(default_factory=dict)  # hook, segment, end_card, language
    exports: list[ExportResult] = field(default_factory=list)
    pre_checks: list[CheckResult] = field(default_factory=list)
    timeline: dict[str, float] = field(default_factory=dict)
    master: dict[str, Any] = field(default_factory=dict)
    contact_sheet: Path | None = None
    work_dir: Path | None = None
    seconds: float = 0.0

    @property
    def label(self) -> str:
        """``concept`` for a concept without variants, else ``concept hook/segment/end_card/language``."""
        v = self.variant
        if not v or all(v.get(k) == "main" for k in ("hook", "segment", "end_card")):
            return self.concept if not v or not v.get("language") else f"{self.concept} {v['language']}"
        return f"{self.concept} {v['hook']}/{v['segment']}/{v['end_card']}/{v['language']}"

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
    licenses: list[dict[str, Any]] = field(default_factory=list)

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
        license_overrides: dict[str, str] | None = None,
    ) -> None:
        require_tools()
        self.spec = spec
        self.license_overrides = license_overrides or {}
        self.licenses = None
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
        self._ocr_lock = threading.Lock()

    @property
    def ocr(self) -> OcrBackend | None:
        """Detected on first use; concept threads share one backend (the Vision helper is compiled once)."""
        with self._ocr_lock:
            if not self._ocr_ready:
                self._ocr = None if self._ocr_pref == "off" else detect_backend(self._ocr_pref)
                self._ocr_ready = True
            return self._ocr

    # ---------------------------------------------------------------------------------------------------- shared assets
    def _landing(self, c: Concept, shared: Path) -> GameplayClip:
        src = self.spec.resolve(c.gameplay.path)
        if src is None or not src.exists():
            raise FileNotFoundError(f"gameplay clip not found: {src}")
        self.log(f"[{c.id}] gameplay segment")
        d = shared / "landing"
        d.mkdir(parents=True, exist_ok=True)
        return prepare_gameplay(src, c.gameplay, self.spec.render, d)

    def _segment(self, c: Concept, sid: str, seg: GameplaySpec | None, shared: Path, memo: Memo) -> GameplayClip:
        gp = memo.get(("landing", c.id), lambda: self._landing(c, shared))
        if seg is None:
            return gp
        src = self.spec.resolve(seg.path)
        if src is None or not src.exists():
            raise FileNotFoundError(f"segment {sid}: gameplay clip not found: {src}")
        self.log(f"[{c.id}] segment {sid}")
        d = shared / f"segment-{sid}"
        d.mkdir(parents=True, exist_ok=True)
        extra = prepare_gameplay(src, seg, self.spec.render, d)
        return join_gameplay(gp, extra, self.spec.render, d / "joined")

    def _hook(self, c: Concept, hid: str, h: HookSpec, shared: Path, memo: Memo) -> HookAssets:
        """First frame, image edit, video generation, landing check, trim and interpolation: once per hook, however
        many segments, end cards, languages and formats use it (and the generations are cached across runs)."""
        spec, r = self.spec, self.spec.render
        who = c.id if hid == "main" else f"{c.id}/{hid}"
        gp = memo.get(("landing", c.id), lambda: self._landing(c, shared))
        work = shared / f"hook-{hid}"
        work.mkdir(parents=True, exist_ok=True)
        self.log(f"[{who}] hook first frame")
        first = prepare_first_frame(gp.real_frame, h.text_regions, h.effects, work / "first_frame_base.png")
        if h.image_prompt:
            refs = tuple(spec.resolve(x) for x in h.reference_images)  # type: ignore[misc]
            edited = self.gen.image_edit(
                ImageEditRequest(h.image_prompt, first, spec.image_edit_model, refs, "9:16", h.edit_region), who
            )
            if h.edit_region:  # the model's pixels survive only inside the region it was allowed to change
                edited = restore_outside(first, edited, h.edit_region, work / "first_frame.png")
            first = edited
        pre = [check_image_text(first, spec.qa.known_text, self.ocr, "first_frame_text", "the first frame")]
        self.log(f"[{who}] hook video ({h.samples} x {h.duration_s}s)")
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
            who,
        )
        veo = clips[h.pick]
        landing = extract_frame(veo, max(0.0, h.effective_cut - 1 / 24))
        q = spec.qa
        pre.append(
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
        self.log(f"[{who}] hook trim + interpolation")
        return HookAssets(prepare_hook(veo, gp.real_frame, h, r, work), pre)

    def _music(self, c: Concept) -> Path | None:
        if c.music is None:
            return None
        if c.music.path is not None:
            return self.spec.resolve(c.music.path)
        # one track per concept, long enough for its longest render (the cache key does not include the length)
        longest = max(timeline_for(self.spec, c, h, seg, e)["total"] for _, h, _, seg, _, e, _ in self.spec.matrix(c))
        return self.gen.music(
            MusicRequest(
                c.music.prompt or "",
                self.spec.models.music,
                c.music.negative_prompt,
                c.music.seed,
                longest + 0.5 + c.music.start_offset_s,
            ),
            c.id,
        )

    # ---------------------------------------------------------------------------------------------------- one render
    def run_variant(self, c: Concept, variant: tuple, shared: Path, memo: Memo) -> ConceptResult:
        """One (hook, segment, end card, language) of a concept: assemble, mix, master, export every format, QA."""
        spec, r, a, e = self.spec, self.spec.render, self.spec.audio, self.spec.export
        hid, h, sid, seg, eid, end_spec, lang = variant
        res = ConceptResult(c.id, variant={"hook": hid, "segment": sid, "end_card": eid, "language": lang})
        t0 = time.time()
        work = Path(tempfile.mkdtemp(prefix=f"{c.id}-", dir=self.work_root))  # private per job: no shared temp files
        res.work_dir = work
        tag = f"[{res.label}]"
        try:
            hook = memo.get(("hook", c.id, hid), lambda: self._hook(c, hid, h, shared, memo))
            res.pre_checks = [CheckResult(**{**x.__dict__}) for x in hook.pre_checks]  # a copy per render
            gp = memo.get(("segment", c.id, sid), lambda: self._segment(c, sid, seg, shared, memo))
            end = render_end_card(gp.video, end_spec, r, work, spec.resolve, lang)
            self.log(f"{tag} assemble")
            disclaimer = localized(h.disclaimer, lang)
            assembled, tl = assemble(hook.clip, gp, end, r, disclaimer, work)
            res.timeline = {k: round(v, 3) for k, v in tl.items()}
            overlay_box = None
            if disclaimer:
                bb = disclaimer_overlay(disclaimer, r.width, r.height).getbbox()
                overlay_box = [bb[0] / r.width, bb[1] / r.height, bb[2] / r.width, bb[3] / r.height] if bb else None

            music = memo.get(("music", c.id), lambda: self._music(c))
            fx: list[Placed] = []
            if hook.clip.audio:
                fx.append(Placed(hook.clip.audio, 0.0))
            if gp.audio:
                fx.append(Placed(gp.audio, tl["gameplay_start"]))
            fx += sfx_placements(c.sfx, spec.resolve)
            self.log(f"{tag} mix + master")
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

            models = self.models_for(h.image_prompt is not None, c.music is not None and c.music.prompt is not None)
            for fmt in c.formats:
                ids = CreativeIds(spec.name, c.id, hid, sid, eid, format_token(fmt), lang)
                out = self.out_dir / f"{ids.ad_name}.mp4"
                tags = export_tags(c.hook_tags(hid), ids.format, tl["total"])
                self.log(f"{tag} export {fmt}")
                if fmt == "9:16":
                    export_9x16(muxed, out, tl["total"], e)
                    size = (r.width, r.height)
                else:
                    export_4x5(muxed, out, tl["total"], r.width, e)
                    size = size_4x5(r.width)
                write_sidecar(
                    out,
                    {
                        "creative_id": ids.creative_id,
                        "ad_name": ids.ad_name,
                        "ids": ids.to_dict(),
                        "tags": tags,
                        "models": models,
                        "concept": c.id,
                        "format": fmt,
                        "size": list(size),
                        "timeline": res.timeline,
                        "overlay_text": [disclaimer] if disclaimer else [],
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
                        ids,
                        tags,
                    )
                )

            sheet_src = next((x.path for x in res.exports if x.format == "9:16"), res.exports[0].path)
            x = tl["crossfade"]
            first_ids = res.exports[0].creative
            res.contact_sheet = contact_sheet(
                sheet_src,
                [
                    ("hook start", 0.1),
                    ("before cut", max(0.0, tl["hook_end"] - 1 / r.fps)),
                    ("after cut", tl["gameplay_start"] + x + 1 / r.fps),
                    ("gameplay", (tl["gameplay_start"] + tl["gameplay_end"]) / 2),
                    ("end card", tl["end_card_start"] + (tl["total"] - tl["end_card_start"]) * 0.7),
                ],
                self.out_dir / f"{first_ids.variant if first_ids else c.id}.sheet.jpg",
            )
            if not self.keep_work:
                shutil.rmtree(work, ignore_errors=True)
                res.work_dir = None
        except Exception as ex:  # one failed render must not take the batch down; the report shows it
            res.error = f"{type(ex).__name__}: {ex}"
            self.log(f"{tag} FAILED: {res.error}\n{traceback.format_exc(limit=3)}")
        res.seconds = round(time.time() - t0, 1)
        return res

    def run_concept(self, c: Concept) -> list[ConceptResult]:
        """Every render of one concept, sequentially (``run`` spreads renders of all concepts over the pool)."""
        memo = Memo()
        shared = Path(tempfile.mkdtemp(prefix=f"{c.id}-shared-", dir=self.work_root))
        try:
            return [self.run_variant(c, v, shared, memo) for v in self.spec.matrix(c)]
        finally:
            if not self.keep_work:
                shutil.rmtree(shared, ignore_errors=True)

    def models_for(self, image_edit: bool, music: bool) -> list[dict[str, Any]]:
        """The models behind one export and their licences (sidecar and publish manifest)."""
        spec, p = self.spec, self.gen.providers
        used: list[tuple[str, str, str]] = []
        if image_edit:
            used.append(("image_edit", spec.image_edit_model, p.image_edit.name))
        used.append(("video", spec.models.video, p.video.name))
        if music:
            used.append(("music", spec.models.music, p.music.name))
        return [{"role": r, "model": m, "provider": prov, "license": describe_license(m)} for r, m, prov in used]

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
                                targets[path][0].label,
                            )
                        )
                    except Exception as ex:
                        raw[path].append({"error": f"{type(ex).__name__}: {ex}"})
        q = self.spec.qa
        for path, (_cr, ex) in targets.items():
            agg = aggregate(raw[path], q.judge_unstable_stdev)
            ex.checks.append(check_judge(agg, 2, q.judge_unstable_stdev, q.judge_min_overall))
        return {code: targets[p][0].label for code, p in codes.items()}

    # ---------------------------------------------------------------------------------------------------- batch
    def run(self, only: list[str] | None = None) -> RunResult:
        t0 = time.time()
        unknown = sorted(set(only or []) - {c.id for c in self.spec.concepts})
        if unknown:  # a typo here used to produce an empty, "successful" run
            raise ValueError(f"--only: no concept with id {', '.join(unknown)} in {self.spec.name}")
        concepts = [c for c in self.spec.concepts if not only or c.id in only]
        blocking = lint_errors(lint(self.spec))
        if blocking:
            raise ValueError("; ".join(f"{i.concept}: [{i.rule}] {i.message}" for i in blocking))
        licences = check_licenses(self.spec, self.license_overrides)
        licences.raise_if_refused()  # before any generation: a refused model is never called
        self.ledger.license_overrides = override_records(licences)
        self.licenses = licences
        plan = estimate(self.spec, self.pricing, self.judge_runs)
        issues = lint(self.spec)
        memos = {c.id: Memo() for c in concepts}
        shared = {c.id: Path(tempfile.mkdtemp(prefix=f"{c.id}-shared-", dir=self.work_root)) for c in concepts}
        jobs = [(c, v) for c in concepts for v in self.spec.matrix(c)]
        try:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                results = list(pool.map(lambda j: self.run_variant(j[0], j[1], shared[j[0].id], memos[j[0].id]), jobs))
        finally:
            if not self.keep_work:
                for d in shared.values():
                    shutil.rmtree(d, ignore_errors=True)
        key = self.judge(results)
        self.ledger.write(self.out_dir / "cost_ledger.json")
        return RunResult(
            self.spec.name,
            self.out_dir,
            results,
            self.ledger,
            plan,
            issues,
            key,
            round(time.time() - t0, 1),
            [f.to_dict() for f in licences.findings],
        )
