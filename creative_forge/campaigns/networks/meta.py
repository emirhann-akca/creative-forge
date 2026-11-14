"""Meta Marketing API (v27.0): an Advantage+ app campaign, created PAUSED.

Since v25.0 an Advantage+ app campaign is no longer created with ``smart_promotion_type=SMART_APP_PROMOTION``; it is a
campaign whose three levers are on, which Meta then reports as ``advantage_state_info.advantage_state =
ADVANTAGE_PLUS_APP``:

* Advantage+ campaign budget: the daily budget sits on the campaign (``daily_budget``, ``bid_strategy``);
* Advantage+ audience: ``targeting.targeting_automation.advantage_audience = 1`` with only ``geo_locations`` set;
* Advantage+ placements: no placement fields at all (the API default).

Calls (``https://graph.facebook.com/v27.0``), all with ``status=PAUSED``:

1. ``POST /act_<id>/campaigns``: ``objective=OUTCOME_APP_PROMOTION``, ``special_ad_categories=[]`` (required),
   ``daily_budget`` in the currency's minor unit, ``spend_cap`` when the total cap is at least Meta's minimum;
2. ``POST /act_<id>/adsets``: ``optimization_goal=APP_INSTALLS``, ``billing_event=IMPRESSIONS``,
   ``promoted_object={application_id, object_store_url}``, ``start_time`` / ``end_time`` from the caps;
3. per creative ``POST /act_<id>/advideos`` (multipart ``source``), then ``GET /<video id>/thumbnails`` for the cover;
4. ``POST /act_<id>/adcreatives`` with ``object_story_spec.video_data`` and ``call_to_action INSTALL_MOBILE_APP``;
5. ``POST /act_<id>/ads`` with the creative and ``status=PAUSED``.

Meta has no idempotency parameter on these endpoints (``execution_options`` offers ``validate_only`` only), so the key
is part of every object's name and ``find`` searches for it with ``filtering=[{field: name, operator: CONTAIN}]``.
``activate`` switches ads, then the ad set, then the campaign to ACTIVE; ``pause`` sets the campaign (the master
switch) to PAUSED. Sources: ``versions.META_DOCS``. Exercised against the mock network only.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from creative_forge.campaigns.config import ZERO_DECIMAL, MetaNetwork
from creative_forge.campaigns.http import ERROR, OK, SERVER, THROTTLE, NetworkError, Upload
from creative_forge.campaigns.networks.base import MOCK_TOKEN, Adapter, SafetyError, env_secret
from creative_forge.campaigns.plan import Op, PlanContext, ref, tag_of, tagged
from creative_forge.campaigns.state import CampaignRecord
from creative_forge.campaigns.versions import META_VERSION
from creative_forge.perf.http import strip_query_param
from creative_forge.perf.meta import FIELDS as INSIGHT_FIELDS
from creative_forge.perf.meta import THROTTLE_CODES, TRANSIENT_CODES, to_row
from creative_forge.perf.store import PerfRow

ENV_TOKEN = "CREATIVE_FORGE_META_ADS_TOKEN"  # a token with ads_management (writes); never ads_read only
THUMBNAIL = "<the video's preferred thumbnail, read when the creative is created>"
EDGES = {"campaign": "campaigns", "adset": "adsets", "video": "advideos", "creative": "adcreatives", "ad": "ads"}
MIN_SPEND_CAP_USD = 100.0


def _form(payload: dict[str, Any]) -> dict[str, str]:
    """Graph API form fields: nested values travel as JSON strings, as in Meta's curl examples."""
    return {k: (v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))) for k, v in payload.items()}


