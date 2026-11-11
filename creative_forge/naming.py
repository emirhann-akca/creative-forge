"""Deterministic creative IDs and an ad-name format that round-trips.

One export is one creative: (spec, concept, hook, gameplay segment, end card, format, language). Its ID is a hash of
those seven names, so a re-render of the same variant keeps its ID, and its ad name carries all seven plus the ID:

    cf1.<spec>.<concept>.<hook>.<segment>.<end_card>.<format>.<language>.<creative_id>
    cf1.puzzle-ads.dust-blower.blower.lvl21.fill.9x16.tr.91c97d00dc

``parse(name)`` gives the seven names back and checks the ID against them, so a hand-edited or truncated name is
rejected instead of being booked against the wrong creative. ``find(text)`` locates such a name inside a longer
string, because ad networks append to names ("... - Copy", " (2)"). The separator is ".", which no component may
contain: spec, concept and variant ids are lowercase a-z, 0-9, "-" and "_"; formats are "9x16" / "4x5"; languages are
BCP 47-style lowercase tags ("en", "tr", "pt-br").

The file name of an export is its ad name, so whatever the uploader keeps (file name or ad name) maps back.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

PREFIX = "cf1"
ID_LEN = 10
_COMPONENT = r"[a-z0-9][a-z0-9_-]{0,63}"
_FORMAT = r"9x16|4x5"
_LANG = r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?"
_NAME = (
    rf"{PREFIX}\.(?P<spec>{_COMPONENT})\.(?P<concept>{_COMPONENT})\.(?P<hook>{_COMPONENT})\.(?P<segment>{_COMPONENT})"
    rf"\.(?P<end_card>{_COMPONENT})\.(?P<format>{_FORMAT})\.(?P<language>{_LANG})\.(?P<creative_id>[0-9a-f]{{{ID_LEN}}})"
)
NAME_RE = re.compile(rf"^{_NAME}$")
FIND_LOOSE = re.compile(_NAME, re.IGNORECASE)
LANG_RE = re.compile(rf"^{_LANG}$")


class AdNameError(ValueError):
    """Not a creative-forge ad name, or its creative ID does not match its components."""


def format_token(fmt: str) -> str:
    """ "9:16" -> "9x16" (names and file names cannot hold ":")."""
    return fmt.replace(":", "x")


def format_ratio(token: str) -> str:
    return token.replace("x", ":")


@dataclass(frozen=True)
class CreativeIds:
    spec: str
    concept: str
    hook: str
    segment: str
    end_card: str
    format: str  # "9x16" or "4x5"
    language: str

    def __post_init__(self) -> None:
        for field_name in ("spec", "concept", "hook", "segment", "end_card"):
            if not re.fullmatch(_COMPONENT, getattr(self, field_name)):
                raise AdNameError(f"{field_name} {getattr(self, field_name)!r} is not a valid name component")
        if not re.fullmatch(_FORMAT, self.format):
            raise AdNameError(f"format {self.format!r} must be 9x16 or 4x5")
        if not LANG_RE.fullmatch(self.language):
            raise AdNameError(f"language {self.language!r} is not a lowercase language tag")

    @property
    def components(self) -> tuple[str, ...]:
        return (self.spec, self.concept, self.hook, self.segment, self.end_card, self.format, self.language)

    @property
    def creative_id(self) -> str:
        return hashlib.sha256("|".join((PREFIX, *self.components)).encode()).hexdigest()[:ID_LEN]

    @property
    def ad_name(self) -> str:
        return ".".join((PREFIX, *self.components, self.creative_id))

    @property
    def variant(self) -> str:
        """The format-independent part (one render, before the 9:16 / 4:5 exports)."""
        return ".".join((self.spec, self.concept, self.hook, self.segment, self.end_card, self.language))

    def to_dict(self) -> dict[str, str]:
        return asdict(self) | {"creative_id": self.creative_id, "ad_name": self.ad_name}


def parse(name: str) -> CreativeIds:
    m = NAME_RE.match(name.strip().lower())
    if not m:
        raise AdNameError(f"not a creative-forge ad name: {name!r}")
    g = m.groupdict()
    ids = CreativeIds(**{k: v for k, v in g.items() if k != "creative_id"})
    if ids.creative_id != g["creative_id"]:
        raise AdNameError(f"{name!r}: creative id {g['creative_id']} does not match its components ({ids.creative_id})")
    return ids


def find(text: str) -> CreativeIds | None:
    """The first valid creative-forge ad name inside ``text`` (a network ad name, a file name, a creative set)."""
    for m in FIND_LOOSE.finditer(text):
        try:
            return parse(m.group(0))
        except AdNameError:
            continue
    return None
