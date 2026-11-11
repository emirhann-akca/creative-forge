"""Plan-time rules learned from production, checked before anything is generated."""

from __future__ import annotations

import re
from dataclasses import dataclass

from creative_forge.spec import AdSpec, Concept, HookSpec
from creative_forge.tags import Taxonomy


@dataclass(frozen=True)
class LintIssue:
    concept: str
    rule: str
    message: str
    severity: str = "warn"  # "error" blocks `forge plan` (exit 1) and `forge run` before any call


TAG_DIMENSIONS_WANTED = ("hook_type", "mechanic_shown", "emotion")


_HAND = re.compile(r"\b(hand|hands|finger|fingers|tool|nozzle|lance|torch|blower|brush|knife)\b", re.I)
# Needs an entering verb: "wiping dust from the top-right corner" (a real Gemini draft) is not an edge entry.
_EDGE = re.compile(
    r"\b(enters?|entering|reach(es|ing)? in|comes? in|coming in)\b[^.]*\b(edge|corner|side|bottom|top|left|right)\b",
    re.I,
)
_NO_TEXT = re.compile(r"\b(no|not|without|don'?t|do not)\b[^.]*\btext\b", re.I)
_COVER_ALL = re.compile(r"\b(cover|bury|coat)\b[^.]*\b(entire|whole|everything|all)\b", re.I)
_EFFECT_WORDS = re.compile(r"\b(dust|mud|ice|frost|snow|grime|dirt)\b", re.I)


def lint_concept(spec: AdSpec, c: Concept) -> list[LintIssue]:
    """Every hook of the concept (one, or each hook variant), named ``concept/hook`` for variants."""
    hooks = c.hook_variants()
    issues = []
    for hid, h in hooks:
        issues += lint_hook(c.id if hid == "main" else f"{c.id}/{hid}", h)
    langs = spec.languages_for(c)
    if len(langs) > 1:
        for eid, e in c.end_card_variants():
            for name, text in (("headline", e.headline), ("button", e.button)):
                if isinstance(text, str):
                    issues.append(
                        LintIssue(
                            c.id,
                            "not-localized",
                            f"end card {eid} {name} is one string for {len(langs)} languages ({', '.join(langs)}); "
                            "give it per language ({en: ..., tr: ...})",
                        )
                    )
    return issues


def lint_hook(label: str, h: HookSpec) -> list[LintIssue]:
    issues: list[LintIssue] = []
    ip = h.image_prompt or ""
    if ip and _HAND.search(ip) and not _EDGE.search(ip):
        issues.append(
            LintIssue(
                label,
                "hand-enters-from-edge",
                "the image prompt adds a hand/tool but does not say it enters from an edge or corner; "
                "a hand that covers UI makes Veo invent fake UI behind it",
            )
        )
    if ip and not _NO_TEXT.search(ip):
        issues.append(LintIssue(label, "no-new-text", "the image prompt should say not to add any text"))
    if ip and _COVER_ALL.search(ip) and _EFFECT_WORDS.search(ip):
        issues.append(
            LintIssue(
                label,
                "effects-programmatic",
                "the image prompt asks the model to cover the whole frame in a material; it redraws the "
                "products underneath. Use hook.effects on the real frame and ask the model only for the "
                "hand/tool",
            )
        )
    cut = h.effective_cut
    if cut > h.duration_s - 1.0 + 1e-9:
        issues.append(
            LintIssue(
                label,
                "tail-drift",
                f"cut at {cut:.2f}s keeps Veo's last second, where stylized characters drift photoreal",
            )
        )
    elif cut > h.duration_s - 3.0 and not h.patches:
        issues.append(
            LintIssue(
                label,
                "tail-ui",
                f"cut at {cut:.2f}s is inside the last 3 s, where Veo often invents UI; review the "
                "frames, cut mid-transition, or add hook.patches",
            )
        )
    if not h.text_regions:
        issues.append(
            LintIssue(
                label,
                "strip-text",
                "no hook.text_regions: any text visible in the first frame will be copied and garbled "
                "by Veo; list the text boxes of the real frame",
            )
        )
    return issues


def lint_tags(spec: AdSpec, c: Concept) -> list[LintIssue]:
    taxonomy = Taxonomy.load(spec.resolve(spec.taxonomy))
    issues = [LintIssue(c.id, rule, msg, "error") for rule, msg in taxonomy.problems(c.tags, f"{c.id}.tags")]
    for raw in c.variants.hooks if c.variants else []:
        if raw.get("tags"):
            where = f"{c.id}/{raw.get('id')}.tags"
            issues += [LintIssue(c.id, rule, msg, "error") for rule, msg in taxonomy.problems(raw["tags"], where)]
    merged = [c.hook_tags(hid) for hid, _ in c.hook_variants()]
    missing = [d for d in TAG_DIMENSIONS_WANTED if d in taxonomy.dimensions and any(d not in t for t in merged)]
    if missing:
        issues.append(
            LintIssue(
                c.id,
                "untagged",
                f"no {', '.join(missing)} tag: `forge perf report` cannot group this concept's results by it",
            )
        )
    return issues


def lint(spec: AdSpec) -> list[LintIssue]:
    return [i for c in spec.concepts for i in lint_concept(spec, c) + lint_tags(spec, c)]


def errors(issues: list[LintIssue]) -> list[LintIssue]:
    return [i for i in issues if i.severity == "error"]
