"""Every generation goes through here: cache lookup -> budget reservation -> provider call -> cache -> ledger."""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from creative_forge.cache import GenerationCache, content_key
from creative_forge.ledger import BudgetGuard, CostLedger, LedgerEntry
from creative_forge.pricing import Pricing
from creative_forge.providers.base import (
    ImageEditRequest,
    JudgeRequest,
    MusicRequest,
    Provider,
    ProviderResult,
    ProviderSet,
    VideoRequest,
)


class Generator:
    def __init__(
        self,
        providers: ProviderSet,
        cache: GenerationCache,
        ledger: CostLedger,
        guard: BudgetGuard,
        pricing: Pricing,
        scratch: Path,
    ) -> None:
        self.providers = providers
        self.cache = cache
        self.ledger = ledger
        self.guard = guard
        self.pricing = pricing
        self.scratch = Path(scratch)
        self.scratch.mkdir(parents=True, exist_ok=True)

    def _call(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        provider: Provider,
        list_usd: float | None,
        detail: str,
        concept: str,
        model: str,
        call: Callable[[Path], ProviderResult],
    ) -> list[Path]:
        key = content_key(f"{kind}:{provider.name}", payload)
        hit = self.cache.get(kind, key)
        if hit is not None:
            self.ledger.record(
                LedgerEntry(
                    concept,
                    kind,
                    provider.name,
                    model,
                    detail + " (cache hit)",
                    0.0,
                    list_usd,
                    True,
                    provider.billable,
                    key,
                )
            )
            return hit
        reserved = self.guard.reserve(list_usd if provider.billable else 0.0, f"{kind} for {concept}")
        try:
            with tempfile.TemporaryDirectory(prefix=f"{kind}-", dir=self.scratch) as td:
                result = call(Path(td))
                files = self.cache.put(
                    kind,
                    key,
                    result.files,
                    {"provider": provider.name, "model": model, "detail": detail, "info": result.info},
                )
            usd = (list_usd or 0.0) if provider.billable else 0.0
            self.ledger.record(
                LedgerEntry(concept, kind, provider.name, model, detail, usd, list_usd, False, provider.billable, key)
            )
            return files
        finally:
            self.guard.release(reserved)

    def image_edit(self, req: ImageEditRequest, concept: str) -> Path:
        p = self.providers.image_edit
        files = self._call(
            kind="image_edit",
            payload=req.cache_payload(),
            provider=p,
            list_usd=self.pricing.image(1),
            detail="1 image edit",
            concept=concept,
            model=req.model,
            call=lambda d: p.edit(req, d),
        )
        return files[0]

    def video(self, req: VideoRequest, concept: str) -> list[Path]:
        p = self.providers.video
        key = content_key(f"video:{p.name}", req.cache_payload())
        pending = self.cache.pending("video", key)

        def call(d: Path) -> ProviderResult:
            return p.generate(
                req,
                d,
                operation=(pending or {}).get("operation"),
                on_submitted=lambda op: self.cache.set_pending("video", key, {"operation": op}),
            )

        detail = f"{req.sample_count} x {req.duration_s}s {'with' if req.generate_audio else 'no'} audio"
        if pending:
            detail += " (resumed operation)"
        return self._call(
            kind="video",
            payload=req.cache_payload(),
            provider=p,
            list_usd=self.pricing.video(req.duration_s, req.sample_count, req.generate_audio),
            detail=detail,
            concept=concept,
            model=req.model,
            call=call,
        )

    def music(self, req: MusicRequest, concept: str) -> Path:
        p = self.providers.music
        files = self._call(
            kind="music",
            payload=req.cache_payload(),
            provider=p,
            list_usd=self.pricing.music(1),
            detail="1 track",
            concept=concept,
            model=req.model,
            call=lambda d: p.compose(req, d),
        )
        return files[0]

    def judge(self, req: JudgeRequest, concept: str) -> dict[str, Any]:
        """Judge runs are never cached: repeated runs are the point (single runs are noise)."""
        p = self.providers.judge
        price = self.pricing.judge(1)
        reserved = self.guard.reserve(price if p.billable else 0.0, f"judge run for {concept}")
        try:
            result = p.score(req)
        finally:
            self.guard.release(reserved)
        usd = (price or 0.0) if p.billable else 0.0
        self.ledger.record(
            LedgerEntry(
                concept,
                "judge",
                p.name,
                req.model,
                f"blind run {req.run_index + 1}" + ("" if price is not None else " (unpriced)"),
                usd,
                price,
                False,
                p.billable,
                "",
            )
        )
        return result
