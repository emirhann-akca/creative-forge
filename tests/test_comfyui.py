"""ComfyUI provider against a scripted HTTP session, plus the edit_region guarantee and spec wiring."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import requests
from PIL import Image

from creative_forge.config import Settings
from creative_forge.imaging import restore_outside
from creative_forge.pricing import Pricing, estimate
from creative_forge.providers import build_providers
from creative_forge.providers.base import ImageEditRequest
from creative_forge.providers.comfyui import (
    WORKFLOWS,
    ComfyUIError,
    ComfyUIImageEditProvider,
    fill,
    load_workflow,
)
from creative_forge.spec import AdSpec, ComfyUISpec


class Resp:
    def __init__(self, status: int = 200, payload: Any = None, content: bytes = b"") -> None:
        self.status_code, self._payload, self.content = status, payload, content
        self.text = json.dumps(payload) if payload is not None else ""

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def png_bytes(size: tuple[int, int], color=(200, 40, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


class FakeComfy:
    """Records requests; answers like ComfyUI 0.3x: upload -> name, /prompt -> id, /history pending then done."""

    def __init__(self, *, history: list[dict] | None = None, prompt_status: int = 200, prompt_body=None) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self.uploads: dict[str, Image.Image] = {}
        self.graph: dict | None = None
        done = {
            "p-1": {
                "outputs": {"8": {"images": [{"filename": "ComfyUI_temp_1.png", "subfolder": "", "type": "temp"}]}},
                "status": {"status_str": "success", "completed": True, "messages": []},
            }
        }
        self.history = history if history is not None else [{}, done]  # queued, then finished
        self.prompt_status, self.prompt_body = prompt_status, prompt_body

    def request(self, method: str, url: str, timeout: float | None = None, **kw: Any) -> Resp:
        path = re.sub(r"^http://[^/]+", "", url)
        self.calls.append((method, path, kw))
        if path == "/upload/image":
            name, data, _mime = kw["files"]["image"]
            assert kw["data"] == {"type": "temp", "overwrite": "true"}
            self.uploads[name] = Image.open(io.BytesIO(data))
            return Resp(payload={"name": name, "subfolder": "", "type": "temp"})
        if path == "/prompt":
            self.graph = kw["json"]["prompt"]
            return Resp(self.prompt_status, self.prompt_body or {"prompt_id": "p-1", "number": 0, "node_errors": {}})
        if path == "/history/p-1":
            return Resp(payload=self.history.pop(0) if len(self.history) > 1 else self.history[0])
        if path == "/view":
            assert kw["params"]["type"] == "temp"
            return Resp(content=png_bytes((768, 1344)))
        raise AssertionError(f"unexpected {method} {path}")


def provider(fake: FakeComfy, **spec) -> ComfyUIImageEditProvider:
    return ComfyUIImageEditProvider(
        "http://127.0.0.1:8188",
        ComfyUISpec(**spec),
        session=fake,
        sleep=lambda _: None,  # type: ignore[arg-type]
    )


@pytest.fixture
def frame(tmp_path: Path) -> Path:
    p = tmp_path / "frame.png"
    Image.new("RGB", (360, 640), (30, 90, 160)).save(p)
    return p


def test_inpaint_flow_fills_the_shipped_workflow(tmp_path, frame):
    fake = FakeComfy()
    req = ImageEditRequest(
        "a cartoon hand holding a brush", frame, "sd_xl_base_1.0.safetensors", mask_box=(200, 300, 360, 600)
    )
    result = provider(fake, seed=42).edit(req, tmp_path)

    assert [c[1] for c in fake.calls] == [
        "/upload/image",
        "/upload/image",
        "/prompt",
        "/history/p-1",
        "/history/p-1",
        "/view",
    ]
    frame_up, mask_up = fake.uploads.values()
    assert frame_up.size == mask_up.size == (768, 1344)  # SDXL-sized canvas, not the 360x640 frame
    g = fake.graph
    assert g is not None and "$" not in json.dumps(g)
    assert g["1"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"
    assert g["6"]["inputs"]["seed"] == 42 and g["6"]["inputs"]["denoise"] == 1.0
    assert g["6"]["inputs"]["latent_image"] == ["10", 0] and g["10"]["class_type"] == "SetLatentNoiseMask"
    assert g["2"]["inputs"]["image"].endswith(" [temp]") and g["9"]["inputs"]["image"].endswith(" [temp]")
    assert g["8"]["class_type"] == "PreviewImage"  # nothing lands in ComfyUI's output/ folder

    out = Image.open(result.files[0])
    assert out.size == (360, 640)  # back at frame size
    assert result.info["workflow"] == "sdxl_inpaint.json" and result.info["work_size"] == [768, 1344]


def test_without_a_mask_it_is_a_light_img2img(tmp_path, frame):
    fake = FakeComfy()
    provider(fake).edit(ImageEditRequest("p", frame, "x.safetensors"), tmp_path)
    assert fake.graph is not None and "10" not in fake.graph
    assert fake.graph["6"]["inputs"]["denoise"] == 0.45 and len(fake.uploads) == 1


def test_validation_error_names_the_node(tmp_path, frame):
    body = {
        "error": {"message": "Prompt outputs failed validation"},
        "node_errors": {"1": {"class_type": "CheckpointLoaderSimple", "errors": [{"message": "Value not in list"}]}},
    }
    with pytest.raises(ComfyUIError, match=r"CheckpointLoaderSimple.*Value not in list"):
        provider(FakeComfy(prompt_status=400, prompt_body=body)).edit(
            ImageEditRequest("p", frame, "missing.safetensors"), tmp_path
        )


def test_execution_error_is_reported(tmp_path, frame):
    failed = {
        "p-1": {
            "outputs": {},
            "status": {
                "status_str": "error",
                "completed": False,
                "messages": [["execution_error", {"node_type": "KSampler", "exception_message": "MPS out of memory"}]],
            },
        }
    }
    with pytest.raises(ComfyUIError, match="KSampler: MPS out of memory"):
        provider(FakeComfy(history=[failed])).edit(ImageEditRequest("p", frame, "x.safetensors"), tmp_path)


def test_server_down_is_a_clear_error(tmp_path, frame):
    class Down:
        def request(self, *a, **k):
            raise requests.ConnectionError("refused")

    with pytest.raises(ComfyUIError, match="is it running"):
        ComfyUIImageEditProvider("http://127.0.0.1:1", session=Down()).edit(  # type: ignore[arg-type]
            ImageEditRequest("p", frame, "x.safetensors"), tmp_path
        )


def test_every_placeholder_in_the_shipped_workflows_is_filled():
    names = {"checkpoint", "image", "mask", "prompt", "negative_prompt", "seed", "steps", "cfg", "sampler"}
    names |= {"scheduler", "denoise"}
    for kind in WORKFLOWS:
        found = set(re.findall(r'"\$(\w+)"', json.dumps(load_workflow(kind))))
        assert found <= names, (kind, found - names)
    with pytest.raises(ComfyUIError, match=r"\$seed"):
        fill({"a": "$seed"}, {})
    assert fill({"a": ["$n", "x"]}, {"n": 3}) == {"a": [3, "x"]}  # typed, not stringified


def test_sampler_settings_are_part_of_the_cache_key():
    a = ComfyUIImageEditProvider("http://x", ComfyUISpec(denoise=0.8)).cache_params()
    b = ComfyUIImageEditProvider("http://x", ComfyUISpec(denoise=0.7)).cache_params()
    assert a != b and a["workflows"] == b["workflows"]


def test_restore_outside_keeps_every_pixel_outside_the_region(tmp_path):
    base, edited = tmp_path / "base.png", tmp_path / "edited.png"
    Image.fromarray(np.random.default_rng(1).integers(0, 255, (640, 360, 3), np.uint8)).save(base)
    Image.new("RGB", (768, 1344), (255, 0, 0)).save(edited)  # the model "changed" everything, at its own size
    box = (200, 300, 360, 600)
    out = np.asarray(Image.open(restore_outside(base, edited, box, tmp_path / "out.png")))
    ref = np.asarray(Image.open(base))
    outside = np.ones(ref.shape[:2], bool)
    outside[300:601, 200:361] = False
    assert np.array_equal(out[outside], ref[outside])
    assert (out[450, 300] == (255, 0, 0)).all()  # the centre of the region is the edit


def test_spec_can_route_the_image_step_to_comfyui():
    spec = AdSpec.model_validate(
        {
            "name": "t",
            "image_provider": "comfyui",
            "comfyui": {"checkpoint": "sd_xl_base_1.0.safetensors", "denoise": 0.85},
            "concepts": [
                {
                    "id": "c1",
                    "gameplay": {"path": "g.mp4", "in_s": 0, "out_s": 10},
                    "hook": {"video_prompt": "v", "image_prompt": "a hand", "edit_region": [600, 900, 1080, 1700]},
                }
            ],
        }
    )
    line = next(x for x in estimate(spec, Pricing.load()).lines if x.kind == "hook image")
    assert (line.model, line.usd) == ("sd_xl_base_1.0.safetensors", 0.0)
    ps = build_providers("fake", Settings(project=None, comfyui_url="http://127.0.0.1:9"), image="comfyui")
    assert isinstance(ps.image_edit, ComfyUIImageEditProvider) and ps.image_edit.base == "http://127.0.0.1:9"
    with pytest.raises(ValueError, match="checkpoint"):
        AdSpec.model_validate({**spec.model_dump(exclude={"base_dir"}), "comfyui": {"checkpoint": "../x.bin"}})
