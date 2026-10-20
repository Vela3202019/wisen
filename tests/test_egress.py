"""
Canary egress test: a reflection document carrying marker strings in fields that
are not on the CloudReflection allow-list is exported, and no marker may appear
in the JSON that leaves the machine. Allow-listed fields (evidence quotes too)
must round-trip. No store needed.
Run: python3 -m tests.test_egress
"""
import asyncio
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from wisen.export import export_reflections
from wisen.synthesis.schema import CloudReflection, Reflection

QUOTE = "why does the hook fire twice?"

MARKERS = [
    "CANARY_TOP_LEVEL_EXTRA",
    "CANARY_LEGACY_KEY",
    "CANARY_KEY_MOMENT_QUOTE",
    "CANARY_DAY_SUMMARY",
    "CANARY_RAW_METADATA",
    "CANARY_NESTED_EXTRA",
]


def _reflection() -> dict:
    mood = {"label": "iterating", "summary": "small loops", "evidence": QUOTE}
    question = {"question": "Is the hook registered twice?", "evidence": QUOTE}
    return {
        "date": "2026-10-17",
        "mind_map_nodes": [{"topic": "hooks", "weight": 0.8, "is_new": True}],
        "mind_map_edges": [["hooks", "settings"]],
        "your_mood": mood,
        "your_question": question,
        "agent_mood": {**mood, "label": "confident"},
        "agent_question": question,
        "insight": "One hook ran from two settings files.",
        "tldr": ["🔁 Hook ran twice"],
        "alignment_score": 0.7,
        "alignment_label": "mild-friction",
        "friction_points": [{"type": "config-conflict", "description": "Two hooks.", "evidence": QUOTE}],
        "recommendations": [{"target": "claude-md", "action": "Remove one hook.", "reason": "Duplicate."}],
        "config_diffs": [{"file": "~/.claude/settings.json", "change_type": "modified", "summary": "Added a hook."}],
        "friction_deltas": [{"description": "Two hooks.", "status": "new", "days_persisting": 0}],
        "model_usage": [{"source": "claude-code", "model": "claude-sonnet-4-6", "session_count": 3}],
        "acted_on_recommendations": [],
        "ignored_recommendations": [],
        "seven_day_pattern": [{"date": "2026-10-16", "alignment_score": 0.6, "alignment_label": "friction"}],
        "synthesis_context": "The hook ran twice because two files registered it.",
    }


def _poisoned_doc() -> dict:
    doc = _reflection()
    doc["_id"] = "CANARY_RAW_METADATA"
    doc["synthesized_at"] = datetime.now(UTC)
    doc["notes"] = "CANARY_TOP_LEVEL_EXTRA"
    doc["raw_transcript"] = "CANARY_LEGACY_KEY"
    doc["observation"] = {
        "key_moments": [{"session_source": "claude-code", "quote": "CANARY_KEY_MOMENT_QUOTE", "significance": "x"}],
        "day_summary": "CANARY_DAY_SUMMARY",
    }
    doc["key_moments"] = [{"session_source": "codex", "quote": "CANARY_KEY_MOMENT_QUOTE", "significance": "x"}]
    doc["your_mood"]["raw_prompt"] = "CANARY_NESTED_EXTRA"
    return doc


class _FakeStore:
    def __init__(self, docs):
        self._docs = docs

    async def list_reflections(self):
        return self._docs


def _export(docs) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        count = asyncio.run(export_reflections(_FakeStore(docs), Path(tmp)))
        assert count == len(docs)
        return "\n".join(p.read_text() for p in sorted(Path(tmp).glob("*.json")))


def test_no_marker_leaves():
    out = _export([_poisoned_doc()])
    leaked = [m for m in MARKERS if m in out]
    assert not leaked, f"leaked: {leaked}"
    assert "synthesized_at" not in out
    print("✓ egress: no canary marker in exported JSON")


def test_allow_listed_fields_round_trip():
    out = json.loads(_export([_poisoned_doc()]))
    assert out == _reflection()
    assert out["friction_points"][0]["evidence"] == QUOTE
    print("✓ egress: allow-listed fields (incl. evidence quotes) round-trip")


def test_allow_list_is_reflection_fields():
    # Fails when Reflection gains a field: decide whether it may leave the machine,
    # then update CloudReflection (or this test, to keep it local).
    assert set(CloudReflection.model_fields) == set(Reflection.model_fields)
    print("✓ egress: CloudReflection lists exactly the Reflection result fields")


if __name__ == "__main__":
    test_no_marker_leaves()
    test_allow_listed_fields_round_trip()
    test_allow_list_is_reflection_fields()
    print("\nall egress tests passed")
