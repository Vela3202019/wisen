"""
Training data for a local model (MLX LoRA; see docs/training.md), in mlx-lm's
chat format: one {"messages": [...]} object per line of train.jsonl and
valid.jsonl, plus meta.json describing the run (counts and case IDs, no text).

export_cases: from the mined case bank. The prompt is the replay prompt. A
correction's target is the reply that would have needed no correction: a
teacher model's rewrite or, by default, the agent's own reply
after the correction. A clean case's target is the agent's original reply.
Holdout cases and frozen manifest cases never enter, so the replay suite
stays a test the model has not trained on.

distill_demo: the demo rules as training data. A teacher writes fresh
requests per rule, drops any close to a demo case, and answers the rest under
the rules. The examples carry no instructions, so a LoRA trained on them has
to hold the rules in its weights.
"""
import asyncio
import json
import os
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable
from hashlib import sha256
from pathlib import Path

from wisen.connectors.claude_code import (
    _group_by_session,
    _parse_jsonl,
    _plain_text,
    _tool_rejection_text,
)
from wisen.evals.cases import Case
from wisen.evals.demo import load_bank, rules
from wisen.evals.miner import _HARNESS_RE
from wisen.evals.replay import build_prompt

Ask = Callable[[str, str], Awaitable[str]]  # (instructions, prompt) -> reply
_TRUNCATED = "… [truncated "
_NEAR_DUPLICATE = 0.5

_REWRITE = """You write training targets for a coding assistant. In the conversation below, the
assistant's reply drew a correction from the user. Write the reply the assistant should have
given instead, at the same point, so the user would not have needed to correct it. Keep what
was right in the original reply and follow the preference the correction reveals. Write only
the reply, as the assistant, without mentioning the correction."""

_WRITER = "You write realistic requests developers send to a coding assistant, for training data."
_REQUESTS = """Write {n} different requests where an assistant that ignored the rule about "{rule}"
below would likely break it. Vary the task, wording and length; each request must stand alone.
One request per line, no numbering.

Rules:
{rules}"""
_CLEAN_REQUESTS = """Write {n} different requests that none of the rules below bears on, across
everyday coding work. Vary the task, wording and length; each request must stand alone.
One request per line, no numbering.

Rules:
{rules}"""
_ANSWER = ("You are a coding assistant. Answer the request directly and concisely. "
           "Follow these rules without mentioning them:\n\n{rules}")


def teacher(model: str | None = None) -> Ask:
    """Ask the eval model (or `model`) for one reply."""
    from pydantic_ai import Agent

    from wisen.llm import eval_model

    name = model or eval_model()

    async def ask(instructions: str, prompt: str) -> str:
        return (await Agent(name, instructions=instructions).run(prompt)).output
    ask.model = name
    return ask


def example(prompt: str, reply: str) -> dict:
    return {"messages": [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply.strip()}]}


def write_dataset(examples: list[tuple[str, dict]], out: Path, valid_fraction: float = 0.1) -> dict[str, list[str]]:
    """Split (key, example) pairs by key hash, so a rerun splits the same way,
    and write train.jsonl and valid.jsonl. mlx-lm needs both non-empty."""
    if not 0 < valid_fraction < 1:
        raise ValueError("valid fraction must be between 0 and 1")
    if len(examples) < 2:
        raise ValueError(f"{len(examples)} usable examples; training needs at least two (one for validation)")
    ranked = sorted(examples, key=lambda e: sha256(e[0].encode()).hexdigest())
    n_valid = min(len(ranked) - 1, max(1, round(len(ranked) * valid_fraction)))
    splits = {"train": ranked[n_valid:], "valid": ranked[:n_valid]}
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    for name, rows in splits.items():
        _write_private(out / f"{name}.jsonl", "".join(json.dumps(row) + "\n" for _, row in rows))
    return {name: [key for key, _ in rows] for name, rows in splits.items()}


def _write_private(path: Path, text: str) -> None:
    """Write `text` readable by the owner only: exports hold session excerpts."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def _write_meta(out: Path, meta: dict) -> dict:
    _write_private(out / "meta.json", json.dumps(meta, indent=2) + "\n")
    return meta


# ── From the case bank ──────────────────────────────────────────────────────

def trainable(cases: Iterable[Case], exclude: set[str], include_mined: bool = False, *,
              exclude_sessions: frozenset[str] = frozenset()) -> list[Case]:
    """Confirmed eval-split cases outside `exclude`. A whole session goes when
    any of its cases is excluded: a later case's context holds the earlier ones."""
    cases = list(cases)
    held = exclude_sessions | {c.session_id for c in cases if c.id in exclude or c.split == "holdout"}
    return [c for c in cases if c.split == "eval" and c.session_id not in held and c.id not in exclude
            and c.status != "rejected" and (include_mined or c.status == "confirmed")]


def _trailing_reply(case: Case) -> str:
    """The agent turns at the end of the context: the reply the prompt drew."""
    replies = []
    for turn in reversed(case.context):
        if turn.role != "assistant":
            break
        replies.append(turn.content)
    return "\n\n".join(reversed(replies))


