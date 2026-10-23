"""
Replay a case headlessly under a CLAUDE.md variant.

Sandbox: a temp git worktree of the case's project at the commit nearest the
case's date, so the agent sees the repo roughly as it was. Config: ~/.claude is
staged into a temp dir (CLAUDE.md swapped for the variant) and the run uses
CLAUDE_CONFIG_DIR, so the real config is never touched. Bash is not in the
allowed tools — the agent can read and edit the worktree copy, nothing else.

A variant can also replace other files, so a Gardener proposal is replayed
with exactly the file it changes: `config_files` are written into the staged
config dir (paths relative to ~/.claude), `project_files` into the worktree.
"""
import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from wisen.evals.cases import Case

_CLAUDE_HOME = Path.home() / ".claude"
_STAGED_FILES = ["CLAUDE.md", "settings.json", ".credentials.json"]
_ALLOWED_TOOLS = "Read,Grep,Glob,Edit,Write"
_MAX_TURNS = 8
_TIMEOUT_S = 300


@dataclass
class ReplayResult:
    final_text: str
    diff: str          # what the agent changed in the worktree
    error: str = ""
    tokens: int = 0    # what the replay spent, as `claude -p` reports it
    cost_usd: float | None = None


def stage_config(claude_md: Path | None, staging: Path) -> None:
    """Copy the real config into staging, swapping CLAUDE.md for the variant."""
    staging.mkdir(parents=True, exist_ok=True)
    for name in _STAGED_FILES:
        src = _CLAUDE_HOME / name
        if src.exists():
            shutil.copy(src, staging / name)
    # Login/onboarding state — without it the replayed agent is logged out.
    state = Path.home() / ".claude.json"
    if state.exists():
        shutil.copy(state, staging / ".claude.json")
    # macOS keeps the OAuth token in the keychain, which a custom config dir
    # can't see — export it as file credentials (staging dir is a private
    # tempdir, deleted after the run).
    cred = staging / ".credentials.json"
    if not cred.exists():
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=False,
        )
        if r.returncode == 0 and r.stdout.strip():
            cred.write_text(r.stdout)
            cred.chmod(0o600)
    if claude_md is not None:
        shutil.copy(claude_md, staging / "CLAUDE.md")


def commit_near(project: Path, date: str) -> str | None:
    """SHA of the last commit on or before the case date, if project is a repo."""
    r = subprocess.run(
        ["git", "-C", str(project), "rev-list", "-1", f"--before={date}T23:59:59", "HEAD"],
        capture_output=True, text=True, check=False,
    )
    sha = r.stdout.strip()
    return sha or None


def build_prompt(case: Case) -> str:
    """Context up to the last user instruction — trailing agent turns dropped so
    the fresh agent acts at the point where the original one got corrected."""
    turns = list(case.context)
    while turns and turns[-1].role == "assistant":
        turns.pop()
    if not turns:
        return case.correction
    *earlier, last = turns
    parts = []
    if earlier:
        transcript = "\n".join(f"{t.role}: {t.content}" for t in earlier)
        parts.append(f"<earlier-conversation>\n{transcript}\n</earlier-conversation>")
    parts.append(last.content)
    return "\n\n".join(parts)


def _write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = (root / rel).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError(f"{rel} is outside {root}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _tokens(usage: dict) -> int:
    return sum(usage.get(k) or 0 for k in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def run_replay(case: Case, claude_md: Path | None = None, *, config_files: dict[str, str] | None = None,
               project_files: dict[str, str] | None = None) -> ReplayResult:
    project = Path(case.project_path)
    if not (project / ".git").exists():
        return ReplayResult("", "", error=f"{project} is not a git repo")
    sha = commit_near(project, case.date)
    if sha is None:
        return ReplayResult("", "", error=f"no commit on/before {case.date} in {project}")

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "config"
        stage_config(claude_md, staging)
        _write_files(staging, config_files or {})
        worktree = Path(tmp) / "repo"
        subprocess.run(
            ["git", "-C", str(project), "worktree", "add", "--detach", str(worktree), sha],
            capture_output=True, text=True, check=True,
        )
        try:
            if project_files:
                _write_files(worktree, project_files)
                # Staged, so the `git diff` below shows only the agent's edits.
                subprocess.run(["git", "-C", str(worktree), "add", "-A"], capture_output=True, check=True)
            r = subprocess.run(
                ["claude", "-p", build_prompt(case),
                 "--output-format", "json",
                 "--max-turns", str(_MAX_TURNS),
                 "--permission-mode", "acceptEdits",
                 "--allowedTools", _ALLOWED_TOOLS],
                cwd=worktree, capture_output=True, text=True, timeout=_TIMEOUT_S,
                env={**__import__("os").environ, "CLAUDE_CONFIG_DIR": str(staging)},
                check=False,
            )
            try:
                data = json.loads(r.stdout)
            except json.JSONDecodeError:
                return ReplayResult("", "", error=f"claude -p failed: {r.stderr[:500]}")
            if isinstance(data, list):  # message stream; the result object is last
                data = next(
                    (d for d in reversed(data)
                     if isinstance(d, dict) and d.get("type") == "result"), {},
                )
            final_text = data.get("result", "")
            diff = subprocess.run(
                ["git", "-C", str(worktree), "diff"],
                capture_output=True, text=True, check=False,
            ).stdout
            return ReplayResult(final_text=final_text, diff=diff, tokens=_tokens(data.get("usage") or {}),
                                cost_usd=data.get("total_cost_usd"))
        except subprocess.TimeoutExpired:
            return ReplayResult("", "", error=f"replay timed out after {_TIMEOUT_S}s")
        finally:
            subprocess.run(
                ["git", "-C", str(project), "worktree", "remove", "--force", str(worktree)],
                capture_output=True, text=True, check=False,
            )
