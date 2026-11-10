"""Vertex AI providers against a mocked HTTP session: request shapes, polling, retries. No network."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest
import requests
from PIL import Image

from creative_forge.config import ConfigError, Settings
from creative_forge.providers import build_providers
from creative_forge.providers.base import ImageEditRequest, JudgeRequest, MusicRequest, VideoRequest
from creative_forge.providers.vertex import (
    OperationStillRunning,
    VertexClient,
    VertexError,
    VertexGeminiImageProvider,
    VertexGeminiJudgeProvider,
    VertexLyriaProvider,
    VertexVeoProvider,
    parse_judge_response,
)
from creative_forge.retry import RetryPolicy

PROJECT = "test-project"


class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text or json.dumps(payload or {})

    def json(self) -> dict:
        return self._payload or {}


class FakeSession:
    """Stands in for google.auth's AuthorizedSession (a requests.Session)."""

    def __init__(self, responses: list) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "body": json.loads(data), "headers": headers})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def client(responses: list) -> tuple[VertexClient, FakeSession]:
    s = FakeSession(responses)
    return VertexClient(PROJECT, s, retry=RetryPolicy(attempts=3, base_delay_s=0), sleep=lambda _: None), s


@pytest.fixture
def frames(tmp_path: Path) -> tuple[Path, Path]:
    a, b = tmp_path / "first.png", tmp_path / "real.png"
    Image.new("RGB", (36, 64), (10, 20, 30)).save(a)
    Image.new("RGB", (36, 64), (200, 100, 50)).save(b)
    return a, b


def b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def test_veo_request_shape_and_polling(tmp_path, frames):
    first, real = frames
    c, s = client([
        FakeResponse(200, {"name": "projects/test-project/locations/us-central1/operations/op1"}),
        FakeResponse(200, {"done": False}),
        FakeResponse(200, {"done": True, "response": {
            "videos": [{"bytesBase64Encoded": base64.b64encode(b"MP4DATA").decode(), "mimeType": "video/mp4"}],
            "raiMediaFilteredCount": 0}}),
    ])  # fmt: skip
    submitted = []
    veo = VertexVeoProvider(c, "us-central1", sleep=lambda _: None)
    req = VideoRequest("prompt", first, "veo-3.1-generate-001", last_frame=real, duration_s=8, generate_audio=True,
                       sample_count=2, negative_prompt="text, logo")  # fmt: skip
    res = veo.generate(req, tmp_path, on_submitted=submitted.append)

    submit = s.calls[0]
    assert submit["url"] == (
        "https://us-central1-aiplatform.googleapis.com/v1/projects/test-project/locations/us-central1"
        "/publishers/google/models/veo-3.1-generate-001:predictLongRunning"
    )
    inst = submit["body"]["instances"][0]
    assert inst["prompt"] == "prompt"
    assert inst["image"] == {"bytesBase64Encoded": b64(first), "mimeType": "image/png"}
    assert inst["lastFrame"] == {"bytesBase64Encoded": b64(real), "mimeType": "image/png"}
    assert submit["body"]["parameters"] == {
        "aspectRatio": "9:16", "durationSeconds": 8, "resolution": "1080p", "generateAudio": True,
        "sampleCount": 2, "negativePrompt": "text, logo",
    }  # fmt: skip
    assert submit["headers"]["x-goog-user-project"] == PROJECT
    assert s.calls[1]["url"].endswith(":fetchPredictOperation")
    assert s.calls[1]["body"] == {"operationName": "projects/test-project/locations/us-central1/operations/op1"}
    assert submitted == ["projects/test-project/locations/us-central1/operations/op1"]
    assert res.files[0].read_bytes() == b"MP4DATA"


def test_veo_resume_does_not_submit(tmp_path, frames):
    c, s = client([FakeResponse(200, {"done": True, "response": {"videos": [{"bytesBase64Encoded": "AA=="}]}})])
    VertexVeoProvider(c, sleep=lambda _: None).generate(
        VideoRequest("p", frames[0], "veo"), tmp_path, operation="ops/existing"
    )
    assert len(s.calls) == 1 and s.calls[0]["url"].endswith(":fetchPredictOperation")


def test_veo_poll_timeout_keeps_operation_name(tmp_path, frames):
    c, _ = client([FakeResponse(200, {"name": "ops/slow"})] + [FakeResponse(200, {"done": False})] * 2)
    veo = VertexVeoProvider(c, max_polls=2, sleep=lambda _: None)
    with pytest.raises(OperationStillRunning) as e:
        veo.generate(VideoRequest("p", frames[0], "veo"), tmp_path)
    assert e.value.operation == "ops/slow"


def test_veo_filtered_output_is_an_error(tmp_path, frames):
    c, _ = client([FakeResponse(200, {"name": "ops/1"}),
                   FakeResponse(200, {"done": True, "response": {"raiMediaFilteredCount": 1,
                                                                 "raiMediaFilteredReasons": ["policy"]}})])  # fmt: skip
    with pytest.raises(VertexError, match="filtered: 1"):
        VertexVeoProvider(c, sleep=lambda _: None).generate(VideoRequest("p", frames[0], "veo"), tmp_path)


def test_submit_is_not_retried_on_server_error_or_timeout(frames):
    c, s = client([FakeResponse(500, text="internal")])
    with pytest.raises(VertexError, match="500"):
        c.post("https://x/y:predictLongRunning", {}, idempotent=False)
    assert len(s.calls) == 1
    c, s = client([requests.Timeout("read timed out")])
    with pytest.raises(VertexError, match="may exist"):
        c.post("https://x/y:predictLongRunning", {}, idempotent=False)
    assert len(s.calls) == 1