def reply_after(case: Case) -> str:
    """The agent's text after the case's turn, up to the next human turn (text
    or a tool rejection), read
    from the source transcript; empty when the transcript is gone."""
    path = Path(case.source_path)
    try:
        index = int(case.id.rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return ""
    if not path.is_file():
        return ""
    events = _group_by_session(_parse_jsonl(path)).get(case.session_id, [])
    parts = []
    for event in events[index + 1:]:
        msg = event.get("message")
        if event.get("isMeta") or event.get("isSidechain") or not isinstance(msg, dict):
            continue
        text = _plain_text(msg.get("content", "")).strip()
        if msg.get("role") == "user" and ((text and not _HARNESS_RE.match(text))
                                          or _tool_rejection_text(msg.get("content", ""))):
            break
        if msg.get("role") == "assistant" and text:
            parts.append(text)
    return "\n\n".join(parts)


async def _target(case: Case, ask: Ask | None) -> str | None:
    if case.kind == "clean":
        reply = _trailing_reply(case)
        return reply if reply.strip() and _TRUNCATED not in reply else None
    after = reply_after(case)
    if ask is None:
        return after or None
    return await ask(_REWRITE, (
        f"## Conversation\n{build_prompt(case)}\n\n"
        f"## The assistant's reply\n{_trailing_reply(case) or '(not recorded)'}\n\n"
        f"## The user's correction\n{case.correction}\n\n"
        f"## What the assistant did after the correction\n{after or '(not recorded)'}"))


async def export_cases(cases: Iterable[Case], out: Path, *, exclude: set[str] = frozenset(), ask: Ask | None = None,
                       include_mined: bool = False, valid_fraction: float = 0.1, concurrency: int = 4,
                       exclude_sessions: frozenset[str] = frozenset()) -> dict:
    picked = trainable(cases, set(exclude), include_mined, exclude_sessions=exclude_sessions)
    gate = asyncio.Semaphore(concurrency)

    async def one(case: Case) -> str | None:
        async with gate:
            return await _target(case, ask)
    targets = await asyncio.gather(*(one(c) for c in picked), return_exceptions=True)
    examples = [(case.id, example(build_prompt(case), target)) for case, target in zip(picked, targets)
                if isinstance(target, str) and target.strip()]
    ids = write_dataset(examples, out, valid_fraction)
    kinds = {case.id: case.kind for case in picked}
    return _write_meta(out, {
        "source": "case-bank", "teacher": getattr(ask, "model", "custom") if ask else None,
        "train": len(ids["train"]), "valid": len(ids["valid"]),
        "kinds": dict(Counter(kinds[i] for split in ids.values() for i in split)),
        "skipped": len(picked) - len(examples), "failed": sum(isinstance(t, BaseException) for t in targets),
        "excluded": len(exclude), "case_ids": ids})


# ── Demo rules ──────────────────────────────────────────────────────────────

def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def near_duplicate(text: str, others: Iterable[str], threshold: float = _NEAR_DUPLICATE) -> bool:
    """Word-set Jaccard similarity at or above `threshold` with any of `others`."""
    words = _words(text)
    return any(len(words & (o := _words(other))) / max(1, len(words | o)) >= threshold for other in others)


def _lines(text: str) -> list[str]:
    lines = (re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip() for line in text.splitlines())
    return list(dict.fromkeys(line for line in lines if line))


async def distill_demo(out: Path, *, per_rule: int = 40, clean: int = 40, ask: Ask | None = None,
                       valid_fraction: float = 0.1, concurrency: int = 4) -> dict:
    ask = ask or teacher()
    bank = load_bank()
    rule_names = {rule for cohort, rule, _ in bank if cohort != "clean"}
    text = rules()
    asks = {rule: _REQUESTS.format(n=per_rule + per_rule // 4, rule=rule, rules=text) for rule in sorted(rule_names)}
    if clean:
        asks["clean"] = _CLEAN_REQUESTS.format(n=clean + clean // 4, rules=text)
    written = await asyncio.gather(*(ask(_WRITER, prompt) for prompt in asks.values()))
    seen = [case.context[0].content for _, _, case in bank]
    kept: list[tuple[str, str]] = []
    dropped = 0
    for rule, reply in zip(asks, written):
        want = clean if rule == "clean" else per_rule
        got = 0
        for line in _lines(reply):
            if got == want:
                break
            if near_duplicate(line, seen):
                dropped += 1
                continue
            seen.append(line)
            kept.append((rule, line))
            got += 1
    gate = asyncio.Semaphore(concurrency)

    async def answer(prompt: str) -> str:
        async with gate:
            return await ask(_ANSWER.format(rules=text), prompt)
    answers = await asyncio.gather(*(answer(prompt) for _, prompt in kept), return_exceptions=True)
    used = [(rule, prompt, reply) for (rule, prompt), reply in zip(kept, answers)
            if isinstance(reply, str) and reply.strip()]
    ids = write_dataset([(prompt, example(prompt, reply)) for _, prompt, reply in used], out, valid_fraction)
    return _write_meta(out, {
        "source": "demo-distill", "teacher": getattr(ask, "model", "custom"),
        "train": len(ids["train"]), "valid": len(ids["valid"]),
        "by_rule": dict(Counter(rule for rule, _, _ in used)), "dropped_near_duplicate": dropped,
        "near_duplicate_threshold": _NEAR_DUPLICATE, "failed": len(kept) - len(used)})
