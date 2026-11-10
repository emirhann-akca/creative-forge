"""Plan-time rules learned from production, checked before anything is generated."""

from __future__ import annotations

import re
from dataclasses import dataclass

from creative_forge.spec import AdSpec, Concept


@dataclass(frozen=True)
class LintIssue:
    concept: str
    rule: str
    message: str


_HAND = re.compile(r"\b(hand|hands|finger|fingers|tool|nozzle|lance|torch|blower|brush|knife)\b", re.I)
_EDGE = re.compile(r"\b(enter|enters|entering|from)\b.*\b(edge|corner|side|bottom|top|left|right)\b", re.I)
_NO_TEXT = re.compile(r"\b(no|not|without|don'?t|do not)\b[^.]*\btext\b", re.I)
_COVER_ALL = re.compile(r"\b(cover|bury|coat)\b[^.]*\b(entire|whole|everything|all)\b", re.I)
_EFFECT_WORDS = re.compile(r"\b(dust|mud|ice|frost|snow|grime|dirt)\b", re.I)


def lint_concept(spec: AdSpec, c: Concept) -> list[LintIssue]:
    issues: list[LintIssue] = []
    h = c.hook
    ip = h.image_prompt or ""
    if ip and _HAND.search(ip) and not _EDGE.search(ip):
        issues.append(
            LintIssue(
                c.id,
                "hand-enters-from-edge",
                "the image prompt adds a hand/tool but does not say it enters from an edge or corner; "
                "a hand that covers UI makes Veo invent fake UI behind it",
            )
        )
    if ip and not _NO_TEXT.search(ip):
        issues.append(LintIssue(c.id, "no-new-text", "the image prompt should say not to add any text"))
    if ip and _COVER_ALL.search(ip) and _EFFECT_WORDS.search(ip):
        issues.append(
            LintIssue(
                c.id,
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
                c.id,
                "tail-drift",
                f"cut at {cut:.2f}s keeps Veo's last second, where stylized characters drift photoreal",
            )
        )
    elif cut > h.duration_s - 3.0 and not h.patches:
        issues.append(
            LintIssue(
                c.id,
                "tail-ui",
                f"cut at {cut:.2f}s is inside the last 3 s, where Veo often invents UI; review the "
                "frames, cut mid-transition, or add hook.patches",
            )
        )
    if not h.text_regions:
        issues.append(
            LintIssue(
                c.id,
                "strip-text",
                "no hook.text_regions: any text visible in the first frame will be copied and garbled "
                "by Veo; list the text boxes of the real frame",
            )
        )
    return issues


def lint(spec: AdSpec) -> list[LintIssue]:
    return [i for c in spec.concepts for i in lint_concept(spec, c)]
