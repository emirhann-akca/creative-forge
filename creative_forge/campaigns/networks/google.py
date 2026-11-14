"""Google Ads API (v26): an App campaign with video assets, created PAUSED.

A new campaign's status defaults to ENABLED in the Google Ads API, so ``status=PAUSED`` is set explicitly on the
campaign, the ad group and the ad. Calls (``https://googleads.googleapis.com/v26/customers/<id>``), JSON, with the
``developer-token`` header, an OAuth access token and, for a manager account, ``login-customer-id``:

1. ``campaignBudgets:mutate``: ``amountMicros`` = daily budget, ``explicitlyShared=false`` (App campaigns cannot share);
2. ``campaigns:mutate``: ``MULTI_CHANNEL`` / ``APP_CAMPAIGN``, ``appCampaignSetting {appId, appStore,
   biddingStrategyGoalType}``, ``targetCpa`` or ``maximizeConversions``, ``startDateTime`` / ``endDateTime``
   ("yyyy-MM-dd HH:mm:ss" in the account's time zone), ``containsEuPoliticalAdvertising``;
3. ``campaignCriteria:mutate``: locations and languages;
4. ``adGroups:mutate``;
5. per creative, the video goes to YouTube first: ``POST /resumable/upload/v26/.../youTubeVideoUploads:create``
   (start, then the bytes with ``upload, finalize``), then ``googleAds:search`` until the upload is ``PROCESSED``,
   then ``assets:mutate`` with a ``youtubeVideoAsset``;
6. ``adGroupAds:mutate``: one App ad holding the headlines, descriptions and every video.

There is no idempotency parameter; the key is in every name and ``find`` searches with GAQL ``LIKE``. In sandbox mode
the account must report ``customer.test_account = true`` (test accounts never serve). Per-video performance comes from
``ad_group_ad_asset_view``; one video cannot be paused on its own (the App ad holds them all), so a kill verdict on
Google stays a recommendation even with auto-pause on. Sources: ``versions.GOOGLE_ADS_DOCS``. Mock network only.
"""

from __future__ import annotations

import urllib.parse
from datetime import date
from typing import Any

from creative_forge.campaigns.config import GoogleNetwork
from creative_forge.campaigns.http import ERROR, OK, SERVER, THROTTLE, NetworkError
from creative_forge.campaigns.networks.base import MOCK_TOKEN, Adapter, SafetyError, env_secret
from creative_forge.campaigns.plan import Op, PlanContext, ref, tag_of, tagged, tagged_short
from creative_forge.campaigns.state import CampaignRecord
from creative_forge.campaigns.versions import GOOGLE_ADS_VERSION
from creative_forge.perf.http import num
from creative_forge.perf.store import PerfRow

V = GOOGLE_ADS_VERSION
ENV_DEVELOPER_TOKEN = "CREATIVE_FORGE_GOOGLE_ADS_DEVELOPER_TOKEN"
ENV_CLIENT_ID = "CREATIVE_FORGE_GOOGLE_ADS_CLIENT_ID"
ENV_CLIENT_SECRET = "CREATIVE_FORGE_GOOGLE_ADS_CLIENT_SECRET"
ENV_REFRESH_TOKEN = "CREATIVE_FORGE_GOOGLE_ADS_REFRESH_TOKEN"
VIDEO_ID = "<youtube video id of the upload, read once it is PROCESSED>"
FIND = {
    "budget": ("campaign_budget", "campaign_budget.name"),
    "campaign": ("campaign", "campaign.name"),
    "adgroup": ("ad_group", "ad_group.name"),
    "video_upload": ("you_tube_video_upload", "you_tube_video_upload.video_title"),
    "video_asset": ("asset", "asset.name"),
    "ad": ("ad_group_ad", "ad_group_ad.ad.name"),
}
RESULT_KEY = {
    "campaign_budget": "campaignBudget",
    "campaign": "campaign",
    "ad_group": "adGroup",
    "you_tube_video_upload": "youTubeVideoUpload",
    "asset": "asset",
    "ad_group_ad": "adGroupAd",
}


def _micros(amount: float) -> str:
    return str(round(amount * 1_000_000))


