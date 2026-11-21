"""
`wisen demo`: what a day with wisen looks like, in a few seconds, with no model
call and nothing read from this machine. The day below is made up; its shape
is the real Reflection and the real Gardener proposal (`wisen gardener list`).
"""
import textwrap

from wisen.synthesis.schema import Reflection

SAMPLE_REFLECTION = {
    "date": "2026-07-20",
    "mind_map_nodes": [
        {"topic": "billing API", "weight": 0.9, "is_new": False},
        {"topic": "code comments", "weight": 0.6, "is_new": True},
        {"topic": "flaky tests", "weight": 0.4, "is_new": False},
    ],
    "mind_map_edges": [["billing API", "flaky tests"]],
    "your_mood": {
        "label": "iterating",
        "summary": "Steady progress on the billing API, slowed by the same correction "
                   "over and over.",
        "evidence": "\"again: no summary comments above functions\"",
    },
    "your_question": {
        "question": "Why does the agent keep doing something I already said no to?",
        "evidence": "The same correction in 3 sessions today, and once on Monday.",
    },
    "agent_mood": {
        "label": "confident",
        "summary": "Fast and willing, but it forgets corrections between sessions.",
        "evidence": "Added a summary comment to 6 of the 9 functions it wrote.",
    },
    "agent_question": {
        "question": "Which comment style does this repo want?",
        "evidence": "CLAUDE.md says nothing about comments.",
    },
    "insight": "You corrected the same habit four times this week. Each correction "
               "lasted one session, because none of them reached CLAUDE.md.",
    "tldr": ["🔁 Same correction, four times", "🧾 CLAUDE.md silent on comments",
             "✅ Billing API shipped"],
    "alignment_score": 0.62,
    "alignment_label": "mild-friction",
    "friction_points": [{
        "type": "missing-skill",
        "description": "You removed agent-written summary comments in 3 of today's 4 "
                       "Claude Code sessions.",
        "evidence": "\"stop writing summary comments\" (14:02, 15:40, 17:15)",
    }],
    "recommendations": [{
        "target": "claude-md",
        "action": "Add one line to the repo's CLAUDE.md: no summary comments above functions.",
        "reason": "A rule in CLAUDE.md reaches every session; a correction in chat reaches one.",
    }],
    "config_diffs": [],
    "friction_deltas": [{"description": "summary comments", "status": "persisting",
                         "days_persisting": 3}],
    "model_usage": [{"source": "claude-code", "model": "claude-sonnet", "session_count": 4},
                    {"source": "codex", "model": "gpt-5-codex", "session_count": 1}],
    "acted_on_recommendations": [],
    "ignored_recommendations": [],
    "seven_day_pattern": [],
    "synthesis_context": "A productive day on the billing API, with one habit you keep fixing by hand.",
}

SAMPLE_PROPOSAL = {
    "id": "3f9a2c1d84b7e05a6cd91f2b7e4a8c63",
    "target": "~/code/billing/CLAUDE.md",
    "rationale": "You removed summary comments by hand on four days; one line here "
                 "makes every session follow it.",
    "eval_summary": "Replay on 4 eval and 6 clean-set cases: pass rate 25% → 100%; clean set "
                    "100% → 100%. Meets the accept rule (pass rate rises, clean set holds): yes.",
    "diff": (
        "--- a/CLAUDE.md\n"
        "+++ b/CLAUDE.md\n"
        "@@ -3,3 +3,4 @@\n"
        " ## Style\n"
        " - Prefer small, focused functions.\n"
        " - Run `pnpm test` before you say a change is done.\n"
        "+- Don't add summary comments above functions.\n"
    ),
}


def _wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(text, width=88, initial_indent=indent, subsequent_indent=indent)


def render() -> str:
    r = Reflection.model_validate(SAMPLE_REFLECTION)
    p = SAMPLE_PROPOSAL
    sessions = sum(m.session_count for m in r.model_usage)
    sources = ", ".join(m.source for m in r.model_usage)
    friction = r.friction_points[0]
    rec = r.recommendations[0]
    lines = [
        "wisen demo: a sample day. No model calls, nothing read from your machine.",
        "",
        f"1. The reflection  ({r.date}, {sessions} sessions from {sources}; `wisen synthesize`)",
        "",
        f"  You     {r.your_mood.label}",
        _wrap(r.your_mood.summary),
        f"  Agent   {r.agent_mood.label}",
        _wrap(r.agent_mood.summary),
        f"  Alignment {r.alignment_score:.2f} ({r.alignment_label})",
        "",
        "  Insight",
        _wrap(r.insight),
        f"  Friction ({friction.type})",
        _wrap(f"{friction.description} {friction.evidence}"),
        f"  Recommendation ({rec.target})",
        _wrap(rec.action),
        "",
        "2. The Gardener's proposal  (`wisen gardener list`)",
        "",
        f"  {p['id']}  proposed  {p['target']}",
        _wrap(p["rationale"]),
        _wrap(p["eval_summary"]),
        "",
        *(f"    {line}" for line in p["diff"].splitlines()),
        "",
        "3. You decide",
        "",
        f"  wisen gardener approve {p['id']}",
        "      makes a git branch with the change; you merge it when you choose",
        f"  wisen gardener reject {p['id']} --note \"why\"",
        "      the note reaches the Gardener's next run",
        "",
        "Try it on your own sessions:",
        "  wisen doctor       what wisen finds on this machine, and the next command to run",
        "  wisen synthesize   your latest day's reflection (needs `claude` logged in, or an API key)",
    ]
    return "\n".join(lines)


def main() -> None:
    print(render())
