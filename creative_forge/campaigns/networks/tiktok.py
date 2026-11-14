"""TikTok API for Business (v1.3): an app-install campaign, created with ``operation_status=DISABLE``.

Calls (``https://business-api.tiktok.com/open_api/v1.3``; the sandbox is ``https://sandbox-ads.tiktok.com``), JSON
bodies, the token in the ``Access-Token`` header, and every answer ``{"code": 0, "message": "OK", "data": ...}``:

1. ``POST campaign/create/``: ``objective_type=APP_PROMOTION``, ``app_promotion_type=APP_INSTALL``,
   ``budget_mode=BUDGET_MODE_TOTAL`` with the total cap as ``budget`` (the network stops the campaign there);
2. ``POST adgroup/create/``: the app's TikTok ``app_id``, ``promotion_type`` APP_ANDROID / APP_IOS, automatic
   placement, ``location_ids``, ``budget_mode=BUDGET_MODE_DAY``, ``schedule_type=SCHEDULE_START_END``,
   ``optimization_goal=INSTALL``, ``billing_event=OCPM``, ``bid_type=BID_TYPE_NO_BID``;
3. per creative ``POST file/video/ad/upload/`` (multipart, ``upload_type=UPLOAD_BY_FILE``, ``video_signature`` = MD5),
   then the cover: ``GET file/video/ad/info/`` and ``POST file/image/ad/upload/`` with ``UPLOAD_BY_URL``;
4. ``POST ad/create/`` with one ``SINGLE_VIDEO`` creative, ``operation_status=DISABLE``.

``campaign/create`` and ``adgroup/create`` take a ``request_id``; it is derived from the idempotency key, and the key
is in every name as well, so ``find`` can look objects up through the ``.../get/`` endpoints. Status changes go through
``campaign|adgroup|ad/status/update/`` with ENABLE / DISABLE. Sources: ``versions.TIKTOK_DOCS``. Exercised against
the mock network only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from creative_forge.campaigns.config import TikTokNetwork
from creative_forge.campaigns.http import ERROR, OK, SERVER, THROTTLE, Upload
from creative_forge.campaigns.networks.base import MOCK_TOKEN, Adapter, SafetyError, env_secret
from creative_forge.campaigns.plan import Op, PlanContext, ref, tag_of, tagged, tagged_short
from creative_forge.campaigns.state import CampaignRecord
from creative_forge.campaigns.versions import TIKTOK_VERSION
from creative_forge.perf.http import num
from creative_forge.perf.store import PerfRow

ENV_TOKEN = "CREATIVE_FORGE_TIKTOK_TOKEN"
API = f"/open_api/{TIKTOK_VERSION}"
THROTTLE_CODES = {40100, 40133}  # "too many requests" family
SERVER_CODES = {50000, 50002, 51003}
LIST_PATHS = {"campaign": "campaign/get/", "adgroup": "adgroup/get/", "ad": "ad/get/"}
NAME_FIELDS = {"campaign": "campaign_name", "adgroup": "adgroup_name", "ad": "ad_name"}
ID_FIELDS = {"campaign": "campaign_id", "adgroup": "adgroup_id", "ad": "ad_id"}


def request_id(key: str) -> str:
    """TikTok's request_id is a numeric string; 15 hex digits of the key fit well inside 64 bits."""
    return str(int(key[:15], 16))


