"""The tools the ideation agent calls, declared for Gemini function calling and executed locally.

Every tool takes JSON arguments and returns a JSON object. A bad argument comes back as ``{"error": ...}`` in the
function response, so the model can correct itself; it does not end the run. The checks are the same code the
pipeline uses (``AdSpec`` validation, ``lint``, ``estimate``), so a concept the agent submits is one ``forge run``
accepts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from creative_forge.ffmpeg import probe
from creative_forge.ideate.library import LibraryIndex
from creative_forge.lint import lint_concept
from creative_forge.pricing import Pricing, estimate
from creative_forge.spec import AdSpec

# Rules a model cannot satisfy from text alone: text_regions are pixel boxes on the real frame.
HUMAN_TODO = {"strip-text": "fill hook.text_regions with the text boxes of the real first frame"}

DRAFT = {
    "type": "OBJECT",
    "properties": {
        "id": {"type": "STRING", "description": "unique lowercase id: a-z, 0-9 and dashes"},
        "clip": {"type": "STRING", "description": "a gameplay clip path exactly as list_gameplay_clips returns it"},
        "in_s": {"type": "NUMBER", "description": "segment start (s). Its first frame is where the AI hook must land"},
        "out_s": {"type": "NUMBER", "description": "segment end (s), inside the clip"},
        "image_prompt": {
            "type": "STRING",
            "description": "edit of the real first frame: add only a hand or tool entering from an edge; no text",
        },
        "video_prompt": {"type": "STRING", "description": "image-to-video prompt that ends exactly on the real frame"},
        "duration_s": {"type": "INTEGER", "description": "generated clip length: 4, 6 or 8"},
        "cut_s": {"type": "NUMBER", "description": "cut point in the generated clip, at least 1 s before its end"},
        "effect": {
            "type": "STRING",
            "enum": ["none", "dust", "mud", "frost", "darken"],
            "description": "painted on the real frame in code before the image edit",
        },
        "headline": {"type": "STRING", "description": "end-card headline, at most 4 words"},
        "music_prompt": {"type": "STRING", "description": "instrumental music prompt"},
        "rationale": {"type": "STRING", "description": "which library entries this concept draws on, and why"},
    },
    "required": ["id", "clip", "in_s", "out_s", "image_prompt", "video_prompt", "duration_s", "cut_s", "headline"],
}

DECLARATIONS = [
    {
        "name": "search_creative_library",
        "description": "Search past production lessons and past concepts with their QA outcomes.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "what you want to know, in plain words"},
                "k": {"type": "INTEGER", "description": "number of results, 1-8 (default 4)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_gameplay_clips",
        "description": "List the real gameplay clips available, with duration and size.",  # no parameters
    },
    {
        "name": "lint_concept",
        "description": "Validate one draft concept and run the production lint rules on it.",
        "parameters": {"type": "OBJECT", "properties": {"concept": DRAFT}, "required": ["concept"]},
    },
    {
        "name": "estimate_cost",
        "description": "Upper-bound generation cost of one draft concept in USD (image edit, video, music).",
        "parameters": {"type": "OBJECT", "properties": {"concept": DRAFT}, "required": ["concept"]},
    },
    {
        "name": "submit_concepts",
        "description": "Submit the final concepts. Accepted only if there are exactly the requested number and "
        "none has a blocking issue; otherwise the problems are returned.",
        "parameters": {
            "type": "OBJECT",
            "properties": {"concepts": {"type": "ARRAY", "items": DRAFT}},
            "required": ["concepts"],
        },
    },
]


def to_concept(d: dict[str, Any]) -> dict[str, Any]:
    """Flat draft (easy for a model to fill) -> the nested ``concepts[]`` entry of a spec."""
    hook: dict[str, Any] = {
        "image_prompt": d.get("image_prompt"),
        "video_prompt": d.get("video_prompt"),
        "duration_s": int(d.get("duration_s", 8)),
        "cut_s": float(d["cut_s"]) if d.get("cut_s") is not None else None,
    }
    if d.get("effect") not in (None, "", "none"):
        hook["effects"] = [{"type": d["effect"]}]
    concept: dict[str, Any] = {
        "id": d.get("id"),
        "gameplay": {"path": d.get("clip"), "in_s": float(d.get("in_s", 0)), "out_s": float(d.get("out_s", 0))},
        "hook": hook,
        "end_card": {"headline": d.get("headline") or "Try it yourself!", "button": "PLAY NOW"},
    }
    if d.get("music_prompt"):
        concept["music"] = {"prompt": d["music_prompt"]}
    return concept


class Toolbox:
    def __init__(self, index: LibraryIndex, clips: list[Path], pricing: Pricing, n: int) -> None:
        self.index = index
        self.pricing = pricing
        self.n = n
        self.clips = {str(p): probe(p) for p in clips}  # a missing clip fails here, before any model call
        self.accepted: list[dict[str, Any]] | None = None

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        fn = {
            "search_creative_library": self.search_creative_library,
            "list_gameplay_clips": self.list_gameplay_clips,
            "lint_concept": self.lint_concept,
            "estimate_cost": self.estimate_cost,
            "submit_concepts": self.submit_concepts,
        }.get(name)
        if fn is None:
            return {"error": f"unknown tool {name!r}"}
        try:
            return fn(**args)
        except (TypeError, ValueError, KeyError) as e:  # bad arguments from the model: tell it, don't crash
            return {"error": f"{type(e).__name__}: {e}"}

    # --------------------------------------------------------------------------------------------------- tools
    def search_creative_library(self, query: str, k: int = 4) -> dict[str, Any]:
        hits = self.index.search(query, max(1, min(int(k), 8)))
        return {
            "results": [
                {"id": e.id, "kind": e.kind, "synthetic": e.synthetic, "title": e.title, "text": e.text, "score": s}
                for s, e in hits
            ]
        }

    def list_gameplay_clips(self) -> dict[str, Any]:
        return {
            "clips": [
                {"path": p, "duration_s": round(i.duration, 2), "width": i.width, "height": i.height}
                for p, i in self.clips.items()
            ]
        }

    def check(self, draft: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
        """(spec concept or None, blocking issues, human to-dos)."""
        blocking: list[str] = []
        clip = self.clips.get(str(draft.get("clip")))
        if clip is None:
            blocking.append(f"unknown-clip: {draft.get('clip')!r} is not in list_gameplay_clips")
        elif float(draft.get("out_s", 0)) > clip.duration + 0.05:
            blocking.append(f"clip-range: out_s {draft.get('out_s')} is past the clip end ({clip.duration:.2f} s)")
        concept = to_concept(draft)
        try:
            spec = AdSpec.model_validate({"name": "ideas", "concepts": [concept]})
        except ValidationError as e:
            blocking += [f"invalid: {'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()]
            return None, blocking, []
        todo = []
        for issue in lint_concept(spec, spec.concepts[0]):
            if issue.rule in HUMAN_TODO:
                todo.append(f"{issue.rule}: {HUMAN_TODO[issue.rule]}")
            else:
                blocking.append(f"{issue.rule}: {issue.message}")
        return concept, blocking, todo

    def lint_concept(self, concept: dict[str, Any]) -> dict[str, Any]:
        _, blocking, todo = self.check(concept)
        return {"ok": not blocking, "blocking": blocking, "left_for_a_human": todo}

    def estimate_cost(self, concept: dict[str, Any]) -> dict[str, Any]:
        spec = AdSpec.model_validate({"name": "ideas", "qa": {"judge_runs": 0}, "concepts": [to_concept(concept)]})
        plan = estimate(spec, self.pricing)
        return {
            "usd": plan.total_priced,
            "lines": [{"step": x.kind, "model": x.model, "usd": x.usd} for x in plan.lines],
            "prices_as_of": self.pricing.as_of,
        }

    def submit_concepts(self, concepts: list[dict[str, Any]]) -> dict[str, Any]:
        problems: list[str] = []
        if len(concepts) != self.n:
            problems.append(f"expected exactly {self.n} concepts, got {len(concepts)}")
        ids = [c.get("id") for c in concepts]
        if len(set(ids)) != len(ids):
            problems.append(f"concept ids must be unique: {ids}")
        checked = []
        for d in concepts:
            concept, blocking, todo = self.check(d)
            problems += [f"{d.get('id')}: {b}" for b in blocking]
            checked.append({"concept": concept, "todo": todo, "rationale": d.get("rationale", "")})
        if problems:
            return {"accepted": False, "problems": problems}
        self.accepted = checked
        return {"accepted": True, "count": len(checked)}
