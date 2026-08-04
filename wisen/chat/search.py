"""
Chat history search service.

Searches across all coding agent session sources using keyword matching.
Used as a tool by the Wisen chat agent so it can look up specific conversations
on demand rather than loading everything into context.

Strategy by source:
  claude-code   → grep ~/.claude/projects/**/*.jsonl, then parse matching files
  codex         → grep ~/.codex/sessions + archived_sessions/*.jsonl
  gemini-cli    → grep ~/.gemini/tmp/*.json
  cursor        → in-memory scan via connector (SQLite, can't grep)
  opencode      → in-memory scan via connector (SQLite, can't grep)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from wisen import config

SourceName = Literal["claude-code", "codex", "gemini-cli", "cursor", "opencode"]

_EXCERPT_CHARS = 500   # chars per matched turn
_CONTEXT_TURNS = 1     # turns before + after match to include for context
_MAX_RESULTS = 20


@dataclass
class SearchMatch:
    source: str
    project_name: str
    date: str
    role: str           # "user" | "assistant"
    excerpt: str        # matched turn content, truncated
    context: list[dict] # [{role, content}] — 1 turn before + match + 1 after
    file_path: str

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "project": self.project_name,
            "date": self.date,
            "role": self.role,
            "excerpt": self.excerpt,
            "context": self.context,
            "file_path": self.file_path,
        }


# ---------------------------------------------------------------------------
# Grep helpers
# ---------------------------------------------------------------------------

def _grep_candidate_files(query: str, root: Path, file_glob: str) -> list[Path]:
    """
    Return files under root matching file_glob that contain query (case-insensitive).
    Uses grep -ril for speed — only file names, no content parsing yet.
    Returns [] if root doesn't exist, grep times out, or no matches.
    """
    if not root.exists():
        return []
    try:
        result = subprocess.run(
            ["grep", "-ril", "--include", file_glob, query, str(root)],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        # returncode 0 = matches found, 1 = no matches, other = error
        if result.returncode not in (0, 1):
            return []
        paths = [Path(p.strip()) for p in result.stdout.splitlines() if p.strip()]
        return [p for p in paths if p.is_file()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


# ---------------------------------------------------------------------------
# Per-source file parsers
# ---------------------------------------------------------------------------

def _text_from_content(content) -> str:
    """Extract plain text from a Claude-style message content field."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


def _turns_from_excerpt(turns: list[dict], i: int) -> list[dict]:
    """Return context window: 1 turn before match, match, 1 turn after."""
    lo = max(0, i - _CONTEXT_TURNS)
    hi = min(len(turns), i + _CONTEXT_TURNS + 1)
    return [{"role": t["role"], "content": t["content"][:200]} for t in turns[lo:hi]]


def _match_turns(
    turns: list[dict],
    compiled: re.Pattern,
    source: str,
    path: Path,
    project_name: str,
) -> list[SearchMatch]:
    """Scan a turn list and return SearchMatch for each turn matching compiled."""
    date = turns[0].get("timestamp", "")[:10] if turns else "unknown"
    results = []
    for i, turn in enumerate(turns):
        if not compiled.search(turn["content"]):
            continue
        excerpt = turn["content"][:_EXCERPT_CHARS]
        if len(turn["content"]) > _EXCERPT_CHARS:
            excerpt += "…"
        results.append(SearchMatch(
            source=source,
            project_name=project_name,
            date=date,
            role=turn["role"],
            excerpt=excerpt,
            context=_turns_from_excerpt(turns, i),
            file_path=str(path),
        ))
    return results


_CLAUDE_SKIP_TYPES = {
    "permission-mode", "file-history-snapshot", "attachment",
    "ai-title", "last-prompt", "queue-operation",
}

# Harness-injected "user" content that isn't the user speaking. Skill preambles
# set isMeta, compaction summaries set isCompactSummary; the prefixes catch
# injections that carry neither flag.
_CLAUDE_NOISE_PREFIXES = (
    "Base directory for this skill",
    "This session is being continued from a previous conversation",
    "Caveat: the messages below were generated by the user while running local commands",
    "<fork-boilerplate>",
    "<system-reminder>",
    "<command-name>",
    "<local-command-stdout>",
)


