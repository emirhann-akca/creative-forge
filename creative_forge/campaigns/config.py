"""The campaign YAML: what to promote, with which approved creatives, on which networks, under which caps.

Budgets are in the ad account's currency (``currency``; the adapters refuse an account whose currency differs).
``limits`` is required: a campaign whose daily budget or total cap exceeds it is refused at plan time, again at create
time and again at activation, so a typo in ``budget`` cannot become spend.

The schedule is derived from the caps: the campaign runs ``floor(total_cap / daily)`` days from ``start`` and the
networks are given that end date (plus Meta's ``spend_cap`` and TikTok's total campaign budget where they apply), so
the networks themselves stop a campaign at roughly its total cap even if nobody runs ``forge campaign sync``.
"""

from __future__ import annotations

import math
import re
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, field_validator, model_validator

from creative_forge.campaigns.store_url import StoreListing, StoreUrlError, parse_store_url

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
API_NETWORKS = ("meta", "tiktok", "google")
MANUAL_NETWORKS = ("applovin", "unity", "mintegral")
MODES = ("mock", "sandbox", "live")
FORMATS = ("9:16", "4:5")
# Meta stores budgets in the currency's smallest unit; these currencies have no minor unit there (offset 1).
ZERO_DECIMAL = frozenset({"CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND"})


