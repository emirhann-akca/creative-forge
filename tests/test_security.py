"""Application security regressions: the UI's headers, CSRF on every state-changing route, Fetch Metadata, the
access token for a network bind, a run's report.html without scripts, uploads, the desktop app's own endpoints, the
reporting APIs' host pinning, the ComfyUI worker URL and hostile playable zips."""

from __future__ import annotations

import base64
import json
import re
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from creative_forge.perf.http import PerfApiError
from creative_forge.perf.meta import MetaInsights
from creative_forge.playable import pack, static
from creative_forge.providers.comfyui import ComfyUIClient, ComfyUIError, check_worker_url, preflight
from creative_forge.retry import RetryPolicy
from creative_forge.ui import app as ui_app
from creative_forge.ui import security
from creative_forge.ui.app import create_app

TOKEN = "csrf-test-token"
ACCESS = "network-access-token-0123456789"
BASE = "http://127.0.0.1:8765"
TEMPLATES = Path(ui_app.__file__).parent / "templates"
RID = "r"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    run = tmp_path / "out" / RID
    run.mkdir(parents=True)
    (run / "r_a_9x16.mp4").write_bytes(b"\0" * 256)
    report = {
        "name": "r",
        "concepts": [
            {
                "concept": "a",
                "pre_checks": [],
                "exports": [{"format": "9:16", "file": "r_a_9x16.mp4", "status": "pass", "checks": []}],
            }
        ],
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    # a report.html from someone else's run folder
    (run / "report.html").write_text("<html><body><script>steal()</script>QA</body></html>", encoding="utf-8")
    return tmp_path / "out"


@pytest.fixture
def app(root: Path, tmp_path: Path):
    return create_app(root, token=TOKEN, perf_db=tmp_path / "perf.sqlite", poster_cache=tmp_path / "posters")


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, base_url=BASE)


def _csp(r) -> dict[str, str]:
    out = {}
    for part in r.headers["content-security-policy"].split(";"):
        name, _, value = part.strip().partition(" ")
        out[name] = value
    return out


# -------------------------------------------------------------------------------------------- headers and the CSP
@pytest.mark.parametrize("path", ["/", "/creatives", "/tests", "/tests/new", "/perf", "/playables", "/settings"])
def test_pages_carry_the_headers_and_no_inline_script(client, path):
    r = client.get(path)
    assert r.status_code == 200, path
    assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    csp = _csp(r)
    assert csp["script-src"] == "'self'" and csp["frame-ancestors"] == "'none'" and csp["object-src"] == "'none'"
    inline = re.findall(r"<script(?![^>]*\bsrc=)([^>]*)>(.*?)</script>", r.text, re.S)
    assert all("application/json" in attrs for attrs, _ in inline), f"{path}: an inline script the CSP blocks"
    markup = re.sub(r"<(script|style)\b.*?</\1>", "", r.text, flags=re.S | re.I)
    assert not re.search(r"<[^>]*\son[a-z]+\s*=", markup, re.I), f"{path}: an inline event handler"


def test_static_files_and_errors_carry_the_headers_too(client):
    for path, status in (("/static/forge.js", 200), ("/static/theme.js", 200), ("/no-such-page", 404)):
        r = client.get(path)
        assert r.status_code == status and r.headers["x-frame-options"] == "DENY", path
    r = client.get("/", headers={"host": "evil.example"})
    assert r.status_code == 400 and "content-security-policy" in r.headers


def test_no_template_uses_inline_handlers_or_hx_on():
    offenders = [
        t.name
        for t in TEMPLATES.glob("*.html")
        if re.search(r"\shx-on[:-]|\son[a-z]+\s*=\s*[\"']", t.read_text(encoding="utf-8"), re.I)
    ]
    assert not offenders


