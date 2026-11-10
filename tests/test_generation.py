"""Cache, budget guard, ledger, retries and resumable operations, through the Generator with fake providers."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from creative_forge.cache import GenerationCache, content_key
from creative_forge.generator import Generator
from creative_forge.ledger import BudgetExceeded, BudgetGuard, CostLedger
from creative_forge.pricing import Pricing
from creative_forge.providers.base import ImageEditRequest, JudgeRequest, ProviderSet, VideoRequest
from creative_forge.providers.fake import (
    FakeImageEditProvider,
    FakeJudgeProvider,
    FakeMusicProvider,
    FakeVideoProvider,
)
from creative_forge.retry import RetryPolicy, TransientError, with_retry


@pytest.fixture
def frame(tmp_path: Path) -> Path:
    p = tmp_path / "frame.png"
    Image.new("RGB", (90, 160), (120, 80, 200)).save(p)
    return p


def make_gen(tmp_path: Path, *, billable: bool = False, budget: float | None = None) -> Generator:
    providers = ProviderSet(
        FakeImageEditProvider(billable=billable),
        FakeVideoProvider(billable=billable),
        FakeMusicProvider(billable=billable),
        FakeJudgeProvider(billable=billable),
    )
    ledger = CostLedger()
    return Generator(providers, GenerationCache(tmp_path / "cache"), ledger, BudgetGuard(ledger, budget),
                     Pricing.load(), tmp_path / "scratch")  # fmt: skip


def test_content_key_depends_on_file_bytes(tmp_path, frame):
    k1 = content_key("image", {"prompt": "a", "img": frame})
    assert k1 == content_key("image", {"img": frame, "prompt": "a"})  # order-independent
    assert k1 != content_key("image", {"prompt": "b", "img": frame})
    Image.new("RGB", (90, 160), (0, 0, 0)).save(frame)
    assert k1 != content_key("image", {"prompt": "a", "img": frame})


def test_cache_hit_never_pays_twice(tmp_path, frame):
    gen = make_gen(tmp_path, billable=True)
    req = ImageEditRequest("add a hand", frame, "m")
    a = gen.image_edit(req, "c1")
    b = gen.image_edit(req, "c1")
    assert a == b
    assert gen.providers.image_edit.calls == 1
    first, second = gen.ledger.entries
    assert (first.cached, first.usd) == (False, pytest.approx(0.134))
    assert (second.cached, second.usd) == (True, 0.0)
    assert gen.ledger.spent == pytest.approx(0.134)
    assert gen.ledger.saved_by_cache == pytest.approx(0.134)


def test_budget_guard_blocks_before_the_call(tmp_path, frame):
    gen = make_gen(tmp_path, billable=True, budget=1.0)
    req = VideoRequest("v", frame, "veo", last_frame=frame, duration_s=8, generate_audio=True)
    with pytest.raises(BudgetExceeded, match="Nothing was sent"):
        gen.video(req, "c1")  # 8 s x $0.40 = $3.20 > $1.00
    assert gen.providers.video.calls == 0
    assert gen.ledger.entries == []


def test_budget_guard_counts_spend(tmp_path, frame):
    gen = make_gen(tmp_path, billable=True, budget=3.5)
    gen.video(VideoRequest("v", frame, "veo", duration_s=8, generate_audio=False), "c1")  # $1.60
    assert gen.ledger.spent == pytest.approx(1.6)
    gen.video(VideoRequest("v", frame, "veo", duration_s=8, generate_audio=False), "c1")  # cache hit, free
    with pytest.raises(BudgetExceeded):
        gen.video(VideoRequest("other", frame, "veo", duration_s=8, generate_audio=True), "c1")  # +$3.20
    assert gen.providers.video.calls == 1


def test_unpriced_judge_refused_under_budget(tmp_path, frame):
    gen = make_gen(tmp_path, billable=True, budget=10)
    with pytest.raises(BudgetExceeded, match="no price"):
        gen.judge(JudgeRequest(frame, "p", "j"), "c1")


def test_fake_calls_are_free_but_show_list_price(tmp_path, frame):
    gen = make_gen(tmp_path, billable=False, budget=0.01)
    gen.video(VideoRequest("v", frame, "veo", duration_s=4, generate_audio=True), "c1")
    e = gen.ledger.entries[0]
    assert e.usd == 0.0 and e.list_usd == pytest.approx(1.6)


def test_pending_operation_is_resumed_not_resubmitted(tmp_path, frame):
    gen = make_gen(tmp_path)
    req = VideoRequest("v", frame, "veo", last_frame=frame, duration_s=4, generate_audio=False)
    key = content_key("video:fake-video", req.cache_payload())
    gen.cache.set_pending("video", key, {"operation": "projects/x/operations/resume-me"})
    files = gen.video(req, "c1")
    meta = json.loads((files[0].parent / "meta.json").read_text())
    assert meta["info"]["operation"] == "projects/x/operations/resume-me"
    assert "resumed" in gen.ledger.entries[0].detail


def test_pending_operation_is_read_after_waiting_for_the_key_lock(tmp_path, frame):
    """Job B waits while job A holds the key; A submits, then dies before finishing. B must resume A's operation
    instead of submitting (and paying for) a second one."""
    gen = make_gen(tmp_path)
    req = VideoRequest("v", frame, "veo", duration_s=4, generate_audio=False)
    key = content_key("video:fake-video", req.cache_payload())
    lock = gen._lock(key)
    lock.acquire()  # job A is inside the lock
    results: list[list[Path]] = []
    job_b = threading.Thread(target=lambda: results.append(gen.video(req, "b")))
    job_b.start()
    time.sleep(0.3)  # B is now blocked on the key lock
    gen.cache.set_pending("video", key, {"operation": "projects/x/operations/from-job-a"})  # A submitted, then died
    lock.release()
    job_b.join(10)
    meta = json.loads((results[0][0].parent / "meta.json").read_text())
    assert meta["info"]["operation"] == "projects/x/operations/from-job-a"


def test_cache_hit_is_not_labelled_as_a_resume(tmp_path, frame):
    gen = make_gen(tmp_path)
    req = VideoRequest("v", frame, "veo", duration_s=4, generate_audio=False)
    gen.video(req, "c1")
    gen.video(req, "c1")  # pending.json stays in the entry after success; this is a plain cache hit
    assert [e.detail for e in gen.ledger.entries] == ["1 x 4s no audio", "1 x 4s no audio (cache hit)"]


def test_operation_name_is_persisted_on_submit(tmp_path, frame):
    gen = make_gen(tmp_path)
    req = VideoRequest("v", frame, "veo", duration_s=4, generate_audio=False)
    gen.video(req, "c1")
    key = content_key("video:fake-video", req.cache_payload())
    assert gen.cache.pending("video", key)["operation"].startswith("fake-operation-")


def test_identical_concurrent_requests_generate_once(tmp_path, frame):
    from concurrent.futures import ThreadPoolExecutor

    gen = make_gen(tmp_path)
    req = ImageEditRequest("same", frame, "m")
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: gen.image_edit(req, "c"), range(4)))
    assert len(set(results)) == 1
    assert gen.providers.image_edit.calls == 1


def test_retry_backoff():
    calls = []
    sleeps: list[float] = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise TransientError("429")
        return "ok"

    assert with_retry(flaky, RetryPolicy(attempts=5, base_delay_s=1, jitter=0), sleep=sleeps.append) == "ok"
    assert sleeps == [1, 2]


def test_retry_gives_up_and_ignores_other_errors():
    with pytest.raises(TransientError):
        with_retry(lambda: (_ for _ in ()).throw(TransientError("x")), RetryPolicy(attempts=2), sleep=lambda _: None)
    n = []

    def bad():
        n.append(1)
        raise ValueError("permanent")

    with pytest.raises(ValueError):
        with_retry(bad, RetryPolicy(attempts=5), sleep=lambda _: None)
    assert len(n) == 1
