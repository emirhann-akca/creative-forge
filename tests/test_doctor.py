"""`forge doctor`: blocking vs non-blocking items, JSON output, ComfyUI probe, and no secrets in the output."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from creative_forge.cli import main
from creative_forge.doctor import FAIL, OK, WARN, failed, run_doctor, to_json
from creative_forge.ffmpeg import ENV_FFMPEG_DIR


def by_name(items):
    return {i.name: i for i in items}


def test_doctor_is_ready_on_a_working_setup(capsys, monkeypatch):
    monkeypatch.delenv("CREATIVE_FORGE_COMFYUI_URL", raising=False)
    items = by_name(run_doctor())
    assert items["ffmpeg"].status == OK and items["ffprobe"].status == OK
    assert items["ffmpeg"].details["aac_encoders"] and items["ffmpeg"].details["missing"] == []
    assert items["directories"].status == OK and items["comfyui"].status == "info"
    assert main(["doctor"]) == 0
    assert capsys.readouterr().out.rstrip().endswith("ready")


def test_doctor_json_and_a_blocking_ffmpeg_problem(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(ENV_FFMPEG_DIR, str(tmp_path))  # an empty folder: explicit, wrong, blocking
    assert main(["doctor", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    ffmpeg = next(i for i in data["items"] if i["name"] == "ffmpeg")
    assert ffmpeg["status"] == FAIL and ENV_FFMPEG_DIR in ffmpeg["summary"]


def test_vertex_credentials_are_reported_without_their_contents(tmp_path, monkeypatch, capsys):
    secret = "1//0g-not-a-real-refresh-token-but-treat-it-as-one"
    cred = tmp_path / "adc.json"
    cred.write_text(json.dumps({"type": "authorized_user", "refresh_token": secret, "client_secret": "s3"}), "utf-8")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(cred))
    monkeypatch.setenv("CREATIVE_FORGE_PROJECT", "my-project")
    items = by_name(run_doctor())
    assert items["vertex"].details["type"] == "authorized_user" and items["vertex"].details["project_set"]
    main(["doctor", "--json"])
    main(["doctor"])
    out = capsys.readouterr().out
    assert secret not in out and '"s3"' not in out


@pytest.fixture
def comfy(tmp_path):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"system": {"comfyui_version": "0.3.99"}, "devices": [{"name": "mps"}]}).encode("utf-8")
            self.send_response(200 if self.path == "/system_stats" else 404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_comfyui_reachability_is_optional(comfy):
    up = by_name(run_doctor(comfyui_url=comfy))["comfyui"]
    assert up.status == OK and "0.3.99" in up.summary and up.details["devices"] == ["mps"]
    down = by_name(run_doctor(comfyui_url="http://127.0.0.1:9"))["comfyui"]
    assert down.status == WARN
    assert not failed([down])  # an unreachable ComfyUI never blocks: only image_provider: comfyui needs it


def test_unwritable_working_directory_blocks(tmp_path, monkeypatch):
    import creative_forge.doctor as doc

    monkeypatch.setattr(doc, "_writable", lambda d: "PermissionError: denied" if d == tmp_path else None)
    item = by_name(run_doctor(cwd=tmp_path))["directories"]
    assert item.status == FAIL and "working directory" in item.summary


def test_json_shape_is_stable():
    """The UI and CI read these keys."""
    data = json.loads(to_json(run_doctor()))
    assert set(data) == {"ok", "items"}
    assert all(set(i) == {"name", "status", "summary", "details"} for i in data["items"])
