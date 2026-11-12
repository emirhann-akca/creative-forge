"""``forge ideate``: a tool-calling loop that drafts hook concepts as spec entries.

The model only acts through the tools in ``tools.py`` (``toolConfig`` mode ``ANY``), and the loop ends when
``submit_concepts`` accepts a set that passes the same validation and lint as ``forge run``, or after
``max_steps`` model requests. The transcript stays in Gemini's ``contents`` format, so the scripted model used in
tests and offline demos exercises exactly the code path a real model does.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from creative_forge.ideate.tools import DECLARATIONS, Toolbox
from creative_forge.spec import load_spec

SYSTEM = """You write hook concepts for mobile game video ads. Every ad is: an AI hook (an image model edits the
real first gameplay frame, then an image-to-video model animates it and must END on that same real frame), a 0.3 s
crossfade into the same level's real gameplay, then an end card.

Work only through the tools:
1. search_creative_library before writing, to learn what failed and what worked.
2. list_gameplay_clips; choose in_s/out_s inside a clip with at least 3 s of gameplay.
3. lint_concept on your drafts and fix every blocking issue.
4. submit_concepts with exactly {n} concepts. If it is rejected, fix the problems and submit again.

Production rules: the hand or tool enters from an edge or a corner and never covers the board centre or the UI.
Dust, mud, frost or darkness is an `effect` painted in code; never ask the image model to cover the frame. The image
prompt must say not to add any text. cut_s is at least 1 s before the generated clip ends, and preferably before
its last 3 s. Each concept is a different idea. Write prompts in English."""


class ChatModel(ABC):
    name: str = "chat"

    def __init__(self) -> None:
        self.requests = 0

    @abstractmethod
    def generate(self, system: str, contents: list[dict], tools: list[dict]) -> dict[str, Any]:
        """One ``generateContent`` call. Returns the response JSON (``candidates``, ``usageMetadata``)."""


class VertexGeminiChat(ChatModel):
    # gemini-2.5-* retired on Vertex AI on 20 Oct 2026 (model-versions page). gemini-3.5-flash-lite on the global
    # endpoint answered studio-pulse's market radar on 12 Nov 2026; change it with `forge ideate --model`.
    def __init__(
        self, client, model: str = "gemini-3.5-flash-lite", location: str = "global", temperature: float = 0.4
    ):
        super().__init__()
        self.client = client
        self.model = model
        self.location = location
        self.temperature = temperature
        self.name = model

    def generate(self, system: str, contents: list[dict], tools: list[dict]) -> dict[str, Any]:
        self.requests += 1
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": contents,
            "tools": [{"functionDeclarations": tools}],
            "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
            "generationConfig": {"temperature": self.temperature},
        }
        return self.client.post(self.client.model_url(self.location, self.model, "generateContent"), body)


Turn = list[tuple[str, dict[str, Any]]] | Callable[[list[dict]], list[tuple[str, dict[str, Any]]]]


class ScriptedChat(ChatModel):
    """Plays back tool calls in Gemini's response format. A turn is a list of ``(tool, args)`` or a function of the
    transcript so far (to use earlier tool results)."""

    name = "scripted"

    def __init__(self, turns: list[Turn]) -> None:
        super().__init__()
        self.turns = turns

    def generate(self, system: str, contents: list[dict], tools: list[dict]) -> dict[str, Any]:
        turn = self.turns[min(self.requests, len(self.turns) - 1)]
        self.requests += 1
        calls = turn(contents) if callable(turn) else turn
        parts = [{"functionCall": {"name": n, "args": a}} for n, a in calls]
        return {"candidates": [{"content": {"role": "model", "parts": parts}}], "usageMetadata": {}}


@dataclass
class AgentResult:
    accepted: bool
    concepts: list[dict[str, Any]]
    steps: int
    usage: dict[str, int] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)


def run_agent(model: ChatModel, tools: Toolbox, brief: str, n: int, max_steps: int = 8) -> AgentResult:
    contents: list[dict] = [
        {"role": "user", "parts": [{"text": f"Game and goal:\n{brief}\n\nWrite {n} hook concepts."}]}
    ]
    usage: dict[str, int] = {}
    trace: list[dict[str, Any]] = []
    system = SYSTEM.format(n=n)
    for step in range(1, max_steps + 1):
        resp = model.generate(system, contents, DECLARATIONS)
        for k, v in (resp.get("usageMetadata") or {}).items():
            if isinstance(v, int):
                usage[k] = usage.get(k, 0) + v
        parts = ((resp.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
        contents.append({"role": "model", "parts": parts})  # verbatim: keeps thought signatures intact
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not calls:
            trace.append({"step": step, "tool": None, "result": "no tool call"})
            contents.append({"role": "user", "parts": [{"text": "Use the tools; finish with submit_concepts."}]})
            continue
        responses = []
        for call in calls:
            result = tools.call(call["name"], call.get("args") or {})
            trace.append({"step": step, "tool": call["name"], "args": call.get("args") or {}, "result": result})
            responses.append({"functionResponse": {"name": call["name"], "response": result}})
        contents.append({"role": "user", "parts": responses})
        if tools.accepted is not None:
            return AgentResult(True, tools.accepted, step, usage, trace)
    return AgentResult(False, [], max_steps, usage, trace)


def summarize(result: dict[str, Any]) -> str:
    """One line per tool result for the console trace."""
    if "error" in result:
        return f"error: {result['error']}"
    if "results" in result:
        return f"{len(result['results'])} results: {', '.join(x['id'] for x in result['results'])}"
    if "accepted" in result:
        return "accepted" if result["accepted"] else f"rejected: {'; '.join(result['problems'][:3])}"
    if "ok" in result:
        return "ok" if result["ok"] else f"blocking: {'; '.join(result['blocking'][:3])}"
    if "usd" in result:
        return f"${result['usd']:.2f}"
    return f"{len(result.get('clips', []))} clip(s)"


def write_spec(result: AgentResult, out: Path, name: str, header: list[str]) -> Path:
    """Write accepted concepts as a spec next to ``out`` (clip paths made relative to it), then load it back."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    concepts = []
    for item in result.concepts:
        c = dict(item["concept"])
        c["gameplay"] = {**c["gameplay"], "path": os.path.relpath(Path(c["gameplay"]["path"]).resolve(), out.parent)}
        concepts.append(c)
    lines = [f"# {h}" for h in header]
    for item in result.concepts:
        lines += [f"# {item['concept']['id']}: TODO {t}" for t in item["todo"]]
        if item["rationale"]:
            lines.append(f"# {item['concept']['id']} rationale: {' '.join(str(item['rationale']).split())}")
    spec = {"version": 1, "name": name, "providers": "vertex", "concepts": concepts}
    out.write_text("\n".join(lines) + "\n" + yaml.safe_dump(spec, sort_keys=False, width=110), encoding="utf-8")
    load_spec(out)  # the written file is a valid spec, or this raises
    return out


