"""Model licence guard: which models a spec would call, and whether their licences allow paid ad production.

``data/model_licenses.yaml`` lists every model the tool can call, with its code and weights licences, a commercial
status, conditions, the source URL and the date the source was read. ``check()`` resolves every model a spec uses
against it:

* ``refuse``: non-commercial weights, a territory clause the spec's regions break, or a revenue cap the spec's
  declared revenue reaches. ``forge plan`` exits 1 and ``forge run`` stops before any call, naming the reason.
* ``warn``: a conditional licence (revenue cap, registration, territory that the regions respect), or a model the
  registry does not know.
* ``ok`` / ``service``: open weights that allow it, or a hosted API governed by its service terms.

A refusal can be overridden only per model, with a reason (``--license-override MODEL=REASON``). The override is
written into the run's cost ledger next to the refused licence, so the decision is on record with the outputs.
"""

from __future__ import annotations

import fnmatch
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from creative_forge.spec import AdSpec

REFUSE, WARN, OK, SERVICE, OVERRIDDEN = "refuse", "warn", "ok", "service", "overridden"
# the 27 member states
EU = frozenset(
    {"AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "IE", "IT"}
    | {"LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}
)
_ALIASES = {"GB": "UK", "EL": "GR"}


class LicenseRefused(ValueError):
    """A model the spec would call is not licensed for paid ad production. Nothing was generated."""


@dataclass(frozen=True)
class LicenseEntry:
    id: str
    kind: str
    match: tuple[str, ...]
    code_license: str | None
    weights_license: str | None
    commercial: str
    source: str
    checked: str | None
    note: str = ""
    conditions: tuple[dict[str, Any], ...] = ()

    def matches(self, model: str) -> bool:
        m = model.lower()
        return any(fnmatch.fnmatchcase(m, pat) for pat in self.match)


@dataclass(frozen=True)
class Registry:
    as_of: str
    entries: tuple[LicenseEntry, ...]

    @classmethod
    def load(cls, path: str | Path | None = None) -> Registry:
        if path is None:
            text = resources.files("creative_forge.data").joinpath("model_licenses.yaml").read_text(encoding="utf-8")
        else:
            text = Path(path).read_text(encoding="utf-8")
        d = yaml.safe_load(text)
        entries = []
        for e in d["models"]:
            commercial = str(e["commercial"]).lower()
            if commercial not in ("yes", "no", "conditional", "service"):
                raise ValueError(f"model_licenses: {e['id']}: commercial must be yes/no/conditional/service")
            entries.append(
                LicenseEntry(
                    id=e["id"],
                    kind=e["kind"],
                    match=tuple(p.lower() for p in e["match"]),
                    code_license=e.get("code_license"),
                    weights_license=e.get("weights_license"),
                    commercial=commercial,
                    source=e["source"],
                    checked=None if e.get("checked") is None else str(e["checked"]),
                    note=e.get("note", ""),
                    conditions=tuple(e.get("conditions") or ()),
                )
            )
        return cls(str(d["as_of"]), tuple(entries))

    def lookup(self, model: str) -> LicenseEntry | None:
        return next((e for e in self.entries if e.matches(model)), None)


@dataclass(frozen=True)
class ModelUse:
    role: str  # image_edit, video, music, judge, ...
    model: str  # as written in the spec (Vertex model id or a ComfyUI file name)
    provider: str


@dataclass
class Finding:
    role: str
    model: str
    provider: str
    status: str
    reason: str
    registry_id: str | None = None
    weights_license: str | None = None
    source: str | None = None
    checked: str | None = None
    override_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LicenseReport:
    findings: list[Finding] = field(default_factory=list)
    registry_as_of: str = ""

    @property
    def refused(self) -> list[Finding]:
        return [f for f in self.findings if f.status == REFUSE]

    @property
    def overridden(self) -> list[Finding]:
        return [f for f in self.findings if f.status == OVERRIDDEN]

    def table(self) -> str:
        rows = [("role", "model", "licence", "status", "reason")]
        for f in self.findings:
            reason = f.reason + (f" [override: {f.override_reason}]" if f.override_reason else "")
            rows.append((f.role, f.model, f.weights_license or "-", f.status, reason))
        widths = [max(len(r[i]) for r in rows) for i in range(4)]
        out = []
        for r in rows:
            out.append("  ".join(c.ljust(w) for c, w in zip(r[:4], widths, strict=True)) + "  " + r[4])
        out.insert(1, "  ".join("-" * w for w in widths) + "  " + "-" * 6)
        out.append(f"licence registry as of {self.registry_as_of}; sources: `forge licenses`")
        return "\n".join(out)

    def raise_if_refused(self) -> None:
        if self.refused:
            raise LicenseRefused("; ".join(f"{f.role} model {f.model}: {f.reason}" for f in self.refused))


def models_in_use(spec: AdSpec) -> list[ModelUse]:
    """Every model a real run of this spec would call (a fake-provider spec is judged as the real run it rehearses)."""
    uses: list[ModelUse] = []
    real = "vertex"
    if any(h.image_prompt for c in spec.concepts for _hid, h in c.hook_variants()):
        if spec.image_provider == "comfyui":
            uses.append(ModelUse("image_edit", spec.comfyui.checkpoint, "comfyui"))
        else:
            uses.append(ModelUse("image_edit", spec.models.image_edit, real))
    if spec.video_provider == "comfyui":
        for role, name in spec.comfyui_video.model_files().items():
            uses.append(ModelUse(f"video ({role})", name, "comfyui"))
        if spec.comfyui_video.cloud_fallback:
            uses.append(ModelUse("video (cloud fallback)", spec.models.video, real))
    else:
        uses.append(ModelUse("video", spec.models.video, real))
    if any(c.music and c.music.prompt for c in spec.concepts):
        uses.append(ModelUse("music", spec.models.music, real))
    if spec.qa.judge_runs:
        uses.append(ModelUse("judge", spec.models.judge, real))
    return uses


def _regions(spec: AdSpec) -> set[str]:
    out: set[str] = set()
    for r in spec.targets.regions:
        r = _ALIASES.get(r.upper(), r.upper())
        out |= (EU | {"EU"}) if r in ("EU", "EEA") else {r}
        if r in EU:
            out.add("EU")
    return out


def _condition(spec: AdSpec, cond: dict[str, Any]) -> tuple[str, str]:
    kind = cond.get("type")
    if kind == "revenue_cap":
        cap = float(cond["max_annual_revenue_usd"])
        rev = spec.targets.annual_revenue_usd
        if rev is not None and rev >= cap:
            return REFUSE, f"needs a paid licence at ${cap:,.0f} annual revenue or more (targets: ${rev:,.0f})"
        declared = "not declared (targets.annual_revenue_usd)" if rev is None else f"${rev:,.0f} declared"
        return WARN, f"free only below ${cap:,.0f} annual revenue; revenue {declared}"
    if kind == "territory":
        excluded = {_ALIASES.get(x.upper(), x.upper()) for x in cond.get("excluded", [])}
        regions = _regions(spec)
        if not regions:
            return REFUSE, (
                f"licence excludes {', '.join(sorted(excluded))} and the spec declares no targets.regions, so the "
                "ads may serve there"
            )
        hit = sorted(regions & excluded)
        if hit:
            return REFUSE, f"licence excludes {', '.join(hit)}, which targets.regions includes"
        return WARN, f"outputs may not be used or displayed in {', '.join(sorted(excluded))}"
    if kind == "registration":
        return WARN, str(cond.get("text", "registration required for commercial use"))
    return WARN, f"unknown licence condition {kind!r}; review the source"


def evaluate(spec: AdSpec, use: ModelUse, registry: Registry) -> Finding:
    e = registry.lookup(use.model)
    if e is None:
        return Finding(
            use.role,
            use.model,
            use.provider,
            WARN,
            "not in the licence registry; check its weights licence before a paid run",
        )
    base = dict(registry_id=e.id, weights_license=e.weights_license, source=e.source, checked=e.checked)
    if e.commercial == "no":
        reason = f"non-commercial weights ({e.weights_license})"
        if e.note:
            reason += f": {e.note}"
        return Finding(use.role, use.model, use.provider, REFUSE, reason, **base)
    if e.commercial == "service":
        return Finding(use.role, use.model, use.provider, SERVICE, e.note or "hosted API", **base)
    if e.commercial == "conditional":
        results = [_condition(spec, c) for c in e.conditions] or [(WARN, "conditional licence; review the source")]
        status = REFUSE if any(s == REFUSE for s, _ in results) else WARN
        return Finding(use.role, use.model, use.provider, status, "; ".join(m for _, m in results), **base)
    return Finding(use.role, use.model, use.provider, OK, e.note or "commercial use allowed", **base)


def parse_overrides(items: list[str] | None) -> dict[str, str]:
    """``MODEL=REASON`` pairs from ``--license-override``; a reason is mandatory."""
    out: dict[str, str] = {}
    for item in items or []:
        model, sep, reason = item.partition("=")
        if not sep or not model.strip() or not reason.strip():
            raise ValueError(f"--license-override needs MODEL=REASON, got {item!r}")
        out[model.strip().lower()] = reason.strip()
    return out


def check(spec: AdSpec, overrides: dict[str, str] | None = None, registry: Registry | None = None) -> LicenseReport:
    registry = registry or Registry.load()
    overrides = {k.lower(): v for k, v in (overrides or {}).items()}
    report = LicenseReport(registry_as_of=registry.as_of)
    seen: set[tuple[str, str]] = set()
    for use in models_in_use(spec):
        if (use.role, use.model) in seen:
            continue
        seen.add((use.role, use.model))
        f = evaluate(spec, use, registry)
        if f.status == REFUSE:
            reason = overrides.get(use.model.lower())
            if reason is None and f.registry_id:
                reason = overrides.get(f.registry_id.lower())
            if reason:
                f.status = OVERRIDDEN
                f.override_reason = reason
        report.findings.append(f)
    return report


def override_records(report: LicenseReport, at: datetime | None = None) -> list[dict[str, Any]]:
    """What goes into the cost ledger for every overridden refusal."""
    stamp = (at or datetime.now(UTC)).isoformat(timespec="seconds")
    return [
        {
            "model": f.model,
            "role": f.role,
            "registry_id": f.registry_id,
            "refused_because": f.reason,
            "override_reason": f.override_reason,
            "source": f.source,
            "at": stamp,
        }
        for f in report.overridden
    ]


def describe(model: str, registry: Registry | None = None) -> dict[str, Any] | None:
    """Licence facts for a model name, for sidecars and the publish manifest."""
    e = (registry or Registry.load()).lookup(model)
    if e is None:
        return None
    return {
        "registry_id": e.id,
        "code_license": e.code_license,
        "weights_license": e.weights_license,
        "commercial": e.commercial,
        "source": e.source,
        "checked": e.checked,
    }
