"""
Reads agent configuration and harness files from all supported coding agents.
Used by the synthesis agent to detect config↔prompt misalignment.

Covered per agent:
  Claude Code  — CLAUDE.md (global + per-project), settings.json (incl. MCP servers),
                 skills/*.md, agents/*.md, settings.local.json
  Cursor       — .cursorrules per project, ~/.cursor/mcp.json
  Codex        — AGENTS.md (global + per-project), config.toml, rules/
  opencode     — AGENTS.md per project, ~/.config/opencode config
  Gemini CLI   — GEMINI.md per project, config

Common harness files searched in every project:
  AGENTS.md, CLAUDE.md, GEMINI.md, .cursorrules,
  soul.md, SOUL.md,
  .mcp.json, mcp.json, mcp_config.json
"""
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

_CLAUDE_DIR   = Path.home() / ".claude"
_CODEX_DIR    = Path.home() / ".codex"
_GEMINI_DIR   = Path.home() / ".gemini"
_CURSOR_DIR   = Path.home() / ".cursor"
_OPENCODE_DIR = Path.home() / ".config" / "opencode"

# All config file names to look for in every git repo / project dir
_PROJECT_CONFIG_FILES: list[tuple[str, str]] = [
    ("claude-code-project", "CLAUDE.md"),
    ("cursor-rules",        ".cursorrules"),
    ("codex-project",       "AGENTS.md"),
    ("gemini-cli-project",  "GEMINI.md"),
    ("soul",                "soul.md"),
    ("soul",                "SOUL.md"),
    ("mcp-config",          ".mcp.json"),
    ("mcp-config",          "mcp.json"),
    ("mcp-config",          "mcp_config.json"),
]


class AgentConfig:
    def __init__(self, source: str, path: str, content: str, date: str):
        self.source = source
        self.path = path
        self.content = content
        self.date = date
        self.snapshot_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "path": self.path,
            "content": self.content,
            "date": self.date,
            "snapshot_at": self.snapshot_at,
        }


def _read(path: Path, max_chars: int = 8000) -> str | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[:max_chars] if len(text) > max_chars else text
    except Exception:
        return None


def _decode_project_path(encoded: str) -> Path:
    return Path("/" + encoded.lstrip("-").replace("-", "/"))


def iter_config_files() -> list[tuple[str, Path, int]]:
    """
    Enumerate every existing agent config / harness file as (source, path, max_chars).
    Shared by ingest() and by the chat agent's config-search tool.
    """
    files: list[tuple[str, Path, int]] = []
    seen: set[str] = set()

    def _add(source: str, path: Path, max_chars: int = 8000):
        if str(path) in seen or not path.exists() or not path.is_file():
            return
        seen.add(str(path))
        files.append((source, path, max_chars))

    # ── Claude Code globals ──────────────────────────────────────────────────
    _add("claude-code-global",         _CLAUDE_DIR / "CLAUDE.md")
    _add("claude-code-settings",       _CLAUDE_DIR / "settings.json")
    _add("claude-code-settings-local", _CLAUDE_DIR / "settings.local.json")

    skills_dir = _CLAUDE_DIR / "skills"
    if skills_dir.exists():
        for f in skills_dir.rglob("*.md"):
            _add("claude-code-skill", f, 3000)

    agents_dir = _CLAUDE_DIR / "agents"
    if agents_dir.exists():
        for f in agents_dir.rglob("*.md"):
            _add("claude-code-agent", f, 3000)

    # ── Codex globals ────────────────────────────────────────────────────────
    _add("codex-global", _CODEX_DIR / "config.toml")
    _add("codex-global", _CODEX_DIR / "AGENTS.md")

    rules_dir = _CODEX_DIR / "rules"
    if rules_dir.exists():
        for f in rules_dir.rglob("*"):
            _add("codex-rule", f, 3000)

    # ── Gemini CLI globals ───────────────────────────────────────────────────
    for fname in ("config", "config.json", "settings.json"):
        _add("gemini-cli-global", _GEMINI_DIR / fname)

    # ── Cursor + opencode globals ────────────────────────────────────────────
    _add("cursor-global", _CURSOR_DIR / "mcp.json")
    for fname in ("opencode.json", "opencode.jsonc", "AGENTS.md"):
        _add("opencode-global", _OPENCODE_DIR / fname)

    # ── Per-project configs: scan via Claude Code's known projects ───────────
    projects_dir = _CLAUDE_DIR / "projects"
    if projects_dir.exists():
        for project_dir in projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            project_path = _decode_project_path(project_dir.name)
            if not project_path.exists():
                continue
            for source, fname in _PROJECT_CONFIG_FILES:
                _add(source, project_path / fname)

    # ── Scan GIT_SCAN_DIRS for repos not in ~/.claude/projects ──────────────
    scan_dirs = [
        Path(d.strip())
        for d in os.getenv("GIT_SCAN_DIRS", str(Path.home() / "Developer")).split(",")
        if d.strip()
    ]
    for base in scan_dirs:
        if not base.exists():
            continue
        for depth_glob in ("*", "*/*"):
            for repo_dir in base.glob(depth_glob):
                if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
                    continue
                for source, fname in _PROJECT_CONFIG_FILES:
                    _add(source, repo_dir / fname)

    return files


async def ingest(date: str) -> AsyncIterator[AgentConfig]:
    for source, path, max_chars in iter_config_files():
        content = _read(path, max_chars)
        if content:
            yield AgentConfig(source=source, path=str(path), content=content, date=date)
