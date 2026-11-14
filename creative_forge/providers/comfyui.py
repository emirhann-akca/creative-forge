"""Hook image edit on a local ComfyUI server (SDXL), over ComfyUI's HTTP API.

The workflow is data, not code: ``data/comfyui/sdxl_img2img.json`` and ``sdxl_inpaint.json`` are API-format graphs
whose ``"$name"`` strings are filled with typed values (checkpoint, prompt, seed, denoise, ...).

One edit:

1. The frame is resized to an SDXL-sized canvas (about 1 MP, sides a multiple of 64) and uploaded with
   ``POST /upload/image`` into ComfyUI's *temp* folder, plus a mask when ``hook.edit_region`` is set.
2. ``POST /prompt`` queues the filled graph; ``GET /history/{prompt_id}`` is polled until it finishes.
3. ``GET /view`` fetches the ``PreviewImage`` result (also temp), which is resized back to the frame size.

Inputs and results stay in ComfyUI's temp folder, which ComfyUI clears on start and exit, so a run leaves nothing
in its ``input/`` or ``output/`` folders. The call is free and local, so it is not billable and has no list price.

``ComfyUIClient`` is the HTTP layer shared with the Wan video provider (``comfyui_video.py``): base URL from
``CREATIVE_FORGE_COMFYUI_URL`` (a worker on the LAN, e.g. ``http://192.168.1.20:8188``), connect and read timeouts,
an optional ``Authorization`` header for a reverse proxy in front of ComfyUI (ComfyUI itself has no authentication;
the token comes from ``CREATIVE_FORGE_COMFYUI_TOKEN`` and never appears in an error), ``/system_stats`` with VRAM,
``/object_info``, ``/models/{folder}``, ``/queue``, uploads and downloads.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFilter

from creative_forge.providers.base import ImageEditProvider, ImageEditRequest, ProviderResult
from creative_forge.spec import ComfyUISpec

WORKFLOWS = {"img2img": "sdxl_img2img.json", "inpaint": "sdxl_inpaint.json"}


ENV_TOKEN = "CREATIVE_FORGE_COMFYUI_TOKEN"
ENV_AUTH_HEADER = "CREATIVE_FORGE_COMFYUI_AUTH_HEADER"


class ComfyUIError(RuntimeError):
    pass


METADATA_HOSTS = frozenset({"169.254.169.254", "fd00:ec2::254", "metadata.google.internal", "metadata"})


def check_worker_url(base_url: str) -> str:
    """The worker URL (``CREATIVE_FORGE_COMFYUI_URL`` or ``--url``) is usually on the LAN, so private addresses are
    fine; it must be http(s), carry no ``user:password@`` (the token goes in a header) and never be a link-local or
    cloud metadata address (a request there from a cloud VM hands out its credentials, and the token would go too)."""
    import ipaddress
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url.strip())
        parts.port  # noqa: B018 (raises on a malformed port)
    except ValueError as e:
        raise ComfyUIError(f"ComfyUI URL {base_url!r} is not valid: {e}") from None
    host = (parts.hostname or "").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if parts.scheme not in ("http", "https") or not host:
        raise ComfyUIError(f"ComfyUI URL must be http(s)://host[:port], not {base_url!r}")
    if parts.username is not None or parts.password is not None:
        raise ComfyUIError(f"ComfyUI URL must not carry user:password@; set {ENV_TOKEN} instead")
    if host in METADATA_HOSTS or (ip is not None and ip.is_link_local):
        raise ComfyUIError(f"ComfyUI URL points at a link-local or cloud metadata address ({host})")
    return base_url


class PromptLost(ComfyUIError):
    """The worker knows nothing of a prompt id (it restarted): the job has to be queued again (local, so free)."""


@dataclass(frozen=True)
class Device:
    name: str
    type: str
    vram_total_gb: float | None
    vram_free_gb: float | None


@dataclass(frozen=True)
class Health:
    version: str
    devices: tuple[Device, ...]
    raw: dict[str, Any]

    @property
    def max_vram_gb(self) -> float | None:
        values = [d.vram_total_gb for d in self.devices if d.vram_total_gb is not None and d.type != "cpu"]
        return max(values) if values else None


class ComfyUIClient:
    """ComfyUI's HTTP API with timeouts, an optional auth header for a reverse proxy, and error messages that name
    the worker and the node but never the token."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        auth_header: str = "Authorization",
        session: Any = None,
        connect_timeout_s: float = 5.0,
        read_timeout_s: float = 60.0,
    ) -> None:
        self.base = check_worker_url(base_url).rstrip("/")
        self.session = session or requests.Session()
        self.timeout = (connect_timeout_s, read_timeout_s)
        self._headers = {auth_header: token if " " in token else f"Bearer {token}"} if token else {}

    @classmethod
    def from_env(cls, base_url: str, env: dict[str, str] | None = None, **kw: Any) -> ComfyUIClient:
        env = dict(os.environ if env is None else env)
        return cls(
            base_url, token=env.get(ENV_TOKEN) or None, auth_header=env.get(ENV_AUTH_HEADER, "Authorization"), **kw
        )

    def request(self, method: str, path: str, **kw: Any) -> requests.Response:
        if self._headers:
            kw["headers"] = {**self._headers, **kw.get("headers", {})}
        failure = None
        try:
            r = self.session.request(method, f"{self.base}{path}", timeout=self.timeout, **kw)
        except requests.Timeout:
            failure = f"{method} {path}: no answer from ComfyUI at {self.base} within {self.timeout[1]:g} s"
        except requests.ConnectionError as e:
            failure = f"cannot reach ComfyUI at {self.base} (is it running and listening there?): {type(e).__name__}"
        if failure:
            raise ComfyUIError(failure)
        if r.status_code in (401, 403):
            raise ComfyUIError(
                f"{method} {path}: HTTP {r.status_code} from {self.base}; the reverse proxy in front of ComfyUI "
                f"refused the request (check {ENV_TOKEN})"
            )
        if r.status_code != 200:
            raise ComfyUIError(f"{method} {path}: HTTP {r.status_code}: {_errors(r)}")
        return r

    def system_stats(self) -> dict[str, Any]:
        return self.request("GET", "/system_stats").json()

    def health(self) -> Health:
        stats = self.system_stats()
        devices = []
        for d in stats.get("devices", []) if isinstance(stats, dict) else []:
            total, free = d.get("vram_total"), d.get("vram_free")
            devices.append(
                Device(
                    str(d.get("name", "?")),
                    str(d.get("type", "?")),
                    round(total / 2**30, 1) if isinstance(total, int | float) else None,
                    round(free / 2**30, 1) if isinstance(free, int | float) else None,
                )
            )
        version = str((stats.get("system") or {}).get("comfyui_version", "unknown")) if isinstance(stats, dict) else "?"
        return Health(version, tuple(devices), stats if isinstance(stats, dict) else {})

    def object_info(self) -> dict[str, Any]:
        return self.request("GET", "/object_info").json()

    def models(self, folder: str) -> list[str]:
        try:
            data = self.request("GET", f"/models/{folder}").json()
        except ComfyUIError as e:
            if "HTTP 404" in str(e):
                return []
            raise
        return [str(x) for x in data] if isinstance(data, list) else []

    def upload(self, data: bytes, name: str, kind: str = "temp") -> str:
        r = self.request(
            "POST",
            "/upload/image",
            files={"image": (name, data, "image/png")},
            data={"type": kind, "overwrite": "true"},
        )
        return f"{r.json()['name']} [{kind}]"  # the annotation makes LoadImage read from that folder

    def queue(self, graph: dict[str, Any], client_id: str) -> str:
        return self.request("POST", "/prompt", json={"prompt": graph, "client_id": client_id}).json()["prompt_id"]

    def history(self, prompt_id: str) -> dict[str, Any] | None:
        return self.request("GET", f"/history/{prompt_id}").json().get(prompt_id)

    def queue_position(self, prompt_id: str) -> str | None:
        """ "running", "pending (n ahead)" or None when the worker's queue does not hold the prompt."""
        q = self.request("GET", "/queue").json()
        if any(len(item) > 1 and item[1] == prompt_id for item in q.get("queue_running", [])):
            return "running"
        pending = q.get("queue_pending", [])
        for i, item in enumerate(sorted(pending, key=lambda x: x[0] if x else 0)):
            if len(item) > 1 and item[1] == prompt_id:
                return f"pending ({i} ahead)"
        return None

    def wait(
        self,
        prompt_id: str,
        timeout_s: float,
        *,
        poll_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        check_queue: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        """Poll ``/history`` until the prompt finished. With ``check_queue`` a prompt that is neither finished nor in
        the queue raises ``PromptLost``; the queue position is reported through ``progress``."""
        deadline = clock() + timeout_s
        last = None
        while True:
            entry = self.history(prompt_id)
            status = (entry or {}).get("status") or {}
            if status.get("status_str") == "error":
                raise ComfyUIError(f"workflow failed: {_execution_error(status)}")
            if entry and status.get("completed"):
                return entry
            if check_queue and not entry:
                where = self.queue_position(prompt_id)
                if where is None:
                    raise PromptLost(f"ComfyUI at {self.base} does not know prompt {prompt_id} (worker restarted?)")
                if progress and where != last:
                    progress(f"prompt {prompt_id}: {where}")
                last = where
            if clock() > deadline:
                raise ComfyUIError(
                    f"prompt {prompt_id} not finished after {timeout_s:g} s; it keeps running on the worker, and a "
                    "rerun resumes it instead of queueing it again"
                )
            sleep(poll_s)

    def view(self, filename: str, subfolder: str, kind: str) -> bytes:
        return self.request("GET", "/view", params={"filename": filename, "subfolder": subfolder, "type": kind}).content

    def interrupt(self) -> None:
        self.request("POST", "/interrupt")

    def free(self, unload_models: bool = True) -> None:
        self.request("POST", "/free", json={"unload_models": unload_models, "free_memory": True})


def load_workflow(kind: str) -> dict[str, Any]:
    text = resources.files("creative_forge.data").joinpath("comfyui", WORKFLOWS[kind]).read_text(encoding="utf-8")
    return json.loads(text)


def fill(node: Any, params: dict[str, Any]) -> Any:
    """Replace every ``"$name"`` string with ``params[name]`` (keeping its type). A missing value is an error, so a
    workflow and the code that fills it cannot drift apart silently."""
    if isinstance(node, dict):
        return {k: fill(v, params) for k, v in node.items()}
    if isinstance(node, list):
        return [fill(v, params) for v in node]
    if isinstance(node, str) and node.startswith("$"):
        if node[1:] not in params:
            raise ComfyUIError(f"workflow placeholder {node} has no value")
        return params[node[1:]]
    return node


def work_size(width: int, height: int, pixels: int = 1024 * 1024, multiple: int = 64) -> tuple[int, int]:
    """SDXL works best near 1 MP; a 1080x1920 (or 360x640) frame becomes 768x1344."""
    scale = math.sqrt(pixels / (width * height))
    return (
        max(multiple, round(width * scale / multiple) * multiple),
        max(multiple, round(height * scale / multiple) * multiple),
    )


def edit_mask(size: tuple[int, int], box: tuple[int, int, int, int], feather: int = 8) -> Image.Image:
    """White inside ``box`` (may change), black outside, softened so the inpaint blends at the border."""
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rectangle(box, fill=255)
    return m.filter(ImageFilter.GaussianBlur(feather))


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class ComfyUIImageEditProvider(ImageEditProvider):
    name = "comfyui"
    billable = False
    local = True

    def __init__(
        self,
        base_url: str,
        settings: ComfyUISpec | None = None,
        *,
        session: requests.Session | None = None,
        poll_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        client: ComfyUIClient | None = None,
    ) -> None:
        self.base = base_url.rstrip("/")
        self.s = settings or ComfyUISpec()
        self.client = client or ComfyUIClient.from_env(base_url, session=session)
        self.poll_s = poll_s
        self.sleep = sleep
        self.clock = clock
        self.client_id = f"creative-forge-{uuid.uuid4().hex[:8]}"

    def cache_params(self) -> dict[str, Any]:
        s = self.s
        workflows = {k: hashlib.sha256(json.dumps(load_workflow(k)).encode()).hexdigest()[:16] for k in WORKFLOWS}
        return {
            "steps": s.steps,
            "cfg": s.cfg,
            "sampler": s.sampler,
            "scheduler": s.scheduler,
            "seed": s.seed,
            "denoise": s.denoise,
            "negative_prompt": s.negative_prompt,
            "workflows": workflows,
        }

    # ------------------------------------------------------------------------------------------------------- HTTP
    def _upload(self, data: bytes, name: str) -> str:
        return self.client.upload(data, name, "temp")

    def queue(self, graph: dict[str, Any]) -> str:
        return self.client.queue(graph, self.client_id)

    def wait(self, prompt_id: str) -> dict[str, Any]:
        return self.client.wait(prompt_id, self.s.timeout_s, poll_s=self.poll_s, sleep=self.sleep, clock=self.clock)

    def fetch_first_image(self, entry: dict[str, Any]) -> Image.Image:
        for output in entry.get("outputs", {}).values():
            for im in output.get("images", []):
                data = self.client.view(im["filename"], im.get("subfolder", ""), im["type"])
                return Image.open(io.BytesIO(data)).convert("RGB")
        raise ComfyUIError("workflow finished without an image output")

    # ------------------------------------------------------------------------------------------------------- edit
    def edit(self, req: ImageEditRequest, out_dir: Path) -> ProviderResult:
        t0 = self.clock()
        frame = Image.open(req.base_image).convert("RGB")
        size = work_size(*frame.size)
        frame_png = _png(frame.resize(size, Image.LANCZOS))
        tag = hashlib.sha256(frame_png).hexdigest()[:16]
        params: dict[str, Any] = {
            "checkpoint": req.model,
            "image": self._upload(frame_png, f"forge-{tag}.png"),
            "prompt": req.prompt,
            "negative_prompt": self.s.negative_prompt,
            "seed": self.s.seed,
            "steps": self.s.steps,
            "cfg": self.s.cfg,
            "sampler": self.s.sampler,
            "scheduler": self.s.scheduler,
        }
        if req.mask_box is not None:
            kind = "inpaint"
            mask_png = _png(edit_mask(frame.size, req.mask_box).resize(size, Image.BILINEAR))
            params["mask"] = self._upload(mask_png, f"forge-{tag}-mask-{hashlib.sha256(mask_png).hexdigest()[:8]}.png")
            params["denoise"] = self.s.denoise or 1.0
        else:
            kind = "img2img"
            params["denoise"] = self.s.denoise or 0.45
        prompt_id = self.queue(fill(load_workflow(kind), params))
        result = self.fetch_first_image(self.wait(prompt_id))
        out = out_dir / "image_0.png"
        result.resize(frame.size, Image.LANCZOS).save(out)
        info = {
            "prompt_id": prompt_id,
            "workflow": WORKFLOWS[kind],
            "checkpoint": req.model,
            "work_size": list(size),
            "denoise": params["denoise"],
            "seconds": round(self.clock() - t0, 1),
        }
        if req.reference_images:
            info["note"] = "reference images are not used by the SDXL workflows"
        return ProviderResult([out], info)


def preflight(base_url: str, checkpoint: str, timeout_s: float = 5.0) -> str | None:
    """For ``forge run --dry-run``: is the server up, and does it have the checkpoint? Returns a problem or None."""
    try:
        url = f"{check_worker_url(base_url).rstrip('/')}/object_info/CheckpointLoaderSimple"
    except ComfyUIError as e:
        return str(e)
    try:
        r = requests.get(url, timeout=timeout_s)
        r.raise_for_status()
        spec = r.json()["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"]
    except (requests.RequestException, ValueError, KeyError) as e:
        return f"image_provider=comfyui but no ComfyUI API at {base_url}: {e}"
    names = spec[0] if isinstance(spec[0], list) else (spec[1] or {}).get("options", [])
    if checkpoint not in names:
        return f"ComfyUI at {base_url} has no checkpoint {checkpoint!r} (has: {', '.join(names) or 'none'})"
    return None


def _errors(r: requests.Response) -> str:
    """ComfyUI's validation errors name the node and the input; keep those, drop the rest."""
    try:
        d = r.json()
    except ValueError:
        return r.text[:500]
    parts = [str((d.get("error") or {}).get("message", ""))]
    for node_id, ne in (d.get("node_errors") or {}).items():
        for e in ne.get("errors", []):
            parts.append(f"node {node_id} ({ne.get('class_type')}): {e.get('message')} {e.get('details', '')}".strip())
    return "; ".join(p for p in parts if p)[:800] or r.text[:500]


def _execution_error(status: dict[str, Any]) -> str:
    for event, data in status.get("messages", []):
        if event == "execution_error":
            return f"{data.get('node_type')}: {data.get('exception_message', '').strip()}"[:800]
    return "unknown error"
