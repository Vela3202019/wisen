import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from scripts import train_mlx
from wisen.connectors.claude_code import TOOL_REJECTION_MARKER
from wisen.evals import demo, replay, training
from wisen.evals.cases import Case, ContextTurn


def _event(role, content, ts):
    return {"sessionId": "s1", "timestamp": f"2026-07-01T10:00:0{ts}", "message": {"role": role, "content": content}}


def _case(case_id, transcript, *, split="eval", status="confirmed", kind="correction", context=None, session="s1"):
    return Case(id=case_id, date="2026-07-01", session_id=session, source_path=str(transcript), project_path="/p",
                detector=None if kind == "clean" else "explicit-correction",
                context=context or [ContextTurn(role="user", content="add requests"),
                                    ContextTurn(role="assistant", content="pip install requests")],
                correction="no, use uv", behavior_check="Did it use pip?", split=split, status=status, kind=kind)


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_frozen_session_stays_out_after_case_bank_changes(tmp_path):
    live = _case("new-case", tmp_path / "unused", session="frozen-session")
    assert not training.trainable([live], {"old-case"}, exclude_sessions=frozenset({"frozen-session"}))


def test_mac_training_wrapper_uses_both_splits_and_a_fresh_adapter(tmp_path, monkeypatch):
    rows = [(str(i), training.example(str(i), "answer")) for i in range(2)]
    data = tmp_path / "data with spaces"
    ids = training.write_dataset(rows, data, valid_fraction=0.99)
    assert len(ids["train"]) == len(ids["valid"]) == 1
    def process(row):
        offset = len(row["messages"][0]["content"].split())
        return [0] * (offset + 2), offset

    dataset = SimpleNamespace(ChatDataset=lambda rows, tokenizer, mask_prompt: SimpleNamespace(process=process))
    monkeypatch.setitem(sys.modules, "mlx_lm.tuner.datasets", dataset)
    monkeypatch.setitem(sys.modules, "mlx_lm.utils", SimpleNamespace(load_tokenizer=lambda name: None))
    calls = []
    monkeypatch.setattr(train_mlx.subprocess, "run", lambda args, **kwargs: calls.append(args))
    adapter = tmp_path / "adapter"
    train_mlx.train(data, adapter, "base", 5, 4)
    args, = calls
    for flag, value in (("--data", str(data)), ("--adapter-path", str(adapter)), ("--model", "base"), ("--iters", "5")):
        assert args[args.index(flag) + 1] == value
    for split in ("train", "valid"):
        path = data / f"{split}.jsonl"
        original = path.read_text()
        path.write_text(json.dumps(training.example("four token prompt here", "answer")) + "\n")
        with pytest.raises(ValueError, match=f"{split}.jsonl:1: no reply tokens fit"):
            train_mlx.train(data, adapter, "base", 5, 4)
        path.write_text(original)
    adapter.mkdir()
    with pytest.raises(ValueError, match="new adapter directory"):
        train_mlx.train(data, adapter, "base", 5, 4)
    assert len(calls) == 1