def md5(path: Any) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TikTokAdapter(Adapter):
    network = "tiktok"
    version = TIKTOK_VERSION
    live_base = "https://business-api.tiktok.com"
    sandbox_base = "https://sandbox-ads.tiktok.com"
    env = (ENV_TOKEN,)

    @classmethod
    def account_of(cls, net: TikTokNetwork) -> str:
        return net.advertiser_id

    @classmethod
    def settings_of(cls, net: TikTokNetwork) -> dict[str, Any]:
        return {"app_id": net.app_id}

    @classmethod
    def load_secrets(cls, mode: str) -> dict[str, str]:
        return {"token": MOCK_TOKEN if mode == "mock" else env_secret(ENV_TOKEN, "TikTok campaign writes")}

    @staticmethod
    def classify(status: int, body: Any) -> tuple[str, str]:
        code = body.get("code") if isinstance(body, dict) else None
        if status == 429 or code in THROTTLE_CODES:
            return THROTTLE, f"HTTP {status}, code {code}"
        if status >= 500 or code in SERVER_CODES:
            return SERVER, f"HTTP {status}, code {code}"
        if 200 <= status < 300 and code == 0:
            return OK, ""
        msg = body.get("message") if isinstance(body, dict) else str(body)[:300]
        return ERROR, f"HTTP {status}, code {code}: {msg}"

    # ---------------------------------------------------------------------------------------------- plan side
    @classmethod
    def build(cls, ctx: PlanContext, net: TikTokNetwork) -> tuple[list[Op], list[str]]:
        cfg = ctx.cfg
        adv = net.advertiser_id
        notes = [
            f"total cap {cfg.budget.total_cap:.2f} {cfg.currency} is the campaign's BUDGET_MODE_TOTAL budget; "
            "the ad group carries the daily budget"
        ]
        kc, kg = ctx.key("campaign"), ctx.key("adgroup")
        camp = {
            "advertiser_id": adv,
            "campaign_name": tagged(ctx.base_name, kc),
            "objective_type": "APP_PROMOTION",
            "app_promotion_type": "APP_INSTALL",
            "budget_mode": "BUDGET_MODE_TOTAL",
            "budget": round(cfg.budget.total_cap, 2),
            "operation_status": "DISABLE",
            "request_id": request_id(kc),
        }
        ops = [
            Op(
                kc,
                "tiktok",
                "campaign",
                camp["campaign_name"],
                "POST",
                f"{API}/campaign/create/",
                camp,
                f"{camp['campaign_name']}  APP_PROMOTION/APP_INSTALL, total budget {camp['budget']}, DISABLE",
            )
        ]
        group = {
            "advertiser_id": adv,
            "campaign_id": ref(kc),
            "adgroup_name": tagged(f"{ctx.base_name} {'-'.join(cfg.countries).lower()}", kg),
            "app_id": net.app_id,
            "promotion_type": "APP_ANDROID" if ctx.listing.platform == "android" else "APP_IOS",
            "placement_type": "PLACEMENT_TYPE_AUTOMATIC",
            "location_ids": net.location_ids,
            "budget_mode": "BUDGET_MODE_DAY",
            "budget": round(cfg.budget.daily, 2),
            "schedule_type": "SCHEDULE_START_END",
            "schedule_start_time": f"{ctx.start.isoformat()} 00:00:00",
            "schedule_end_time": f"{ctx.end.isoformat()} 23:59:59",
            "optimization_goal": "INSTALL",
            "billing_event": "OCPM",
            "bid_type": "BID_TYPE_NO_BID",
            "operation_status": "DISABLE",
            "request_id": request_id(kg),
        }
        ops.append(
            Op(
                kg,
                "tiktok",
                "adgroup",
                group["adgroup_name"],
                "POST",
                f"{API}/adgroup/create/",
                group,
                f"{group['adgroup_name']}  INSTALL/OCPM, daily {group['budget']}, {ctx.start} .. {ctx.end}, DISABLE",
            )
        )
        for c in ctx.creatives:
            kv, ki, ka = ctx.key("video", c.ad_name), ctx.key("cover", c.ad_name), ctx.key("ad", c.ad_name)
            video = {
                "advertiser_id": adv,
                "upload_type": "UPLOAD_BY_FILE",
                "file_name": tagged_short(c.ad_name, kv),
                "video_signature": "<MD5 of the file, computed at upload>",
            }
            ops.append(
                Op(
                    kv,
                    "tiktok",
                    "video",
                    video["file_name"],
                    "POST",
                    f"{API}/file/video/ad/upload/",
                    video,
                    f"{c.path.name}  ({c.format}, sha256 {c.sha256[:12]}, reviewed by {c.reviewer})",
                    upload=c.path,
                    creative=c.ad_name,
                )
            )
            cover = {
                "advertiser_id": adv,
                "upload_type": "UPLOAD_BY_URL",
                "file_name": tagged_short(c.ad_name + ".cover", ki),
                "image_url": "<video_cover_url from file/video/ad/info/>",
                "_from_video": ref(kv),  # not sent: the video whose cover is uploaded
            }
            ops.append(
                Op(
                    ki,
                    "tiktok",
                    "cover",
                    cover["file_name"],
                    "POST",
                    f"{API}/file/image/ad/upload/",
                    cover,
                    f"cover of {c.ad_name} (the network's own frame)",
                    creative=c.ad_name,
                )
            )
            ad = {
                "advertiser_id": adv,
                "adgroup_id": ref(kg),
                "creatives": [
                    {
                        "ad_name": tagged(c.ad_name, ka),
                        "ad_format": "SINGLE_VIDEO",
                        "video_id": ref(kv),
                        "image_ids": [ref(ki)],
                        "ad_text": net.ad_text,
                        "call_to_action": net.call_to_action,
                        "identity_type": net.identity_type,
                        "identity_id": net.identity_id,
                        "operation_status": "DISABLE",
                    }
                ],
            }
            ops.append(
                Op(
                    ka,
                    "tiktok",
                    "ad",
                    ad["creatives"][0]["ad_name"],
                    "POST",
                    f"{API}/ad/create/",
                    ad,
                    f"{ad['creatives'][0]['ad_name']}  SINGLE_VIDEO, DISABLE",
                    creative=c.ad_name,
                )
            )
        return ops, notes

    # ------------------------------------------------------------------------------------------- network side
    def _headers(self) -> dict[str, str]:
        return {"Access-Token": self.secrets["token"]}

    def _get(self, path: str, params: dict[str, Any], op: str | None = None) -> Any:
        q = {k: (v if isinstance(v, str) else json.dumps(v, separators=(",", ":"))) for k, v in params.items()}
        return (
            self.client.call(
                "GET", f"{self.base}{API}/{path}", write=False, op=op, params=q, headers=self._headers()
            ).body.get("data")
            or {}
        )

    def _post(self, path: str, body: dict[str, Any], op: str | None = None) -> Any:
        return (
            self.client.call(
                "POST", f"{self.base}{path}", write=True, op=op, json_body=body, headers=self._headers()
            ).body.get("data")
            or {}
        )

    def preflight(self, currency: str) -> dict[str, Any]:
        data = self._get("advertiser/info/", {"advertiser_ids": [self.account]})
        info = (data.get("list") or [{}])[0]
        if info.get("currency") != currency:
            raise SafetyError(
                f"tiktok: advertiser {self.account} bills in {info.get('currency')}, the config says {currency}"
            )
        return info

    def execute(self, op: Op, payload: dict[str, Any], ids: dict[str, str]) -> str:
        if op.kind == "video":
            assert op.upload is not None
            fields = {**payload, "video_signature": md5(op.upload)}
            data = self.client.call(
                "POST",
                f"{self.base}{op.path}",
                write=True,
                op=op.key,
                data=fields,
                upload=Upload("video_file", op.upload, "video/mp4"),
                headers=self._headers(),
            ).body.get("data")
            item = data[0] if isinstance(data, list) and data else (data or {})
            self._expect(bool(item.get("video_id")), "no video_id in the upload answer")
            return str(item["video_id"])
        if op.kind == "cover":
            video_id = payload.pop("_from_video")
            info = self._get("file/video/ad/info/", {"advertiser_id": self.account, "video_ids": [video_id]}, op.key)
            items = info.get("list") or []
            self._expect(
                bool(items and items[0].get("video_cover_url")), "the video has no cover yet; run create again"
            )
            payload["image_url"] = items[0]["video_cover_url"]
            data = self._post(op.path, payload, op.key)
            self._expect(bool(data.get("image_id")), "no image_id in the answer")
            return str(data["image_id"])
        data = self._post(op.path, payload, op.key)
        if op.kind == "ad":
            ad_ids = data.get("ad_ids") or []
            self._expect(len(ad_ids) == 1, "ad/create did not return one ad id")
            return str(ad_ids[0])
        field = ID_FIELDS[op.kind]
        self._expect(bool(data.get(field)), f"no {field} in the answer")
        return str(data[field])

    def find(self, op: Op, ids: dict[str, str] | None = None) -> str | None:
        tag = tag_of(op.name)
        if op.kind in LIST_PATHS:
            name_field = NAME_FIELDS[op.kind]
            for page in range(1, 21):
                data = self._get(
                    LIST_PATHS[op.kind],
                    {"advertiser_id": self.account, "page": page, "page_size": 100, "filtering": {name_field: tag}},
                    op.key,
                )
                for item in data.get("list") or []:
                    if tag in str(item.get(name_field, "")):
                        return str(item[ID_FIELDS[op.kind]])
                info = data.get("page_info") or {}
                if page >= int(info.get("total_page") or 1):
                    break
            return None
        path, idf = (
            ("file/video/ad/search/", "video_id") if op.kind == "video" else ("file/image/ad/search/", "image_id")
        )
        for page in range(1, 21):
            data = self._get(
                path,
                {"advertiser_id": self.account, "page": page, "page_size": 100, "filtering": {"file_names": [op.name]}},
                op.key,
            )
            for item in data.get("list") or []:
                if tag in str(item.get("file_name", "")):
                    return str(item[idf])
            info = data.get("page_info") or {}
            if page >= int(info.get("total_page") or 1):
                break
        return None

    def _switch(self, kind: str, ids: list[str], active: bool) -> None:
        if not ids:
            return
        body = {
            "advertiser_id": self.account,
            f"{kind}_ids": ids,
            "operation_status": "ENABLE" if active else "DISABLE",
        }
        self._post(f"{API}/{kind}/status/update/", body)

    def set_status(self, rec: CampaignRecord, active: bool) -> None:
        if not active:
            self._switch("campaign", [rec.campaign_id], False)
            return
        self._switch("ad", list(rec.ads.values()), True)
        self._switch("adgroup", rec.children.get("adgroup", []), True)
        self._switch("campaign", [rec.campaign_id], True)

    def set_ad_status(self, rec: CampaignRecord, ad_id: str, active: bool) -> None:
        self._switch("ad", [ad_id], active)

    def read(self, rec: CampaignRecord) -> dict[str, Any]:
        camp = self._get(
            "campaign/get/", {"advertiser_id": self.account, "filtering": {"campaign_ids": [rec.campaign_id]}}
        )
        groups = self._get(
            "adgroup/get/", {"advertiser_id": self.account, "filtering": {"campaign_ids": [rec.campaign_id]}}
        )
        c = (camp.get("list") or [{}])[0]
        daily = sum(float(g.get("budget") or 0) for g in groups.get("list") or [])
        report = self._get(
            "report/integrated/get/",
            {
                "advertiser_id": self.account,
                "report_type": "BASIC",
                "data_level": "AUCTION_CAMPAIGN",
                "dimensions": ["campaign_id"],
                "metrics": ["spend"],
                "query_lifetime": True,
                "filtering": [
                    {"field_name": "campaign_ids", "filter_type": "IN", "filter_value": json.dumps([rec.campaign_id])}
                ],
            },
        )
        spend = sum(num((r.get("metrics") or {}).get("spend")) or 0 for r in report.get("list") or [])
        info = self._get("advertiser/info/", {"advertiser_ids": [self.account]})
        return {
            "status": "ACTIVE" if c.get("operation_status") == "ENABLE" else "PAUSED",
            "effective_status": c.get("secondary_status"),
            "daily": daily,
            "total_budget": float(c.get("budget") or 0),
            "spend": float(spend),
            "currency": str(((info.get("list") or [{}])[0]).get("currency", "")),
        }

    def insights(self, rec: CampaignRecord, since: date, until: date) -> list[PerfRow]:
        """``GET report/integrated/get/`` at AUCTION_AD level by day; 2- and 6-second views stay in their columns."""
        out: list[PerfRow] = []
        for page in range(1, 101):
            data = self._get(
                "report/integrated/get/",
                {
                    "advertiser_id": self.account,
                    "report_type": "BASIC",
                    "data_level": "AUCTION_AD",
                    "dimensions": ["ad_id", "stat_time_day"],
                    "metrics": [
                        "ad_name",
                        "spend",
                        "impressions",
                        "clicks",
                        "app_install",
                        "video_watched_2s",
                        "video_watched_6s",
                        "video_views_p25",
                        "video_views_p50",
                        "video_views_p75",
                        "video_views_p100",
                    ],
                    "start_date": since.isoformat(),
                    "end_date": until.isoformat(),
                    "page": page,
                    "page_size": 1000,
                    "filtering": [
                        {
                            "field_name": "campaign_ids",
                            "filter_type": "IN",
                            "filter_value": json.dumps([rec.campaign_id]),
                        }
                    ],
                },
            )
            for r in data.get("list") or []:
                d, m = r.get("dimensions") or {}, r.get("metrics") or {}
                day = str(d.get("stat_time_day", ""))[:10]
                out.append(
                    PerfRow(
                        network="tiktok",
                        account=self.account,
                        level="ad",
                        date=day,
                        ad_key=str(d.get("ad_id")),
                        ad_name=m.get("ad_name"),
                        impressions=num(m.get("impressions")),
                        clicks=num(m.get("clicks")),
                        spend=num(m.get("spend")),
                        installs=num(m.get("app_install")),
                        video_2s=num(m.get("video_watched_2s")),
                        video_6s=num(m.get("video_watched_6s")),
                        p25=num(m.get("video_views_p25")),
                        p50=num(m.get("video_views_p50")),
                        p75=num(m.get("video_views_p75")),
                        p100=num(m.get("video_views_p100")),
                        source="forge-campaign-sync",
                    )
                )
            info = data.get("page_info") or {}
            if page >= int(info.get("total_page") or 1):
                break
        return out
