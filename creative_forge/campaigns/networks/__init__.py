"""Network adapters: Meta, TikTok and Google Ads write through their APIs; AppLovin, Unity and Mintegral get a
manual upload pack (see ``manual.py`` for why)."""

from __future__ import annotations

from creative_forge.campaigns.networks.base import Adapter, SafetyError
from creative_forge.campaigns.networks.manual import manual_plan, write_packs


def adapter_class(network: str) -> type[Adapter]:
    from creative_forge.campaigns.networks.google import GoogleAdsAdapter
    from creative_forge.campaigns.networks.meta import MetaAdapter
    from creative_forge.campaigns.networks.tiktok import TikTokAdapter

    classes: dict[str, type[Adapter]] = {"meta": MetaAdapter, "tiktok": TikTokAdapter, "google": GoogleAdsAdapter}
    if network not in classes:
        raise ValueError(f"no API adapter for {network!r}")
    return classes[network]


__all__ = ["Adapter", "SafetyError", "adapter_class", "manual_plan", "write_packs"]