def test_a_run_report_is_shown_without_scripts_or_this_origin(client):
    r = client.get(f"/runs/{RID}/report.html")
    assert r.status_code == 200 and r.headers["content-security-policy"] == security.REPORT_CSP
    csp = _csp(r)
    assert csp["default-src"] == "'none'" and "sandbox" in csp and "script-src" not in csp


# ----------------------------------------------------------------------------------------------------------- CSRF
def _post_routes(app) -> list[str]:
    return sorted(
        re.sub(r"\{([^}:]+)(:path)?\}", lambda m: "1" if m.group(1).endswith("_id") else "x", r.path)
        for r in app.routes
        if "POST" in (getattr(r, "methods", None) or ())
    )


def test_every_state_changing_route_needs_the_token_and_this_origin(app, client, root):
    routes = _post_routes(app)
    assert {"/demo", "/settings/import", "/tests", "/tests/1/x", "/runs/x/review"} <= set(routes)
    assert not [
        r.path
        for r in app.routes
        if set(getattr(r, "methods", None) or ()) - {"GET", "HEAD", "POST"}  # no PUT/PATCH/DELETE to forget
    ]
    for path in routes:
        url = path.replace("/runs/x/", f"/runs/{RID}/")
        assert client.post(url).status_code == 403, path
        assert client.post(url, data={"token": "guess"}).status_code == 403, path
        assert client.post(url, headers={"X-Forge-Token": "guess"}).status_code == 403, path
        r = client.post(url, data={"token": TOKEN}, headers={"Origin": "https://evil.example"})
        assert r.status_code == 403, path
        r = client.post(url, data={"token": TOKEN}, headers={"Origin": "null"})
        assert r.status_code == 403, path
        r = client.post(url, data={"token": TOKEN}, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403, path
    assert json.loads((root / RID / "report.json").read_text(encoding="utf-8"))["name"] == "r"
    assert not (root / RID / "review.json").exists()  # nothing was decided


def test_the_ui_own_posts_pass(client, root):
    ok = {"X-Forge-Token": TOKEN, "Origin": BASE, "Sec-Fetch-Site": "same-origin"}
    r = client.post(f"/runs/{RID}/review", data={"file": "r_a_9x16.mp4", "decision": "approve"}, headers=ok)
    assert r.status_code == 200
    assert json.loads((root / RID / "review.json").read_text(encoding="utf-8"))["decisions"]


def test_cross_site_subresources_are_refused_but_links_work(client):
    img = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors", "Sec-Fetch-Dest": "image"}
    assert client.get(f"/runs/{RID}/media/r_a_9x16.mp4", headers=img).status_code == 403
    assert client.get("/doctor.json", headers={**img, "Sec-Fetch-Mode": "cors"}).status_code == 403
    nav = {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
    assert client.get("/", headers=nav).status_code == 200


# ---------------------------------------------------------------------------------------------- network binding
def test_a_ui_on_the_network_needs_the_access_token(root, tmp_path):
    with pytest.raises(ValueError):
        create_app(root, token=TOKEN, allowed_hosts=None)  # any Host without a token: refused
    app = create_app(root, token=TOKEN, allowed_hosts=None, access_token=ACCESS, perf_db=tmp_path / "p.sqlite")
    c = TestClient(app, base_url="http://192.168.1.20:8765", follow_redirects=False)
    for path in ("/", "/settings", f"/runs/{RID}/media/r_a_9x16.mp4", "/static/forge.js", "/doctor.json"):
        r = c.get(path)
        assert r.status_code == 401 and TOKEN not in r.text, path
    assert c.post(f"/runs/{RID}/review", data={"token": TOKEN, "file": "r_a_9x16.mp4"}).status_code == 401
    assert c.get("/", params={"access": "wrong"}).status_code == 401
    r = c.get("/creatives", params={"access": ACCESS, "qa": "pass"})
    assert r.status_code == 303 and r.headers["location"] == "/creatives?qa=pass"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    for sneaky in ("/%2Fevil.example", "/%5Cevil.example", "/%2F%5Cevil.example"):  # //host, /\\host: no open redirect
        r = c.get(sneaky, params={"access": ACCESS})
        loc = r.headers["location"]
        assert r.status_code == 303 and loc.startswith("/") and loc[1] not in "/\\", loc
    c.cookies.set(security.ACCESS_COOKIE, ACCESS)
    assert c.get("/").status_code == 200
    c.cookies.clear()
    assert c.get("/", headers={"Authorization": f"Bearer {ACCESS}"}).status_code == 200


def test_forge_ui_on_the_network_prints_a_fresh_access_token(root, monkeypatch, capsys):
    seen: list[dict] = []

    class Sock:
        def getsockname(self):
            return ("0.0.0.0", 8765)

    class Server:
        def __init__(self, config):
            pass

        def run(self, sockets):
            pass

    import uvicorn

    monkeypatch.setattr(ui_app, "create_app", lambda r, **kw: seen.append(kw) or object())
    monkeypatch.setattr(ui_app, "_bind", lambda host, port, explicit: Sock())
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(uvicorn, "Config", lambda app, **kw: None)
    assert ui_app.serve(root, host="0.0.0.0", open_browser=False) == 0
    assert ui_app.serve(root, host="192.168.1.20", open_browser=False) == 0
    assert ui_app.serve(root, open_browser=False) == 0
    wildcard, lan, local = seen
    assert wildcard["allowed_hosts"] is None and len(wildcard["access_token"]) >= 32
    assert "192.168.1.20" in lan["allowed_hosts"] and lan["access_token"]
    assert local["access_token"] is None and local["allowed_hosts"] == ui_app.LOOPBACK
    out = capsys.readouterr().out
    assert f"?access={wildcard['access_token']}" in out and "access token" in out


def test_desktop_endpoints_refuse_other_sites_and_other_hosts():
    from creative_forge.desktop import Heartbeat

    async def ui(scope, receive, send):  # pragma: no cover - not reached
        raise AssertionError

    beat = Heartbeat(ui)
    c = TestClient(beat, base_url="http://127.0.0.1")
    assert c.post("/__app/bye", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert c.get("/__app/ping", headers={"host": "evil.example"}).status_code == 403
    assert beat.bye_at is None and not beat.pinged
    assert c.get("/__app/ping").status_code == 200 and beat.pinged


# ------------------------------------------------------------------------------------------------------- uploads
@pytest.mark.parametrize("name", ["..", "../../evil.csv", "..\\..\\evil.csv", "/etc/cron.d/x.csv"])
def test_an_upload_name_cannot_leave_the_temp_folder(client, tmp_path, monkeypatch, name):
    seen: list[Path] = []

    def fake_import(store, path, network, account):
        seen.append(path)
        raise ValueError("stop here")

    monkeypatch.setattr("creative_forge.perf.commands.import_csv", fake_import)
    files = {"file": (name, b"Date,Ad name\n", "text/csv")}
    r = client.post("/settings/import", data={"network": "tiktok", "token": TOKEN}, files=files)
    assert r.status_code == 422
    for p in seen:
        assert p.parent.name.startswith("forge-upload-") and ".." not in p.parts and "\\" not in p.name


# ----------------------------------------------------------------------------------------- outbound: perf APIs
class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.headers = payload, status, {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Session:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(headers or {})))
        return self.script.pop(0)


@pytest.mark.parametrize(
    "nxt",
    [
        "https://evil.example/v27.0/act_1/insights?after=x",
        "http://graph.facebook.com/v27.0/act_1/insights?after=x",  # a downgrade
        "https://graph.facebook.com.evil.example/x",
        "https://user:pw@graph.facebook.com/x",
    ],
)
def test_a_pagination_link_to_another_host_never_gets_the_token(nxt):
    token = "EAAsecretTOKEN0123456789"
    page = {"data": [], "paging": {"next": nxt}}
    s = _Session([_Resp(page), _Resp({"data": []})])
    m = MetaInsights(token, "1234567890", session=s, retry=RetryPolicy(attempts=1, base_delay_s=0, max_delay_s=0))
    with pytest.raises(PerfApiError) as e:
        list(m.rows("2026-10-20", "2026-10-21"))
    assert len(s.calls) == 1 and s.calls[0][0].startswith("https://graph.facebook.com/")
    assert token not in str(e.value)


# -------------------------------------------------------------------------------------------- ComfyUI worker URL
@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest",
        "http://metadata.google.internal",
        "http://[fe80::1]:8188",
        "http://user:pw@192.168.1.20:8188",
        "file:///etc/passwd",
        "gopher://192.168.1.20:8188",
        "192.168.1.20:8188",
    ],
)
def test_comfyui_worker_url_is_checked_before_the_token_is_sent(url):
    with pytest.raises(ComfyUIError):
        ComfyUIClient(url, token="secret-proxy-token")
    assert "no ComfyUI" not in (preflight(url, "x.safetensors") or "")  # refused, not tried