class MetaAdapter(Adapter):
    network = "meta"
    version = META_VERSION
    live_base = "https://graph.facebook.com"
    sandbox_base = "https://graph.facebook.com"  # a sandbox ad account lives on the same host
    env = (ENV_TOKEN,)

    @classmethod
    def account_of(cls, net: MetaNetwork) -> str:
        return net.ad_account

    @classmethod
    def settings_of(cls, net: MetaNetwork) -> dict[str, Any]:
        return {"page_id": net.page_id, "app_id": net.app_id}

    @classmethod
    def load_secrets(cls, mode: str) -> dict[str, str]:
        return {"token": MOCK_TOKEN if mode == "mock" else env_secret(ENV_TOKEN, "Meta campaign writes")}

    @staticmethod
    def classify(status: int, body: Any) -> tuple[str, str]:
        err = body.get("error", {}) if isinstance(body, dict) else {}
        code = err.get("code") if isinstance(err, dict) else None
        if 200 <= status < 300 and not err:
            return OK, ""
        if code in THROTTLE_CODES or status == 429:
            return THROTTLE, f"HTTP {status}, error code {code}"
        if code in TRANSIENT_CODES or status >= 500:
            return SERVER, f"HTTP {status}, error code {code}"
        msg = err.get("message") if isinstance(err, dict) else str(body)[:300]
        return ERROR, f"HTTP {status}, error code {code} ({err.get('type', '') if err else ''}): {msg}"

    # ---------------------------------------------------------------------------------------------- plan side
    @classmethod
    def build(cls, ctx: PlanContext, net: MetaNetwork) -> tuple[list[Op], list[str]]:
        cfg, v, act = ctx.cfg, META_VERSION, net.ad_account
        notes = ["Advantage+ app campaign: campaign budget + Advantage+ audience + Advantage+ placements"]
        ops: list[Op] = []
        kc = ctx.key("campaign")
        camp: dict[str, Any] = {
            "name": tagged(ctx.base_name, kc),
            "objective": "OUTCOME_APP_PROMOTION",
            "status": "PAUSED",
            "special_ad_categories": [],
            "buying_type": "AUCTION",
            "daily_budget": cfg.minor(cfg.budget.daily),
            "bid_strategy": "LOWEST_COST_WITHOUT_CAP",
        }
        if cfg.currency == "USD" and cfg.budget.total_cap >= MIN_SPEND_CAP_USD:
            camp["spend_cap"] = cfg.minor(cfg.budget.total_cap)
        else:
            notes.append(
                f"no spend_cap (Meta's minimum is about {MIN_SPEND_CAP_USD:.0f} USD): the ad set's end_time and "
                "`forge campaign sync` hold the total cap"
            )
        unit = "" if cfg.currency in ZERO_DECIMAL else " (minor units)"
        ops.append(
            Op(
                kc,
                "meta",
                "campaign",
                camp["name"],
                "POST",
                f"/{v}/{act}/campaigns",
                camp,
                f"{camp['name']}  OUTCOME_APP_PROMOTION, PAUSED, daily_budget={camp['daily_budget']}{unit}"
                + (f", spend_cap={camp['spend_cap']}" if "spend_cap" in camp else ""),
            )
        )
        ks = ctx.key("adset")
        adset = {
            "name": tagged(f"{ctx.base_name} {'-'.join(cfg.countries).lower()}", ks),
            "campaign_id": ref(kc),
            "status": "PAUSED",
            "optimization_goal": "APP_INSTALLS",
            "billing_event": "IMPRESSIONS",
            "promoted_object": {"application_id": net.app_id, "object_store_url": ctx.listing.url},
            "targeting": {
                "geo_locations": {"countries": cfg.countries},
                "targeting_automation": {"advantage_audience": 1},
            },
            "start_time": f"{ctx.start.isoformat()}T00:00:00+0000",
            "end_time": f"{ctx.end.isoformat()}T23:59:59+0000",
        }
        ops.append(
            Op(
                ks,
                "meta",
                "adset",
                adset["name"],
                "POST",
                f"/{v}/{act}/adsets",
                adset,
                f"{adset['name']}  APP_INSTALLS, {', '.join(cfg.countries)}, {ctx.start} .. {ctx.end}, PAUSED",
            )
        )
        for c in ctx.creatives:
            kv, kr, ka = ctx.key("video", c.ad_name), ctx.key("creative", c.ad_name), ctx.key("ad", c.ad_name)
            video = {"name": tagged(c.ad_name, kv), "title": tagged(c.ad_name, kv)}
            ops.append(
                Op(
                    kv,
                    "meta",
                    "video",
                    video["name"],
                    "POST",
                    f"/{v}/{act}/advideos",
                    video,
                    f"{c.path.name}  ({c.format}, sha256 {c.sha256[:12]}, reviewed by {c.reviewer})",
                    upload=c.path,
                    creative=c.ad_name,
                )
            )
            spec: dict[str, Any] = {
                "page_id": net.page_id,
                "video_data": {
                    "video_id": ref(kv),
                    "image_url": THUMBNAIL,
                    "message": net.message,
                    "call_to_action": {"type": "INSTALL_MOBILE_APP", "value": {"link": ctx.listing.url}},
                },
            }
            if net.instagram_user_id:
                spec["instagram_user_id"] = net.instagram_user_id
            creative = {"name": tagged(c.ad_name, kr), "object_story_spec": spec}
            ops.append(
                Op(
                    kr,
                    "meta",
                    "creative",
                    creative["name"],
                    "POST",
                    f"/{v}/{act}/adcreatives",
                    creative,
                    f"{c.ad_name}  video + INSTALL_MOBILE_APP -> store URL",
                    creative=c.ad_name,
                )
            )
            ad = {
                "name": tagged(c.ad_name, ka),
                "adset_id": ref(ks),
                "creative": {"creative_id": ref(kr)},
                "status": "PAUSED",
            }
            ops.append(
                Op(
                    ka,
                    "meta",
                    "ad",
                    ad["name"],
                    "POST",
                    f"/{v}/{act}/ads",
                    ad,
                    f"{ad['name']}  PAUSED",
                    creative=c.ad_name,
                )
            )
        return ops, notes

    # ------------------------------------------------------------------------------------------- network side
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secrets['token']}"}

    def _get(self, path: str, params: dict[str, Any] | None = None, op: str | None = None) -> Any:
        return self.client.call(
            "GET", f"{self.base}{path}", write=False, op=op, params=params, headers=self._headers()
        ).body

    def _post(self, path: str, data: dict[str, Any], op: str | None = None, upload: Upload | None = None) -> Any:
        return self.client.call(
            "POST", f"{self.base}{path}", write=True, op=op, data=_form(data), upload=upload, headers=self._headers()
        ).body

    def preflight(self, currency: str) -> dict[str, Any]:
        acct = self._get(f"/{self.version}/{self.account}", {"fields": "name,currency,account_status,funding_source"})
        if acct.get("currency") != currency:
            raise SafetyError(
                f"meta: account {self.account} bills in {acct.get('currency')}, the config says {currency}; budgets "
                "would mean something else"
            )
        if self.mode == "sandbox" and acct.get("funding_source"):
            raise SafetyError(
                f"meta: {self.account} has a payment method attached, so it is not a sandbox ad account; use the "
                "sandbox account from your app's Marketing API > Tools page"
            )
        return acct

    def execute(self, op: Op, payload: dict[str, Any], ids: dict[str, str]) -> str:
        if op.kind == "video":
            assert op.upload is not None
            body = self._post(op.path, payload, op.key, Upload("source", op.upload, "video/mp4"))
        else:
            if op.kind == "creative":
                video_id = payload["object_story_spec"]["video_data"]["video_id"]
                thumbs = self._get(f"/{self.version}/{video_id}/thumbnails", {"fields": "uri,is_preferred"}, op.key)
                data = thumbs.get("data") or []
                if not data:
                    raise NetworkError("meta: the video is still processing (no thumbnail yet); run create again")
                best = next((t for t in data if t.get("is_preferred")), data[0])
                payload["object_story_spec"]["video_data"]["image_url"] = best["uri"]
            body = self._post(op.path, payload, op.key)
        self._expect(isinstance(body, dict) and bool(body.get("id")), f"no id in the answer to {op.kind}")
        return str(body["id"])

    def find(self, op: Op, ids: dict[str, str] | None = None) -> str | None:
        tag = tag_of(op.name)
        edge = EDGES[op.kind]
        field = "title" if op.kind == "video" else "name"
        params: dict[str, Any] = {"fields": f"id,{field}", "limit": 100}
        if op.kind != "video":
            params["filtering"] = json.dumps([{"field": "name", "operator": "CONTAIN", "value": tag}])
        url: str | None = f"{self.base}/{self.version}/{self.account}/{edge}"
        for _ in range(20):
            if not url:
                break
            body = self.client.call("GET", url, write=False, op=op.key, params=params, headers=self._headers()).body
            for item in body.get("data", []):
                if tag in str(item.get(field, "")):
                    return str(item["id"])
            nxt = (body.get("paging") or {}).get("next")
            url, params = (strip_query_param(nxt, "access_token"), {}) if nxt else (None, {})
        return None

    def _status(self, object_id: str, status: str) -> None:
        body = self._post(f"/{self.version}/{object_id}", {"status": status})
        self._expect(bool(body.get("success", True)), f"status change of {object_id} not confirmed")

    def set_status(self, rec: CampaignRecord, active: bool) -> None:
        if not active:
            self._status(rec.campaign_id, "PAUSED")
            return
        for ad_id in rec.ads.values():
            self._status(ad_id, "ACTIVE")
        for adset_id in rec.children.get("adset", []):
            self._status(adset_id, "ACTIVE")
        self._status(rec.campaign_id, "ACTIVE")

    def set_ad_status(self, rec: CampaignRecord, ad_id: str, active: bool) -> None:
        self._status(ad_id, "ACTIVE" if active else "PAUSED")

    def read(self, rec: CampaignRecord) -> dict[str, Any]:
        c = self._get(
            f"/{self.version}/{rec.campaign_id}", {"fields": "id,name,status,effective_status,daily_budget,spend_cap"}
        )
        acct = self._get(f"/{self.version}/{self.account}", {"fields": "currency"})
        ins = self._get(f"/{self.version}/{rec.campaign_id}/insights", {"fields": "spend", "date_preset": "maximum"})
        currency = str(acct.get("currency", ""))
        offset = 1 if currency in ZERO_DECIMAL else 100
        rows = ins.get("data") or []
        return {
            "status": "ACTIVE" if c.get("status") == "ACTIVE" else "PAUSED",
            "effective_status": c.get("effective_status"),
            "daily": float(c.get("daily_budget") or 0) / offset,
            "spend": float(rows[0].get("spend") or 0) if rows else 0.0,
            "currency": currency,
        }

    def insights(self, rec: CampaignRecord, since: date, until: date) -> list[PerfRow]:
        """``GET /act_<id>/insights`` at ad level, daily, mapped by ``perf.meta.to_row`` (the importer's own code)."""
        params: dict[str, Any] = {
            "level": "ad",
            "fields": INSIGHT_FIELDS + ",campaign_id",
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}, separators=(",", ":")),
            "time_increment": 1,
            "filtering": json.dumps([{"field": "campaign.id", "operator": "IN", "value": [rec.campaign_id]}]),
            "limit": 500,
        }
        url: str | None = f"{self.base}/{self.version}/{self.account}/insights"
        out: list[PerfRow] = []
        while url:
            body = self.client.call("GET", url, write=False, params=params, headers=self._headers()).body
            out += [to_row(d, self.account, source="forge-campaign-sync") for d in body.get("data", [])]
            nxt = (body.get("paging") or {}).get("next")
            url, params = (strip_query_param(nxt, "access_token"), {}) if nxt else (None, {})
        return out
