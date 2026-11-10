"""Vertex AI providers over plain REST (``google-auth`` Application Default Credentials + ``requests``).

Request shapes are the ones the production scripts sent:

* Veo 3.1 image-to-video: ``:predictLongRunning`` with ``image`` (first frame), ``lastFrame`` (the real gameplay
  frame) and ``parameters`` (aspect ratio, duration, resolution, audio, sample count, negative prompt), then
  ``:fetchPredictOperation`` polling every 10 s.
* Gemini 3 Pro Image: ``:generateContent`` with inline PNGs + text, ``responseModalities: [TEXT, IMAGE]``.
* Gemini video judge: ``:generateContent`` with the ad as inline MP4 (downscaled to 540 px wide, sampled at 5 fps),
  temperature 0, JSON response.

No project ID, bucket or key lives in code: the project comes from ``CREATIVE_FORGE_PROJECT`` and credentials from
``gcloud auth application-default login`` (or a service account in CI).
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import requests

from creative_forge.ffmpeg import run as ffmpeg_run
from creative_forge.providers.base import (
    ImageEditProvider,
    ImageEditRequest,
    JudgeProvider,
    JudgeRequest,
    MusicProvider,
    MusicRequest,
    ProviderResult,
    VideoProvider,
    VideoRequest,
)
from creative_forge.retry import RetryPolicy, TransientError, with_retry

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class VertexError(RuntimeError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OperationStillRunning(VertexError):
    """Polling gave up while the operation was still running. Rerun to resume polling the SAME operation;
    submitting a new request would pay twice."""

    def __init__(self, operation: str) -> None:
        super().__init__(f"operation still running after the polling window: {operation}")
        self.operation = operation


def _b64(path: Path) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode()


def _mime(path: Path) -> str:
    return "image/png" if str(path).lower().endswith(".png") else "image/jpeg"


class VertexClient:
    """Authorized JSON POSTs with retry. ``idempotent=False`` (submitting a paid long-running job) retries only
    when the server clearly did not accept the request (429, 503, connection refused)."""

    def __init__(
        self,
        project: str,
        session: requests.Session | None = None,
        *,
        timeout_s: float = 300.0,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not project:
            raise VertexError("a Google Cloud project is required")
        self.project = project
        self.timeout_s = timeout_s
        self.retry = retry or RetryPolicy()
        self.sleep = sleep
        if session is None:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession

            credentials, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
            session = AuthorizedSession(credentials)
        self.session = session

    def model_url(self, location: str, model: str, method: str) -> str:
        host = "aiplatform.googleapis.com" if location == "global" else f"{location}-aiplatform.googleapis.com"
        return (
            f"https://{host}/v1/projects/{self.project}/locations/{location}/publishers/google/models/{model}:{method}"
        )

    def post(self, url: str, body: dict[str, Any], *, idempotent: bool = True) -> dict[str, Any]:
        headers = {"Content-Type": "application/json", "x-goog-user-project": self.project}

        def once() -> dict[str, Any]:
            try:
                r = self.session.post(url, data=json.dumps(body), headers=headers, timeout=self.timeout_s)
            except requests.ConnectionError as e:
                # Refused / unresolvable / connect timeout: the request never reached the server.
                # A connection dropped mid-request may have been accepted, so a submit is not retried.
                never_sent = isinstance(e, requests.ConnectTimeout) or any(
                    marker in repr(e) for marker in ("NewConnectionError", "NameResolutionError", "Failed to resolve")
                )
                if idempotent or never_sent:
                    raise TransientError(f"connection error: {e}") from e
                raise VertexError(f"connection dropped while submitting; the job may exist. Not retried: {e}") from e
            except requests.Timeout as e:
                if idempotent:
                    raise TransientError(f"timeout: {e}") from e
                raise VertexError(f"timeout while submitting; the job may exist. Not retried: {e}") from e
            if r.status_code == 200:
                return r.json()
            text = r.text[:1500]
            if r.status_code == 429 or r.status_code == 503 or (idempotent and r.status_code >= 500):
                raise TransientError(f"HTTP {r.status_code}: {text}")
            raise VertexError(f"HTTP {r.status_code}: {text}", r.status_code)

        return with_retry(once, self.retry, sleep=self.sleep)


class VertexVeoProvider(VideoProvider):
    name = "vertex-veo"

    def __init__(
        self,
        client: VertexClient,
        location: str = "us-central1",
        *,
        poll_interval_s: float = 10.0,
        max_polls: int = 120,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client
        self.location = location
        self.poll_interval_s = poll_interval_s
        self.max_polls = max_polls
        self.sleep = sleep

    @staticmethod
    def build_body(req: VideoRequest) -> dict[str, Any]:
        instance: dict[str, Any] = {
            "prompt": req.prompt,
            "image": {"bytesBase64Encoded": _b64(req.first_frame), "mimeType": _mime(req.first_frame)},
        }
        if req.last_frame is not None:
            instance["lastFrame"] = {"bytesBase64Encoded": _b64(req.last_frame), "mimeType": _mime(req.last_frame)}
        if req.reference_images:
            instance["referenceImages"] = [
                {"image": {"bytesBase64Encoded": _b64(p), "mimeType": _mime(p)}, "referenceType": "asset"}
                for p in req.reference_images[:3]
            ]
        parameters: dict[str, Any] = {
            "aspectRatio": req.aspect_ratio,
            "durationSeconds": req.duration_s,
            "resolution": req.resolution,
            "generateAudio": req.generate_audio,
            "sampleCount": req.sample_count,
        }
        if req.negative_prompt:
            parameters["negativePrompt"] = req.negative_prompt
        return {"instances": [instance], "parameters": parameters}

    def generate(
        self,
        req: VideoRequest,
        out_dir: Path,
        *,
        operation: str | None = None,
        on_submitted: Callable[[str], None] | None = None,
    ) -> ProviderResult:
        if operation is None:
            op = self.client.post(
                self.client.model_url(self.location, req.model, "predictLongRunning"),
                self.build_body(req),
                idempotent=False,
            )
            operation = op["name"]
            if on_submitted:
                on_submitted(operation)  # persisted before polling, so a crash can never lose a paid job
        poll_url = self.client.model_url(self.location, req.model, "fetchPredictOperation")
        for _ in range(self.max_polls):
            self.sleep(self.poll_interval_s)
            d = self.client.post(poll_url, {"operationName": operation})
            if d.get("done"):
                break
        else:
            raise OperationStillRunning(operation)
        if "error" in d:
            raise VertexError(f"Veo operation failed: {json.dumps(d['error'])[:1500]}")
        resp = d.get("response", {})
        files = []
        for i, v in enumerate(resp.get("videos", [])):
            if "bytesBase64Encoded" not in v:
                raise VertexError(
                    f"video {i} returned as {v.get('gcsUri')!r}; inline bytes expected (no storageUri set)"
                )
            p = out_dir / f"veo_{i}.mp4"
            p.write_bytes(base64.b64decode(v["bytesBase64Encoded"]))
            files.append(p)
        info = {
            "operation": operation,
            "rai_filtered": resp.get("raiMediaFilteredCount", 0),
            "rai_reasons": resp.get("raiMediaFilteredReasons"),
        }
        if not files:
            raise VertexError(
                f"Veo returned no video (filtered: {info['rai_filtered']}, reasons: {info['rai_reasons']})"
            )
        return ProviderResult(files, info)


class VertexGeminiImageProvider(ImageEditProvider):
    name = "vertex-gemini-image"

    def __init__(self, client: VertexClient, location: str = "global") -> None:
        self.client = client
        self.location = location

    @staticmethod
    def build_body(req: ImageEditRequest) -> dict[str, Any]:
        parts: list[dict[str, Any]] = [
            {"inlineData": {"mimeType": _mime(p), "data": _b64(p)}} for p in (req.base_image, *req.reference_images)
        ]
        parts.append({"text": req.prompt})
        return {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": req.aspect_ratio},
            },
        }

    def edit(self, req: ImageEditRequest, out_dir: Path) -> ProviderResult:
        d = self.client.post(self.client.model_url(self.location, req.model, "generateContent"), self.build_body(req))
        files, texts = [], []
        for cand in d.get("candidates", []):
            for part in cand.get("content", {}).get("parts", []):
                if "inlineData" in part:
                    p = out_dir / f"image_{len(files)}.png"
                    p.write_bytes(base64.b64decode(part["inlineData"]["data"]))
                    files.append(p)
                elif part.get("text"):
                    texts.append(part["text"][:300])
        if not files:
            raise VertexError(f"image model returned no image: {json.dumps(d)[:800]}")
        return ProviderResult(files, {"model_text": texts, "usage": d.get("usageMetadata", {})})


class VertexLyriaProvider(MusicProvider):
    """Music via Vertex AI's ``:predict`` shape for Lyria (``instances[].prompt / negative_prompt / seed``,
    ``predictions[].bytesBase64Encoded``).

    Honest note: the October 2026 tracks were made with Lyria 3 Pro through a different Google endpoint whose
    script was not kept. This class follows the documented Vertex Lyria request shape and is only tested
    against mocked responses; verify it against the current docs before a paid run.
    """

    name = "vertex-lyria"

    def __init__(self, client: VertexClient, location: str = "global") -> None:
        self.client = client
        self.location = location

    @staticmethod
    def build_body(req: MusicRequest) -> dict[str, Any]:
        instance: dict[str, Any] = {"prompt": req.prompt}
        if req.negative_prompt:
            instance["negative_prompt"] = req.negative_prompt
        if req.seed is not None:
            instance["seed"] = req.seed
            return {"instances": [instance], "parameters": {}}
        return {"instances": [instance], "parameters": {"sample_count": 1}}

    def compose(self, req: MusicRequest, out_dir: Path) -> ProviderResult:
        d = self.client.post(self.client.model_url(self.location, req.model, "predict"), self.build_body(req))
        preds = d.get("predictions", [])
        if not preds or "bytesBase64Encoded" not in preds[0]:
            raise VertexError(f"music model returned no audio: {json.dumps(d)[:800]}")
        mime = preds[0].get("mimeType", "audio/wav")
        p = out_dir / ("music.mp3" if "mpeg" in mime else "music.wav")
        p.write_bytes(base64.b64decode(preds[0]["bytesBase64Encoded"]))
        return ProviderResult([p], {"mime": mime})


def parse_judge_response(d: dict[str, Any]) -> dict[str, Any]:
    if "error" in d:
        err = d["error"]
        return {"error": str(err.get("message", err) if isinstance(err, dict) else err)[:300]}
    try:
        parts = d["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError):
        return {"error": f"no candidates: {json.dumps(d)[:300]}"}
    text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    try:
        result = json.loads(text[text.index("{") : text.rindex("}") + 1])
    except ValueError:
        return {"error": f"judge did not return JSON: {text[:200]}"}
    result["usage"] = d.get("usageMetadata", {})
    return result


class VertexGeminiJudgeProvider(JudgeProvider):
    name = "vertex-gemini-judge"

    def __init__(self, client: VertexClient, location: str = "global") -> None:
        self.client = client
        self.location = location

    @staticmethod
    def downscale(video: Path, out: Path) -> Path:
        ffmpeg_run(
            [
                "-i",
                str(video),
                "-vf",
                "scale=540:-2",
                "-c:v",
                "libx264",
                "-crf",
                "28",
                "-preset",
                "fast",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(out),
            ]
        )
        return out

    @staticmethod
    def build_body(video_bytes: bytes, prompt: str, fps: int) -> dict[str, Any]:
        return {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "inlineData": {"mimeType": "video/mp4", "data": base64.b64encode(video_bytes).decode()},
                            "videoMetadata": {"fps": fps},
                        },
                        {"text": prompt},
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "responseMimeType": "application/json"},
        }

    def score(self, req: JudgeRequest) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="forge-judge-") as td:  # one private temp dir per call
            small = self.downscale(req.video, Path(td) / "judge.mp4")
            body = self.build_body(small.read_bytes(), req.prompt, req.fps)
        try:
            d = self.client.post(self.client.model_url(self.location, req.model, "generateContent"), body)
        except VertexError as e:
            return {"error": str(e)[:300]}
        return parse_judge_response(d)