def test_comfyui_lan_and_local_workers_are_fine():
    for url in ("http://127.0.0.1:8188", "http://192.168.1.20:8188/", "https://gpu.example.com"):
        assert check_worker_url(url) == url


# -------------------------------------------------------------------------------------------- hostile playables
def _zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return path


@pytest.mark.parametrize("name", ["../evil.js", "/etc/x.js", "a/../../b.js", "C:/x.js", "a\\..\\b.js"])
def test_zip_members_with_unsafe_names_are_refused(tmp_path, name):
    z = _zip(tmp_path / "p.zip", {"index.html": b"<html></html>", name: b"x"})
    with pytest.raises(static.PlayableError, match="unsafe"):
        static.load(z)
    with pytest.raises(pack.PackError, match="unsafe"):
        pack.read_source(z)


def test_a_zip_bomb_is_refused_before_it_is_inflated(tmp_path, monkeypatch):
    monkeypatch.setattr(static, "MAX_ZIP_BYTES", 1_000_000)
    z = _zip(tmp_path / "bomb.zip", {"index.html": b"<html></html>", "big.bin": b"\0" * 2_000_000})
    assert z.stat().st_size < 50_000  # small on disk
    with pytest.raises(static.PlayableError, match="uncompressed"):
        static.load(z)
    monkeypatch.setattr(static, "MAX_ZIP_MEMBERS", 3)
    many = _zip(tmp_path / "many.zip", {f"f{i}.js": b"1" for i in range(5)})
    with pytest.raises(static.PlayableError, match="files in the zip"):
        static.load(many)


