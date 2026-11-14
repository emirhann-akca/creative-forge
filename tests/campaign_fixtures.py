"""Synthetic inputs for the campaign tests: a `forge publish` folder and a campaign config (no real app or account)."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from creative_forge.naming import CreativeIds

STORE = "https://play.google.com/store/apps/details?id=com.forgetest.sortgame"


def ids(concept: str, fmt: str = "9x16") -> CreativeIds:
    return CreativeIds("spring", concept, "main", "main", "main", fmt, "en")


def deliver(folder: Path, concepts: tuple[str, ...] = ("alpha", "beta", "gamma"), fmt: str = "9x16") -> Path:
    """A publish folder: one small fake .mp4 per concept, approved by a reviewer, QA pass."""
    folder.mkdir(parents=True, exist_ok=True)
    exports = []
    for c in concepts:
        cid = ids(c, fmt)
        f = folder / f"{cid.ad_name}.mp4"
        f.write_bytes(cid.creative_id.encode() * 64)
        exports.append(
            {
                "file": f.name,
                "creative_id": cid.creative_id,
                "ad_name": cid.ad_name,
                "sha256": hashlib.sha256(f.read_bytes()).hexdigest(),
                "qa_status": "pass",
                "failed_checks": [],
                "overridden": False,
                "reviewer": "reviewer-a",
                "format": fmt.replace("x", ":"),
            }
        )
    (folder / "manifest.json").write_text(json.dumps({"run": "spring", "exports": exports}), encoding="utf-8")
    return folder


BASE: dict[str, Any] = {
    "version": 1,
    "name": "spring-test",
    "creatives": "deliver",
    "platform": "android",
    "store_url": STORE,
    "countries": ["TR"],
    "budget": {"daily": 40, "total_cap": 400, "start": "2026-11-15"},
    "limits": {"max_daily": 50, "max_total": 500},
    "networks": {
        "meta": {"ad_account": "act_1001", "page_id": "2002", "app_id": "3003", "message": "Sort it."},
        "tiktok": {"advertiser_id": "7001", "app_id": "7002", "location_ids": ["298795"], "identity_id": "idn-1",
                   "ad_text": "Sort it."},
        "google": {"customer_id": "1234567890", "headlines": ["Sort it"], "descriptions": ["A calm sorting puzzle."],
                   "location_ids": ["2792"], "language_ids": ["1037"]},
    },
}  # fmt: skip


def config(**changes: Any) -> dict[str, Any]:
    d = copy.deepcopy(BASE)
    for k, v in changes.items():
        d[k] = v
    return d