class CampaignConfigError(ValueError):
    """The campaign YAML is not acceptable; nothing was planned or sent."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Budget(_Strict):
    daily: float = Field(gt=0)
    total_cap: float = Field(gt=0)
    start: date | None = None  # default: tomorrow, so nothing can serve on the day it is created


class Limits(_Strict):
    max_daily: float = Field(gt=0)
    max_total: float = Field(gt=0)


class Automation(_Strict):
    auto_pause: bool = False  # kill verdicts and a reached cap pause by themselves only when this is true


class _Network(_Strict):
    mode: Literal["mock", "sandbox", "live"] = "mock"
    formats: list[Literal["9:16", "4:5"]] | None = None  # overrides the campaign's formats for this network


class MetaNetwork(_Network):
    ad_account: str  # act_<digits>
    page_id: str
    app_id: str  # the app's Meta app id (promoted_object.application_id)
    message: str = ""
    instagram_user_id: str | None = None

    @field_validator("ad_account")
    @classmethod
    def _act(cls, v: str) -> str:
        v = v.strip()
        v = v if v.startswith("act_") else f"act_{v}"
        if not v[4:].isdigit():
            raise ValueError("ad_account must be act_<digits>")
        return v


class TikTokNetwork(_Network):
    advertiser_id: str
    app_id: str  # the app's id in TikTok's App Management (not the store id)
    location_ids: list[str] = Field(min_length=1)
    identity_type: str = "CUSTOMIZED_USER"
    identity_id: str
    ad_text: str = Field(min_length=1, max_length=100)
    call_to_action: str = "DOWNLOAD_NOW"


class GoogleNetwork(_Network):
    customer_id: str
    login_customer_id: str | None = None
    headlines: list[str] = Field(min_length=1, max_length=5)
    descriptions: list[str] = Field(min_length=1, max_length=5)
    location_ids: list[str] = Field(min_length=1)  # geoTargetConstants ids (2792 = Turkey)
    language_ids: list[str] = []  # languageConstants ids (1037 = Turkish)
    target_install_cost: float | None = Field(default=None, gt=0)

    @field_validator("customer_id", "login_customer_id")
    @classmethod
    def _cid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = v.replace("-", "").strip()
        if not re.fullmatch(r"\d{10}", v):
            raise ValueError("a Google Ads customer id has 10 digits")
        return v

    @field_validator("headlines")
    @classmethod
    def _headlines(cls, v: list[str]) -> list[str]:
        if any(not 0 < len(h) <= 30 for h in v):
            raise ValueError("App ad headlines are 1-30 characters")
        return v

    @field_validator("descriptions")
    @classmethod
    def _descriptions(cls, v: list[str]) -> list[str]:
        if any(not 0 < len(d) <= 90 for d in v):
            raise ValueError("App ad descriptions are 1-90 characters")
        return v


class ManualNetwork(_Strict):
    mode: Literal["manual"] = "manual"
    formats: list[Literal["9:16", "4:5"]] | None = None


class Networks(_Strict):
    meta: MetaNetwork | None = None
    tiktok: TikTokNetwork | None = None
    google: GoogleNetwork | None = None
    applovin: ManualNetwork | None = None
    unity: ManualNetwork | None = None
    mintegral: ManualNetwork | None = None


class CampaignConfig(_Strict):
    version: Literal[1] = 1
    name: str
    creatives: str  # a `forge publish` folder (manifest.json), relative to this file
    platform: Literal["android", "ios"]
    store_url: str
    countries: list[str] = Field(min_length=1)  # ISO 3166-1 alpha-2
    currency: str = "USD"
    budget: Budget
    limits: Limits
    automation: Automation = Automation()
    formats: list[Literal["9:16", "4:5"]] = ["9:16"]
    control: str | None = None  # creative id, ad name or concept of the test cell's control (default: first)
    test_rule: dict[str, Any] | None = None  # perf.cells.Rule fields
    networks: Networks
    _base: Path = PrivateAttr(default=Path("."))

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not NAME_RE.fullmatch(v):
            raise ValueError("name is 2-41 characters of a-z, 0-9 and '-'")
        return v

    @field_validator("countries")
    @classmethod
    def _countries(cls, v: list[str]) -> list[str]:
        out = [c.strip().upper() for c in v]
        if any(not re.fullmatch(r"[A-Z]{2}", c) for c in out):
            raise ValueError("countries are ISO 3166-1 alpha-2 codes (TR, US, DE)")
        return out

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        if not re.fullmatch(r"[A-Z]{3}", v):
            raise ValueError("currency is an ISO 4217 code (USD, EUR, TRY)")
        return v

    @model_validator(mode="after")
    def _caps(self) -> CampaignConfig:
        b, lim = self.budget, self.limits
        problems = []
        if b.daily > lim.max_daily:
            problems.append(f"budget.daily {b.daily:g} is above limits.max_daily {lim.max_daily:g}")
        if b.total_cap > lim.max_total:
            problems.append(f"budget.total_cap {b.total_cap:g} is above limits.max_total {lim.max_total:g}")
        if b.total_cap < b.daily:
            problems.append("budget.total_cap is below one day of budget.daily")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    # ------------------------------------------------------------------------------------------------- derived
    @property
    def base_dir(self) -> Path:
        return self._base

    @property
    def creatives_dir(self) -> Path:
        p = Path(self.creatives)
        return p if p.is_absolute() else (self._base / p)

    def listing(self) -> StoreListing:
        listing = parse_store_url(self.store_url)
        if listing.platform != self.platform:
            raise StoreUrlError(f"platform is {self.platform} but {self.store_url!r} is a {listing.platform} listing")
        return listing

    def start_date(self, today: date | None = None) -> date:
        return self.budget.start or ((today or date.today()) + timedelta(days=1))

    @property
    def days(self) -> int:
        return max(1, math.floor(self.budget.total_cap / self.budget.daily + 1e-9))

    def end_date(self, today: date | None = None) -> date:
        """The last day the campaign may serve: ``days`` days from the start, inclusive."""
        return self.start_date(today) + timedelta(days=self.days - 1)

    def minor(self, amount: float) -> int:
        """An amount in the currency's smallest unit (Meta budgets)."""
        return round(amount) if self.currency in ZERO_DECIMAL else round(amount * 100)

    def api_networks(self) -> list[tuple[str, Any]]:
        return [(n, getattr(self.networks, n)) for n in API_NETWORKS if getattr(self.networks, n) is not None]

    def manual_networks(self) -> list[str]:
        return [n for n in MANUAL_NETWORKS if getattr(self.networks, n) is not None]

    def formats_for(self, network: str) -> list[str]:
        net = getattr(self.networks, network, None)
        return list((net.formats if net is not None and net.formats else None) or self.formats)


def parse_config(data: dict[str, Any], base: Path) -> CampaignConfig:
    try:
        cfg = CampaignConfig.model_validate(data)
    except ValidationError as e:
        lines = []
        for err in e.errors():
            where = ".".join(str(x) for x in err["loc"]) or "config"
            lines.append(f"{where}: {err['msg']}")
        raise CampaignConfigError("; ".join(lines)) from None
    cfg._base = Path(base)
    return cfg


def load_config(path: Path | str) -> CampaignConfig:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CampaignConfigError(f"campaign config not found: {path}") from None
    except yaml.YAMLError as e:
        raise CampaignConfigError(f"{path}: not valid YAML ({e})") from None
    if not isinstance(data, dict):
        raise CampaignConfigError(f"{path}: expected a mapping at the top level")
    return parse_config(data, path.resolve().parent)
