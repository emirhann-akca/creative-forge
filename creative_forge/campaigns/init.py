"""A starter campaign config: what the Kampanya page's form and ``forge campaign init`` write.

The API networks get mock accounts (``mode: mock``: the in-process mock network, no request leaves the machine), the
others ``mode: manual`` (an upload pack). Real ids and ``mode: sandbox`` / ``live`` are a person's edit of the file;
nothing here can make a config that spends.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml

STARTER_NETWORKS: dict[str, Any] = {
    "meta": {"mode": "mock", "ad_account": "act_100200300", "page_id": "100000000000001",
             "app_id": "200000000000002", "message": ""},
    "tiktok": {"mode": "mock", "advertiser_id": "7000000000000000001", "app_id": "7100000000000000001",
               "location_ids": ["298795"], "identity_id": "identity-1", "ad_text": "Play now"},
    "google": {"mode": "mock", "customer_id": "1234567890", "headlines": ["Play now"],
               "descriptions": ["A short, calm puzzle."], "location_ids": ["2792"], "language_ids": ["1037"]},
    "applovin": {"mode": "manual"},
    "unity": {"mode": "manual"},
    "mintegral": {"mode": "manual"},
}  # fmt: skip
HEADER = "# forge campaign config (starter): mock mode. Put real ids here and set mode to go further.\n"


def starter(
    *,
    name: str,
    creatives: str,
    store_url: str,
    daily: float,
    total_cap: float,
    max_daily: float,
    max_total: float,
    countries: list[str],
    networks: list[str],
    app: str | None = None,
    formats: list[str] | None = None,
) -> dict[str, Any]:
    from creative_forge.campaigns.store_url import parse_store_url

    listing = parse_store_url(store_url)
    unknown = [n for n in networks if n not in STARTER_NETWORKS]
    if unknown or not networks:
        raise ValueError(f"networks: pick from {', '.join(STARTER_NETWORKS)}")
    data: dict[str, Any] = {
        "version": 1,
        "name": name,
        "creatives": creatives,
        "platform": listing.platform,
        "store_url": store_url,
        "countries": [c.strip().upper() for c in countries if c.strip()],
        "currency": "USD",
        "budget": {"daily": daily, "total_cap": total_cap},
        "limits": {"max_daily": max_daily, "max_total": max_total},
        "automation": {"auto_pause": False},
        "formats": formats or ["9:16"],
        "networks": {n: dict(STARTER_NETWORKS[n]) for n in dict.fromkeys(networks)},
    }
    if app:
        data["app"] = app
    return data


def write_starter(out: Path | None, *, root: Path, name: str, creatives: Path, **kw: Any) -> Path:
    """Validate and write; refuses an existing file (a config may hold real ids a person put in)."""
    from creative_forge.campaigns.config import parse_config

    folder = (Path(out).parent if out else Path(root) / "campaigns").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    src = Path(creatives).resolve()
    if not (src / "manifest.json").is_file():
        raise ValueError(f"{src} is not a `forge publish` folder (no manifest.json)")
    try:
        rel = src.relative_to(folder.parent).as_posix()
        ref = f"../{rel}"
    except ValueError:
        ref = str(src)
    data = starter(name=name, creatives=ref, **kw)
    parse_config(data, folder)
    target = Path(out).resolve() if out else folder / f"{name}.yaml"
    n = 2
    while target.exists():
        if out:
            raise ValueError(f"{target} exists; pick another --out")
        target = folder / f"{name}-{n}.yaml"
        n += 1
    target.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return target


def default_name(prefix: str = "kampanya") -> str:
    return f"{prefix}-{date.today().strftime('%Y%m%d')}"
