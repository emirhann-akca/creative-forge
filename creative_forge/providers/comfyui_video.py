"""Hook video on a ComfyUI GPU worker with Wan (Apache-2.0 weights), and the worker check behind ``forge comfy check``.

Two shipped API-format workflows (``data/comfyui/``), filled like the SDXL ones (``"$name"`` placeholders):

* ``wan21_flf2v.json``: Wan 2.1 first-last-frame-to-video (``WanFirstLastFrameToVideo``). The first frame is the
  edited hook frame and the last frame is the real gameplay frame, the same recipe as Veo's ``lastFrame``. 720P model.
* ``wan22_ti2v_5b.json``: Wan 2.2 TI2V-5B image-to-video (``Wan22ImageToVideoLatent``) for small cards. It has no
  last-frame input, so the landing relies on the crossfade, ``hook.patches`` and the ``hook_landing`` check.

One generation: upload the frame(s) (cover-resized to the workflow size), queue the graph, save the ``prompt_id`` the
moment it is accepted (the generator persists it like Veo's operation name), poll ``/history`` and ``/queue``, download
the ``SaveVideo`` output with ``/view``. A rerun after a crash or a timeout resumes the same ``prompt_id``; if the
worker restarted and lost it, the job is queued again (it runs locally, so that costs time, not money). Ctrl-C sends
``/interrupt`` so the GPU does not keep rendering a clip nobody waits for.

Not yet run on a GPU: everything here is tested against a mocked ComfyUI server. Node and input names follow ComfyUI's
native Wan nodes; ``forge comfy check`` compares them with the worker's ``/object_info`` before anything is queued.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from creative_forge.providers.base import ProviderResult, VideoProvider, VideoRequest
from creative_forge.providers.comfyui import ComfyUIClient, ComfyUIError, Health, PromptLost, fill
from creative_forge.spec import WAN_WORKFLOWS, ComfyUIVideoSpec

WORKFLOW_FILES = {
    "wan21_flf2v": "wan21_flf2v.json",
    "wan22_ti2v_5b": "wan22_ti2v_5b.json",
    "sdxl_inpaint": "sdxl_inpaint.json",
    "sdxl_img2img": "sdxl_img2img.json",
}
FOLDERS = {
    "diffusion_model": "diffusion_models",
    "text_encoder": "text_encoders",
    "vae": "vae",
    "clip_vision": "clip_vision",
    "checkpoint": "checkpoints",
}
_WAN21 = "Comfy-Org/Wan_2.1_ComfyUI_repackaged"
_WAN22 = "Comfy-Org/Wan_2.2_ComfyUI_Repackaged"
# Where each default file comes from (Hugging Face, apache-2.0 per the repo card; file list read 2026-11-11).
DOWNLOADS = {
    "wan2.1_flf2v_720p_14B_fp16.safetensors": (_WAN21, "split_files/diffusion_models"),
    "wan2.1_flf2v_720p_14B_fp8_e4m3fn.safetensors": (_WAN21, "split_files/diffusion_models"),
    "umt5_xxl_fp8_e4m3fn_scaled.safetensors": (_WAN21, "split_files/text_encoders"),
    "umt5_xxl_fp16.safetensors": (_WAN21, "split_files/text_encoders"),
    "wan_2.1_vae.safetensors": (_WAN21, "split_files/vae"),
    "clip_vision_h.safetensors": (_WAN21, "split_files/clip_vision"),
    "wan2.2_ti2v_5B_fp16.safetensors": (_WAN22, "split_files/diffusion_models"),
    "wan2.2_vae.safetensors": (_WAN22, "split_files/vae"),
}
VIDEO_SUFFIXES = (".mp4", ".webm", ".mov", ".mkv", ".gif", ".webp")


def download_url(name: str) -> str | None:
    if name not in DOWNLOADS:
        return None
    repo, path = DOWNLOADS[name]
    return f"https://huggingface.co/{repo}/resolve/main/{path}/{name}"


def load_workflow(name: str) -> dict[str, Any]:
    text = resources.files("creative_forge.data").joinpath("comfyui", WORKFLOW_FILES[name]).read_text(encoding="utf-8")
    return json.loads(text)


def nodes_of(graph: dict[str, Any]) -> dict[str, set[str]]:
    """Node class -> the inputs this workflow sets on it."""
    out: dict[str, set[str]] = {}
    for node in graph.values():
        out.setdefault(node["class_type"], set()).update(node.get("inputs", {}))
    return out


def frames_for(seconds: float, fps: int) -> int:
    """Wan generates 4k + 1 frames: the nearest such count at or above ``seconds * fps`` (plus the first frame)."""
    n = round(seconds * fps)
    return n + 1 + (-n % 4)


def vram_note(gb: float | None) -> str:
    """What the research could source for a card of this size; everything else is marked unverified."""
    if gb is None:
        return "no GPU reported (CPU or MPS): Wan video is impractical here; use this machine for orchestration"
    if gb < 8:
        return f"{gb:g} GB: below what any source states for Wan video"
    if gb < 16:
        return (
            f"{gb:g} GB: Wan 2.2 TI2V-5B; ComfyUI's docs say it 'should fit well on 8GB vram with the ComfyUI native "
            "offloading' (docs.comfy.org/tutorials/video/wan/wan2_2). 14B FLF2V at this size is unverified"
        )
    if gb < 24:
        return f"{gb:g} GB: TI2V-5B fits; Wan 2.1 FLF2V 14B fp8 with offloading is unverified at this size: measure it"
    return (
        f"{gb:g} GB: Wan 2.1 FLF2V 14B 720P (fp8 or fp16 with offloading); the Wan 2.2 README states 24 GB (RTX 4090) "
        "for TI2V-5B in its own CLI"
    )


@dataclass
class WorkerReport:
    url: str
    workflow: str
    health: Health | None = None
    problems: list[str] = field(default_factory=list)
    missing_nodes: list[str] = field(default_factory=list)
    unknown_inputs: dict[str, list[str]] = field(default_factory=dict)
    missing_models: dict[str, list[str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.problems

    def text(self) -> str:
        lines = [f"ComfyUI worker {self.url}, workflow {self.workflow}"]
        if self.health:
            lines.append(f"  ComfyUI {self.health.version}")
            for d in self.health.devices:
                vram = (
                    f"{d.vram_total_gb:g} GB VRAM ({d.vram_free_gb:g} GB free)" if d.vram_total_gb else "no VRAM figure"
                )
                lines.append(f"  device: {d.name} [{d.type}], {vram}")
            lines.append(f"  tier: {vram_note(self.health.max_vram_gb)}")
        for p in self.problems:
            lines.append(f"  problem: {p}")
        for folder, names in self.missing_models.items():
            for n in names:
                url = download_url(n)
                lines.append(f"  missing model: models/{folder}/{n}" + (f"  <- {url}" if url else ""))
        lines.append("  ready" if self.ok else "  NOT ready")
        return "\n".join(lines)


def check_worker(client: ComfyUIClient, workflow: str, files: dict[str, str]) -> WorkerReport:
    """Health (``/system_stats``, VRAM), node classes and input names (``/object_info``) and model files
    (``/models/{folder}``) for one workflow. Nothing is queued."""
    rep = WorkerReport(client.base, workflow)
    try:
        rep.health = client.health()
    except ComfyUIError as e:
        rep.problems.append(str(e))
        return rep
    try:
        info = client.object_info()
    except ComfyUIError as e:
        rep.problems.append(f"/object_info: {e}")
        return rep
    for cls, used in sorted(nodes_of(load_workflow(workflow)).items()):
        if cls not in info:
            rep.missing_nodes.append(cls)
            continue
        declared = set((info[cls].get("input") or {}).get("required", {})) | set(
            (info[cls].get("input") or {}).get("optional", {})
        )
        unknown = sorted(used - declared)
        if unknown:
            rep.unknown_inputs[cls] = unknown
    if rep.missing_nodes:
        rep.problems.append(
            f"missing node(s): {', '.join(rep.missing_nodes)} (update ComfyUI; these are native nodes in current "
            "releases)"
        )
    for cls, names in rep.unknown_inputs.items():
        rep.problems.append(f"{cls} does not declare input(s) {', '.join(names)} on this ComfyUI version")
    for role, name in files.items():
        folder = FOLDERS[role]
        try:
            have = client.models(folder)
        except ComfyUIError as e:
            rep.problems.append(f"/models/{folder}: {e}")
            continue
        if name not in have and not any(h.replace("\\", "/").endswith("/" + name) for h in have):
            rep.missing_models.setdefault(folder, []).append(name)
    if rep.missing_models:
        n = sum(len(v) for v in rep.missing_models.values())
        rep.problems.append(f"{n} model file(s) missing on the worker")
    return rep


def _cover(path: Path, size: tuple[int, int]) -> bytes:
    im = ImageOps.fit(Image.open(path).convert("RGB"), size, Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _video_outputs(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Every saved video file in a history entry, whatever key the save node used (images / videos / gifs)."""
    found = []
    for output in (entry.get("outputs") or {}).values():
        for value in output.values():
            if isinstance(value, list):
                found += [
                    v
                    for v in value
                    if isinstance(v, dict) and str(v.get("filename", "")).lower().endswith(VIDEO_SUFFIXES)
                ]
    return sorted(found, key=lambda v: not str(v["filename"]).lower().endswith(".mp4"))