def test_export_cases_targets_splits_and_exclusions(tmp_path):
    transcript = tmp_path / "s1.jsonl"
    events = [_event("user", "add requests", 0), _event("assistant", "pip install requests", 1),
              _event("user", "no, use uv", 2), _event("assistant", [{"type": "text", "text": "uv add requests"}], 3),
              _event("user", "<system-reminder>ignored</system-reminder>", 4),
              _event("user", [{"type": "tool_result", "is_error": False, "content": TOOL_REJECTION_MARKER}], 4),
              _event("user", [{"type": "tool_result", "content": f"example: {TOOL_REJECTION_MARKER}"}], 4),
              _event("assistant", "Done.", 5),
              _event("user", [{"type": "tool_result", "content": TOOL_REJECTION_MARKER}], 5),
              _event("assistant", "after a rejection", 5),
              _event("user", "thanks", 6), _event("assistant", "later turn", 7)]
    transcript.write_text("".join(json.dumps(e) + "\n" for e in events))
    clean_context = [ContextTurn(role="user", content="run the tests"), ContextTurn(role="assistant", content="All pass.")]
    cases = [_case("2026-07-01-s1-2", transcript), _case("2026-07-01-s1-9", transcript, kind="clean",
                                                          context=clean_context),
             _case("held", transcript, split="holdout", session="s3"), _case("frozen", transcript, session="s2"),
             _case("same-session-as-holdout", transcript, session="s3"),
             _case("same-session-as-frozen", transcript, session="s2"),
             _case("rejected", transcript, status="rejected"), _case("unreviewed", transcript, status="mined")]
    meta = asyncio.run(training.export_cases(cases, tmp_path / "raw", exclude={"frozen"}))
    rows = _rows(tmp_path / "raw" / "train.jsonl") + _rows(tmp_path / "raw" / "valid.jsonl")
    assert sorted(r["messages"][1]["content"] for r in rows) == ["All pass.", "uv add requests\n\nDone."]
    assert {r["messages"][0]["content"] for r in rows} == {"add requests", "run the tests"}
    assert (meta["train"], meta["valid"], meta["excluded"]) == (1, 1, 1)
    assert {p.stat().st_mode & 0o777 for p in (tmp_path / "raw").iterdir()} == {0o600}
    assert sorted(meta["case_ids"]["train"] + meta["case_ids"]["valid"]) == ["2026-07-01-s1-2", "2026-07-01-s1-9"]

    seen = []

    async def ask(instructions, prompt):
        seen.append(prompt)
        return "uv add requests"
    asyncio.run(training.export_cases(cases, tmp_path / "taught", exclude={"frozen"}, ask=ask, include_mined=True))
    assert len(seen) == 2  # the confirmed and the unreviewed correction; the clean case needs no teacher
    assert "pip install requests" in seen[0] and "no, use uv" in seen[0] and "uv add requests" in seen[0]


def test_distill_demo_drops_near_duplicates_and_carries_no_instructions(tmp_path):
    bank_prompt = demo.load_bank()[0][2].context[0].content

    async def ask(instructions, prompt):
        if instructions == training._WRITER:
            assert not any(case.context[0].content in prompt for _, _, case in demo.load_bank())
            topic = prompt.split('"')[1] if '"' in prompt else "clean"
            fresh = " ".join(f"{topic}{i}" for i in range(5))
            return f"1. {bank_prompt}\n- {fresh}\n- {fresh}\n- {fresh} extra\n"
        assert "uv" in instructions
        return f"answer to {prompt}"
    meta = asyncio.run(training.distill_demo(tmp_path, per_rule=1, clean=1, ask=ask))
    rows = _rows(tmp_path / "train.jsonl") + _rows(tmp_path / "valid.jsonl")
    assert len(rows) == 7 and meta["by_rule"]["clean"] == 1 and meta["dropped_near_duplicate"] == 7
    assert all([m["role"] for m in r["messages"]] == ["user", "assistant"] for r in rows)
    assert not any(training.near_duplicate(r["messages"][0]["content"], [bank_prompt]) for r in rows)
    assert json.loads((tmp_path / "meta.json").read_text())["source"] == "demo-distill"


def test_local_model_can_name_its_own_server(monkeypatch):
    monkeypatch.setenv("WISEN_LOCAL_BASE_URL", "http://localhost:8080/v1")
    assert replay.local_target("local:base") == ("base", "http://localhost:8080/v1")
    assert replay.local_target("local:default_model@http://localhost:8081/v1/") == ("default_model",
                                                                                   "http://localhost:8081/v1")
    assert replay.harness_version("local:a@http://h:1/v1") != replay.harness_version("local:a")


def test_demo_compares_two_models_with_the_same_rules():
    seen = set()

    async def run(case, variant, votes):
        seen.add((variant.model, bool(variant.config_files)))
        return {"case": case.id, "passed": True}
    report = asyncio.run(demo.run_demo("local:base", proposed_model="local:tuned@http://h/v1", rules_in="none",
                                       repeats=1, run=run))
    assert seen == {("local:base", False), ("local:tuned@http://h/v1", False)}
    assert report["arms"] == {"current": "local:base", "proposed": "local:tuned@http://h/v1"}
