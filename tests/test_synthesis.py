"""
End-to-end synthesis test. Run from the repo root:
  python3 -m tests.test_synthesis             # today
  python3 -m tests.test_synthesis 2026-06-26  # specific date
"""
import asyncio
import sys
import json
from wisen.db import ensure_indexes
from wisen.ingestion.pipeline import ingest_all
from wisen.synthesis.agent import synthesize


async def main():
    date = sys.argv[1] if len(sys.argv) > 1 else None
    print(f"Ingesting for date={date or 'today'}...")

    await ensure_indexes()
    result = await ingest_all(date)
    summary = result.summary()
    print(f"  sessions={summary['total_sessions']}  configs={summary['total_configs']}  by_source={summary['by_source']}")

    if not result.sessions:
        print("No sessions found — nothing to synthesize.")
        return

    print("\nSynthesizing reflection...")
    reflection = await synthesize(result, date)

    print(f"\n{'='*60}")
    print(f"Date:            {reflection.date}")
    print(f"Alignment:       {reflection.alignment_score:.2f} ({reflection.alignment_label})")
    print(f"\nYour mood:       {reflection.your_mood.label}")
    print(f"  {reflection.your_mood.summary}")
    print(f"\nAgent mood:      {reflection.agent_mood.label}")
    print(f"  {reflection.agent_mood.summary}")
    print(f"\nYour question:   {reflection.your_question.question}")
    print(f"Agent question:  {reflection.agent_question.question}")
    print(f"\nTopics ({len(reflection.mind_map_nodes)}):")
    for n in sorted(reflection.mind_map_nodes, key=lambda x: -x.weight):
        new_tag = " [NEW]" if n.is_new else ""
        print(f"  {n.weight:.2f}  {n.topic}{new_tag}")
    if reflection.friction_points:
        print(f"\nFriction ({len(reflection.friction_points)}):")
        for f in reflection.friction_points:
            print(f"  [{f.type}] {f.description}")
    if reflection.recommendations:
        print(f"\nRecommendations:")
        for r in reflection.recommendations:
            print(f"  → [{r.target}] {r.action}")
    print(f"\nSynthesis context:\n{reflection.synthesis_context}")
    print(f"{'='*60}")
    print("\nSaved to MongoDB reflections.")


asyncio.run(main())
