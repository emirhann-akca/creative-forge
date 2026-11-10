"""``forge`` command line: plan, run, qa, demo."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from creative_forge import __version__
from creative_forge.config import ENV_PROJECT, ConfigError, Settings
from creative_forge.ledger import BudgetExceeded
from creative_forge.lint import lint
from creative_forge.pricing import Pricing, estimate
from creative_forge.qa.result import CheckResult, Status, worst
from creative_forge.spec import AdSpec, load_spec


def _print_checks(title: str, checks: list[CheckResult]) -> None:
    print(f"\n{title}  [{worst(checks).value.upper()}]")
    w = max((len(c.check) for c in checks), default=5)
    for c in checks:
        note = f"  {c.message}" if c.message else ""
        print(f"  {c.check.ljust(w)}  {c.status.value:4}  {c.display_value:>16}  ({c.threshold}){note}")


def _load(path: str) -> AdSpec:
    try:
        return load_spec(path)
    except FileNotFoundError:
        raise SystemExit(f"spec not found: {path}") from None


def cmd_plan(a: argparse.Namespace) -> int:
    spec = _load(a.spec)
    pricing = Pricing.load(a.pricing)
    print(estimate(spec, pricing, a.judge_runs).table())
    issues = lint(spec)
    if issues:
        print("\nspec lint:")
        for i in issues:
            print(f"  {i.concept}: [{i.rule}] {i.message}")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    spec = _load(a.spec)
    kind = a.providers or spec.providers
    settings = Settings.from_env()
    pricing = Pricing.load(a.pricing)
    plan = estimate(spec, pricing, a.judge_runs)
    if a.dry_run:
        print(plan.table())
        problems = []
        for c in spec.concepts:
            for label, p in (
                [("gameplay", spec.resolve(c.gameplay.path))]
                + [("reference", spec.resolve(r)) for r in c.hook.reference_images]
                + ([("music", spec.resolve(c.music.path))] if c.music and c.music.path else [])
            ):
                if p is not None and not p.exists():
                    problems.append(f"{c.id}: {label} file missing: {p}")
        for tool in ("ffmpeg", "ffprobe"):
            if shutil.which(tool) is None:
                problems.append(f"{tool} not on PATH")
        if kind == "vertex" and not settings.project:
            problems.append(f"providers=vertex but {ENV_PROJECT} is not set")
        if spec.image_provider == "comfyui":
            from creative_forge.providers.comfyui import preflight

            problem = preflight(settings.comfyui_url, spec.comfyui.checkpoint)
            if problem:
                problems.append(problem)
        for i in lint(spec):
            print(f"lint {i.concept}: [{i.rule}] {i.message}")
        for p in problems:
            print(f"problem: {p}")
        if a.budget_usd is not None and plan.total_priced > a.budget_usd:
            print(
                f"note: upper-bound plan ${plan.total_priced:.2f} exceeds --budget-usd {a.budget_usd:.2f}; "
                "cache hits may cover the difference, otherwise the guard stops the run before overspending"
            )
        print("dry run: no generation calls were made")
        return 1 if problems else 0

    from creative_forge.pipeline import Pipeline
    from creative_forge.providers import build_providers
    from creative_forge.report import write_report

    try:
        providers = build_providers(kind, settings, image=spec.image_provider, comfyui=spec.comfyui)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    pipe = Pipeline(
        spec,
        providers,
        settings,
        budget_usd=a.budget_usd,
        jobs=a.jobs,
        pricing=pricing,
        keep_work=a.keep_work,
        ocr=a.ocr,
        judge_runs=a.judge_runs,
    )
    try:
        rr = pipe.run(only=a.only)
    except BudgetExceeded as e:
        print(f"budget guard: {e}", file=sys.stderr)
        return 3
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    html_path, _ = write_report(rr)
    for c in rr.concepts:
        if c.error:
            print(f"\n{c.concept}: FAILED {c.error}")
        for e in c.exports:
            _print_checks(f"{c.concept} {e.format}  {e.path}", e.checks)
    print(f"\nspent ${rr.ledger.spent:.2f} (cache saved ${rr.ledger.saved_by_cache:.2f})")
    print(f"report: {html_path}")
    return 1 if rr.status is Status.FAIL else 0


def cmd_qa(a: argparse.Namespace) -> int:
    from creative_forge.qa.ocr import detect_backend
    from creative_forge.qa.runner import run_video_checks

    spec = _load(a.spec) if a.spec else None
    base = spec or AdSpec.model_validate(
        {
            "name": "qa",
            "concepts": [
                {"id": "x", "gameplay": {"path": "x.mp4", "in_s": 0, "out_s": 10}, "hook": {"video_prompt": "-"}}
            ],
        }
    )
    timeline = None
    if a.hook_end is not None:
        timeline = {"hook_end": a.hook_end, "gameplay_start": a.hook_end, "crossfade": a.crossfade}
        if a.gameplay_end is not None:
            timeline["gameplay_end"] = a.gameplay_end
    ocr = None if a.ocr == "off" else detect_backend(a.ocr)
    all_checks: dict[str, list[dict]] = {}
    failed = False
    for v in a.videos:
        vp = Path(v)
        t = dict(timeline) if timeline else None
        if t is not None and "gameplay_end" not in t:
            from creative_forge.ffmpeg import probe

            t["gameplay_end"] = probe(vp).duration - 3.5  # end card excluded, as in the production loudness script
        checks = run_video_checks(vp, base.qa, base.audio, base.export, timeline=t, expect_format=a.format, ocr=ocr)
        if a.judge_runs:
            checks.append(_judge_one(vp, base, a))
        _print_checks(str(vp), checks)
        all_checks[str(vp)] = [c.to_dict() for c in checks]
        failed = failed or worst(checks) is Status.FAIL
    if a.json:
        Path(a.json).write_text(json.dumps(all_checks, indent=2), encoding="utf-8")
    return 1 if failed else 0


def _judge_one(video: Path, spec: AdSpec, a: argparse.Namespace) -> CheckResult:
    from creative_forge.providers import build_providers
    from creative_forge.providers.base import JudgeRequest
    from creative_forge.qa.judge import aggregate, check_judge, judge_prompt

    if not a.providers:
        raise SystemExit("--judge-runs needs --providers (fake or vertex); vertex calls are billed")
    judge = build_providers(a.providers, Settings.from_env()).judge
    results = [judge.score(JudgeRequest(video, judge_prompt(), spec.models.judge, 5, i)) for i in range(a.judge_runs)]
    return check_judge(
        aggregate(results, spec.qa.judge_unstable_stdev), 2, spec.qa.judge_unstable_stdev, spec.qa.judge_min_overall
    )


def cmd_demo(a: argparse.Namespace) -> int:
    from creative_forge.demo import run_demo
    from creative_forge.report import write_report

    rr = run_demo(Path(a.out), jobs=a.jobs, keep_work=a.keep_work, ocr=a.ocr)
    html_path, json_path = write_report(rr)
    for c in rr.concepts:
        if c.error:
            print(f"\n{c.concept}: FAILED {c.error}")
        for e in c.exports:
            _print_checks(f"{c.concept} {e.format}  {e.path.name}", e.checks)
    print(
        f"\nsimulated list price of this run on Vertex AI: "
        f"${sum((e.list_usd or 0) for e in rr.ledger.entries if not e.cached):.2f} (charged $0, fake providers)"
    )
    print(f"report: {html_path}\njson:   {json_path}")
    return 1 if rr.status is Status.FAIL else 0


def cmd_review(a: argparse.Namespace) -> int:
    from creative_forge.review import ReviewError, decide, load_exports, load_review

    run = Path(a.run_dir)
    try:
        if a.approve or a.reject:
            for f in a.approve or []:
                decide(run, f, "approve", a.note or "", a.reviewer)
            for f in a.reject or []:
                decide(run, f, "reject", a.note or "", a.reviewer)
        else:
            for r in load_exports(run):
                current = load_review(run)["decisions"].get(r.file, {}).get("decision", "undecided")
                flags = f"  failed: {', '.join(r.failed)}" if r.failed else ""
                flags += f"  warn: {', '.join(r.warned)}" if r.warned else ""
                print(f"\n{r.file}  QA {r.status.upper()}  ({current}){flags}")
                choice = input("  [a]pprove / [r]eject / [s]kip: ").strip().lower()[:1]
                if choice in ("a", "r"):
                    note = input("  note: ")
                    decide(run, r.file, "approve" if choice == "a" else "reject", note, a.reviewer)
    except ReviewError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    decisions = load_review(run)["decisions"]
    for r in load_exports(run):
        d = decisions.get(r.file, {})
        print(f"{r.file}  QA {r.status}  {d.get('decision', 'undecided')}  {d.get('note', '')}")
    return 0


def cmd_publish(a: argparse.Namespace) -> int:
    from creative_forge.review import ReviewError, publish

    try:
        manifest = publish(Path(a.run_dir), Path(a.to), a.override)
    except ReviewError as e:
        print(f"publish refused: {e}", file=sys.stderr)
        return 1
    n = len(json.loads(manifest.read_text(encoding="utf-8"))["exports"])
    print(f"published {n} export(s) to {a.to} (manifest: {manifest.name})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge", description="Spec-driven AI-hook video ad pipeline with QA.")
    p.add_argument("--version", action="version", version=f"creative-forge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="cost estimate and spec lint; makes no calls")
    sp.add_argument("spec")
    sp.add_argument("--pricing", help="pricing YAML overriding the bundled October 2026 table")
    sp.add_argument("--judge-runs", type=int, default=None)
    sp.set_defaults(fn=cmd_plan)

    sr = sub.add_parser("run", help="render every concept of a spec")
    sr.add_argument("spec")
    sr.add_argument("--dry-run", action="store_true", help="validate, lint and estimate; no generation calls")
    sr.add_argument("--budget-usd", type=float, default=None, help="refuse any paid call that would exceed this")
    sr.add_argument("--providers", choices=["vertex", "fake"], default=None, help="override the spec")
    sr.add_argument("--jobs", type=int, default=2)
    sr.add_argument("--only", nargs="*", default=None, help="concept ids to run")
    sr.add_argument("--keep-work", action="store_true", help="keep per-job temp directories")
    sr.add_argument("--ocr", choices=["auto", "vision", "tesseract", "off"], default="auto")
    sr.add_argument("--judge-runs", type=int, default=None)
    sr.add_argument("--pricing")
    sr.set_defaults(fn=cmd_run)

    sq = sub.add_parser("qa", help="run the QA checks on finished videos")
    sq.add_argument("videos", nargs="+")
    sq.add_argument("--spec", help="take thresholds from this spec")
    sq.add_argument("--format", choices=["9:16", "4:5"], default=None)
    sq.add_argument("--hook-end", type=float, default=None, help="seconds where the hook crossfade starts")
    sq.add_argument("--gameplay-end", type=float, default=None)
    sq.add_argument("--crossfade", type=float, default=0.3)
    sq.add_argument("--ocr", choices=["auto", "vision", "tesseract", "off"], default="auto")
    sq.add_argument("--judge-runs", type=int, default=0)
    sq.add_argument("--providers", choices=["vertex", "fake"], default=None)
    sq.add_argument("--json", help="write results as JSON")
    sq.set_defaults(fn=cmd_qa)

    sd = sub.add_parser("demo", help="end-to-end run on synthetic gameplay with fake providers (no keys)")
    sd.add_argument("--out", default="out/demo")
    sd.add_argument("--jobs", type=int, default=2)
    sd.add_argument("--keep-work", action="store_true")
    sd.add_argument("--ocr", choices=["auto", "vision", "tesseract", "off"], default="auto")
    sd.set_defaults(fn=cmd_demo)

    sv = sub.add_parser("review", help="approve or reject each export of a run (interactive without flags)")
    sv.add_argument("run_dir", help="output directory of `forge run` (holds report.json)")
    sv.add_argument("--approve", nargs="+", metavar="FILE")
    sv.add_argument("--reject", nargs="+", metavar="FILE")
    sv.add_argument("--note", help="note stored with the decision (required to reject)")
    sv.add_argument("--reviewer", help="defaults to the OS user name")
    sv.set_defaults(fn=cmd_review)

    su = sub.add_parser("publish", help="copy approved exports and a manifest; refuses on failed QA")
    su.add_argument("run_dir")
    su.add_argument("--to", required=True, help="delivery directory")
    su.add_argument("--override", metavar="REASON", help="publish approved exports despite failed checks")
    su.set_defaults(fn=cmd_publish)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