class ComfyUIVideoProvider(VideoProvider):
    name = "comfyui-wan"
    billable = False
    local = True

    def __init__(
        self,
        client: ComfyUIClient,
        spec: ComfyUIVideoSpec | None = None,
        *,
        poll_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.s = spec or ComfyUIVideoSpec()
        self.poll_s = poll_s
        self.sleep = sleep
        self.clock = clock
        self.log = log or (lambda _m: None)
        self.client_id = f"creative-forge-{uuid.uuid4().hex[:8]}"

    def cache_params(self) -> dict[str, Any]:
        s = self.s
        wf = hashlib.sha256(json.dumps(load_workflow(s.workflow), sort_keys=True).encode()).hexdigest()[:16]
        return {
            "workflow": s.workflow,
            "workflow_sha": wf,
            "files": s.model_files(),
            "size": [s.setting("width"), s.setting("height")],
            "fps": s.setting("fps"),
            "steps": s.setting("steps"),
            "cfg": s.setting("cfg"),
            "shift": s.shift,
            "sampler": s.sampler,
            "scheduler": s.scheduler,
            "seed": s.seed,
        }

    def _params(self, req: VideoRequest, first: str, last: str | None, seed: int) -> dict[str, Any]:
        s = self.s
        p: dict[str, Any] = {
            **s.model_files(),
            "first_frame": first,
            "prompt": req.prompt,
            "negative_prompt": req.negative_prompt or "",
            "width": s.setting("width"),
            "height": s.setting("height"),
            "length": frames_for(req.duration_s, s.setting("fps")),
            "fps": s.setting("fps"),
            "steps": s.setting("steps"),
            "cfg": s.setting("cfg"),
            "shift": s.shift,
            "sampler": s.sampler,
            "scheduler": s.scheduler,
            "seed": seed,
            "filename_prefix": "creative-forge/hook",
        }
        if last is not None:
            p["last_frame"] = last
        return p

    def _submit(self, req: VideoRequest) -> list[str]:
        s = self.s
        size = (s.setting("width"), s.setting("height"))
        first_png = _cover(req.first_frame, size)
        tag = hashlib.sha256(first_png).hexdigest()[:12]
        first = self.client.upload(first_png, f"forge-first-{tag}.png", "temp")
        last = None
        if WAN_WORKFLOWS[s.workflow]["last_frame"]:
            if req.last_frame is None:
                raise ComfyUIError(f"{s.workflow} needs a last frame (the real gameplay frame)")
            last_png = _cover(req.last_frame, size)
            last = self.client.upload(last_png, f"forge-last-{hashlib.sha256(last_png).hexdigest()[:12]}.png", "temp")
        graph = load_workflow(s.workflow)
        ids = []
        for i in range(req.sample_count):
            ids.append(self.client.queue(fill(graph, self._params(req, first, last, s.seed + i)), self.client_id))
        return ids

    def _wait(self, prompt_id: str) -> dict[str, Any]:
        try:
            return self.client.wait(
                prompt_id,
                self.s.timeout_s,
                poll_s=self.poll_s,
                sleep=self.sleep,
                clock=self.clock,
                check_queue=True,
                progress=self.log,
            )
        except KeyboardInterrupt:
            try:  # stop the GPU work nobody is waiting for; the prompt id stays saved for a rerun
                self.client.interrupt()
            finally:
                raise

    def generate(
        self,
        req: VideoRequest,
        out_dir: Path,
        *,
        operation: str | None = None,
        on_submitted: Callable[[str], None] | None = None,
    ) -> ProviderResult:
        t0 = self.clock()
        ids = [x for x in (operation or "").split(",") if x]
        requeued = False
        entries: list[dict[str, Any]] = []
        if ids:
            try:
                entries = [self._wait(pid) for pid in ids]
            except PromptLost as e:
                self.log(f"{e}; queueing it again")
                ids, entries, requeued = [], [], True
        if not ids:
            ids = self._submit(req)
            if on_submitted:
                on_submitted(",".join(ids))  # saved before polling: a crash or timeout resumes these prompts
            entries = [self._wait(pid) for pid in ids]
        files = []
        for i, entry in enumerate(entries):
            outs = _video_outputs(entry)
            if not outs:
                raise ComfyUIError(f"prompt {ids[i]} finished without a video output (is SaveVideo in the graph?)")
            v = outs[0]
            data = self.client.view(v["filename"], v.get("subfolder", ""), v.get("type", "output"))
            out = out_dir / f"veo_{i}{Path(v['filename']).suffix.lower()}"
            out.write_bytes(data)
            files.append(out)
        info = {
            "operation": ",".join(ids),
            "prompt_ids": ids,
            "workflow": WORKFLOW_FILES[self.s.workflow],
            "files": self.s.model_files(),
            "frames": frames_for(req.duration_s, self.s.setting("fps")),
            "worker": self.client.base,
            "seconds": round(self.clock() - t0, 1),
        }
        if requeued:
            info["requeued"] = True
        if not WAN_WORKFLOWS[self.s.workflow]["last_frame"]:
            info["note"] = "image-to-video: no last frame; the landing relies on the crossfade and hook.patches"
        return ProviderResult(files, info)


def route_video(
    base_url: str, spec: ComfyUIVideoSpec, fallback: VideoProvider, *, client: ComfyUIClient | None = None,
    log: Callable[[str], None] | None = None,
) -> tuple[VideoProvider, dict[str, Any]]:  # fmt: skip
    """The worker if it is ready for the workflow; the cloud provider only when ``cloud_fallback`` allows it.
    Returns the provider and the routing record the ledger keeps."""
    client = client or ComfyUIClient.from_env(base_url)
    rep = check_worker(client, spec.workflow, spec.model_files())
    vram = rep.health.max_vram_gb if rep.health else None
    if rep.ok:
        record = {"kind": "video", "provider": ComfyUIVideoProvider.name, "worker": client.base, "vram_gb": vram,
                  "reason": f"worker ready for {spec.workflow}"}  # fmt: skip
        return ComfyUIVideoProvider(client, spec, log=log), record
    why = "; ".join(rep.problems)
    if spec.cloud_fallback:
        record = {"kind": "video", "provider": fallback.name, "worker": client.base, "vram_gb": vram,
                  "reason": f"worker not ready ({why}); comfyui_video.cloud_fallback is true"}  # fmt: skip
        return fallback, record
    raise ComfyUIError(
        f"video_provider comfyui: the worker at {client.base} is not ready for {spec.workflow}: {why}. "
        "Fix the worker (`forge comfy check`), or set comfyui_video.cloud_fallback: true to allow the cloud model "
        "(paid, budget-guarded)"
    )