def _parse_claude_code_file(path: Path, compiled: re.Pattern) -> list[SearchMatch]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            events = [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []

    # Group events by sessionId
    by_session: dict[str, list[dict]] = {}
    for e in events:
        by_session.setdefault(e.get("sessionId", "default"), []).append(e)

    # Derive project name from the path: .claude/projects/<encoded-dir>/<session>.jsonl
    try:
        project_name = "/" + path.parent.name.lstrip("-").replace("-", "/")
        project_name = Path(project_name).name
    except (OSError, ValueError):
        project_name = path.stem

    results = []
    for session_events in by_session.values():
        turns = []
        for e in session_events:
            if e.get("type") in _CLAUDE_SKIP_TYPES:
                continue
            if e.get("isMeta") or e.get("isCompactSummary"):
                continue
            msg = e.get("message", {})
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = _text_from_content(msg.get("content", "")).strip()
            if not content:
                continue
            if role == "user" and content.startswith(_CLAUDE_NOISE_PREFIXES):
                continue
            turns.append({"role": role, "content": content, "timestamp": e.get("timestamp", "")})
        results.extend(_match_turns(turns, compiled, "claude-code", path, project_name))
    return results


def _parse_codex_file(path: Path, compiled: re.Pattern) -> list[SearchMatch]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            events = [json.loads(line) for line in f if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []

    cwd = ""
    turns: list[dict] = []
    seen_user_prefixes: set[str] = set()  # deduplicate response_item vs event_msg

    for e in events:
        etype = e.get("type")
        payload = e.get("payload", {})
        ts = e.get("timestamp", "")

        if etype == "session_meta":
            cwd = payload.get("cwd", "")
            continue

        if etype == "response_item":
            role = payload.get("role", "")
            if role not in ("user", "assistant"):
                continue
            text = " ".join(
                b.get("text", "")
                for b in payload.get("content", [])
                if isinstance(b, dict)
                and b.get("type") in ("input_text", "output_text", "text")
            ).strip()
            if not text or text.startswith("<environment_context>"):
                continue
            if role == "user":
                key = text[:60]
                if key in seen_user_prefixes:
                    continue
                seen_user_prefixes.add(key)
            turns.append({"role": role, "content": text, "timestamp": ts})

        elif etype == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message", "").strip()
            if not text:
                continue
            key = text[:60]
            if key in seen_user_prefixes:
                continue
            seen_user_prefixes.add(key)
            turns.append({"role": "user", "content": text, "timestamp": ts})

    project_name = Path(cwd).name if cwd else path.stem
    return _match_turns(turns, compiled, "codex", path, project_name)


def _parse_gemini_cli_file(path: Path, compiled: re.Pattern) -> list[SearchMatch]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []

    history = data.get("conversationHistory", [])
    created = data.get("createdAt", "")
    turns = []
    for msg in history:
        role = {"user": "user", "model": "assistant"}.get(msg.get("role", ""))
        if not role:
            continue
        text = " ".join(
            p.get("text", "")
            for p in msg.get("parts", [])
            if isinstance(p, dict)
        ).strip()
        if text:
            turns.append({"role": role, "content": text, "timestamp": created})

    return _match_turns(turns, compiled, "gemini-cli", path, path.stem)


_PI_ROLE_MAP = {
    "human": "user", "user": "user", "you": "user", "me": "user",
    "assistant": "assistant", "pi": "assistant", "ai": "assistant",
}
_PI_YOU_RE = re.compile(r"^(you|me|human)\s*:\s*", re.IGNORECASE)
_PI_PI_RE  = re.compile(r"^(pi|assistant|ai)\s*:\s*", re.IGNORECASE)


def _parse_pi_json_file(path: Path, compiled: re.Pattern) -> list[SearchMatch]:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []

    def _turns_from_messages(messages: list, title: str) -> list[dict]:
        turns = []
        for m in messages:
            role = _PI_ROLE_MAP.get(m.get("role", "").lower())
            if not role:
                continue
            text = str(m.get("content", m.get("text", ""))).strip()
            if not text:
                continue
            ts = m.get("created_at") or m.get("timestamp", "")
            turns.append({"role": role, "content": text[:2000], "timestamp": ts})
        return turns

    results = []

    if isinstance(data, list):
        # Official Pi export: [{id, title, messages: [{role, content, created_at}]}]
        for conv in data:
            if not isinstance(conv, dict):
                continue
            title = conv.get("title", "Pi conversation")
            turns = _turns_from_messages(conv.get("messages", []), title)
            results.extend(_match_turns(turns, compiled, "pi", path, title))

    elif isinstance(data, dict):
        # Simple format: {turns: [{role, content}]} or {messages: [...]}
        raw = data.get("turns", data.get("messages", []))
        turns = _turns_from_messages(raw, path.stem)
        results.extend(_match_turns(turns, compiled, "pi", path, path.stem))

    return results


def _parse_pi_txt_file(path: Path, compiled: re.Pattern) -> list[SearchMatch]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    has_labels = any(_PI_YOU_RE.match(l) or _PI_PI_RE.match(l) for l in lines)
    turns = []
    for i, line in enumerate(lines):
        if has_labels:
            if _PI_YOU_RE.match(line):
                role, content = "user", _PI_YOU_RE.sub("", line).strip()
            elif _PI_PI_RE.match(line):
                role, content = "assistant", _PI_PI_RE.sub("", line).strip()
            else:
                continue
        else:
            role, content = ("user" if i % 2 == 0 else "assistant"), line

        if content:
            turns.append({"role": role, "content": content[:2000], "timestamp": ""})

    return _match_turns(turns, compiled, "pi", path, path.stem)


# ---------------------------------------------------------------------------
# SQLite source scanners (in-memory via existing connectors)
# ---------------------------------------------------------------------------

async def _scan_cursor(compiled: re.Pattern, max_results: int) -> list[SearchMatch]:
    from wisen.connectors.cursor import ingest as cursor_ingest
    results = []
    async for session in cursor_ingest():
        for i, turn in enumerate(session.turns):
            if not compiled.search(turn.content):
                continue
            excerpt = turn.content[:_EXCERPT_CHARS]
            if len(turn.content) > _EXCERPT_CHARS:
                excerpt += "…"
            ctx_turns = session.turns[max(0, i - _CONTEXT_TURNS): i + _CONTEXT_TURNS + 1]
            results.append(SearchMatch(
                source="cursor",
                project_name=session.project_name,
                date=session.date,
                role=turn.role,
                excerpt=excerpt,
                context=[{"role": t.role, "content": t.content[:200]} for t in ctx_turns],
                file_path=session.raw_source_path,
            ))
            if len(results) >= max_results:
                return results
    return results


async def _scan_opencode(compiled: re.Pattern, max_results: int) -> list[SearchMatch]:
    from wisen.connectors.opencode import ingest as opencode_ingest
    results = []
    async for session in opencode_ingest():
        for i, turn in enumerate(session.turns):
            if not compiled.search(turn.content):
                continue
            excerpt = turn.content[:_EXCERPT_CHARS]
            if len(turn.content) > _EXCERPT_CHARS:
                excerpt += "…"
            ctx_turns = session.turns[max(0, i - _CONTEXT_TURNS): i + _CONTEXT_TURNS + 1]
            results.append(SearchMatch(
                source="opencode",
                project_name=session.project_name,
                date=session.date,
                role=turn.role,
                excerpt=excerpt,
                context=[{"role": t.role, "content": t.content[:200]} for t in ctx_turns],
                file_path=session.raw_source_path,
            ))
            if len(results) >= max_results:
                return results
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_PI_EXPORT_DIR = Path(os.getenv("PI_EXPORT_DIR", str(config.root() / ".wisen" / "exports" / "pi")))

_GREP_SOURCE_MAP: dict[str, list[tuple[Path, str, object]]] = {
    "claude-code": [
        (config.root() / ".claude" / "projects", "*.jsonl", _parse_claude_code_file),
    ],
    "codex": [
        (config.root() / ".codex" / "sessions", "*.jsonl", _parse_codex_file),
        (config.root() / ".codex" / "archived_sessions", "*.jsonl", _parse_codex_file),
    ],
    "gemini-cli": [
        (config.root() / ".gemini" / "tmp", "*.json", _parse_gemini_cli_file),
    ],
    "pi": [
        (_PI_EXPORT_DIR, "*.json", _parse_pi_json_file),
        (_PI_EXPORT_DIR, "*.txt",  _parse_pi_txt_file),
    ],
}


async def search_agent_configs(query: str, max_results: int = _MAX_RESULTS) -> list[dict]:
    """
    Search harness/config files (CLAUDE.md, AGENTS.md, skills, MCP configs,
    settings, .cursorrules, etc.) across all coding agents for a keyword.

    Returns dicts with keys: source, file_path, excerpt (matched lines with
    2 lines of context each side).
    """
    import asyncio

    return await asyncio.to_thread(_search_agent_configs_sync, query, max_results)


def _search_agent_configs_sync(query: str, max_results: int) -> list[dict]:
    from wisen.connectors.agent_configs import iter_config_files

    if not query.strip():
        return []
    compiled = re.compile(re.escape(query), re.IGNORECASE)
    results: list[dict] = []

    for source, path, _max_chars in iter_config_files():
        if len(results) >= max_results:
            break
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits = [i for i, line in enumerate(lines) if compiled.search(line)]
        if not hits:
            continue
        snippets = []
        for i in hits[:3]:
            lo, hi = max(0, i - 2), min(len(lines), i + 3)
            snippets.append("\n".join(lines[lo:hi])[:_EXCERPT_CHARS])
        results.append({
            "source": source,
            "file_path": str(path),
            "excerpt": "\n---\n".join(snippets),
        })

    return results


async def search_chat_history(
    query: str,
    sources: list[SourceName] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    max_results: int = _MAX_RESULTS,
) -> list[dict]:
    """
    Search chat history across all coding agent sources for a keyword or phrase.

    Args:
        query:       Keyword or phrase to search for (case-insensitive).
        sources:     Which sources to search. None = all sources.
                     Options: "claude-code", "codex", "gemini-cli", "cursor", "opencode"
        date_from:   Only return matches on or after this date (YYYY-MM-DD).
        date_to:     Only return matches on or before this date (YYYY-MM-DD).
        max_results: Cap on total matches returned (default 20).

    Returns:
        List of dicts with keys: source, project, date, role, excerpt, context, file_path.
        context is [{role, content}] — the matched turn plus 1 turn each side.
    """
    compiled = re.compile(re.escape(query), re.IGNORECASE)
    all_sources: list[SourceName] = sources or ["claude-code", "codex", "gemini-cli", "pi", "cursor", "opencode"]
    results: list[SearchMatch] = []

    for source in all_sources:
        if len(results) >= max_results:
            break

        if source in _GREP_SOURCE_MAP:
            for root, glob, parser in _GREP_SOURCE_MAP[source]:
                candidate_files = _grep_candidate_files(query, root, glob)
                for fpath in candidate_files:
                    if len(results) >= max_results:
                        break
                    results.extend(parser(fpath, compiled))

        elif source == "cursor":
            remaining = max_results - len(results)
            results.extend(await _scan_cursor(compiled, remaining))

        elif source == "opencode":
            remaining = max_results - len(results)
            results.extend(await _scan_opencode(compiled, remaining))

    # Date filter (applied after collection since file-level grep can't filter by date cheaply)
    if date_from or date_to:
        results = [
            r for r in results
            if (not date_from or r.date >= date_from)
            and (not date_to or r.date <= date_to)
        ]

    return [m.to_dict() for m in results[:max_results]]
