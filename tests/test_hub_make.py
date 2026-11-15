"""`forge make`, `forge snapshot` and the JSON outputs another program drives (studio-pulse's ad centre).

Everything runs offline: fake providers, synthetic gameplay, the in-process mock ad network. No campaign here can
spend: they are created in mock mode, and activation is exercised only against the mock.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from creative_forge.cli import main
from creative_forge.hub import snapshot, store_id, timeline
from creative_forge.make import MakeError, Request, make, spec_for

STORE = "https://play.google.com/store/apps/details?id=com.forgetest.puzzle"


def run_json(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict]:
    code = main(list(argv))
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip().startswith(("{", "[")) else {}


@pytest.fixture(scope="module")
def made(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    root = tmp_path_factory.mktemp("hub")
    res = make(
        Request(app="puzzle_game", app_name="Puzzle Game", store_url=STORE, brief="Dust cleared in 3 s",
                root=root, variants=2, languages=("tr",), mechanic="goods-sort", hook="real-hand",
                ref="meta:123"),
        progress=root / "progress.json", ocr="off",
    )  # fmt: skip
    return root, res


def test_make_renders_a_run_for_review_and_marks_its_game(made):
    root, res = made
    run = Path(res["run_dir"])
    assert res["schema"] == "creative-forge/make/v1" and res["providers"] == "fake" and res["spent_usd"] == 0
    assert len(res["exports"]) == 4  # 2 hooks x 1 language x 2 formats
    assert {e["format"] for e in res["exports"]} == {"9:16", "4:5"}
    assert json.loads((run / "app.json").read_text(encoding="utf-8"))["id"] == "puzzle_game"
    meta = json.loads((run / "make.json").read_text(encoding="utf-8"))
    assert meta["brief"] == "Dust cleared in 3 s" and meta["hook"] == "real-hand" and meta["ref"] == "meta:123"
    # the asked-for hook leads: its recipe is the concept's main hook
    assert "wash" in res["exports"][0]["ad_name"]
    prog = json.loads((root / "progress.json").read_text(encoding="utf-8"))
    assert prog["state"] == "done" and prog["done"] == prog["total"] and prog["result"]["run_dir"] == str(run)
    assert not (run / "review.json").exists()  # nothing approved by make


@pytest.mark.parametrize(
    ("kw", "word"),
    [
        ({"providers": "vertex"}, "budget"),
        ({"providers": "vertex", "budget_usd": 5.0}, "gameplay"),
        ({"app": "Bad App!"}, "game's id"),
        ({"variants": 9}, "variants"),
        ({"format": "playable"}, "forge playable"),
    ],
)
def test_make_refuses_before_rendering(tmp_path, kw, word):
    base = {"app": "puzzle_game", "brief": "", "root": tmp_path}
    with pytest.raises(MakeError, match=word):
        make(Request(**{**base, **kw}))
    assert not (tmp_path / "runs").exists() or not any((tmp_path / "runs").iterdir())


def test_make_refuses_a_placeholder_store_url(tmp_path):
    with pytest.raises(ValueError, match="placeholder"):
        make(
            Request(
                app="g",
                brief="",
                root=tmp_path,
                store_url="https://play.google.com/store/apps/details?id=com.example.x",
            )
        )


def test_every_recipe_passes_lint_in_every_language(tmp_path):
    from creative_forge.lint import errors, lint
    from creative_forge.spec import AdSpec

    clip = tmp_path / "g.mp4"
    clip.write_bytes(b"")
    data = spec_for(Request(app="g", brief="b", root=tmp_path, variants=4, languages=("en", "tr", "de")), clip,
                    tmp_path, "s")  # fmt: skip
    spec = AdSpec.model_validate(data)
    assert errors(lint(spec)) == []
    assert len(spec.matrix(spec.concepts[0])) == 4 * 3


def test_snapshot_review_publish_and_campaigns_as_json(made, tmp_path, capsys, monkeypatch):
    root, res = made
    run = res["run_dir"]
    state = tmp_path / "state"
    files = [e["file"] for e in res["exports"] if e["format"] == "9:16"]
    code, rv = run_json(capsys, "review", run, "--approve", *files, "--note", "reads well", "--reviewer", "t", "--json")
    assert code == 0 and sum(e["decision"] == "approve" for e in rv["exports"]) == 2
    code, snap = run_json(capsys, "snapshot", "--root", str(root), "--state", str(state), "--no-posters", "--json")
    assert code == 0 and snap["schema"] == "creative-forge/snapshot/v1"
    mine = [c for c in snap["creatives"] if c["rid"].endswith(Path(run).name)]
    assert {c["review"] for c in mine} == {"approve", "pending"}
    assert all(c["app"]["id"] == "puzzle_game" and c["app"]["store_id"] == "com.forgetest.puzzle" for c in mine)
    assert snap["runs"][0]["approved"] == 2 and snap["audit"]["ok"] and snap["timeline"] == []

    deliver = root / "deliver" / "t1"
    code, pub = run_json(capsys, "publish", run, "--to", str(deliver), "--json")
    assert code == 0 and pub["exports"] == 2 and (deliver / "app.json").is_file()
    code, init = run_json(capsys, "campaign", "init", "--root", str(root), "--name", "pz-test", "--creatives",
                          str(deliver), "--store-url", STORE, "--daily", "20", "--total-cap", "100",
                          "--network", "meta", "--network", "applovin", "--app", "puzzle_game", "--json")  # fmt: skip
    assert code == 0 and Path(init["config"]).name == "pz-test.yaml"
    monkeypatch.setenv("CREATIVE_FORGE_META_ADS_TOKEN", "TEST_TOKEN_NOT_REAL")
    common = ["--state", str(state), "--db", str(tmp_path / "perf.sqlite")]
    code, made_c = run_json(capsys, "campaign", "create", init["config"], "--json", *common)
    assert code == 0 and made_c["ok"] and made_c["active"] is False and len(made_c["packs"]) == 1
    camp = next(n["campaign"] for n in made_c["networks"] if n["network"] == "meta")
    assert camp["status"] == "PAUSED" and camp["mode"] == "mock" and camp["phrase"]["tr"] == "YAYINA AL 20.00 USD"

    code = main(["campaign", "activate", camp["ref"], "--confirm", "YAYINA AL 25.00 USD", "--json", *common])
    assert code == 2 and "does not match" in capsys.readouterr().err
    code, act = run_json(capsys, "campaign", "activate", camp["ref"], "--confirm", "YAYINA AL 20.00 USD", "--json",
                         *common)  # fmt: skip
    assert code == 0 and act["campaign"]["status"] == "ACTIVE"
    code, pau = run_json(capsys, "campaign", "pause", camp["ref"], "--json", *common)
    assert code == 0 and pau["campaign"]["status"] == "PAUSED"

    code, snap = run_json(capsys, "snapshot", "--root", str(root), "--state", str(state), "--no-posters", "--json")
    kinds = [e["kind"] for e in snap["timeline"]]
    assert kinds == ["created_paused", "manual_pack", "activated", "paused"]
    assert all(e["store_id"] == "com.forgetest.puzzle" and e["hash"] for e in snap["timeline"])
    assert snap["timeline"][2]["detail"]["daily"] == 20.0 and snap["timeline"][2]["detail"]["creatives"]
    assert snap["configs"][0]["app"] == "puzzle_game" and snap["configs"][0]["networks"] == {
        "meta": "mock",
        "applovin": "manual",
    }
    assert snap["packs"][0]["network"] == "applovin" and snap["packs"][0]["store_id"] == "com.forgetest.puzzle"
    assert snap["published"][0]["app"]["id"] == "puzzle_game"


def test_timeline_derives_a_budget_change_from_two_syncs(tmp_path):
    from creative_forge.campaigns.state import CampaignState

    recs = [
        {
            "seq": 1,
            "at": "2026-11-01T10:00:00+00:00",
            "kind": "sync",
            "campaign": "p1:meta",
            "daily": 20.0,
            "hash": "a",
        },
        {
            "seq": 2,
            "at": "2026-11-02T10:00:00+00:00",
            "kind": "sync",
            "campaign": "p1:meta",
            "daily": 20.0,
            "hash": "b",
        },
        {
            "seq": 3,
            "at": "2026-11-03T10:00:00+00:00",
            "kind": "sync",
            "campaign": "p1:meta",
            "daily": 35.0,
            "hash": "c",
        },
        {"seq": 4, "at": "2026-11-03T11:00:00+00:00", "kind": "api_call", "hash": "d"},
    ]
    out = timeline(CampaignState(tmp_path), recs)
    assert [(e["kind"], e["detail"]) for e in out] == [("budget_changed", {"before": 20.0, "after": 35.0})]
    assert out[0]["network"] == "meta" and out[0]["hash"] == "c"


def test_store_id():
    assert store_id(STORE) == "com.forgetest.puzzle"
    assert store_id("https://apps.apple.com/tr/app/x/id1234567890") == "1234567890"
    assert store_id("https://example.org") is None and store_id(None) is None


def test_playable_sample_pack_and_snapshot(tmp_path, capsys):
    args = ["--root", str(tmp_path), "--name", "Sort Demo", "--title", "Puzzle Game", "--store-url", STORE]
    code, s = run_json(capsys, "playable", "sample", *args, "--app", "puzzle_game", "--language", "tr", "--json")
    assert code == 0 and Path(s["html"]).is_file()
    assert main(["playable", "sample", "--root", str(tmp_path), "--name", "Sort Demo", "--store-url", STORE]) == 2
    capsys.readouterr()
    for net in ("applovin", "meta", "google"):
        code, p = run_json(capsys, "playable", "pack", s["folder"], "--network", net, "--cta-adapter", "--out",
                           str(tmp_path / "playables" / "packed" / net), "--json")  # fmt: skip
        assert code == 0 and p["check"]["status"] == "pass", p["check"]
    snap = snapshot(tmp_path, state_dir=tmp_path / "state", posters=False)
    (pl,) = snap["playables"]
    assert pl["name"] == "sort-demo" and pl["app"]["id"] == "puzzle_game"
    assert {b["network"] for b in pl["builds"]} == {"applovin", "meta", "google"}
    assert all(b["checks"] and b["checks"][-1]["status"] == "pass" for b in pl["builds"])
