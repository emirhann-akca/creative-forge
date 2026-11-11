"""ComfyUI worker over the LAN and the Wan provider, against a mocked ComfyUI (no GPU here).

The mock is a real ``http.server`` on 127.0.0.1 that answers like ComfyUI: /system_stats with VRAM, /object_info,
/models/{folder}, /upload/image, /prompt, /history/{id}, /queue, /view, /interrupt. A queued graph is rendered with the
fake video provider from the frames that were uploaded, so the pipeline downstream works on a real clip.
"""

from __future__ import annotations

import http.server
import json
import re
import threading
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from creative_forge.cli import main
from creative_forge.config import ConfigError, Settings
from creative_forge.ffmpeg import probe, run
from creative_forge.ledger import BudgetExceeded
from creative_forge.licenses import REFUSE, check
from creative_forge.pricing import Pricing, estimate
from creative_forge.providers import build_providers
from creative_forge.providers.base import VideoRequest
from creative_forge.providers.comfyui import ENV_TOKEN, ComfyUIClient, ComfyUIError
from creative_forge.providers.comfyui_video import (
    ComfyUIVideoProvider,
    check_worker,
    frames_for,
    load_workflow,
    nodes_of,
    route_video,
)
from creative_forge.providers.fake import FakeVideoProvider
from creative_forge.spec import AdSpec, ComfyUIVideoSpec

FLF2V_FILES = {
    "diffusion_models": ["wan2.1_flf2v_720p_14B_fp16.safetensors", "wan2.2_ti2v_5B_fp16.safetensors"],
    "text_encoders": ["umt5_xxl_fp8_e4m3fn_scaled.safetensors"],
    "vae": ["wan_2.1_vae.safetensors", "wan2.2_vae.safetensors"],
    "clip_vision": ["clip_vision_h.safetensors"],
}


def object_info() -> dict[str, Any]:
    """Every node the shipped workflows use, declaring exactly the inputs they set."""
    info: dict[str, Any] = {}
    for wf in ("wan21_flf2v", "wan22_ti2v_5b"):
        for cls, inputs in nodes_of(load_workflow(wf)).items():
            req = info.setdefault(cls, {"input": {"required": {}, "optional": {}}})["input"]["required"]
            req.update({name: [] for name in inputs})
    return info


class Worker:
    def __init__(self, tmp: Path, *, token: str | None = None, vram_gb: float = 24.0, done_after: int = 1) -> None:
        self.tmp = tmp
        self.token = token
        self.vram = int(vram_gb * 2**30)
        self.done_after = done_after
        self.info = object_info()
        self.models = {k: list(v) for k, v in FLF2V_FILES.items()}
        self.uploads: dict[str, Path] = {}
        self.graphs: dict[str, dict] = {}
        self.history: dict[str, dict] = {}
        self.pending: dict[str, int] = {}  # prompt id -> polls left before it completes
        self.videos: dict[str, Path] = {}
        self.calls: list[tuple[str, str]] = []
        self.interrupted = 0
        self.n = 0

    def render(self, pid: str, graph: dict) -> None:
        loads = [n["inputs"]["image"] for n in graph.values() if n["class_type"] == "LoadImage"]
        first, last = (self.uploads[x.split(" [")[0]] for x in (loads + loads)[:2])
        fps = next(n["inputs"]["fps"] for n in graph.values() if n["class_type"] == "CreateVideo")
        length = next(n["inputs"]["length"] for n in graph.values() if "length" in n["inputs"])
        size = next(n["inputs"] for n in graph.values() if "width" in n["inputs"])
        w, h, d = size["width"], size["height"], (length - 1) / fps
        out = self.tmp / f"{pid}.mp4"
        # motion first (so cadence is measurable), then a dissolve that lands on the uploaded last frame by 2.4 s
        end = last if any(n["class_type"] == "WanFirstLastFrameToVideo" for n in graph.values()) else first
        run([
            "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={d}",
            "-loop", "1", "-framerate", str(fps), "-t", str(d), "-i", str(end),
            "-filter_complex", f"[0:v]format=yuv420p[a];[1:v]scale={w}:{h},setsar=1,format=yuv420p[b];"
            f"[a][b]xfade=transition=fade:duration=0.6:offset=1.8,trim=duration={d}[v]",
            "-map", "[v]", "-c:v", "libx264", "-preset", "veryfast", "-r", str(fps), str(out),
        ])  # fmt: skip
        self.videos[pid] = out
        name = f"hook_{len(self.videos):05d}_.mp4"
        self.history[pid] = {
            "outputs": {"16": {"images": [{"filename": name, "subfolder": "creative-forge", "type": "output"}],
                               "animated": [True]}},
            "status": {"status_str": "success", "completed": True, "messages": []},
        }  # fmt: skip
        self.videos[name] = self.videos[pid]


