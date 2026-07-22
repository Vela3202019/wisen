"""
Reads all local agent data into memory. Nothing is written to MongoDB here.
MongoDB only receives the final Reflection produced by the synthesis agent.

Returns:
  IngestResult.sessions — NormalizedSession objects from all connectors
  IngestResult.configs  — AgentConfig objects (CLAUDE.md, AGENTS.md, MCP, etc.)
"""
from datetime import datetime, timezone
from dataclasses import dataclass, field

from ingestion.normalizer import NormalizedSession
from connectors.agent_configs import AgentConfig
from connectors import (
    claude_code, agent_configs, opencode, cursor,
    codex, gemini_cli, pi, generic_export, git_commits,
)


@dataclass
class IngestResult:
    sessions: list[NormalizedSession] = field(default_factory=list)
    configs: list[AgentConfig] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        by_source: dict[str, int] = {}
        for s in self.sessions:
            by_source[s.source] = by_source.get(s.source, 0) + 1
        return {
            "total_sessions": len(self.sessions),
            "total_configs": len(self.configs),
            "by_source": by_source,
            "errors": self.errors,
        }


async def ingest_all(date: str | None = None) -> IngestResult:
    result = IngestResult()
    today = date or datetime.now(timezone.utc).date().isoformat()

    session_connectors = [
        ("claude-code",  claude_code.ingest(date)),
        ("codex",        codex.ingest(date)),
        ("opencode",     opencode.ingest(date)),
        ("cursor",       cursor.ingest(date)),
        ("gemini-cli",   gemini_cli.ingest(date)),
        ("pi",           pi.ingest(date=date)),
        ("export",       generic_export.ingest(date=date)),
        ("git",          git_commits.ingest(date)),
    ]

    for source_name, gen in session_connectors:
        try:
            async for session in gen:
                result.sessions.append(session)
        except Exception as e:
            result.errors.append(f"{source_name}: {e}")

    try:
        async for config in agent_configs.ingest(today):
            result.configs.append(config)
    except Exception as e:
        result.errors.append(f"agent_configs: {e}")

    return result
