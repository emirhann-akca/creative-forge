"""`forge campaign demo`: render -> QA -> approve -> publish -> create (mock, paused) -> deliver -> report, offline."""

from __future__ import annotations

import json

import pytest

from creative_forge.campaigns.audit import AuditLog
from creative_forge.cli import main


@pytest.mark.slow
def test_campaign_demo_runs_the_whole_loop_offline(tmp_path, capsys):
    out = tmp_path / "Kampanya Ş"
    assert main(["campaign", "demo", "--out", str(out), "--ocr", "off"]) == 0
    printed = capsys.readouterr().out
    assert "9/9 audit log" in printed and "no real money, no network calls" in printed
    rep = json.loads((out / "campaign_report.json").read_text(encoding="utf-8"))
    assert rep["seconds"] > 0  # wall time is recorded, not asserted: shared CI runners took 71 s where an M4 takes 12
    assert rep["published"] == 3 and len(rep["excluded"]) == 1 and "rejected by the reviewer" in rep["excluded"][0]
    assert rep["created"] == {"meta": 11, "tiktok": 8, "google": 9}
    assert rep["created_again"] == {"meta": 0, "tiktok": 0, "google": 0}
    assert rep["paused_at_creation"] is True and rep["paused_day_rows"] == 0 and rep["delivered_rows"] > 0
    assert rep["audit_ok"] is True and AuditLog(out / "state" / "audit.jsonl").verify().ok
    for network in ("meta", "tiktok", "google"):
        assert 0 < rep["spend"][network] <= 400  # never past the total cap
        acts = {r["target"].split(".")[2] + "." + r["target"].split(".")[6]: r["action"]
                for r in rep["recommendations"][network] if r["target"].startswith("cf1.")}  # fmt: skip
        assert acts["dust-blower.9x16"] == "scale", (network, acts)
        assert not any(r["applied"] for r in rep["recommendations"][network])
    meta = {r["target"].split(".")[2] + "." + r["target"].split(".")[6]: r["action"]
            for r in rep["recommendations"]["meta"] if r["target"].startswith("cf1.")}  # fmt: skip
    assert meta["dust-blower.4x5"] == "pause_ad"
    assert (out / "plan.txt").read_text(encoding="utf-8").count("PAUSED") >= 4
    pack_file = next(iter((out / "state" / "manual").glob("*/applovin/pack.json")))
    pack = json.loads(pack_file.read_text(encoding="utf-8"))
    assert pack["status_at_creation"] == "PAUSED" and pack["store_url"].endswith("com.forgedemo.sortpuzzle")