def serve(worker: Worker):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: Any) -> None:
            pass

        def _send(self, status: int, body: Any, ctype: str = "application/json") -> None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth(self) -> bool:
            if worker.token and self.headers.get("Authorization") != f"Bearer {worker.token}":
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def do_GET(self) -> None:
            path, _, query = self.path.partition("?")
            worker.calls.append(("GET", path))
            if not self._auth():
                return
            if path == "/system_stats":
                gpu = {"name": "cuda:0 NVIDIA GeForce RTX 4090", "type": "cuda", "vram_total": worker.vram,
                       "vram_free": worker.vram // 2}  # fmt: skip
                return self._send(200, {"system": {"comfyui_version": "0.4.1", "os": "nt"}, "devices": [gpu]})
            if path == "/object_info":
                return self._send(200, worker.info)
            m = re.fullmatch(r"/models/(\w+)", path)
            if m:
                return self._send(200, worker.models.get(m.group(1), []))
            m = re.fullmatch(r"/history/([\w-]+)", path)
            if m:
                pid = m.group(1)
                if pid in worker.pending:
                    worker.pending[pid] -= 1
                    if worker.pending[pid] > 0:
                        return self._send(200, {})
                    del worker.pending[pid]
                    worker.render(pid, worker.graphs[pid])
                return self._send(200, {pid: worker.history[pid]} if pid in worker.history else {})
            if path == "/queue":
                running = [[0, p, {}, {}, []] for p in list(worker.pending)[:1]]
                pend = [[i + 1, p, {}, {}, []] for i, p in enumerate(list(worker.pending)[1:])]
                return self._send(200, {"queue_running": running, "queue_pending": pend})
            if path == "/view":
                q = urllib.parse.parse_qs(query)
                return self._send(200, worker.videos[q["filename"][0]].read_bytes(), "video/mp4")
            return self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path
            worker.calls.append(("POST", path))
            body = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
            if not self._auth():
                return
            if path == "/upload/image":
                boundary = self.headers["Content-Type"].split("boundary=", 1)[1].encode()
                part = next(x for x in body.split(b"--" + boundary) if b'name="image"' in x)
                head, _, data = part.partition(b"\r\n\r\n")
                name = re.search(rb'filename="([^"]+)"', head).group(1).decode()  # type: ignore[union-attr]
                p = worker.tmp / name
                p.write_bytes(data[:-2])  # the part ends with CRLF before the next boundary
                worker.uploads[name] = p
                return self._send(200, {"name": name, "subfolder": "", "type": "temp"})
            if path == "/prompt":
                graph = json.loads(body)["prompt"]
                worker.n += 1
                pid = f"p-{worker.n}"
                worker.graphs[pid] = graph
                worker.pending[pid] = worker.done_after
                return self._send(200, {"prompt_id": pid, "number": worker.n, "node_errors": {}})
            if path == "/interrupt":
                worker.interrupted += 1
                return self._send(200, {})
            return self._send(404, {})

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


@pytest.fixture
def worker(tmp_path):
    w = Worker(tmp_path / "worker")
    w.tmp.mkdir()
    httpd, url = serve(w)
    w.url = url  # type: ignore[attr-defined]
    yield w
    httpd.shutdown()
    httpd.server_close()


def small(**over) -> ComfyUIVideoSpec:
    return ComfyUIVideoSpec(**({"width": 176, "height": 320, "fps": 8} | over))


@pytest.fixture
def frames(tmp_path) -> tuple[Path, Path]:
    from PIL import Image

    a, b = tmp_path / "first.png", tmp_path / "real.png"
    Image.new("RGB", (360, 640), (200, 60, 40)).save(a)
    Image.new("RGB", (360, 640), (40, 90, 200)).save(b)
    return a, b


def provider(worker: Worker, spec: ComfyUIVideoSpec, **kw: Any) -> ComfyUIVideoProvider:
    return ComfyUIVideoProvider(ComfyUIClient(worker.url), spec, poll_s=0.0, sleep=lambda _s: None, **kw)  # type: ignore[attr-defined]


def test_every_placeholder_of_both_wan_workflows_is_filled(frames):
    for wf in ("wan21_flf2v", "wan22_ti2v_5b"):
        found = set(re.findall(r'"\$(\w+)"', json.dumps(load_workflow(wf))))
        p = ComfyUIVideoProvider(ComfyUIClient("http://x"), ComfyUIVideoSpec(workflow=wf))
        params = p._params(VideoRequest("p", frames[0], "m", duration_s=4), "a.png [temp]", "b.png [temp]", 7)
        assert found <= set(params), (wf, found - set(params))
    assert [frames_for(s, 16) for s in (4, 6, 8)] == [65, 97, 129]  # Wan needs 4k + 1 frames
    assert frames_for(4, 24) == 97 and frames_for(5, 24) == 121


def test_comfy_check_reports_a_ready_worker_with_its_vram(worker, capsys):
    assert main(["comfy", "check", "--url", worker.url, "--workflow", "all"]) == 0
    out = capsys.readouterr().out
    assert "ComfyUI 0.4.1" in out and "24 GB VRAM" in out and "Wan 2.1 FLF2V 14B 720P" in out and "ready" in out


def test_comfy_check_names_missing_nodes_inputs_and_models(worker, capsys):
    del worker.info["WanFirstLastFrameToVideo"]
    del worker.info["CLIPVisionEncode"]["input"]["required"]["crop"]
    worker.models["clip_vision"] = []
    assert main(["comfy", "check", "--url", worker.url, "--workflow", "wan21_flf2v"]) == 1
    out = capsys.readouterr().out
    assert "missing node(s): WanFirstLastFrameToVideo" in out
    assert "CLIPVisionEncode does not declare input(s) crop" in out
    assert "missing model: models/clip_vision/clip_vision_h.safetensors" in out
    assert "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/" in out
    assert "NOT ready" in out
    # the 5B workflow needs neither node: it is still ready on the same worker
    files = small(workflow="wan22_ti2v_5b", width=192).model_files()
    assert check_worker(ComfyUIClient(worker.url), "wan22_ti2v_5b", files).ok


def test_flf2v_lands_on_the_real_frame_and_saves_the_prompt_id(worker, frames, tmp_path):
    saved: list[str] = []
    req = VideoRequest(
        "hand sweeps the dust", frames[0], "wan", last_frame=frames[1], duration_s=4, negative_prompt="text"
    )
    out = tmp_path / "out"
    out.mkdir()
    result = provider(worker, small()).generate(req, out, on_submitted=saved.append)
    assert saved == ["p-1"] and result.info["prompt_ids"] == ["p-1"]
    graph = worker.graphs["p-1"]
    assert "$" not in json.dumps(graph)
    flf = next(n for n in graph.values() if n["class_type"] == "WanFirstLastFrameToVideo")["inputs"]
    assert (flf["width"], flf["height"], flf["length"]) == (176, 320, 33)  # 4 s x 8 fps + 1
    loads = [n["inputs"]["image"] for n in graph.values() if n["class_type"] == "LoadImage"]
    assert len(loads) == 2 and loads[0].startswith("forge-first-") and loads[1].startswith("forge-last-")
    assert all(x.endswith(" [temp]") for x in loads)
    assert next(n for n in graph.values() if n["class_type"] == "UNETLoader")["inputs"]["unet_name"] == (
        "wan2.1_flf2v_720p_14B_fp16.safetensors"
    )
    assert probe(result.files[0]).video_duration == pytest.approx(4.0, abs=0.2)
    assert ("POST", "/prompt") in worker.calls and ("GET", "/view") in worker.calls


def test_ti2v_5b_uploads_only_the_first_frame(worker, frames, tmp_path):
    req = VideoRequest("p", frames[0], "wan", last_frame=frames[1], duration_s=4)
    result = provider(worker, small(workflow="wan22_ti2v_5b", width=192)).generate(req, tmp_path)
    assert len(worker.uploads) == 1 and "no last frame" in result.info["note"]
    assert any(n["class_type"] == "Wan22ImageToVideoLatent" for n in worker.graphs["p-1"].values())


def test_a_rerun_resumes_the_saved_prompt_id_instead_of_queueing_again(worker, frames, tmp_path):
    req = VideoRequest("p", frames[0], "wan", last_frame=frames[1], duration_s=4)
    first = provider(worker, small()).generate(req, tmp_path)
    posts = [c for c in worker.calls if c == ("POST", "/prompt")]
    (tmp_path / "again").mkdir()
    again = provider(worker, small()).generate(req, tmp_path / "again", operation=first.info["operation"])
    assert [c for c in worker.calls if c == ("POST", "/prompt")] == posts  # nothing new queued
    assert again.info["prompt_ids"] == ["p-1"] and again.files[0].read_bytes() == first.files[0].read_bytes()


def test_a_queued_prompt_is_waited_for_and_a_lost_one_is_queued_again(worker, frames, tmp_path):
    req = VideoRequest("p", frames[0], "wan", last_frame=frames[1], duration_s=4)
    worker.done_after = 3
    progress: list[str] = []
    provider(worker, small(), log=progress.append).generate(req, tmp_path)
    assert any("p-1: running" in p for p in progress)  # the queue position is reported while it waits
    (tmp_path / "lost").mkdir()
    worker.done_after = 1
    result = provider(worker, small()).generate(req, tmp_path / "lost", operation="p-from-before-a-restart")
    assert result.info["requeued"] and result.info["prompt_ids"] == ["p-2"]


def test_timeout_names_the_prompt_and_says_a_rerun_resumes_it(worker, frames, tmp_path):
    worker.done_after = 10**9
    clock = iter(range(0, 10**6, 100))
    p = ComfyUIVideoProvider(ComfyUIClient(worker.url), small(timeout_s=250), poll_s=0, sleep=lambda _s: None,
                             clock=lambda: next(clock))  # fmt: skip
    with pytest.raises(ComfyUIError, match=r"prompt p-1 not finished after 250 s.*rerun resumes it"):
        p.generate(VideoRequest("p", frames[0], "wan", last_frame=frames[1], duration_s=4), tmp_path)


def test_reverse_proxy_token_is_sent_and_never_printed(worker, monkeypatch):
    worker.token = "s3cret-proxy-token"
    with pytest.raises(ComfyUIError, match="refused the request") as e:
        ComfyUIClient(worker.url, token="wrong-token-value").system_stats()
    assert "wrong-token-value" not in str(e.value)
    monkeypatch.setenv(ENV_TOKEN, "s3cret-proxy-token")
    assert ComfyUIClient.from_env(worker.url).health().max_vram_gb == 24.0


def test_without_the_flag_a_dead_worker_never_falls_back_to_the_cloud(frames):
    spec = small()
    settings = Settings(project=None, comfyui_url="http://127.0.0.1:9")
    with pytest.raises(ConfigError, match="cloud_fallback: true"):
        build_providers("fake", settings, video="comfyui", comfyui_video=spec)
    ps = build_providers("fake", settings, video="comfyui", comfyui_video=small(cloud_fallback=True))
    assert ps.video.name == "fake-video" and ps.routing[0]["provider"] == "fake-video"
    assert "worker not ready" in ps.routing[0]["reason"]


def test_the_cloud_fallback_reserves_budget_before_calling(tmp_path, frames):
    from creative_forge.cache import GenerationCache
    from creative_forge.generator import Generator
    from creative_forge.ledger import BudgetGuard, CostLedger

    paid = FakeVideoProvider(billable=True)
    video, record = route_video("http://127.0.0.1:9", small(cloud_fallback=True), paid)
    assert video is paid and record["provider"] == "fake-video"
    ledger = CostLedger()
    gen = Generator(
        type("P", (), {"video": video})(),  # type: ignore[arg-type]
        GenerationCache(tmp_path / "c"),
        ledger,
        BudgetGuard(ledger, 0.5),
        Pricing.load(),
        tmp_path / "s",
    )
    with pytest.raises(BudgetExceeded):
        gen.video(VideoRequest("p", frames[0], "veo-3.1-generate-001", last_frame=frames[1], duration_s=4), "c1")
    assert paid.calls == 0  # refused before anything was sent


def spec_dict(url_dir: str, **over: Any) -> dict:
    d = {
        "name": "wan",
        "providers": "fake",
        "video_provider": "comfyui",
        "comfyui_video": {"width": 176, "height": 320, "fps": 8},
        "output_dir": url_dir,
        "render": {"width": 180, "height": 320, "preset": "veryfast"},
        "export": {"preset": "veryfast"},
        "qa": {"judge_runs": 0, "duration_s": [4, 60]},
        "targets": {"regions": ["TR"]},
        "concepts": [
            {
                "id": "c1",
                "gameplay": {"path": "g.mp4", "in_s": 0.5, "out_s": 4.5},
                "hook": {"video_prompt": "the board gets cleaned", "duration_s": 4, "cut_s": 2.6,
                         "text_regions": [[0, 0, 10, 10]], "audio": False},
                "end_card": {"duration_s": 1.0},
                "formats": ["9:16"],
            }
        ],
    }  # fmt: skip
    d.update(over)
    return d


def test_plan_prices_the_local_worker_at_zero_and_the_fallback_at_the_cloud_price():
    s = AdSpec.model_validate(spec_dict("out"))
    line = next(x for x in estimate(s, Pricing.load()).lines if x.kind == "hook video")
    assert (line.model, line.usd) == ("wan2.1_flf2v_720p_14B_fp16.safetensors", 0.0)
    s2 = AdSpec.model_validate(spec_dict("out", comfyui_video={"cloud_fallback": True}))
    line2 = next(x for x in estimate(s2, Pricing.load()).lines if x.kind == "hook video")
    assert line2.usd == pytest.approx(4 * 0.20) and "fallback" in line2.detail  # silent hook: Veo's silent rate
    roles = {f.role: f.status for f in check(s2).findings}
    assert roles["video (diffusion_model)"] == "ok" and roles["video (cloud fallback)"] == "service"
    flux = AdSpec.model_validate(
        spec_dict("out", comfyui_video={"diffusion_model": "hunyuan_video_i2v_720p.safetensors"})
    )
    assert {f.role: f.status for f in check(flux).findings}["video (diffusion_model)"] == "warn"  # TR only: allowed
    with pytest.raises(ValueError, match="multiple of 32"):
        ComfyUIVideoSpec(workflow="wan22_ti2v_5b", width=720)


@pytest.mark.slow
def test_pipeline_hook_stage_runs_end_to_end_on_the_mock_worker(worker, tmp_path):
    from creative_forge.pipeline import Pipeline
    from creative_forge.spec import load_spec

    run(["-f", "lavfi", "-i", "testsrc2=s=180x320:r=30:d=6", "-c:v", "libx264", "-preset", "veryfast",
         str(tmp_path / "g.mp4")])  # fmt: skip
    p = tmp_path / "wan.yaml"
    p.write_text(json.dumps(spec_dict("out")), encoding="utf-8")
    spec = load_spec(p)
    settings = Settings(project=None, cache_dir=tmp_path / "cache", comfyui_url=worker.url)
    providers = build_providers("fake", settings, video="comfyui", comfyui_video=spec.comfyui_video)
    assert providers.video.name == "comfyui-wan" and providers.routing[0]["vram_gb"] == 24.0
    providers.video.poll_s = 0.0
    rr = Pipeline(spec, providers, settings, ocr="off", log=lambda _: None).run()
    (cr,) = rr.concepts
    assert cr.error is None, cr.error
    checks = {c.check: c for c in cr.pre_checks + cr.exports[0].checks}
    for name in ("hook_landing", "seam", "cadence"):
        assert checks[name].status.value != "skip" and checks[name].value is not None, checks[name]
    # the mock lands on the uploaded real frame (cover-resized to 176 x 320 and re-encoded, so not pixel-exact)
    assert checks["hook_landing"].status.value in ("pass", "warn") and checks["hook_landing"].value < 16
    video = [e for e in rr.ledger.entries if e.stage == "video"]
    assert video[0].provider == "comfyui-wan" and video[0].usd == 0 and video[0].list_usd == 0
    ledger = json.loads((tmp_path / "out" / "cost_ledger.json").read_text(encoding="utf-8"))
    assert ledger["routing"][0]["provider"] == "comfyui-wan"
    assert REFUSE not in {f["status"] for f in rr.licenses}
