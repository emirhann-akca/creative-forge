"""``forge`` command line: plan, run, qa, demo, ideate, review, publish, setup, doctor, ui, licenses, perf,
segments, playable, comfy."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from creative_forge import __version__
from creative_forge.config import ENV_PROJECT, ConfigError, Settings
from creative_forge.ledger import BudgetExceeded
from creative_forge.lint import errors as lint_errors
from creative_forge.lint import lint
from creative_forge.pricing import Pricing, estimate
from creative_forge.qa.result import CheckResult, Status, worst
from creative_forge.spec import AdSpec, load_spec

OCR_CHOICES = ["auto", "vision", "rapidocr", "tesseract", "off"]
LICENSE_OVERRIDE_HELP = (
    "run a model whose licence the registry refuses, with the reason written into the cost ledger (repeatable)"
)


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


def _licence_report(spec: AdSpec, a: argparse.Namespace):
    from creative_forge.licenses import check, parse_overrides

    try:
        overrides = parse_overrides(getattr(a, "license_override", None))
    except ValueError as e:
        raise SystemExit(f"error: {e}") from None
    report = check(spec, overrides)
    print("\nmodel licences:")
    print("  " + report.table().replace("\n", "\n  "))
    for f in report.refused:
        print(
            f"refused: {f.role} model {f.model}: {f.reason} ({f.source}, checked {f.checked}). Nothing is generated; "
            f"to proceed anyway: --license-override {f.model}='<reason>' (recorded in the cost ledger)",
            file=sys.stderr,
        )
    return report, overrides


def cmd_plan(a: argparse.Namespace) -> int:
    spec = _load(a.spec)
    pricing = Pricing.load(a.pricing)
    print(estimate(spec, pricing, a.judge_runs).table())
    issues = lint(spec)
    if issues:
        print("\nspec lint:")
        for i in issues:
            print(f"  {'ERROR ' if i.severity == 'error' else ''}{i.concept}: [{i.rule}] {i.message}")
    report, _ = _licence_report(spec, a)
    return 1 if report.refused or lint_errors(issues) else 0


def cmd_licenses(a: argparse.Namespace) -> int:
    from creative_forge.licenses import Registry

    reg = Registry.load()
    if a.model:
        e = reg.lookup(a.model)
        if e is None:
            print(f"{a.model}: not in the licence registry (as of {reg.as_of})")
            return 1
        print(f"{a.model} -> {e.id}: commercial={e.commercial}; weights {e.weights_license}; code {e.code_license}")
        if e.conditions:
            print(f"  conditions: {list(e.conditions)}")
        if e.note:
            print(f"  {e.note}")
        print(f"  source: {e.source} (checked {e.checked or 'not checked'})")
        return 0
    print(f"licence registry as of {reg.as_of} ({len(reg.entries)} entries)")
    for e in reg.entries:
        print(f"  {e.id:24} {e.kind:18} {e.commercial:11} {e.weights_license or '-'}  ({e.checked or 'not checked'})")
    return 0


def cmd_run(a: argparse.Namespace) -> int:
    spec = _load(a.spec)
    kind = a.providers or spec.providers
    settings = Settings.from_env()
    pricing = Pricing.load(a.pricing)
    plan = estimate(spec, pricing, a.judge_runs)
    licences, overrides = _licence_report(spec, a)
    if licences.refused:
        return 4
    blocking = lint_errors(lint(spec))
    for i in blocking:
        print(f"error: {i.concept}: [{i.rule}] {i.message}", file=sys.stderr)
    if blocking:
        return 2
    if a.dry_run:
        print(plan.table())
        problems = []
        for c in spec.concepts:
            for label, p in (
                [("gameplay", spec.resolve(c.gameplay.path))]
                + [(f"segment {sid}", spec.resolve(seg.path)) for sid, seg in c.segment_variants() if seg is not None]
                + [(f"reference ({hid})", spec.resolve(r)) for hid, h in c.hook_variants() for r in h.reference_images]
                + ([("music", spec.resolve(c.music.path))] if c.music and c.music.path else [])
            ):
                if p is not None and not p.exists():
                    problems.append(f"{c.id}: {label} file missing: {p}")
        from creative_forge.ffmpeg import FFmpegError, find_tools

        try:
            find_tools()
        except FFmpegError as e:
            problems.append(str(e))
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
        license_overrides=overrides,
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
            print(f"\n{c.label}: FAILED {c.error}")
        for e in c.exports:
            _print_checks(f"{c.label} {e.format}  {e.path}", e.checks)
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
            print(f"\n{c.label}: FAILED {c.error}")
        for e in c.exports:
            _print_checks(f"{c.label} {e.format}  {e.path.name}", e.checks)
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


def cmd_ideate(a: argparse.Namespace) -> int:
    from creative_forge.ideate.agent import VertexGeminiChat, offline_script, run_agent, summarize, write_spec
    from creative_forge.ideate.library import HashingEmbedder, LibraryIndex, VertexTextEmbedder, load_library
    from creative_forge.ideate.tools import Toolbox

    settings = Settings.from_env()
    client = None
    if a.llm == "vertex" or a.embedder == "vertex":
        from creative_forge.providers.vertex import VertexClient

        client = VertexClient(settings.require_project())
    embedder = VertexTextEmbedder(client, a.embed_location) if a.embedder == "vertex" else HashingEmbedder()
    index = LibraryIndex(load_library(), embedder, settings.cache_dir / f"library-{embedder.name}.json")
    embedded = index.build()
    model = VertexGeminiChat(client, a.model, settings.genai_location) if a.llm == "vertex" else offline_script(a.n)
    tools = Toolbox(index, [Path(c) for c in a.clips], Pricing.load(), a.n)
    result = run_agent(model, tools, a.game, a.n, a.max_steps)

    for t in result.trace:
        print(f"step {t['step']}  {t['tool']}: {summarize(t['result'])}")
    line = f"\n{model.name}: {model.requests} request(s)"
    if result.usage.get("totalTokenCount"):
        line += f", {result.usage['totalTokenCount']} tokens"
    line += f"; library {embedder.name}: {embedded} entries embedded now"
    if isinstance(embedder, VertexTextEmbedder):
        line += f", {embedder.inputs} embedding inputs sent"
    print(line)
    if not result.accepted:
        print(f"no accepted concept set after {result.steps} step(s); nothing written", file=sys.stderr)
        return 1
    header = [
        f"Drafted by `forge ideate` ({model.name}, {model.requests} model requests, library {embedder.name}).",
        "A draft, not a decision: review the prompts and cut points, and fill the TODOs before a paid run.",
    ]
    out = write_spec(result, Path(a.out), a.name, header)
    print(f"spec: {out}  ({len(result.concepts)} concept(s); `forge plan {out}` for the cost)")
    return 0


def cmd_segments(a: argparse.Namespace) -> int:
    from creative_forge.segments import SegmentsUnavailable, as_yaml, detect_scenes, suggest, to_dict

    video = Path(a.video)
    if not video.is_file():
        print(f"error: {video} not found", file=sys.stderr)
        return 2
    try:
        scenes, fps = detect_scenes(video, detector=a.detector, threshold=a.threshold)
    except SegmentsUnavailable as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    picks = suggest(scenes, min_len_s=a.min_len, max_len_s=a.max_len, margin_s=a.margin)
    if a.json:
        print(json.dumps(to_dict(video, fps, scenes, picks), indent=2))
        return 0
    print(
        f"{video.name}: {len(scenes)} scene(s) at {fps:g} fps; cuts at "
        + ", ".join(f"{s.start_s:.2f}s" for s in scenes[1:])
    )
    for i, sc in enumerate(scenes):
        print(
            f"  scene {i}: {sc.start_s:7.2f} - {sc.end_s:7.2f} s  ({sc.duration:.2f} s, "
            f"frames {sc.start_frame}-{sc.end_frame})"
        )
    print("\n" + as_yaml(video, picks))
    return 0


def cmd_setup(a: argparse.Namespace) -> int:
    from creative_forge.setup_ffmpeg import SetupError, setup_ffmpeg

    def log(msg: str) -> None:
        print(msg, flush=True)

    try:
        res = setup_ffmpeg(force=a.force, log=log)
    except SetupError as e:
        print(f"setup failed: {e}", file=sys.stderr)
        return 1
    from creative_forge.ffmpeg import aac_encoders

    verb = "already usable, kept" if res.action == "kept" else "installed"
    print(f"ffmpeg  {res.tools.ffmpeg}  ({res.tools.source}; {verb})")
    print(f"        {res.features.version}")
    print(f"ffprobe {res.tools.ffprobe}")
    print(f"AAC encoders that open: {', '.join(aac_encoders())}; libx264 and all required filters present")
    return 0


def cmd_doctor(a: argparse.Namespace) -> int:
    from creative_forge.doctor import failed, format_text, run_doctor, to_json

    items = run_doctor(comfyui_url=a.comfyui)
    print(to_json(items) if a.json else format_text(items))
    return 1 if failed(items) else 0


def cmd_ui(a: argparse.Namespace) -> int:
    from creative_forge.ui.app import serve

    return serve(Path(a.root), host=a.host, port=a.port, open_browser=not a.no_browser)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="forge", description="Spec-driven AI-hook video ad pipeline with QA.")
    p.add_argument("--version", action="version", version=f"creative-forge {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="cost estimate and spec lint; makes no calls")
    sp.add_argument("spec")
    sp.add_argument("--pricing", help="pricing YAML overriding the bundled October 2026 table")
    sp.add_argument("--judge-runs", type=int, default=None)
    sp.add_argument("--license-override", action="append", metavar="MODEL=REASON", help=LICENSE_OVERRIDE_HELP)
    sp.set_defaults(fn=cmd_plan)

    sl = sub.add_parser("licenses", help="the model licence registry: every model the tool can call")
    sl.add_argument("model", nargs="?", help="look up one model id or file name")
    sl.set_defaults(fn=cmd_licenses)

    sr = sub.add_parser("run", help="render every concept of a spec")
    sr.add_argument("spec")
    sr.add_argument("--dry-run", action="store_true", help="validate, lint and estimate; no generation calls")
    sr.add_argument("--budget-usd", type=float, default=None, help="refuse any paid call that would exceed this")
    sr.add_argument("--providers", choices=["vertex", "fake"], default=None, help="override the spec")
    sr.add_argument("--jobs", type=int, default=2)
    sr.add_argument("--only", nargs="*", default=None, help="concept ids to run")
    sr.add_argument("--keep-work", action="store_true", help="keep per-job temp directories")
    sr.add_argument("--ocr", choices=OCR_CHOICES, default="auto")
    sr.add_argument("--judge-runs", type=int, default=None)
    sr.add_argument("--pricing")
    sr.add_argument("--license-override", action="append", metavar="MODEL=REASON", help=LICENSE_OVERRIDE_HELP)
    sr.set_defaults(fn=cmd_run)

    sq = sub.add_parser("qa", help="run the QA checks on finished videos")
    sq.add_argument("videos", nargs="+")
    sq.add_argument("--spec", help="take thresholds from this spec")
    sq.add_argument("--format", choices=["9:16", "4:5"], default=None)
    sq.add_argument("--hook-end", type=float, default=None, help="seconds where the hook crossfade starts")
    sq.add_argument("--gameplay-end", type=float, default=None)
    sq.add_argument("--crossfade", type=float, default=0.3)
    sq.add_argument("--ocr", choices=OCR_CHOICES, default="auto")
    sq.add_argument("--judge-runs", type=int, default=0)
    sq.add_argument("--providers", choices=["vertex", "fake"], default=None)
    sq.add_argument("--json", help="write results as JSON")
    sq.set_defaults(fn=cmd_qa)

    sd = sub.add_parser("demo", help="end-to-end run on synthetic gameplay with fake providers (no keys)")
    sd.add_argument("--out", default="out/demo")
    sd.add_argument("--jobs", type=int, default=2)
    sd.add_argument("--keep-work", action="store_true")
    sd.add_argument("--ocr", choices=OCR_CHOICES, default="auto")
    sd.set_defaults(fn=cmd_demo)

    si = sub.add_parser("ideate", help="draft hook concepts as a spec with a tool-calling LLM and the lesson library")
    si.add_argument("--game", required=True, help="short description of the game and the ad goal")
    si.add_argument("--clips", nargs="+", required=True, help="real gameplay clips the concepts may use")
    si.add_argument("--n", type=int, default=3, choices=range(1, 7), metavar="N", help="concepts to write (1-6)")
    si.add_argument("--out", default="out/ideas.yaml")
    si.add_argument("--name", default="ideas", help="spec name (lowercase, a-z0-9_-)")
    si.add_argument("--llm", choices=["fake", "vertex"], default="fake", help="fake: scripted, offline")
    si.add_argument("--model", default="gemini-2.5-flash")
    si.add_argument("--embedder", choices=["hash", "vertex"], default="hash")
    si.add_argument("--embed-location", default="us-central1")
    si.add_argument("--max-steps", type=int, default=8, choices=range(1, 21), metavar="STEPS", help="1-20")
    si.set_defaults(fn=cmd_ideate)

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

    ss = sub.add_parser("setup", help="install dependencies creative-forge manages itself")
    ssub = ss.add_subparsers(dest="what", required=True)
    sf = ssub.add_parser(
        "ffmpeg",
        help="keep a usable ffmpeg, or download the pinned 9.0.2 build (SHA-256 verified) into the user data dir",
    )
    sf.add_argument("--force", action="store_true", help="download the pinned build even if a usable ffmpeg exists")
    sf.set_defaults(fn=cmd_setup)

    sdoc = sub.add_parser("doctor", help="check ffmpeg, OCR, folders, ComfyUI and credentials; exit 1 on a blocker")
    sdoc.add_argument("--json", action="store_true", help="machine-readable output")
    sdoc.add_argument(
        "--comfyui", metavar="URL", help="also check a ComfyUI server (default: $CREATIVE_FORGE_COMFYUI_URL)"
    )
    sdoc.set_defaults(fn=cmd_doctor)

    sg = sub.add_parser("segments", help="suggest gameplay segment in/out points from a capture (PySceneDetect)")
    sg.add_argument("video")
    sg.add_argument("--detector", choices=["content", "adaptive"], default="content")
    sg.add_argument("--threshold", type=float, default=None, help="detector threshold (content 27, adaptive 3)")
    sg.add_argument("--min-len", type=float, default=3.0, help="shortest suggested segment (s)")
    sg.add_argument("--max-len", type=float, default=10.0, help="longest suggested segment (s)")
    sg.add_argument("--margin", type=float, default=0.2, help="trimmed at both ends of a scene (s)")
    sg.add_argument("--json", action="store_true")
    sg.set_defaults(fn=cmd_segments)

    from creative_forge.perf.commands import add_parser as add_perf

    add_perf(sub)

    sui = sub.add_parser("ui", help="local web page: runs, QA reports, players, approve / reject, demo, doctor")
    sui.add_argument("--root", default="out", help="folder searched for runs (default: out)")
    sui.add_argument("--host", default="127.0.0.1", help="bind address; anything but loopback exposes the page")
    sui.add_argument("--port", type=int, default=None, help="default 8765, or any free port if that is taken")
    sui.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    sui.set_defaults(fn=cmd_ui)
    return p


def _utf8_console() -> None:
    """A pipe or a file on Windows gets the ANSI code page (cp1252/cp1254), which cannot encode a path with "Ş";
    printing it would crash. Consoles are UTF-16 already; everything else is switched to UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        enc = (getattr(stream, "encoding", None) or "").lower().replace("-", "")
        if enc != "utf8" and hasattr(stream, "reconfigure") and not os.environ.get("PYTHONIOENCODING"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args))
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
