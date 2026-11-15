"""``forge make``: a brief in, a reviewed-ready run out, in one call another program can drive.

The entry point studio-pulse's ad centre calls from its "Yeni kreatif" wizard (it can equally be typed):

    forge make --app puzzle_game --app-name "Puzzle Game" --brief "dust on the shelves, cleared in 3 s" \\
        --variants 3 --providers fake --json --progress progress.json

It writes a spec from fixed hook recipes (one concept, N hook variants: each recipe is one the lint rules already
accept, so a brief never produces a spec that `forge run` refuses), runs the normal pipeline (QA included), writes the
report, and marks the run with ``app.json`` so the reader knows which game it is for. Nothing is approved here: the
exports wait for ``forge review`` like any other run.

* ``--providers fake`` (default): the fake providers and, without ``--gameplay``, synthetic gameplay. No key, no cost;
  what comes out shows the pipeline and the review flow, not a real ad.
* ``--providers vertex``: the real models, priced and guarded like ``forge run``: ``--budget-usd`` is required and
  ``--gameplay`` (a real capture of the game) is required, because a hook has to land on the game's own frame.

The brief is stored as the concept's hypothesis (what the test is expected to show) and in ``make.json``; it is not
pasted into model prompts, so it cannot break a prompt rule. ``--progress FILE`` is rewritten after each pipeline step
with ``{"state", "done", "total", "message"}`` for a progress bar.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "creative-forge/make/v1"
FORMATS = ("video",)

HOOK_RULES = "Keep every element of the board exactly in the same position and shape. Do not add any text."
# One recipe per hook variant, in this order. Each passes lint: a hand or tool enters from an edge, mess is painted
# in code (effects), the cut leaves the drifting tail out.
RECIPES: list[dict[str, Any]] = [
    {
        "id": "clean",
        "hook_type": "satisfying-clean",
        "emotion": ["satisfaction"],
        "effect": "dust",
        "image": "Add a realistic adult hand holding the nozzle of a small leaf blower, entering from the bottom "
        "right corner.",
        "video": "The hand sweeps the leaf blower across the board; the dust blows away until the board is clean and "
        "looks exactly like the final frame.",
    },
    {
        "id": "wash",
        "hook_type": "real-hand",
        "emotion": ["satisfaction", "calm"],
        "effect": "mud",
        "image": "Add a realistic adult hand holding a pressure washer lance, entering from the bottom right corner.",
        "video": "Satisfying pressure washing: the jet clears the mud stripe by stripe until the board is spotless and "
        "looks exactly like the final frame.",
    },
    {
        "id": "thaw",
        "hook_type": "reveal",
        "emotion": ["curiosity"],
        "effect": "frost",
        "image": "Add a realistic adult hand holding a small hair dryer, entering from the left edge.",
        "video": "Warm air melts the frost from the board piece by piece until it looks exactly like the final frame.",
    },
    {
        "id": "dim",
        "hook_type": "before-after",
        "emotion": ["surprise"],
        "effect": "darken",
        "image": "Add a realistic adult hand holding a small torch, entering from the top edge.",
        "video": "The torch beam sweeps over the dark board and the light spreads until it looks exactly like the "
        "final frame.",
    },
]
END_CARD = {
    "headline": {"en": "Try it yourself!", "tr": "Kendin dene!", "de": "Probier es selbst!"},
    "button": {"en": "PLAY NOW", "tr": "OYNA", "de": "JETZT SPIELEN"},
}


class MakeError(ValueError):
    """The request cannot become a run; nothing was generated."""


@dataclass
class Request:
    app: str
    brief: str
    root: Path
    app_name: str | None = None
    store_url: str | None = None
    variants: int = 2
    languages: tuple[str, ...] = ("en",)
    mechanic: str = "other"
    providers: str = "fake"
    budget_usd: float | None = None
    gameplay: Path | None = None
    gameplay_in: float = 1.0
    gameplay_out: float | None = None
    name: str | None = None
    format: str = "video"
    hook: str | None = None  # a hook_type to lead with (its recipe first), e.g. from a competitor creative
    ref: str | None = None  # where the idea came from (a competitor creative's key); stored, never sent anywhere


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:n].strip("-") or "game") if s else "game"


def spec_for(req: Request, gameplay: Path, out_dir: Path, stamp: str) -> dict[str, Any]:
    if req.variants < 1 or req.variants > len(RECIPES):
        raise MakeError(f"variants is 1..{len(RECIPES)}")
    langs = list(dict.fromkeys(req.languages)) or ["en"]
    unknown = [lang for lang in langs if lang not in END_CARD["headline"]]
    if unknown:
        raise MakeError(f"no end-card text for {', '.join(unknown)} (have: {', '.join(END_CARD['headline'])})")
    order = sorted(RECIPES, key=lambda r: r["hook_type"] != req.hook)  # stable: the asked-for hook first
    picked = order[: req.variants]
    first = picked[0]
    out_s = req.gameplay_out if req.gameplay_out is not None else req.gameplay_in + 8.0

    def hook(r: dict[str, Any]) -> dict[str, Any]:
        h: dict[str, Any] = {
            "image_prompt": f"Edit this exact image. {HOOK_RULES} {r['image']}",
            "video_prompt": r["video"],
            "duration_s": 6,
            "cut_s": 4.4,
            "effects": [{"type": r["effect"], "strength": 0.8}],
        }
        if req.providers == "fake":
            from creative_forge.demo import LABEL_BOX, W

            h["text_regions"] = [list(LABEL_BOX)]
            h["patches"] = [{"box": [0, 0, W, 64], "from_s": 3.6}]
        return h

    concept: dict[str, Any] = {
        "id": "brief",
        "gameplay": {"path": str(gameplay), "in_s": req.gameplay_in, "out_s": out_s},
        "hook": hook(first),
        "end_card": {k: {lang: v[lang] for lang in langs} for k, v in END_CARD.items()},
        "music": {"prompt": "Upbeat playful puzzle game loop, bright marimba, 120 bpm", "volume_db": -19},
        "formats": ["9:16", "4:5"],
        "hypothesis": req.brief.strip()[:500] or None,
        "tags": {"hook_type": first["hook_type"], "mechanic_shown": [req.mechanic], "emotion": first["emotion"]},
    }
    if len(picked) > 1 or len(langs) > 1:
        concept["variants"] = {}
        if len(picked) > 1:
            concept["variants"]["hooks"] = [
                {"id": r["id"], **hook(r), "tags": {"hook_type": r["hook_type"], "emotion": r["emotion"]}}
                for r in picked
            ]
        if len(langs) > 1:
            concept["variants"]["languages"] = langs
    spec: dict[str, Any] = {
        "version": 1,
        "name": req.name or f"{slug(req.app.replace('_', '-'), 30)}-{stamp}",
        "providers": req.providers,
        "output_dir": str(out_dir),
        "language": langs[0],
        "concepts": [concept],
    }
    if req.providers == "fake":
        from creative_forge.demo import H, W

        spec["render"] = {"width": W, "height": H, "fps": 30, "preset": "veryfast"}
        spec["export"] = {"preset": "veryfast"}
        spec["qa"] = {"duration_s": [8, 60], "known_text": ["LEVEL", "PLAY NOW", "Try it yourself"], "judge_runs": 2}
    return spec


class Progress:
    """``--progress FILE``: rewritten atomically after every pipeline step."""

    def __init__(self, path: Path | None, total: int) -> None:
        self.path, self.total, self.done = path, max(total, 1), 0
        self.started = datetime.now(UTC).isoformat(timespec="seconds")
        self._lock = threading.Lock()  # the pipeline logs from its worker threads

    def write(self, state: str, message: str, **extra: Any) -> None:
        if self.path is None:
            return
        with self._lock:
            body = {"schema": SCHEMA, "state": state, "done": min(self.done, self.total), "total": self.total,
                    "message": message, "started_at": self.started, **extra}  # fmt: skip
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            tmp.write_text(json.dumps(body, ensure_ascii=False, default=str), encoding="utf-8")
            try:
                os.replace(tmp, self.path)
            except PermissionError:  # Windows: a reader has the file open this instant; the next step rewrites it
                tmp.unlink(missing_ok=True)

    def step(self, message: str) -> None:
        with self._lock:
            self.done += 1
        self.write("running", message)


def make(req: Request, *, progress: Path | None = None, log: Callable[[str], None] = lambda _m: None,
         jobs: int = 2, ocr: str = "auto") -> dict[str, Any]:  # fmt: skip
    from creative_forge.config import Settings
    from creative_forge.hub import write_app
    from creative_forge.lint import errors as lint_errors
    from creative_forge.lint import lint
    from creative_forge.pipeline import Pipeline
    from creative_forge.providers import build_providers
    from creative_forge.providers.fake import fake_providers
    from creative_forge.report import write_report
    from creative_forge.spec import load_spec

    if not 1 <= req.variants <= len(RECIPES):
        raise MakeError(f"variants is 1..{len(RECIPES)}")
    if req.format not in FORMATS:
        raise MakeError(f"format {req.format!r}: forge make renders video; playables are packed with `forge playable`")
    if not re.fullmatch(r"[a-z0-9_]{1,64}", req.app):
        raise MakeError("app is the game's id: a-z, 0-9 and _ (as in studio-pulse's config)")
    if req.providers not in ("fake", "vertex"):
        raise MakeError("providers is fake or vertex")
    if req.providers == "vertex":
        if req.budget_usd is None or req.budget_usd <= 0:
            raise MakeError("a paid run needs --budget-usd (the most it may spend)")
        if req.gameplay is None:
            raise MakeError("a paid run needs --gameplay: a real capture of the game for the hook to land on")
    if req.store_url:
        from creative_forge.campaigns.store_url import parse_store_url

        parse_store_url(req.store_url)  # StoreUrlError (a ValueError) for a placeholder or a non-store URL
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(req.root).resolve() / "runs" / f"{req.app.replace('_', '-')}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=False)
    meter = Progress(progress, total=2 + req.variants * (3 + 4 * len(req.languages)))
    meter.write("running", "spec")
    if req.gameplay is not None:
        gameplay = Path(req.gameplay).resolve()
        if not gameplay.is_file():
            raise MakeError(f"gameplay file not found: {gameplay}")
    else:
        from creative_forge.demo import synthetic_gameplay

        inputs = out_dir / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        gameplay = synthetic_gameplay(inputs / "gameplay.mp4")
        meter.step("synthetic gameplay")
    data = spec_for(req, gameplay, out_dir, stamp)
    spec_path = out_dir / "spec.yaml"
    spec_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    spec = load_spec(spec_path)
    blocking = lint_errors(lint(spec))
    if blocking:
        raise MakeError("; ".join(f"{i.concept}: [{i.rule}] {i.message}" for i in blocking))
    write_app(out_dir, req.app, req.app_name, req.store_url)
    (out_dir / "make.json").write_text(
        json.dumps({"schema": SCHEMA, "brief": req.brief, "hook": req.hook, "ref": req.ref,
                    "variants": req.variants, "languages": list(req.languages),
                    "providers": req.providers, "budget_usd": req.budget_usd, "format": req.format,
                    "at": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )  # fmt: skip

    def on_log(msg: str) -> None:
        meter.step(msg)
        log(msg)

    if req.providers == "fake":
        providers = fake_providers()
        settings = Settings(project=None, cache_dir=out_dir / ".cache")
    else:
        settings = Settings.from_env()
        providers = build_providers("vertex", settings)
    try:
        rr = Pipeline(spec, providers, settings, budget_usd=req.budget_usd, jobs=jobs, ocr=ocr, log=on_log).run()
    except Exception as e:
        meter.write("failed", f"{type(e).__name__}: {e}", run_dir=str(out_dir))
        raise
    write_report(rr)
    result = result_of(out_dir, rr, req)
    meter.done = meter.total
    meter.write("done", "ready for review", run_dir=str(out_dir), result=result)
    return result


def result_of(out_dir: Path, rr: Any, req: Request) -> dict[str, Any]:
    from creative_forge.review import load_exports

    exports = [
        {"file": e.file, "creative_id": e.creative_id, "ad_name": e.ad_name, "format": e.format,
         "qa_status": e.status, "failed": list(e.failed), "warned": list(e.warned)}
        for e in load_exports(out_dir)
    ]  # fmt: skip
    return {
        "schema": SCHEMA,
        "run_dir": str(out_dir),
        "status": rr.status.value,
        "app": {"id": req.app, "name": req.app_name or req.app, "store_url": req.store_url},
        "providers": req.providers,
        "exports": exports,
        "spent_usd": rr.ledger.spent,
        "list_usd": sum((e.list_usd or 0) for e in rr.ledger.entries if not e.cached),
        "errors": [c.error for c in rr.concepts if c.error],
    }
