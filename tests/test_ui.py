"""`forge ui`: pages, range requests on exports, approve/reject through review.py, the token and host guards, the
background demo and the doctor page."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from creative_forge.review import ReviewError, load_review, publish
from creative_forge.ui.app import _bind, create_app, find_runs

TOKEN = "test-token"


def check(name: str, status: str) -> dict:
    return {"check": name, "status": status, "value": None, "threshold": "", "unit": "", "message": ""}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """out/ with one finished run ("Çalışma Ş/t") whose exports are real bytes and whose concept b failed QA."""
    run = tmp_path / "out" / "Çalışma Ş" / "t"
    run.mkdir(parents=True)
    for name in ("t_a_9x16.mp4", "t_b_9x16.mp4"):
        (run / name).write_bytes(bytes(range(256)) * 40)
    report = {
        "name": "t",
        "concepts": [
            {
                "concept": "a",
                "pre_checks": [],
                "exports": [
                    {"format": "9:16", "file": "t_a_9x16.mp4", "status": "pass", "checks": [check("x", "pass")]}
                ],
            },
            {
                "concept": "b",
                "pre_checks": [],
                "exports": [
                    {"format": "9:16", "file": "t_b_9x16.mp4", "status": "fail", "checks": [check("true_peak", "fail")]}
                ],
            },
        ],
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (run / "report.html").write_text("<html><body>QA report Ş</body></html>", encoding="utf-8")
    (tmp_path / "out" / "inputs").mkdir()
    return tmp_path / "out"


RID = "Çalışma Ş/t"


@pytest.fixture
def client(root: Path) -> TestClient:
    app = create_app(root, token=TOKEN, demo_cmd=[sys.executable, "-c", "print('demo ran: Ş')"])
    return TestClient(app, base_url="http://127.0.0.1:8765")


def test_runs_list_and_run_page(client):
    r = client.get("/")
    assert r.status_code == 200 and RID in r.text and "fail" in r.text
    assert 'X-Forge-Token": "test-token"' in r.text and "/static/htmx.min.js" in r.text
    page = client.get(f"/runs/{RID}?lang=en")
    assert page.status_code == 200
    assert "t_a_9x16.mp4" in page.text and "t_b_9x16.mp4" in page.text and "failed: true_peak" in page.text
    assert "Kalan denetimler: true_peak" in client.get(f"/runs/{RID}?lang=tr").text
    assert client.get(f"/runs/{RID}/report.html").text.endswith("QA report Ş</body></html>")
    assert client.get("/static/htmx.min.js").status_code == 200


def test_exports_stream_with_range_requests(client):
    full = client.get(f"/runs/{RID}/media/t_a_9x16.mp4")
    assert full.status_code == 200 and len(full.content) == 256 * 40
    part = client.get(f"/runs/{RID}/media/t_a_9x16.mp4", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == full.content[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{256 * 40}"


def test_only_exports_of_a_run_inside_the_root_are_served(client, root):
    (root.parent / "secret.txt").write_text("no", encoding="utf-8")
    assert client.get(f"/runs/{RID}/media/report.json").status_code == 404
    assert client.get("/runs/../secret.txt").status_code == 404
    assert client.get("/runs/%2E%2E/report.html").status_code == 404
    assert client.get("/runs/nope").status_code == 404


def test_approve_goes_through_review_py_and_publish_gate_is_unchanged(client, root, tmp_path):
    run = root / RID
    r = client.post(
        f"/runs/{RID}/review",
        data={"file": "t_a_9x16.mp4", "decision": "approve", "note": "hook reads well", "reviewer": "ayşe"},
        headers={"X-Forge-Token": TOKEN},
    )
    assert r.status_code == 200 and "approve" in r.text and "hook reads well" in r.text
    d = load_review(run)["decisions"]["t_a_9x16.mp4"]
    assert d["reviewer"] == "ayşe" and len(d["sha256"]) == 64
    manifest = json.loads(publish(run, tmp_path / "deliver").read_text(encoding="utf-8"))
    assert [e["file"] for e in manifest["exports"]] == ["t_a_9x16.mp4"]

    # approving the export that failed QA is recorded, and publish still refuses it without an override
    client.post(
        f"/runs/{RID}/review",
        data={"file": "t_b_9x16.mp4", "decision": "approve", "note": "", "token": TOKEN},
    )
    with pytest.raises(ReviewError, match="failed QA checks"):
        publish(run, tmp_path / "deliver2")

    # a re-render after approval voids it, exactly as with `forge review`
    (run / "t_a_9x16.mp4").write_bytes(b"re-rendered")
    with pytest.raises(ReviewError, match="changed since it was reviewed"):
        publish(run, tmp_path / "deliver3", override="ok")


def test_reject_needs_a_note_and_errors_render_in_place(client, root):
    r = client.post(
        f"/runs/{RID}/review",
        data={"file": "t_b_9x16.mp4", "decision": "reject", "note": "  "},
        headers={"X-Forge-Token": TOKEN},
    )
    assert r.status_code == 422 and "Ret için not gerekli" in r.text and "<article" in r.text
    en = client.post(
        f"/runs/{RID}/review?lang=en",
        data={"file": "t_b_9x16.mp4", "decision": "reject", "note": ""},
        headers={"X-Forge-Token": TOKEN},
    )
    assert en.status_code == 422 and "A rejection needs a note" in en.text
    assert load_review(root / RID)["decisions"] == {}


def test_posts_without_the_token_are_refused(client, root):
    for headers in ({}, {"X-Forge-Token": "guess"}):
        r = client.post(f"/runs/{RID}/review", data={"file": "t_a_9x16.mp4", "decision": "approve"}, headers=headers)
        assert r.status_code == 403
    assert client.post("/demo").status_code == 403
    assert load_review(root / RID)["decisions"] == {}


def test_foreign_host_headers_are_refused(root):
    app = create_app(root, token=TOKEN)
    assert TestClient(app, base_url="http://evil.example:8765").get("/").status_code == 400
    assert TestClient(app, base_url="http://localhost:8765").get("/").status_code == 200
    assert TestClient(app, base_url="http://[::1]:8765").get("/").status_code == 200
    access = "network-access-token-for-tests"
    exposed = create_app(root, token=TOKEN, allowed_hosts=None, access_token=access)  # --host 0.0.0.0
    lan = TestClient(exposed, base_url="http://192.168.1.20:8765")
    assert lan.get("/").status_code == 401  # any Host, but only with the printed access token
    assert lan.get("/", headers={"Authorization": f"Bearer {access}"}).status_code == 200


def test_demo_runs_in_the_background_and_reports_its_output(client):
    r = client.post("/demo", headers={"X-Forge-Token": TOKEN})
    assert r.status_code == 200 and ("running" in r.text or "done" in r.text)
    demo = client.app.state.demo
    assert demo.wait(timeout=60) == 0
    status = client.get("/demo/status")
    assert "done" in status.text and "demo ran: Ş" in status.text and "every 1s" not in status.text


def test_doctor_page(client):
    r = client.get("/doctor")
    assert r.status_code == 200 and "ffmpeg" in r.text and "ffprobe" in r.text
    data = client.get("/doctor.json").json()
    assert {i["name"] for i in data["items"]} >= {"python", "ffmpeg", "ffprobe", "ocr", "directories"}


def test_find_runs_skips_inputs_and_hidden_dirs(root):
    (root / ".cache" / "x").mkdir(parents=True)
    (root / ".cache" / "x" / "report.json").write_text("{}", encoding="utf-8")
    assert [r.rid for r in find_runs(root)] == [RID]


def test_default_port_falls_back_when_taken():
    taken = socket.socket()
    taken.bind(("127.0.0.1", 0))
    taken.listen(1)
    port = taken.getsockname()[1]
    try:
        s = _bind("127.0.0.1", port, explicit_port=False)
        assert s.getsockname()[1] != port
        s.close()
        with pytest.raises(OSError):
            _bind("127.0.0.1", port, explicit_port=True)
    finally:
        taken.close()


def test_a_root_that_is_itself_a_run_is_listed(root):
    app = create_app(root / RID, token=TOKEN)
    page = TestClient(app, base_url="http://127.0.0.1:8765").get("/")
    assert page.status_code == 200 and "/runs/t" in page.text


def test_doctor_partial_has_no_page_chrome(client):
    r = client.get("/doctor?partial=1")
    assert r.status_code == 200 and 'id="doctor-table"' in r.text and "<title>" not in r.text
