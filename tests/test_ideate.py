"""Ideation agent: retrieval, tools, the function-calling loop (scripted model) and the written spec."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from creative_forge.cli import main
from creative_forge.ideate.agent import ScriptedChat, VertexGeminiChat, offline_script, run_agent, write_spec
from creative_forge.ideate.library import HashingEmbedder, LibraryIndex, VertexTextEmbedder, load_library
from creative_forge.ideate.tools import DECLARATIONS, Toolbox
from creative_forge.pricing import Pricing
from creative_forge.spec import load_spec


class RecordingClient:
    """Stands in for VertexClient: records posts, answers with a canned function."""

    def __init__(self, answer) -> None:
        self.posts: list[tuple[str, dict]] = []
        self.answer = answer

    def model_url(self, location: str, model: str, method: str) -> str:
        return f"https://{location}/models/{model}:{method}"

    def post(self, url: str, body: dict, *, idempotent: bool = True) -> dict:
        self.posts.append((url, body))
        return self.answer(body)


@pytest.fixture
def index(tmp_path: Path) -> LibraryIndex:
    return LibraryIndex(load_library(), HashingEmbedder(), tmp_path / "index.json")


@pytest.fixture
def tools(index: LibraryIndex, good_video: Path) -> Toolbox:
    return Toolbox(index, [good_video], Pricing.load(), n=2)


def draft(clip: Path, **over) -> dict:
    d = {
        "id": "dust-blower",
        "clip": str(clip),
        "in_s": 0.5,
        "out_s": 5.5,
        "image_prompt": "Add a hand holding a leaf blower, entering from the bottom right corner. Do not add any text.",
        "video_prompt": "The hand blows the dust away until the board matches the final frame.",
        "duration_s": 8,
        "cut_s": 5.0,
        "effect": "dust",
        "headline": "Clean it up!",
    }
    d.update(over)
    return d


def test_library_has_the_14_production_lessons_and_marks_synthetic_examples():
    entries = load_library()
    lessons = [e for e in entries if e.kind == "lesson"]
    assert len(lessons) == 14 and not any(e.synthetic for e in lessons)
    assert all(e.synthetic for e in entries if e.kind == "past_concept")


def test_hashing_embedder_is_deterministic_and_normalised():
    e = HashingEmbedder()
    a = e.embed(["hand covers the UI", "hand covers the UI", ""], query=False)
    assert np.array_equal(a[0], a[1]) and np.isclose(np.linalg.norm(a[0]), 1.0) and not a[2].any()


def test_search_ranks_by_meaning_overlap_and_the_index_file_is_reused(index, tmp_path):
    assert index.build() == len(index.entries)
    top = [e.id for _, e in index.search("the hand covered the UI and the video model invented fake UI", 3)]
    assert "L07-hand-covers-ui" in top
    again = LibraryIndex(load_library(), HashingEmbedder(), tmp_path / "index.json")
    assert again.build() == 0  # nothing re-embedded
    assert LibraryIndex(load_library(), HashingEmbedder(dim=256), tmp_path / "index.json").build() > 0


def test_vertex_embedder_request_shape():
    client = RecordingClient(
        lambda b: {"predictions": [{"embeddings": {"values": [1.0, 0.0]}} for _ in b["instances"]]}
    )
    emb = VertexTextEmbedder(client)
    v = emb.embed(["a", "b"], query=True)
    url, body = client.posts[0]
    assert url.endswith("text-embedding-005:predict") and v.shape == (2, 2) and emb.inputs == 2
    assert body["instances"][0] == {"content": "a", "task_type": "RETRIEVAL_QUERY"}


def test_lint_tool_blocks_production_mistakes_and_leaves_text_boxes_to_a_human(tools, good_video):
    ok = tools.lint_concept(draft(good_video))
    assert ok["ok"] and ok["left_for_a_human"][0].startswith("strip-text")
    bad = tools.lint_concept(draft(good_video, image_prompt="A hand with a brush in the middle.", cut_s=7.6))
    rules = {b.split(":")[0] for b in bad["blocking"]}
    assert not bad["ok"] and {"hand-enters-from-edge", "no-new-text", "tail-drift"} <= rules
    assert "unknown-clip" in tools.lint_concept(draft(Path("other.mp4")))["blocking"][0]
    assert "clip-range" in tools.lint_concept(draft(good_video, out_s=60))["blocking"][0]
    assert "invalid" in tools.lint_concept(draft(good_video, duration_s=5))["blocking"][0]


def test_cost_tool_uses_the_price_table(tools, good_video):
    r = tools.estimate_cost(draft(good_video))
    assert r["usd"] == pytest.approx(0.134 + 8 * 0.40)  # image edit + 8 s of video with audio, no music


def test_loop_fixes_rejections_until_accepted(tools, good_video):
    good, bad = draft(good_video), draft(good_video, id="b", cut_s=7.7)
    model = ScriptedChat(
        [
            [("search_creative_library", {"query": "cut late"}), ("list_gameplay_clips", {})],
            [("submit_concepts", {"concepts": [good]})],  # wrong count
            [("submit_concepts", {"concepts": [good, bad]})],  # b keeps Veo's last second
            [("lint_concept", {})],  # bad arguments come back as an error, not an exception
            [("submit_concepts", {"concepts": [good, {**bad, "cut_s": 5.0}]})],
        ]
    )
    result = run_agent(model, tools, "goods-sort puzzle", n=2, max_steps=8)
    assert result.accepted and result.steps == 5 and model.requests == 5
    results = [t["result"] for t in result.trace]
    assert "exactly 2" in results[2]["problems"][0]
    assert any("tail-drift" in p for p in results[3]["problems"])
    assert "error" in results[4]
    assert [c["concept"]["id"] for c in result.concepts] == ["dust-blower", "b"]


def test_loop_stops_at_max_steps(tools):
    result = run_agent(ScriptedChat([[("list_gameplay_clips", {})]]), tools, "x", n=2, max_steps=3)
    assert not result.accepted and result.steps == 3 and result.concepts == []


def test_gemini_request_uses_function_calling(tools):
    reply = {"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}}], "usageMetadata": {}}
    client = RecordingClient(lambda b: reply)
    run_agent(VertexGeminiChat(client), tools, "x", n=2, max_steps=1)
    url, body = client.posts[0]
    assert url.endswith("gemini-2.5-flash:generateContent")
    assert body["toolConfig"] == {"functionCallingConfig": {"mode": "ANY"}}
    names = [d["name"] for d in body["tools"][0]["functionDeclarations"]]
    assert names == [d["name"] for d in DECLARATIONS] and "submit_concepts" in names
    assert "exactly 2 concepts" in body["systemInstruction"]["parts"][0]["text"]


def test_written_spec_loads_and_points_at_the_clips(tools, good_video, tmp_path):
    result = run_agent(offline_script(2), tools, "x", n=2)
    out = write_spec(result, tmp_path / "specs" / "ideas.yaml", "ideas", ["test"])
    spec = load_spec(out)
    assert [c.id for c in spec.concepts] == ["dust-blower", "pressure-wash"]
    assert spec.resolve(spec.concepts[0].gameplay.path).resolve() == good_video.resolve()
    assert "TODO strip-text" in out.read_text(encoding="utf-8")


def test_ideate_command_offline(good_video, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CREATIVE_FORGE_CACHE", str(tmp_path / "cache"))
    out = tmp_path / "ideas.yaml"
    assert (
        main(["ideate", "--game", "goods-sort puzzle", "--clips", str(good_video), "--n", "2", "--out", str(out)]) == 0
    )
    printed = capsys.readouterr().out
    assert "lint_concept: blocking: hand-enters-from-edge" in printed and "submit_concepts: accepted" in printed
    assert len(load_spec(out).concepts) == 2
