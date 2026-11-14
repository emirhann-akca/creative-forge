"""AppLovin, Unity Ads and Mintegral: a manual upload pack, no API calls.

Each of the three documents a campaign API (read 2026-11-14), but none documents that a campaign is created paused,
and "everything is created PAUSED" is the rule this module exists to keep:

* AppLovin (Axon Campaign Management API, ``api.ads.axon.ai/manage/v1``): ``/campaign/create`` lists ``status`` as
  "Ignored" on create and does not say what a new campaign's status is. Creative sets can be created ``PAUSED``.
* Unity Ads (Advertiser Public API, ``services.api.unity.com/ads/advertiser/public/v1``): ``CreateCampaignDto`` has no
  ``paused`` field; ``paused`` exists only on the update call, and the initial state is not stated.
* Mintegral (Open API): offers are created under an existing campaign with a daily budget; no paused state at
  creation is documented.

So ``forge campaign create`` writes a pack per network instead: the approved files (re-checked by hash), a
``pack.json`` with names, store URL, budget, caps and schedule, and the steps to create the campaign paused by hand.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from creative_forge.campaigns.creatives import Creative
from creative_forge.campaigns.plan import ManualPlan, Plan
from creative_forge.campaigns.versions import MANUAL_DOCS, READ_ON

REASONS = {
    "applovin": "AppLovin's Campaign Management API marks `status` as ignored on /campaign/create and does not say a "
    "new campaign starts paused; forge does not call it.",
    "unity": "Unity's Advertiser Public API has no paused field on campaign create (only on update) and does not "
    "state the initial state; forge does not call it.",
    "mintegral": "Mintegral's Open API creates offers with a daily budget under an existing campaign; no paused "
    "state at creation is documented; forge does not call it.",
}
LABELS = {"applovin": "AppLovin (Axon Ads Manager)", "unity": "Unity Ads (User Acquisition)", "mintegral": "Mintegral"}
STEPS_TR = [
    "Ağın panelinde yeni bir uygulama yükleme kampanyası aç ve DURAKLATILMIŞ olarak kaydet.",
    "Kampanya adını pack.json'daki `campaign_name` ile birebir aynı yaz (performans verisi bu adla eşleşir).",
    "Mağaza bağlantısını pack.json'daki `store_url` ile aynı gir; günlük bütçe ve bitiş tarihini oradan al.",
    "Bu klasördeki videoları yükle; dosya adları reklam adıdır, değiştirme.",
    "Yayına almadan önce bütçeyi ve tarihleri bir kez daha kontrol et; yayına almak ayrı ve bilinçli bir adımdır.",
]
STEPS_EN = [
    "Create a new app-install campaign in the network's dashboard and save it PAUSED.",
    "Name it exactly `campaign_name` from pack.json (performance data maps back by that name).",
    "Use `store_url`, the daily budget and the end date from pack.json.",
    "Upload the videos in this folder; their file names are the ad names, keep them.",
    "Check budget and dates once more before switching it on; activation is a separate, deliberate step.",
]


def manual_plan(network: str, creatives: list[Creative]) -> ManualPlan:
    return ManualPlan(network, creatives, MANUAL_DOCS[network], REASONS[network])


def write_packs(plan: Plan, out_dir: Path) -> list[Path]:
    cfg = plan.cfg
    written = []
    for m in plan.manual:
        folder = Path(out_dir) / plan.plan_id / m.network
        folder.mkdir(parents=True, exist_ok=True)
        files = []
        for c in m.creatives:
            target = folder / c.path.name
            if not target.exists():
                shutil.copy2(c.path, target)
            files.append(
                {
                    "file": c.path.name,
                    "ad_name": c.ad_name,
                    "format": c.format,
                    "sha256": c.sha256,
                    "reviewer": c.reviewer,
                }
            )
        pack: dict[str, Any] = {
            "network": m.network,
            "label": LABELS[m.network],
            "campaign_name": f"{cfg.name} {m.network} [cf:{plan.plan_id}]",
            "status_at_creation": "PAUSED",
            "store_url": plan.listing.url,
            "platform": plan.listing.platform,
            "countries": cfg.countries,
            "currency": cfg.currency,
            "daily_budget": cfg.budget.daily,
            "total_cap": cfg.budget.total_cap,
            "start": plan.start.isoformat(),
            "end": plan.end.isoformat(),
            "files": files,
            "why_manual": m.reason,
            "api_doc": m.doc,
            "api_doc_read_on": READ_ON,
            "steps_tr": STEPS_TR,
            "steps_en": STEPS_EN,
        }
        (folder / "pack.json").write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(folder)
    return written