def test_a_folder_symlink_out_of_the_playable_is_not_packed(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "index.html").write_text('<html><body><img src="leak.txt"></body></html>', encoding="utf-8")
    try:
        (src / "leak.txt").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here (Windows without the privilege)")
    _html, files, _index = pack.read_source(src)
    assert "leak.txt" not in files
    encoded = base64.b64encode(b"TOP SECRET").decode()
    assert encoded not in json.dumps({k: base64.b64encode(v).decode() for k, v in files.items()})


def test_app_json_may_only_name_a_local_port(tmp_path, monkeypatch):
    """The desktop app opens the URL another instance left in app.json; a tampered file must not send the window (or
    a probe) anywhere else."""
    from creative_forge import desktop

    probed: list[str] = []
    monkeypatch.setattr(desktop, "_get", lambda url, timeout=2.0: probed.append(url) or (200, b"creative-forge"))
    for url in ("https://evil.example/", "file:///etc/passwd", "http://127.0.0.1.evil.example:1/", 42):
        desktop._write_state(tmp_path, {"url": url})
        assert desktop.running_url(tmp_path) is None and probed == []
    desktop._write_state(tmp_path, {"url": "http://127.0.0.1:8765/"})
    assert desktop.running_url(tmp_path) == "http://127.0.0.1:8765/"
