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
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import time
import uuid
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFilter

from creative_forge.providers.base import ImageEditProvider, ImageEditRequest, ProviderResult
from creative_forge.spec import ComfyUISpec

WORKFLOWS = {"img2img": "sdxl_img2img.json", "inpaint": "sdxl_inpaint.json"}


class ComfyUIError(RuntimeError):
    pass


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
    ) -> None:
        self.base = base_url.rstrip("/")
        self.s = settings or ComfyUISpec()
        self.session = session or requests.Session()
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
    def _request(self, method: str, path: str, **kw: Any) -> requests.Response:
        try:
            r = self.session.request(method, f"{self.base}{path}", timeout=60, **kw)
        except requests.ConnectionError as e:
            raise ComfyUIError(f"cannot reach ComfyUI at {self.base} (is it running?): {e}") from e
        if r.status_code != 200:
            raise ComfyUIError(f"{method} {path}: HTTP {r.status_code}: {_errors(r)}")
        return r

    def _upload(self, data: bytes, name: str) -> str:
        r = self._request(
            "POST",
            "/upload/image",
            files={"image": (name, data, "image/png")},
            data={"type": "temp", "overwrite": "true"},
        )
        return f"{r.json()['name']} [temp]"  # the annotation makes LoadImage read from the temp folder

    def queue(self, graph: dict[str, Any]) -> str:
        return self._request("POST", "/prompt", json={"prompt": graph, "client_id": self.client_id}).json()["prompt_id"]

    def wait(self, prompt_id: str) -> dict[str, Any]:
        deadline = self.clock() + self.s.timeout_s
        while True:
            entry = self._request("GET", f"/history/{prompt_id}").json().get(prompt_id)
            status = (entry or {}).get("status") or {}
            if status.get("status_str") == "error":
                raise ComfyUIError(f"workflow failed: {_execution_error(status)}")
            if entry and status.get("completed"):
                return entry
            if self.clock() > deadline:
                raise ComfyUIError(f"prompt {prompt_id} not finished after {self.s.timeout_s:g} s")
            self.sleep(self.poll_s)

    def fetch_first_image(self, entry: dict[str, Any]) -> Image.Image:
        for output in entry.get("outputs", {}).values():
            for im in output.get("images", []):
                params = {"filename": im["filename"], "subfolder": im.get("subfolder", ""), "type": im["type"]}
                return Image.open(io.BytesIO(self._request("GET", "/view", params=params).content)).convert("RGB")
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
    url = f"{base_url.rstrip('/')}/object_info/CheckpointLoaderSimple"
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