def test_dropped_connection_is_not_retried_on_submit_but_refused_is():
    dropped = requests.ConnectionError("('Connection aborted.', RemoteDisconnected('closed'))")
    c, s = client([dropped])
    with pytest.raises(VertexError, match="may exist"):
        c.post("https://x/y:predictLongRunning", {}, idempotent=False)
    assert len(s.calls) == 1
    refused = requests.ConnectionError("HTTPSConnectionPool: NewConnectionError('Connection refused')")
    c, s = client([refused, FakeResponse(200, {"name": "ops/1"})])
    assert c.post("https://x/y:predictLongRunning", {}, idempotent=False) == {"name": "ops/1"}
    c, s = client([dropped, FakeResponse(200, {"done": True})])
    assert c.post("https://x/y:fetchPredictOperation", {}) == {"done": True}  # polling is idempotent


def test_rate_limit_is_retried(frames):
    c, s = client([FakeResponse(429, text="quota"), FakeResponse(200, {"ok": 1})])
    assert c.post("https://x/y", {}, idempotent=False) == {"ok": 1}
    assert len(s.calls) == 2
    c, s = client([FakeResponse(400, text="bad request")])
    with pytest.raises(VertexError, match="400"):
        c.post("https://x/y", {})
    assert len(s.calls) == 1


def test_gemini_image_request_and_parse(tmp_path, frames):
    first, ref = frames
    img = base64.b64encode(b"PNGDATA").decode()
    c, s = client([FakeResponse(200, {"candidates": [{"content": {"parts": [
        {"text": "here you go"}, {"inlineData": {"mimeType": "image/png", "data": img}}]}}],
        "usageMetadata": {"totalTokenCount": 10}})])  # fmt: skip
    p = VertexGeminiImageProvider(c, "global")
    res = p.edit(ImageEditRequest("add a hand", first, "gemini-3-pro-image-preview", (ref,), "9:16"), tmp_path)
    call = s.calls[0]
    assert call["url"] == (
        "https://aiplatform.googleapis.com/v1/projects/test-project/locations/global"
        "/publishers/google/models/gemini-3-pro-image-preview:generateContent"
    )
    parts = call["body"]["contents"][0]["parts"]
    assert parts[0]["inlineData"]["data"] == b64(first) and parts[1]["inlineData"]["data"] == b64(ref)
    assert parts[2] == {"text": "add a hand"}
    assert call["body"]["generationConfig"] == {"responseModalities": ["TEXT", "IMAGE"],
                                                "imageConfig": {"aspectRatio": "9:16"}}  # fmt: skip
    assert res.files[0].read_bytes() == b"PNGDATA"
    assert res.info["model_text"] == ["here you go"]


def test_gemini_image_without_image_is_an_error(tmp_path, frames):
    c, _ = client([FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "I can't"}]}}]})])
    with pytest.raises(VertexError, match="no image"):
        VertexGeminiImageProvider(c).edit(ImageEditRequest("x", frames[0], "m"), tmp_path)


def test_lyria_request_and_parse(tmp_path):
    audio = base64.b64encode(b"RIFFWAVE").decode()
    c, s = client([FakeResponse(200, {"predictions": [{"bytesBase64Encoded": audio, "mimeType": "audio/wav"}]})])
    res = VertexLyriaProvider(c, "global").compose(
        MusicRequest("bouncy puzzle music", "lyria-x", "vocals", 7), tmp_path
    )
    body = s.calls[0]["body"]
    assert s.calls[0]["url"].endswith("/models/lyria-x:predict")
    assert body["instances"][0] == {"prompt": "bouncy puzzle music", "negative_prompt": "vocals", "seed": 7}
    assert res.files[0].read_bytes() == b"RIFFWAVE"


def test_judge_body_and_parse(tmp_path, good_video):
    body = VertexGeminiJudgeProvider.build_body(b"MP4", "rubric", 5)
    part = body["contents"][0]["parts"][0]
    assert part["inlineData"]["mimeType"] == "video/mp4" and part["videoMetadata"] == {"fps": 5}
    assert body["generationConfig"] == {"temperature": 0.0, "responseMimeType": "application/json"}

    resp = {"candidates": [{"content": {"parts": [
        {"text": "thinking...", "thought": True},
        {"text": '```json\n{"scores": {"overall": 7}, "flaws": []}\n```'}]}}], "usageMetadata": {"x": 1}}  # fmt: skip
    parsed = parse_judge_response(resp)
    assert parsed["scores"]["overall"] == 7 and parsed["usage"] == {"x": 1}
    assert "error" in parse_judge_response({"error": {"message": "quota"}})
    assert "error" in parse_judge_response({"candidates": [{"content": {"parts": [{"text": "no json"}]}}]})

    c, s = client([FakeResponse(200, resp)])
    out = VertexGeminiJudgeProvider(c).score(JudgeRequest(good_video, "rubric", "gemini-3.1-pro-preview"))
    assert out["scores"]["overall"] == 7
    sent = base64.b64decode(s.calls[0]["body"]["contents"][0]["parts"][0]["inlineData"]["data"])
    assert len(sent) < good_video.stat().st_size * 2 and sent[4:8] == b"ftyp"  # downscaled MP4 was sent


def test_judge_error_payload_may_be_a_plain_string():
    assert parse_judge_response({"error": "quota exceeded"}) == {"error": "quota exceeded"}


def test_vertex_needs_a_project():
    with pytest.raises(ConfigError, match="CREATIVE_FORGE_PROJECT"):
        build_providers("vertex", Settings(project=None))
    assert Settings.from_env({"CREATIVE_FORGE_PROJECT": "p1"}).project == "p1"
    assert Settings.from_env({}).project is None
