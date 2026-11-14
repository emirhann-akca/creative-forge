"""A local mock of the three ad networks' campaign APIs, for contract tests, the demo and trying the UI.

One ``ThreadingHTTPServer`` on 127.0.0.1 serves ``/meta/...`` (Graph API v27.0), ``/tiktok/...`` (API for Business
v1.3) and ``/google/...`` (Google Ads API v26) with the request and response shapes the adapters were written from
(``versions.py``). It is a contract check, not a copy of the networks:

* it rejects a request without the network's auth header, a missing required field, an unknown enum value, a
  reference to an object that does not exist, a wrong upload signature;
* it applies each network's documented default when a status is left out (Meta: required; TikTok:
  ``operation_status`` defaults to ENABLE; Google: a new campaign defaults to ENABLED), so a forgotten "paused" shows up
  as an object created active, which the tests look for;
* TikTok's ``request_id`` is honoured (a repeat returns the first object), Google refuses a duplicate campaign name,
  Meta has neither: the adapters' own idempotency is what the tests exercise;
* faults can be injected (``fault()``): 429 before processing, 500 before, 500 *after* creating the object, or a
  connection dropped after creating it, the cases where a naive client creates duplicates;
* ``advance(days)`` simulates delivery for whatever is active (``delivery.py``), and the reporting endpoints serve it.

State lives in memory and, when a path is given, in a JSON file, so separate CLI runs see the same mock objects.
Nothing here talks to the internet.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import threading
import urllib.parse
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from creative_forge.campaigns.delivery import simulate_day
from creative_forge.campaigns.versions import GOOGLE_ADS_VERSION, META_VERSION, TIKTOK_VERSION

TOKEN = "mock-token"
META_OBJECTIVES = {
    "OUTCOME_APP_PROMOTION",
    "OUTCOME_AWARENESS",
    "OUTCOME_ENGAGEMENT",
    "OUTCOME_LEADS",
    "OUTCOME_SALES",
    "OUTCOME_TRAFFIC",
    "APP_INSTALLS",
}
META_EDGES = {"campaigns": "campaign", "adsets": "adset", "advideos": "video", "adcreatives": "creative", "ads": "ad"}
TT_BUDGET_MODES = {"BUDGET_MODE_INFINITE", "BUDGET_MODE_DAY", "BUDGET_MODE_TOTAL"}
TT_STATUS = {"ENABLE", "DISABLE"}


class Reply(Exception):
    def __init__(self, status: int, body: Any, headers: dict[str, str] | None = None) -> None:
        super().__init__(status)
        self.status, self.body, self.headers = status, body, headers or {}


class Drop(Exception):
    """Close the connection without an answer (the object was created)."""


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _fresh() -> dict[str, Any]:
    return {
        "seq": 1000,
        "meta": {},
        "tiktok": {},
        "google": {},
        "delivery": [],
        "truth": [],
        "clock": None,
        "accounts": {},
        "request_ids": {},
        "sessions": {},
    }


class MockNetwork:
    def __init__(self, path: Path | None = None, seed: int = 7) -> None:
        self.path = Path(path) if path else None
        self.seed = seed
        self.lock = threading.RLock()
        self.faults: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str, str]] = []
        self.data = _fresh()
        if self.path and self.path.is_file():
            self.data = {**_fresh(), **json.loads(self.path.read_text(encoding="utf-8"))}

    # ------------------------------------------------------------------------------------------------ plumbing
    def save(self) -> None:
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)

    def _id(self) -> str:
        self.data["seq"] += 1
        return str(self.data["seq"])

    def account(self, network: str, account: str) -> dict[str, Any]:
        base = {"currency": "USD", "funding_source": "", "test_account": True, "status": "STATUS_ENABLE"}
        return base | self.data["accounts"].get(f"{network}:{account}", {})

    def set_account(self, network: str, account: str, **fields: Any) -> None:
        with self.lock:
            self.data["accounts"][f"{network}:{account}"] = (
                self.data["accounts"].get(f"{network}:{account}", {}) | fields
            )
            self.save()

    def fault(self, network: str, path_contains: str, mode: str, times: int = 1) -> None:
        """mode: ``429``, ``500_before``, ``500_after`` (object created, answer is 500), ``drop_after``."""
        self.faults.append({"network": network, "path": path_contains, "mode": mode, "times": times})

    def set_truth(self, contains: str, ipm: float, ctr: float = 0.012, hook: float = 0.28) -> None:
        with self.lock:
            self.data["truth"].append({"contains": contains, "ipm": ipm, "ctr": ctr, "hook": hook})
            self.save()

    def objects(self, network: str, kind: str | None = None) -> list[dict[str, Any]]:
        return [o for o in self.data[network].values() if kind is None or o["_kind"] == kind]

    def _take_fault(self, network: str, method: str, path: str) -> str | None:
        if method == "GET":
            return None
        for f in self.faults:
            if f["network"] == network and f["path"] in path and f["times"] > 0:
                f["times"] -= 1
                return str(f["mode"])
        return None

    def handle(
        self, method: str, raw_path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, Any, dict[str, str]]:
        parts = urllib.parse.urlsplit(raw_path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parts.query, keep_blank_values=True).items()}
        segs = parts.path.strip("/").split("/", 1)
        network, rest = segs[0], "/" + (segs[1] if len(segs) > 1 else "")
        headers = {k.lower(): v for k, v in headers.items()}
        fields, files = _parse_body(headers.get("content-type", ""), body)
        with self.lock:
            self.calls.append((network, method, rest))
            mode = self._take_fault(network, method, rest)
            try:
                if mode == "429":
                    raise self._throttle(network)
                if mode == "500_before":
                    raise Reply(500, {"error": {"message": "mock: internal error", "code": 2}})
                handler = {"meta": self._meta, "tiktok": self._tiktok, "google": self._google}.get(network)
                if handler is None:
                    raise Reply(404, {"error": f"no mock network {network!r}"})
                status, out, extra = handler(method, rest, query, headers, fields, files)
                self.save()
                if mode == "500_after":
                    raise Reply(500, {"error": {"message": "mock: internal error after commit", "code": 2}})
                if mode == "drop_after":
                    raise Drop()
                return status, out, extra
            except Reply as r:
                self.save()
                return r.status, r.body, r.headers

    @staticmethod
    def _throttle(network: str) -> Reply:
        if network == "meta":
            return Reply(
                400, {"error": {"message": "User request limit reached", "type": "OAuthException", "code": 17}}
            )
        if network == "tiktok":
            return Reply(200, {"code": 40100, "message": "Too many requests", "data": {}})
        return Reply(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota"}})

    # ------------------------------------------------------------------------------------------------------ meta
    def _meta_error(self, msg: str, code: int = 100) -> Reply:
        return Reply(400, {"error": {"message": msg, "type": "OAuthException", "code": code, "fbtrace_id": "mock"}})

    def _meta(self, method, path, query, headers, fields, files):  # type: ignore[no-untyped-def]
        if headers.get("authorization") != f"Bearer {TOKEN}":
            raise self._meta_error("Invalid OAuth access token.", 190)
        prefix = f"/{META_VERSION}/"
        if not path.startswith(prefix):
            raise self._meta_error(f"Unsupported version in {path}", 2635)
        segs = path[len(prefix) :].strip("/").split("/")
        objs = self.data["meta"]
        if segs[0].startswith("act_"):
            act = segs[0]
            if len(segs) == 1:
                a = self.account("meta", act)
                return (
                    200,
                    {
                        "id": act,
                        "name": "mock ad account",
                        "currency": a["currency"],
                        "account_status": 1,
                        "funding_source": a["funding_source"],
                    },
                    {},
                )
            edge = segs[1]
            if edge == "insights" and method == "GET":
                return 200, self._meta_insights(act, query), {}
            if edge not in META_EDGES:
                raise self._meta_error(f"Unknown edge {edge}")
            kind = META_EDGES[edge]
            if method == "GET":
                return 200, self._meta_list(act, kind, query), {}
            return 200, {"id": self._meta_create(act, kind, fields, files)}, {}
        oid = segs[0]
        if oid not in objs:
            raise self._meta_error(f"Object with ID '{oid}' does not exist", 100)
        o = objs[oid]
        if len(segs) == 2 and segs[1] == "thumbnails":
            return (
                200,
                {"data": [{"id": f"{oid}1", "uri": f"https://mock.invalid/thumb/{oid}.jpg", "is_preferred": True}]},
                {},
            )
        if len(segs) == 2 and segs[1] == "insights":
            spend = sum(r["spend"] for r in self.data["delivery"] if r["network"] == "meta" and r["campaign"] == oid)
            return 200, {"data": [{"spend": f"{spend:.2f}", "date_start": "", "date_stop": ""}]}, {}
        if method == "POST":
            status = fields.get("status")
            if status not in ("ACTIVE", "PAUSED", "ARCHIVED", "DELETED"):
                raise self._meta_error("Param status must be one of {ACTIVE, PAUSED, DELETED, ARCHIVED}")
            o["status"] = status
            return 200, {"success": True}, {}
        out = {k: v for k, v in o.items() if not k.startswith("_")}
        if o["_kind"] == "campaign":
            out["effective_status"] = o["status"]
            out["daily_budget"] = str(o.get("daily_budget", ""))
        return 200, out, {}

    def _meta_create(self, act: str, kind: str, f: dict[str, Any], files: dict[str, Any]) -> str:
        objs = self.data["meta"]

        def need(*names: str) -> None:
            for n in names:
                if f.get(n) in (None, ""):
                    raise self._meta_error(f"(#100) The parameter {n} is required")

        def exists(oid: Any, k: str) -> None:
            if str(oid) not in objs or objs[str(oid)]["_kind"] != k or objs[str(oid)]["_account"] != act:
                raise self._meta_error(f"(#100) {k} {oid} does not exist in {act}")

        if kind == "campaign":
            need("name", "objective", "status")
            if "special_ad_categories" not in f:
                raise self._meta_error("(#100) The parameter special_ad_categories is required")
            if f["objective"] not in META_OBJECTIVES:
                raise self._meta_error(f"(#100) Param objective must be one of {sorted(META_OBJECTIVES)}")
        elif kind == "adset":
            need("name", "campaign_id", "optimization_goal", "billing_event", "promoted_object", "targeting", "status")
            exists(f["campaign_id"], "campaign")
            po = f["promoted_object"]
            if not po.get("application_id") or not po.get("object_store_url"):
                raise self._meta_error("(#100) promoted_object needs application_id and object_store_url")
            if "geo_locations" not in f["targeting"]:
                raise self._meta_error("(#100) targeting needs geo_locations")
            if objs[str(f["campaign_id"])].get("daily_budget") and (f.get("daily_budget") or f.get("lifetime_budget")):
                raise self._meta_error("(#100) the campaign has a budget; an ad set cannot have one too")
        elif kind == "video":
            if "source" not in files:
                raise self._meta_error("(#100) no video file in source")
        elif kind == "creative":
            need("name", "object_story_spec")
            vd = (f["object_story_spec"] or {}).get("video_data") or {}
            exists(vd.get("video_id"), "video")
            if not (vd.get("image_url") or vd.get("image_hash")):
                raise self._meta_error("(#100) video_data needs image_url or image_hash")
            if (vd.get("call_to_action") or {}).get("type") != "INSTALL_MOBILE_APP":
                raise self._meta_error("(#100) app promotion needs call_to_action INSTALL_MOBILE_APP")
        elif kind == "ad":
            need("name", "adset_id", "creative", "status")
            exists(f["adset_id"], "adset")
            exists((f["creative"] or {}).get("creative_id"), "creative")
        oid = self._id()
        obj = {k: v for k, v in f.items()} | {
            "id": oid,
            "_kind": kind,
            "_account": act,
            "_created_status": f.get("status"),
        }
        if kind == "video":
            _, data = files["source"]
            obj |= {"_bytes": len(data), "_sha256": hashlib.sha256(data).hexdigest()}
        objs[oid] = obj
        return oid

    def _meta_list(self, act: str, kind: str, q: dict[str, str]) -> dict[str, Any]:
        items = [o for o in self.data["meta"].values() if o["_kind"] == kind and o["_account"] == act]
        for flt in json.loads(q.get("filtering", "[]") or "[]"):
            if flt.get("operator") == "CONTAIN":
                items = [o for o in items if str(flt["value"]) in str(o.get(flt["field"], ""))]
        fields = (q.get("fields") or "id,name").split(",")
        limit, after = int(q.get("limit", 25)), int(q.get("after", 0) or 0)
        page = items[after : after + limit]
        out: dict[str, Any] = {"data": [{k: o.get(k) for k in fields} for o in page]}
        if after + limit < len(items):
            nxt = dict(q) | {"after": str(after + limit), "access_token": TOKEN}
            edge = META_EDGES_REV[kind]
            out["paging"] = {"next": f"{self.base_url}/meta/{META_VERSION}/{act}/{edge}?{urllib.parse.urlencode(nxt)}"}
        return out

    def _meta_insights(self, act: str, q: dict[str, str]) -> dict[str, Any]:
        tr = json.loads(q.get("time_range", "{}") or "{}")
        since, until = tr.get("since", "0000"), tr.get("until", "9999")
        camp_ids: set[str] | None = None
        for flt in json.loads(q.get("filtering", "[]") or "[]"):
            if flt.get("field") == "campaign.id" and flt.get("operator") == "IN":
                camp_ids = {str(v) for v in flt["value"]}
        objs = self.data["meta"]
        rows = []
        for r in self.data["delivery"]:
            if r["network"] != "meta" or not since <= r["date"] <= until:
                continue
            ad = objs.get(r["unit"], {})
            if ad.get("_account") != act or (camp_ids is not None and r["campaign"] not in camp_ids):
                continue
            rows.append(
                {
                    "date_start": r["date"],
                    "date_stop": r["date"],
                    "ad_id": r["unit"],
                    "ad_name": r["name"],
                    "adset_name": objs.get(str(ad.get("adset_id")), {}).get("name"),
                    "campaign_id": r["campaign"],
                    "campaign_name": objs.get(r["campaign"], {}).get("name"),
                    "account_currency": "USD",
                    "impressions": str(r["impressions"]),
                    "clicks": str(r["clicks"]),
                    "spend": f"{r['spend']:.2f}",
                    "actions": [
                        {"action_type": "mobile_app_install", "value": str(r["installs"])},
                        {"action_type": "video_view", "value": str(r["video_3s"])},
                    ],
                    **{
                        f"video_{p}_watched_actions": [{"action_type": "video_view", "value": str(r[p])}]
                        for p in ("p25", "p50", "p75", "p100")
                    },
                }
            )
        limit, after = int(q.get("limit", 25)), int(q.get("after", 0) or 0)
        out: dict[str, Any] = {"data": rows[after : after + limit]}
        if after + limit < len(rows):
            nxt = dict(q) | {"after": str(after + limit), "access_token": TOKEN}
            out["paging"] = {
                "next": f"{self.base_url}/meta/{META_VERSION}/{act}/insights?{urllib.parse.urlencode(nxt)}"
            }
        return out

    # ---------------------------------------------------------------------------------------------------- tiktok
    @staticmethod
    def _tt(data: Any) -> tuple[int, Any, dict[str, str]]:
        return 200, {"code": 0, "message": "OK", "request_id": "mock", "data": data}, {}

    @staticmethod
    def _tt_error(msg: str, code: int = 40002) -> Reply:
        return Reply(200, {"code": code, "message": msg, "request_id": "mock", "data": {}})

    def _tiktok(self, method, path, query, headers, fields, files):  # type: ignore[no-untyped-def]
        if headers.get("access-token") != TOKEN:
            raise self._tt_error("Access token is incorrect or has been revoked.", 40105)
        prefix = f"/open_api/{TIKTOK_VERSION}/"
        if not path.startswith(prefix):
            raise self._tt_error(f"unknown API version in {path}", 40001)
        ep = path[len(prefix) :].strip("/") + "/"
        q = {k: (json.loads(v) if v[:1] in "[{" or v in ("true", "false") else v) for k, v in query.items()}
        objs = self.data["tiktok"]
        if method == "GET":
            if ep == "advertiser/info/":
                return self._tt(
                    {
                        "list": [
                            {
                                "advertiser_id": a,
                                "name": "mock advertiser",
                                **{k: v for k, v in self.account("tiktok", a).items() if k in ("currency", "status")},
                            }
                            for a in q.get("advertiser_ids", [])
                        ]
                    }
                )
            if ep in ("campaign/get/", "adgroup/get/", "ad/get/"):
                return self._tt(self._tt_list(ep.split("/")[0], q))
            if ep == "file/video/ad/info/":
                vids = [objs[v] for v in q.get("video_ids", []) if v in objs]
                return self._tt(
                    {
                        "list": [
                            {
                                "video_id": v["video_id"],
                                "file_name": v["file_name"],
                                "video_cover_url": f"https://mock.invalid/cover/{v['video_id']}.jpg",
                            }
                            for v in vids
                        ]
                    }
                )
            if ep in ("file/video/ad/search/", "file/image/ad/search/"):
                kind = "video" if "video" in ep else "image"
                names = set((q.get("filtering") or {}).get("file_names") or [])
                items = [o for o in objs.values() if o["_kind"] == kind and (not names or o["file_name"] in names)]
                return self._tt(
                    {
                        "list": [{k: v for k, v in o.items() if not k.startswith("_")} for o in items],
                        "page_info": {"page": 1, "page_size": 100, "total_number": len(items), "total_page": 1},
                    }
                )
            if ep == "report/integrated/get/":
                return self._tt(self._tt_report(q))
            raise self._tt_error(f"mock: no GET {ep}", 40001)
        f = fields
        rid = f.get("request_id")
        if rid and f"{ep}{rid}" in self.data["request_ids"]:
            return self._tt(self.data["request_ids"][f"{ep}{rid}"])

        def need(*names: str) -> None:
            for n in names:
                if f.get(n) in (None, "", []):
                    raise self._tt_error(f"{n}: Missing data for required field.")

        def exists(oid: Any, kind: str) -> None:
            if str(oid) not in objs or objs[str(oid)]["_kind"] != kind:
                raise self._tt_error(f"{kind} {oid} does not exist", 40002)

        if ep == "campaign/create/":
            need("advertiser_id", "campaign_name", "objective_type")
            if f["objective_type"] == "APP_PROMOTION":
                need("app_promotion_type")
            mode = f.get("budget_mode", "BUDGET_MODE_INFINITE")
            if mode not in TT_BUDGET_MODES or (mode != "BUDGET_MODE_INFINITE" and not f.get("budget")):
                raise self._tt_error("budget_mode / budget invalid")
            oid = self._tt_new("campaign", f, "campaign_id")
            out = {"campaign_id": oid}
        elif ep == "adgroup/create/":
            need(
                "advertiser_id",
                "campaign_id",
                "adgroup_name",
                "budget_mode",
                "budget",
                "schedule_type",
                "schedule_start_time",
                "optimization_goal",
                "billing_event",
                "location_ids",
            )
            exists(f["campaign_id"], "campaign")
            if objs[str(f["campaign_id"])].get("objective_type") == "APP_PROMOTION":
                need("app_id", "promotion_type")
            out = {"adgroup_id": self._tt_new("adgroup", f, "adgroup_id")}
        elif ep == "file/video/ad/upload/":
            need("advertiser_id", "upload_type")
            if "video_file" not in files:
                raise self._tt_error("video_file: Missing data for required field.")
            _, data = files["video_file"]
            if hashlib.md5(data).hexdigest() != f.get("video_signature"):
                raise self._tt_error("video_signature does not match the file", 40911)
            oid = self._tt_new("video", f | {"_bytes": len(data)}, "video_id")
            return self._tt([{"video_id": oid, "file_name": f.get("file_name"), "size": len(data)}])
        elif ep == "file/image/ad/upload/":
            need("advertiser_id", "upload_type")
            if f["upload_type"] == "UPLOAD_BY_URL":
                need("image_url")
            out = {"image_id": self._tt_new("image", f, "image_id"), "file_name": f.get("file_name")}
        elif ep == "ad/create/":
            need("advertiser_id", "adgroup_id", "creatives")
            exists(f["adgroup_id"], "adgroup")
            ids = []
            for c in f["creatives"]:
                for n in ("ad_name", "ad_format", "video_id", "identity_type", "identity_id", "ad_text"):
                    if not c.get(n):
                        raise self._tt_error(f"creatives.{n}: Missing data for required field.")
                exists(c["video_id"], "video")
                for img in c.get("image_ids") or []:
                    exists(img, "image")
                ids.append(
                    self._tt_new(
                        "ad", c | {"adgroup_id": f["adgroup_id"], "advertiser_id": f["advertiser_id"]}, "ad_id"
                    )
                )
            out = {"ad_ids": ids}
        elif ep in ("campaign/status/update/", "adgroup/status/update/", "ad/status/update/"):
            kind = ep.split("/")[0]
            if f.get("operation_status") not in ("ENABLE", "DISABLE", "DELETE"):
                raise self._tt_error("operation_status invalid")
            for oid in f.get(f"{kind}_ids") or []:
                exists(oid, kind)
                objs[str(oid)]["operation_status"] = f["operation_status"]
            out = {f"{kind}_ids": f.get(f"{kind}_ids")}
        else:
            raise self._tt_error(f"mock: no POST {ep}", 40001)
        if rid:
            self.data["request_ids"][f"{ep}{rid}"] = out
        return self._tt(out)

    def _tt_new(self, kind: str, f: dict[str, Any], id_field: str) -> str:
        oid = "7" + self._id().rjust(18, "0")
        status = f.get("operation_status", "ENABLE") if kind in ("campaign", "adgroup", "ad") else None
        if status is not None and status not in TT_STATUS:
            raise self._tt_error("operation_status invalid")
        obj = dict(f) | {id_field: oid, "_kind": kind, "_created_status": status}
        if status is not None:
            obj["operation_status"] = status
        self.data["tiktok"][oid] = obj
        return oid

    def _tt_list(self, kind: str, q: dict[str, Any]) -> dict[str, Any]:
        flt = q.get("filtering") or {}
        items = [o for o in self.data["tiktok"].values() if o["_kind"] == kind]
        name_field = f"{kind}_name"
        if flt.get(name_field):
            items = [o for o in items if str(flt[name_field]) in str(o.get(name_field, ""))]
        if flt.get("campaign_ids"):
            items = [o for o in items if str(o.get("campaign_id")) in {str(x) for x in flt["campaign_ids"]}]
        page, size = int(q.get("page", 1)), int(q.get("page_size", 10))
        shown = items[(page - 1) * size : page * size]
        total_page = max(1, -(-len(items) // size))
        return {
            "list": [{k: v for k, v in o.items() if not k.startswith("_")} for o in shown],
            "page_info": {"page": page, "page_size": size, "total_number": len(items), "total_page": total_page},
        }

    def _tt_report(self, q: dict[str, Any]) -> dict[str, Any]:
        camp_ids: set[str] = set()
        for flt in q.get("filtering") or []:
            if flt.get("field_name") == "campaign_ids":
                camp_ids |= {str(x) for x in json.loads(flt["filter_value"])}
        rows = [
            r for r in self.data["delivery"] if r["network"] == "tiktok" and (not camp_ids or r["campaign"] in camp_ids)
        ]
        objs = self.data["tiktok"]
        if q.get("data_level") == "AUCTION_CAMPAIGN":
            out = [
                {
                    "dimensions": {"campaign_id": c},
                    "metrics": {"spend": f"{sum(r['spend'] for r in rows if r['campaign'] == c):.2f}"},
                }
                for c in sorted({r["campaign"] for r in rows})
            ]
            return {"list": out, "page_info": {"page": 1, "page_size": 1000, "total_number": len(out), "total_page": 1}}
        since, until = q.get("start_date", "0000"), q.get("end_date", "9999")
        rows = [r for r in rows if since <= r["date"] <= until]
        page, size = int(q.get("page", 1)), int(q.get("page_size", 10))
        shown = rows[(page - 1) * size : page * size]
        out = [
            {
                "dimensions": {"ad_id": r["unit"], "stat_time_day": f"{r['date']} 00:00:00"},
                "metrics": {
                    "ad_name": objs.get(r["unit"], {}).get("ad_name", r["name"]),
                    "spend": f"{r['spend']:.2f}",
                    "impressions": str(r["impressions"]),
                    "clicks": str(r["clicks"]),
                    "app_install": str(r["installs"]),
                    "video_watched_2s": str(r["video_2s"]),
                    "video_watched_6s": str(r["video_6s"]),
                    "video_views_p25": str(r["p25"]),
                    "video_views_p50": str(r["p50"]),
                    "video_views_p75": str(r["p75"]),
                    "video_views_p100": str(r["p100"]),
                },
            }
            for r in shown
        ]
        return {
            "list": out,
            "page_info": {
                "page": page,
                "page_size": size,
                "total_number": len(rows),
                "total_page": max(1, -(-len(rows) // size)),
            },
        }

    # ---------------------------------------------------------------------------------------------------- google
    @staticmethod
    def _g_error(msg: str, status: int = 400, kind: str = "INVALID_ARGUMENT") -> Reply:
        return Reply(status, {"error": {"code": status, "status": kind, "message": msg}})

    def _google(self, method, path, query, headers, fields, files):  # type: ignore[no-untyped-def]
        if headers.get("authorization") != f"Bearer {TOKEN}" or not headers.get("developer-token"):
            raise self._g_error("Request is missing required authentication credential.", 401, "UNAUTHENTICATED")
        v = GOOGLE_ADS_VERSION
        if method == "PUT" and path.startswith("/resumable/session/"):
            sess = self.data["sessions"].pop(path.rsplit("/", 1)[-1], None)
            if sess is None or "finalize" not in headers.get("x-goog-upload-command", ""):
                raise self._g_error("unknown or unfinished upload session", 404, "NOT_FOUND")
            rn = f"customers/{sess['customer']}/youTubeVideoUploads/{self._id()}"
            self.data["google"][rn] = {
                "_kind": "you_tube_video_upload",
                "resourceName": rn,
                "_customer": sess["customer"],
                "videoTitle": sess["title"],
                "videoId": f"yt{self._id()}",
                "state": "PENDING",
                "_polls": 0,
                "_bytes": len(fields.get("_raw", b"")),
            }
            return 200, {"resourceName": rn}, {}
        m = re.fullmatch(rf"/resumable/upload/{v}/customers/(\d{{10}})/youTubeVideoUploads:create", path)
        if m:
            if headers.get("x-goog-upload-command") != "start" or headers.get("x-goog-upload-protocol") != "resumable":
                raise self._g_error("expected x-goog-upload-protocol: resumable and x-goog-upload-command: start")
            up = fields.get("you_tube_video_upload") or {}
            title = up.get("video_title", "")
            if not title or len(title) > 100 or "<" in title or ">" in title:
                raise self._g_error("video_title is required, at most 100 characters, without < or >")
            token = hashlib.sha256(f"{title}{self._id()}".encode()).hexdigest()[:16]
            self.data["sessions"][token] = {"customer": m.group(1), "title": title}
            return (
                200,
                {},
                {
                    "x-goog-upload-url": f"{self.base_url}/google/resumable/session/{token}",
                    "x-goog-upload-chunk-granularity": "262144",
                },
            )
        m = re.fullmatch(rf"/{v}/customers/(\d{{10}})/(\w+):(mutate|search)", path)
        if not m:
            raise self._g_error(f"mock: no {method} {path}", 404, "NOT_FOUND")
        cid, service, verb = m.groups()
        if verb == "search":
            return 200, self._g_search(cid, fields.get("query", "")), {}
        ops = fields.get("operations") or []
        if not ops:
            raise self._g_error("operations is required")
        results = [{"resourceName": self._g_mutate(cid, service, o)} for o in ops]
        return 200, {"results": results}, {}

    def _g_mutate(self, cid: str, service: str, op: dict[str, Any]) -> str:
        g = self.data["google"]
        kinds = {
            "campaignBudgets": "campaign_budget",
            "campaigns": "campaign",
            "campaignCriteria": "campaign_criterion",
            "adGroups": "ad_group",
            "assets": "asset",
            "adGroupAds": "ad_group_ad",
        }
        kind = kinds.get(service)
        if kind is None:
            raise self._g_error(f"mock: no service {service}", 404, "NOT_FOUND")
        if "update" in op:
            u = op["update"]
            rn = u.get("resourceName", "")
            if rn not in g or op.get("updateMask") != "status":
                raise self._g_error(f"cannot update {rn!r} (mock supports updateMask=status)")
            if u.get("status") not in ("ENABLED", "PAUSED", "REMOVED"):
                raise self._g_error("status invalid")
            g[rn]["status"] = u["status"]
            return rn
        c = dict(op.get("create") or {})

        def ref(rn: Any, k: str) -> None:
            if str(rn) not in g or g[str(rn)]["_kind"] != k:
                raise self._g_error(f"{k} {rn} not found", 400)

        if kind == "campaign_budget":
            if not c.get("name") or not str(c.get("amountMicros", "")).isdigit():
                raise self._g_error("budget needs name and amountMicros")
        elif kind == "campaign":
            if not c.get("name"):
                raise self._g_error("campaign name is required")
            if any(o["_kind"] == "campaign" and o["name"] == c["name"] for o in g.values()):
                raise self._g_error("DUPLICATE_CAMPAIGN_NAME", 400)
            ref(c.get("campaignBudget"), "campaign_budget")
            if c.get("advertisingChannelSubType") == "APP_CAMPAIGN":
                s = c.get("appCampaignSetting") or {}
                if c.get("advertisingChannelType") != "MULTI_CHANNEL" or not s.get("appId") or not s.get("appStore"):
                    raise self._g_error("App campaigns need MULTI_CHANNEL and appCampaignSetting.appId/appStore")
                if g[c["campaignBudget"]].get("explicitlyShared"):
                    raise self._g_error("App campaigns cannot use a shared budget")
            c.setdefault("status", "ENABLED")  # the documented default
        elif kind == "campaign_criterion":
            ref(c.get("campaign"), "campaign")
        elif kind == "ad_group":
            ref(c.get("campaign"), "campaign")
            c.setdefault("status", "ENABLED")
        elif kind == "asset":
            vid = (c.get("youtubeVideoAsset") or {}).get("youtubeVideoId")
            if not any(
                o["_kind"] == "you_tube_video_upload" and o.get("videoId") == vid and o["state"] == "PROCESSED"
                for o in g.values()
            ):
                raise self._g_error(f"youtubeVideoId {vid!r} is not a processed upload")
        elif kind == "ad_group_ad":
            ref(c.get("adGroup"), "ad_group")
            app = (c.get("ad") or {}).get("appAd") or {}
            if not 1 <= len(app.get("headlines") or []) <= 5 or not 1 <= len(app.get("descriptions") or []) <= 5:
                raise self._g_error("appAd needs 1-5 headlines and 1-5 descriptions")
            for yv in app.get("youtubeVideos") or []:
                ref(yv.get("asset"), "asset")
            c.setdefault("status", "ENABLED")
        oid = self._id()
        if kind == "ad_group_ad":
            rn = f"customers/{cid}/adGroupAds/{c['adGroup'].rsplit('/', 1)[-1]}~{oid}"
        else:
            rn = f"customers/{cid}/{service}/{oid}"
        g[rn] = c | {"resourceName": rn, "_kind": kind, "_customer": cid, "_created_status": c.get("status")}
        return rn

    def _g_rows(self, cid: str, resource: str) -> list[dict[str, Any]]:
        g = self.data["google"]
        mine = [o for o in g.values() if o.get("_customer") == cid]

        def public(o: dict[str, Any]) -> dict[str, Any]:
            return {k: v for k, v in o.items() if not k.startswith("_")}

        if resource == "customer":
            a = self.account("google", cid)
            return [
                {
                    "customer": {
                        "resourceName": f"customers/{cid}",
                        "id": cid,
                        "currencyCode": a["currency"],
                        "testAccount": a["test_account"],
                    }
                }
            ]
        if resource == "ad_group_ad_asset_view":
            rows = []
            for r in self.data["delivery"]:
                if r["network"] == "google" and r["unit"] in g and g[r["unit"]].get("_customer") == cid:
                    rows.append(
                        {
                            "campaign": {"resourceName": r["campaign"]},
                            "asset": {"resourceName": r["unit"], "name": g[r["unit"]].get("name")},
                            "adGroupAdAssetView": {"fieldType": "YOUTUBE_VIDEO"},
                            "segments": {"date": r["date"]},
                            "metrics": {
                                "impressions": str(r["impressions"]),
                                "clicks": str(r["clicks"]),
                                "costMicros": str(round(r["spend"] * 1e6)),
                                "conversions": float(r["installs"]),
                            },
                        }
                    )
            return rows
        key = _camel(resource)
        rows = []
        for o in mine:
            if o["_kind"] != resource:
                continue
            row: dict[str, Any] = {key: public(o)}
            if resource == "campaign":
                spend = sum(
                    r["spend"]
                    for r in self.data["delivery"]
                    if r["network"] == "google" and r["campaign"] == o["resourceName"]
                )
                row["campaignBudget"] = public(g.get(o.get("campaignBudget", ""), {}))
                row["metrics"] = {"costMicros": str(round(spend * 1e6))}
                row["customer"] = {"currencyCode": self.account("google", cid)["currency"]}
            if resource == "campaign_criterion":
                row["campaign"] = {"resourceName": o.get("campaign")}
            if resource == "you_tube_video_upload":
                o["_polls"] = o.get("_polls", 0) + 1
                if o["_polls"] >= 2:  # PENDING on the first look, PROCESSED after: the adapter has to poll
                    o["state"] = "PROCESSED"
                row = {key: public(o)}
            rows.append(row)
        return rows

    def _g_search(self, cid: str, query: str) -> dict[str, Any]:
        m = re.search(r"\bFROM\s+(\w+)", query)
        if not m or not query.strip().upper().startswith("SELECT"):
            raise self._g_error(f"mock GAQL: cannot parse {query!r}")
        rows = self._g_rows(cid, m.group(1))
        where = query[m.end() :]
        between = re.search(r"segments\.date\s+BETWEEN\s+'([\d-]+)'\s+AND\s+'([\d-]+)'", where)
        if between:
            lo, hi = between.groups()
            rows = [r for r in rows if lo <= r.get("segments", {}).get("date", "") <= hi]
        for field, op, value in re.findall(r"([a-z_]+(?:\.[a-z_]+)+)\s*(=|LIKE)\s*'([^']*)'", where):

            def get(r: dict[str, Any], field: str = field) -> str:
                cur: Any = r
                for part in field.split("."):
                    cur = cur.get(_camel(part), {}) if isinstance(cur, dict) else {}
                return "" if isinstance(cur, dict) else str(cur)

            if op == "=":
                rows = [r for r in rows if get(r) == value]
            else:
                needle = value.strip("%")
                rows = [r for r in rows if needle in get(r)]
        return {"results": rows, "fieldMask": ",".join(re.findall(r"[a-z_]+\.[a-z_.]+", query.split("FROM")[0]))}

    # -------------------------------------------------------------------------------------------------- delivery
    def advance(self, days: int, start: date | None = None) -> int:
        """Simulate ``days`` days of delivery for everything active; returns the rows written."""
        with self.lock:
            day = start or (date.fromisoformat(self.data["clock"]) if self.data["clock"] else self._first_start())
            written = 0
            for _ in range(days):
                for network, camp_key, units, daily, remaining in self._active(day):
                    rows = simulate_day(network, camp_key, day, units, daily, remaining, self.data["truth"], self.seed)
                    self.data["delivery"] += rows
                    written += len(rows)
                day += timedelta(days=1)
            self.data["clock"] = day.isoformat()
            self.save()
            return written

    def _first_start(self) -> date:
        starts = [str(o.get("start_time", ""))[:10] for o in self.data["meta"].values() if o["_kind"] == "adset"]
        starts += [
            str(o.get("schedule_start_time", ""))[:10] for o in self.data["tiktok"].values() if o["_kind"] == "adgroup"
        ]
        starts += [
            str(o.get("startDateTime", ""))[:10] for o in self.data["google"].values() if o["_kind"] == "campaign"
        ]
        starts = [s for s in starts if s]
        return date.fromisoformat(min(starts)) if starts else date.today()

    def _spent(self, network: str, camp: str) -> float:
        return sum(r["spend"] for r in self.data["delivery"] if r["network"] == network and r["campaign"] == camp)

    def _active(self, day: date) -> list[tuple[str, str, list[tuple[str, str]], float, float | None]]:
        d = day.isoformat()
        out = []
        meta = self.data["meta"]
        for c in (o for o in meta.values() if o["_kind"] == "campaign" and o["status"] == "ACTIVE"):
            sets = [
                s
                for s in meta.values()
                if s["_kind"] == "adset"
                and str(s["campaign_id"]) == c["id"]
                and s["status"] == "ACTIVE"
                and str(s.get("start_time", ""))[:10] <= d <= str(s.get("end_time", "9999"))[:10]
            ]
            set_ids = {s["id"] for s in sets}
            units = [
                (a["id"], a["name"])
                for a in meta.values()
                if a["_kind"] == "ad" and str(a["adset_id"]) in set_ids and a["status"] == "ACTIVE"
            ]
            cap = float(c["spend_cap"]) / 100 - self._spent("meta", c["id"]) if c.get("spend_cap") else None
            out.append(("meta", c["id"], units, float(c.get("daily_budget") or 0) / 100, cap))
        tt = self.data["tiktok"]
        for c in (o for o in tt.values() if o["_kind"] == "campaign" and o["operation_status"] == "ENABLE"):
            for gr in (
                o
                for o in tt.values()
                if o["_kind"] == "adgroup"
                and str(o["campaign_id"]) == c["campaign_id"]
                and o["operation_status"] == "ENABLE"
                and str(o["schedule_start_time"])[:10] <= d <= str(o.get("schedule_end_time", "9999"))[:10]
            ):
                units = [
                    (a["ad_id"], a["ad_name"])
                    for a in tt.values()
                    if a["_kind"] == "ad"
                    and str(a["adgroup_id"]) == gr["adgroup_id"]
                    and a["operation_status"] == "ENABLE"
                ]
                cap = (
                    float(c["budget"]) - self._spent("tiktok", c["campaign_id"])
                    if c.get("budget_mode") == "BUDGET_MODE_TOTAL"
                    else None
                )
                out.append(("tiktok", c["campaign_id"], units, float(gr["budget"]), cap))
        g = self.data["google"]
        for c in (
            o
            for o in g.values()
            if o["_kind"] == "campaign"
            and o["status"] == "ENABLED"
            and str(o.get("startDateTime", ""))[:10] <= d <= str(o.get("endDateTime", "9999"))[:10]
        ):
            groups = {
                rn
                for rn, o in g.items()
                if o["_kind"] == "ad_group" and o["campaign"] == c["resourceName"] and o["status"] == "ENABLED"
            }
            units = []
            for ad in (
                o
                for o in g.values()
                if o["_kind"] == "ad_group_ad" and o["adGroup"] in groups and o["status"] == "ENABLED"
            ):
                for yv in ad["ad"]["appAd"].get("youtubeVideos") or []:
                    units.append((yv["asset"], g[yv["asset"]].get("name", "")))
            daily = float(g[c["campaignBudget"]]["amountMicros"]) / 1e6
            out.append(("google", c["resourceName"], units, daily, None))
        return out

    base_url = ""  # set by MockServer so pagination links point back at it


META_EDGES_REV = {v: k for k, v in META_EDGES.items()}


def _parse_body(content_type: str, body: bytes) -> tuple[dict[str, Any], dict[str, tuple[str, bytes]]]:
    if not body:
        return {}, {}
    ct = content_type.lower()
    if ct.startswith("application/json"):
        return json.loads(body.decode("utf-8")), {}
    if ct.startswith("application/x-www-form-urlencoded"):
        pairs = urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True)
        return {k: _maybe_json(v) for k, v in pairs}, {}
    if ct.startswith("multipart/form-data"):
        m = re.search(r"boundary=\"?([^\";]+)\"?", content_type)
        if not m:
            return {}, {}
        fields: dict[str, Any] = {}
        files: dict[str, tuple[str, bytes]] = {}
        for part in body.split(b"--" + m.group(1).encode())[1:-1]:
            head, _, data = part.strip(b"\r\n").partition(b"\r\n\r\n")
            disp = head.decode("utf-8", errors="replace")
            name = re.search(r'name="([^"]*)"', disp)
            fname = re.search(r'filename="([^"]*)"', disp)
            if not name:
                continue
            if fname:
                files[name.group(1)] = (fname.group(1), data)
            else:
                fields[name.group(1)] = _maybe_json(data.decode("utf-8"))
        return fields, files
    return {"_raw": body}, {}


def _maybe_json(v: str) -> Any:
    if v[:1] in "[{":
        with contextlib.suppress(ValueError):
            return json.loads(v)
    return v


class _Handler(BaseHTTPRequestHandler):
    server_version = "forge-mock-adnet/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _serve(self) -> None:
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        mock: MockNetwork = self.server.mock  # type: ignore[attr-defined]
        try:
            status, out, headers = mock.handle(self.command, self.path, dict(self.headers.items()), body)
        except Drop:
            self.close_connection = True
            with contextlib.suppress(OSError):
                self.connection.shutdown(2)
            return
        data = json.dumps(out).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = _serve


class MockServer:
    """``with MockServer(network) as url: ...``: the mock on 127.0.0.1 and a free port, in a daemon thread."""

    def __init__(self, network: MockNetwork | None = None, host: str = "127.0.0.1", port: int = 0) -> None:
        self.network = network or MockNetwork()
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.mock = self.network  # type: ignore[attr-defined]
        self.url = f"http://{host}:{self.httpd.server_address[1]}"
        self.network.base_url = self.url
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def __enter__(self) -> str:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()