def offline_script(n: int) -> ScriptedChat:
    """``forge ideate --llm fake``: research, a first draft that fails lint, then a corrected submission."""

    def clips(contents: list[dict]) -> list[dict]:
        for msg in reversed(contents):
            for p in msg.get("parts", []):
                fr = p.get("functionResponse", {})
                if fr.get("name") == "list_gameplay_clips":
                    return fr["response"]["clips"]
        raise ValueError("list_gameplay_clips was not called")

    ideas = [
        ("dust-blower", "dust", "a leaf blower nozzle", "blows the dust off shelf by shelf"),
        ("pressure-wash", "mud", "a pressure washer lance", "blasts the mud away stripe by stripe"),
        ("frost-scraper", "frost", "an ice scraper", "scrapes the frost off in long strokes"),
        ("night-torch", "darken", "a flashlight", "sweeps light across the dark board until it is lit"),
    ]

    def draft(i: int, clip: dict, edge: bool = True) -> dict[str, Any]:
        cid, effect, tool, action = ideas[i % len(ideas)]
        where = "entering from the bottom right corner" if edge else "in the middle of the board"
        start = round(min(1.0, clip["duration_s"] * 0.1), 2)
        return {
            "id": cid if i < len(ideas) else f"{cid}-{i}",
            "clip": clip["path"],
            "in_s": start,
            "out_s": round(min(clip["duration_s"], start + 10.0), 2),
            "image_prompt": f"Edit this exact image. Keep every item in place. Add a realistic adult hand holding "
            f"{tool}, {where}. Do not add any text.",
            "video_prompt": f"The hand {action} until the board looks exactly like the final frame, then leaves.",
            "duration_s": 8,
            "cut_s": 5.0 if edge else 7.6,
            "effect": effect,
            "headline": "Try it yourself!",
            "music_prompt": "Bright playful puzzle game music, marimba, no vocals",
            "rationale": "L07 (hand from an edge), L08 (effect painted in code), L06 (cut before the last 3 s)",
        }

    return ScriptedChat(
        [
            [
                (
                    "search_creative_library",
                    {"query": "hand or tool covering the board makes the video model invent UI"},
                ),
                ("list_gameplay_clips", {}),
            ],
            lambda c: [("lint_concept", {"concept": draft(0, clips(c)[0], edge=False)})],
            lambda c: [
                ("submit_concepts", {"concepts": [draft(i, clips(c)[i % len(clips(c))]) for i in range(n)]}),
            ],
        ]
    )