def _tag(op: Op) -> str:
    return tag_of(op.name)


class GoogleAdsAdapter(Adapter):
    network = "google"
    version = V
    live_base = "https://googleads.googleapis.com"
    sandbox_base = "https://googleads.googleapis.com"  # test accounts use the production endpoint
    env = (ENV_DEVELOPER_TOKEN, ENV_CLIENT_ID, ENV_CLIENT_SECRET, ENV_REFRESH_TOKEN)

    @classmethod
    def account_of(cls, net: GoogleNetwork) -> str:
        return net.customer_id

    @classmethod
    def settings_of(cls, net: GoogleNetwork) -> dict[str, Any]:
        return {"login_customer_id": net.login_customer_id} if net.login_customer_id else {}

    @classmethod
    def load_secrets(cls, mode: str) -> dict[str, str]:
        if mode == "mock":
            return {"token": MOCK_TOKEN, "developer_token": MOCK_TOKEN}
        developer = env_secret(ENV_DEVELOPER_TOKEN, "Google Ads campaign writes")
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        creds = Credentials(
            None,
            refresh_token=env_secret(ENV_REFRESH_TOKEN, "Google Ads OAuth"),
            client_id=env_secret(ENV_CLIENT_ID, "Google Ads OAuth"),
            client_secret=env_secret(ENV_CLIENT_SECRET, "Google Ads OAuth"),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/adwords"],
        )
        creds.refresh(Request())
        return {"token": str(creds.token), "developer_token": developer}

    @staticmethod
    def classify(status: int, body: Any) -> tuple[str, str]:
        if 200 <= status < 300:
            return OK, ""
        err = body.get("error", {}) if isinstance(body, dict) else {}
        if status == 429 or (isinstance(err, dict) and err.get("status") == "RESOURCE_EXHAUSTED"):
            return THROTTLE, f"HTTP {status}"
        if status >= 500:
            return SERVER, f"HTTP {status}"
        msg = err.get("message") if isinstance(err, dict) else str(body)[:300]
        return ERROR, f"HTTP {status} ({err.get('status', '') if isinstance(err, dict) else ''}): {msg}"

    # ---------------------------------------------------------------------------------------------- plan side
    @classmethod
    def build(cls, ctx: PlanContext, net: GoogleNetwork) -> tuple[list[Op], list[str]]:
        cfg, cid = ctx.cfg, net.customer_id
        base = f"/{V}/customers/{cid}"
        notes = [
            "videos are uploaded to YouTube (a Google-managed channel, UNLISTED, marked as generative content) "
            "before they become assets",
            "App campaigns have no total budget: endDateTime and `forge campaign sync` hold the total cap",
        ]
        kb, kc, kx, kg, ka = (ctx.key(k) for k in ("budget", "campaign", "criteria", "adgroup", "ad"))
        budget = {
            "name": tagged(f"{ctx.base_name} budget", kb),
            "amountMicros": _micros(cfg.budget.daily),
            "deliveryMethod": "STANDARD",
            "explicitlyShared": False,
        }
        ops = [
            Op(
                kb,
                "google",
                "budget",
                budget["name"],
                "POST",
                f"{base}/campaignBudgets:mutate",
                {"operations": [{"create": budget}]},
                f"{budget['name']}  {cfg.budget.daily:.2f} {cfg.currency}/day (amountMicros {budget['amountMicros']})",
            )
        ]
        if net.target_install_cost:
            goal, bidding = (
                "OPTIMIZE_INSTALLS_TARGET_INSTALL_COST",
                {"targetCpa": {"targetCpaMicros": _micros(net.target_install_cost)}},
            )
        else:
            goal, bidding = "OPTIMIZE_INSTALLS_WITHOUT_TARGET_INSTALL_COST", {"maximizeConversions": {}}
        camp = {
            "name": tagged(ctx.base_name, kc),
            "status": "PAUSED",
            "advertisingChannelType": "MULTI_CHANNEL",
            "advertisingChannelSubType": "APP_CAMPAIGN",
            "campaignBudget": ref(kb),
            "appCampaignSetting": {
                "appId": ctx.listing.app_id,
                "appStore": ctx.listing.google_store,
                "biddingStrategyGoalType": goal,
            },
            **bidding,
            "startDateTime": f"{ctx.start.isoformat()} 00:00:00",
            "endDateTime": f"{ctx.end.isoformat()} 23:59:59",
            "containsEuPoliticalAdvertising": "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
        }
        ops.append(
            Op(
                kc,
                "google",
                "campaign",
                camp["name"],
                "POST",
                f"{base}/campaigns:mutate",
                {"operations": [{"create": camp}]},
                f"{camp['name']}  APP_CAMPAIGN {goal}, {ctx.start} .. {ctx.end}, PAUSED",
            )
        )
        criteria = [
            {"create": {"campaign": ref(kc), "location": {"geoTargetConstant": f"geoTargetConstants/{g}"}}}
            for g in net.location_ids
        ]
        criteria += [
            {"create": {"campaign": ref(kc), "language": {"languageConstant": f"languageConstants/{lang}"}}}
            for lang in net.language_ids
        ]
        ops.append(
            Op(
                kx,
                "google",
                "criteria",
                tagged(f"{ctx.base_name} targeting", kx),
                "POST",
                f"{base}/campaignCriteria:mutate",
                {"operations": criteria},
                f"locations {', '.join(net.location_ids)}; languages {', '.join(net.language_ids) or 'all'}",
            )
        )
        group = {
            "name": tagged(f"{ctx.base_name} {'-'.join(cfg.countries).lower()}", kg),
            "campaign": ref(kc),
            "status": "PAUSED",
        }
        ops.append(
            Op(
                kg,
                "google",
                "adgroup",
                group["name"],
                "POST",
                f"{base}/adGroups:mutate",
                {"operations": [{"create": group}]},
                f"{group['name']}  PAUSED",
            )
        )
        asset_keys = []
        for c in ctx.creatives:
            ku, kv = ctx.key("video_upload", c.ad_name), ctx.key("video_asset", c.ad_name)
            asset_keys.append(kv)
            upload = {
                "customer_id": cid,
                "you_tube_video_upload": {
                    "video_title": tagged_short(c.ad_name, ku),
                    "video_description": c.ad_name,
                    "video_privacy": "UNLISTED",
                    "video_content_metadata": {"generative_content_disclosure": "GENERATIVE_CONTENT"},
                },
            }
            ops.append(
                Op(
                    ku,
                    "google",
                    "video_upload",
                    upload["you_tube_video_upload"]["video_title"],
                    "POST",
                    f"/resumable/upload/{V}/customers/{cid}/youTubeVideoUploads:create",
                    upload,
                    f"{c.path.name}  ({c.format}, sha256 {c.sha256[:12]}, reviewed by {c.reviewer}) -> YouTube",
                    upload=c.path,
                    creative=c.ad_name,
                )
            )
            asset = {"name": tagged(c.ad_name, kv), "youtubeVideoAsset": {"youtubeVideoId": VIDEO_ID}}
            ops.append(
                Op(
                    kv,
                    "google",
                    "video_asset",
                    asset["name"],
                    "POST",
                    f"{base}/assets:mutate",
                    {"operations": [{"create": asset}], "_from_upload": ref(ku)},
                    f"{asset['name']}  YOUTUBE_VIDEO",
                    creative=c.ad_name,
                )
            )
        ad = {
            "adGroup": ref(kg),
            "status": "PAUSED",
            "ad": {
                "name": tagged(f"{ctx.base_name} app ad", ka),
                "appAd": {
                    "headlines": [{"text": h} for h in net.headlines],
                    "descriptions": [{"text": d} for d in net.descriptions],
                    "youtubeVideos": [{"asset": ref(k)} for k in asset_keys],
                },
            },
        }
        ops.append(
            Op(
                ka,
                "google",
                "ad",
                ad["ad"]["name"],
                "POST",
                f"{base}/adGroupAds:mutate",
                {"operations": [{"create": ad}]},
                f"{ad['ad']['name']}  App ad: {len(net.headlines)} headline(s), "
                f"{len(net.descriptions)} description(s), {len(asset_keys)} video(s), PAUSED",
            )
        )
        return ops, notes

    # ------------------------------------------------------------------------------------------- network side
    def _headers(self) -> dict[str, str]:
        h = {"Authorization": f"Bearer {self.secrets['token']}", "developer-token": self.secrets["developer_token"]}
        if self.settings.get("login_customer_id"):
            h["login-customer-id"] = str(self.settings["login_customer_id"])
        return h

    def search(self, query: str, op: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        body: dict[str, Any] = {"query": query}
        for _ in range(50):
            r = self.client.call(
                "POST",
                f"{self.base}/{V}/customers/{self.account}/googleAds:search",
                write=False,
                op=op,
                json_body=body,
                headers=self._headers(),
            ).body
            rows += r.get("results") or []
            token = r.get("nextPageToken")
            if not token:
                break
            body = {"query": query, "pageToken": token}
        return rows

    def _mutate(self, path: str, body: dict[str, Any], op: str | None = None) -> list[str]:
        r = self.client.call(
            "POST", f"{self.base}{path}", write=True, op=op, json_body=body, headers=self._headers()
        ).body
        names = [str(x.get("resourceName")) for x in r.get("results") or [] if x.get("resourceName")]
        self._expect(bool(names), f"no resourceName in the answer to {path.rsplit('/', 1)[-1]}")
        return names

    def preflight(self, currency: str) -> dict[str, Any]:
        rows = self.search("SELECT customer.id, customer.currency_code, customer.test_account FROM customer")
        cust = (rows[0] if rows else {}).get("customer", {})
        if cust.get("currencyCode") != currency:
            raise SafetyError(f"google: customer {self.account} bills in {cust.get('currencyCode')}, not {currency}")
        if self.mode == "sandbox" and cust.get("testAccount") is not True:
            raise SafetyError(
                f"google: customer {self.account} is not a test account (customer.test_account is false); sandbox "
                "mode only writes to test accounts, which never serve ads"
            )
        return cust

    def _upload_video(self, op: Op, payload: dict[str, Any]) -> str:
        assert op.upload is not None
        size = op.upload.stat().st_size
        start = self.client.call(
            "POST",
            f"{self.base}{op.path}",
            write=True,
            op=op.key,
            json_body=payload,
            headers={
                **self._headers(),
                "x-goog-upload-protocol": "resumable",
                "x-goog-upload-command": "start",
                "x-goog-upload-header-content-length": str(size),
            },
        )
        session = start.headers.get("x-goog-upload-url", "")
        same_host = urllib.parse.urlsplit(session).netloc == urllib.parse.urlsplit(self.base).netloc
        self._expect(bool(session) and same_host, "no upload session URL on the API host")
        done = self.client.call(
            "PUT",
            session,
            write=True,
            op=op.key,
            data=op.upload.read_bytes(),
            headers={**self._headers(), "x-goog-upload-command": "upload, finalize", "x-goog-upload-offset": "0"},
        ).body
        self._expect(bool(done.get("resourceName")), "no resourceName after finalize")
        return str(done["resourceName"])

    def _video_id(self, upload_rn: str, op: str, tries: int = 6) -> str:
        for i in range(tries):
            rows = self.search(
                "SELECT you_tube_video_upload.resource_name, you_tube_video_upload.video_id, "
                f"you_tube_video_upload.state FROM you_tube_video_upload WHERE "
                f"you_tube_video_upload.resource_name = '{upload_rn}'",
                op,
            )
            up = (rows[0] if rows else {}).get("youTubeVideoUpload", {})
            state = up.get("state")
            if state == "PROCESSED" and up.get("videoId"):
                return str(up["videoId"])
            if state in ("FAILED", "REJECTED", "UNAVAILABLE"):
                raise NetworkError(f"google: the YouTube upload {upload_rn} is {state}")
            if i < tries - 1:
                self.sleep(0.05 if self.mode == "mock" else min(30.0, 5.0 * (i + 1)))  # the mock processes at once
        raise NetworkError("google: the YouTube upload is still processing; run create again in a few minutes")

    def execute(self, op: Op, payload: dict[str, Any], ids: dict[str, str]) -> str:
        if op.kind == "video_upload":
            return self._upload_video(op, payload)
        if op.kind == "video_asset":
            upload_rn = payload.pop("_from_upload")
            payload["operations"][0]["create"]["youtubeVideoAsset"]["youtubeVideoId"] = self._video_id(
                upload_rn, op.key
            )
        return self._mutate(op.path, payload, op.key)[0]

    def find(self, op: Op, ids: dict[str, str] | None = None) -> str | None:
        if op.kind == "criteria":
            camp = next((v for k, v in (ids or {}).items() if v and "/campaigns/" in v), None)
            if not camp:
                return None
            rows = self.search(
                "SELECT campaign_criterion.resource_name FROM campaign_criterion "
                f"WHERE campaign.resource_name = '{camp}'",
                op.key,
            )
            return rows[0]["campaignCriterion"]["resourceName"] if rows else None
        resource, field = FIND[op.kind]
        rows = self.search(
            f"SELECT {resource}.resource_name, {field} FROM {resource} WHERE {field} LIKE '%{_tag(op)}%'", op.key
        )
        if not rows:
            return None
        return str(rows[0][RESULT_KEY[resource]]["resourceName"])

    def _update(self, service: str, resource_names: list[str], status: str) -> None:
        if resource_names:
            body = {
                "operations": [
                    {"update": {"resourceName": r, "status": status}, "updateMask": "status"} for r in resource_names
                ]
            }
            self._mutate(f"/{V}/customers/{self.account}/{service}:mutate", body)

    def set_status(self, rec: CampaignRecord, active: bool) -> None:
        if not active:
            self._update("campaigns", [rec.campaign_id], "PAUSED")
            return
        self._update("adGroupAds", rec.children.get("ad", []), "ENABLED")
        self._update("adGroups", rec.children.get("adgroup", []), "ENABLED")
        self._update("campaigns", [rec.campaign_id], "ENABLED")

    def set_ad_status(self, rec: CampaignRecord, ad_id: str, active: bool) -> None:
        raise SafetyError(
            "google: one video of an App ad cannot be paused on its own; remove it from the ad by hand in Google Ads"
        )

    def read(self, rec: CampaignRecord) -> dict[str, Any]:
        rows = self.search(
            "SELECT campaign.resource_name, campaign.status, campaign_budget.amount_micros, metrics.cost_micros, "
            f"customer.currency_code FROM campaign WHERE campaign.resource_name = '{rec.campaign_id}'"
        )
        r = rows[0] if rows else {}
        c, b, m = r.get("campaign", {}), r.get("campaignBudget", {}), r.get("metrics", {})
        return {
            "status": "ACTIVE" if c.get("status") == "ENABLED" else "PAUSED",
            "effective_status": c.get("status"),
            "daily": (num(b.get("amountMicros")) or 0) / 1_000_000,
            "spend": (num(m.get("costMicros")) or 0) / 1_000_000,
            "currency": str(r.get("customer", {}).get("currencyCode", "")),
        }

    def insights(self, rec: CampaignRecord, since: date, until: date) -> list[PerfRow]:
        """Per video asset per day from ``ad_group_ad_asset_view``; App campaign conversions are the installs."""
        rows = self.search(
            "SELECT asset.resource_name, asset.name, segments.date, metrics.impressions, metrics.clicks, "
            "metrics.cost_micros, metrics.conversions FROM ad_group_ad_asset_view WHERE "
            f"campaign.resource_name = '{rec.campaign_id}' AND ad_group_ad_asset_view.field_type = 'YOUTUBE_VIDEO' "
            f"AND segments.date BETWEEN '{since.isoformat()}' AND '{until.isoformat()}'"
        )
        out = []
        for r in rows:
            a, s, m = r.get("asset", {}), r.get("segments", {}), r.get("metrics", {})
            out.append(
                PerfRow(
                    network="google",
                    account=self.account,
                    level="video_asset",
                    date=str(s.get("date")),
                    ad_key=str(a.get("resourceName")),
                    ad_name=a.get("name"),
                    impressions=num(m.get("impressions")),
                    clicks=num(m.get("clicks")),
                    spend=(num(m.get("costMicros")) or 0) / 1_000_000,
                    installs=num(m.get("conversions")),
                    source="forge-campaign-sync",
                )
            )
        return out
