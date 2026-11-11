"""Tag taxonomy: a controlled vocabulary for creatives, checked at plan time and written into every export.

Concepts (and hook variants) carry hand-set tags such as ``hook_type`` and ``emotion``; the pipeline adds the derived
ones (``format``, ``length_bucket``, ``ai_generated``) per export. Every tag goes into the export's ``.forge.json``
sidecar, ``report.json`` and the publish manifest, which is how ``forge perf report`` groups performance by tag.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

TagValue = str | list[str]


@dataclass(frozen=True)
class Dimension:
    name: str
    values: tuple[str, ...]
    multi: bool = False
    derived: bool = False
    description: str = ""


@dataclass(frozen=True)
class Taxonomy:
    dimensions: dict[str, Dimension]
    source: str

    @classmethod
    def load(cls, path: str | Path | None = None) -> Taxonomy:
        return _load(str(Path(path).resolve()) if path else None)

    @property
    def derived(self) -> list[str]:
        return [d.name for d in self.dimensions.values() if d.derived]

    def problems(self, tags: dict[str, Any], where: str) -> list[tuple[str, str]]:
        """``(rule, message)`` for every unknown dimension, unknown value, wrong arity or hand-set derived tag."""
        out: list[tuple[str, str]] = []
        for dim, value in tags.items():
            d = self.dimensions.get(dim)
            if d is None:
                out.append(
                    ("unknown-tag", f"{where}: unknown tag dimension {dim!r} (known: {', '.join(self.dimensions)})")
                )
                continue
            if d.derived:
                out.append(("derived-tag", f"{where}: {dim!r} is derived per export; do not set it in the spec"))
                continue
            values = value if isinstance(value, list) else [value]
            if isinstance(value, list) and not d.multi:
                out.append(("unknown-tag", f"{where}: {dim!r} takes one value, not a list"))
            for v in values:
                if not isinstance(v, str) or v not in d.values:
                    out.append(
                        ("unknown-tag", f"{where}: {dim}={v!r} is not in the taxonomy (allowed: {', '.join(d.values)})")
                    )
        return out


@functools.lru_cache(maxsize=8)
def _load(path: str | None) -> Taxonomy:
    if path is None:
        text = resources.files("creative_forge.data").joinpath("tag_taxonomy.yaml").read_text(encoding="utf-8")
        source = "bundled tag_taxonomy.yaml"
    else:
        text = Path(path).read_text(encoding="utf-8")
        source = path
    d = yaml.safe_load(text)
    dims = {}
    for name, spec in (d.get("dimensions") or {}).items():
        dims[name] = Dimension(
            name,
            tuple(str(v) for v in spec.get("values", [])),
            bool(spec.get("multi", False)),
            bool(spec.get("derived", False)),
            spec.get("description", ""),
        )
    return Taxonomy(dims, source)


def length_bucket(seconds: float) -> str:
    for upper, name in ((15.5, "s00-15"), (30.5, "s16-30"), (45.5, "s31-45"), (60.5, "s46-60")):
        if seconds < upper:
            return name
    return "s61plus"


def export_tags(tags: dict[str, TagValue], fmt_token: str, total_s: float, ai_generated: bool = True) -> dict[str, Any]:
    """Hand-set tags plus the derived ones, in a stable order."""
    out: dict[str, Any] = {k: tags[k] for k in sorted(tags)}
    out["format"] = fmt_token
    out["length_bucket"] = length_bucket(total_s)
    out["ai_generated"] = "yes" if ai_generated else "no"
    return out


def flatten(tags: dict[str, Any]) -> list[tuple[str, str]]:
    """``{"emotion": ["calm", "humor"]}`` -> ``[("emotion", "calm"), ("emotion", "humor")]``."""
    out = []
    for k, v in tags.items():
        for x in v if isinstance(v, list) else [v]:
            out.append((k, str(x)))
    return out
