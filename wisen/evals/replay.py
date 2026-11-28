"""Headless replay in a detached worktree and a fresh Claude config directory,
or, for a `local:` model, one chat completion from an OpenAI-compatible server
(such as `mlx_lm.server`) with the instruction files as the system message."""
import json
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from wisen import config
from wisen.evals.cases import Case
from wisen.gardener.git import GitError, Repo

_CLAUDE_HOME = Path.home() / ".claude"
_ALLOWED_TOOLS = "Read,Grep,Glob,Edit,Write"
_MAX_TURNS = 8
_TIMEOUT_S = 300
LOCAL_PREFIX = "local:"
_LOCAL_BASE_URL = "http://localhost:8080/v1"
_LOCAL_MAX_TOKENS = 1024
# Above zero so repeats sample different replies instead of one greedy answer.
_LOCAL_TEMPERATURE = 0.7


@dataclass
class ReplayResult:
    final_text: str
    diff: str          # what the agent changed in the worktree
    error: str = ""
    tokens: int = 0    # what the replay spent, as `claude -p` reports it
    cost_usd: float | None = None


def stage_config(claude_md: Path | None, staging: Path) -> None:
    """Stage explicit instructions and authentication, never ambient settings."""
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    cred = staging / ".credentials.json"
    src = _CLAUDE_HOME / cred.name
    if src.is_file():
        shutil.copyfile(src, cred)
        cred.chmod(0o600)
    # macOS keeps the OAuth token in the keychain, which a custom config dir
    # can't see — export it as file credentials (staging dir is a private
    # tempdir, deleted after the run).
    if not cred.exists() and shutil.which("security"):
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
    return Repo(project).out("rev-list", "-1", f"--before={date}T23:59:59", "HEAD", check=False) or None


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


def _write_files(root: Path, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        path = root / rel
        parts = Path(rel).parts
        if (not parts or Path(rel).is_absolute() or ".." in parts or ".git" in parts
                or any((root.joinpath(*parts[:i])).is_symlink() for i in range(1, len(parts) + 1))
                or not path.resolve().is_relative_to(root.resolve())):
            raise ValueError(f"{rel} is outside {root}")
        if text is None:
            path.unlink(missing_ok=True)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def _tokens(usage: dict) -> int:
    return sum(usage.get(k) or 0 for k in (
        "input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def _git(project: Path, *args: str) -> str:
    return Repo(project).out(*args)


def is_local(model: str | None) -> bool:
    return bool(model) and model.startswith(LOCAL_PREFIX)


def local_base_url() -> str:
    return config.get("model", "local_base_url", "WISEN_LOCAL_BASE_URL", _LOCAL_BASE_URL).rstrip("/")


def harness_version(model: str | None = None) -> str:
    if is_local(model):
        return f"single-turn chat completion at {local_base_url()}"
    try:
        result = subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True, timeout=10)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError("could not determine Claude Code version") from None
    return result.stdout.strip()


def run_replay(case: Case, claude_md: Path | None = None, *, config_files: dict[str, str | None] | None = None,
               project_files: dict[str, str | None] | None = None, base_commit: str | None = None,
               model: str | None = None, harness: str | None = None) -> ReplayResult:
    try:
        if is_local(model):
            return _run_local(case, claude_md, config_files or {}, project_files or {}, base_commit, model, harness)
        return _run_replay(case, claude_md, config_files, project_files, base_commit, model, harness)
    except (GitError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        return ReplayResult("", "", error=f"replay setup failed: {type(exc).__name__}: {exc}")
    except subprocess.TimeoutExpired:
        return ReplayResult("", "", error="replay setup timed out")


def instructions(claude_md: Path | None, config_files: dict[str, str | None],
                 project_files: dict[str, str | None]) -> str:
    """The instruction files a local model gets in place of a staged Claude
    config: home CLAUDE.md, rules and skills, then the project's CLAUDE.md
    and AGENTS.md files and its .claude rules and skills."""
    files = {"CLAUDE.md": claude_md.read_text()} if claude_md is not None else {}
    files.update({rel: text for rel, text in config_files.items() if text is not None})
    files.update({f"project/{rel}": text for rel, text in project_files.items()
                  if text is not None and _is_project_instruction(rel)})
    return "\n\n".join(f"<instructions file=\"{rel}\">\n{text.strip()}\n</instructions>"
                       for rel, text in sorted(files.items()) if text.strip())


def _post_chat(url: str, payload: dict) -> dict:
    response = httpx.post(url, json=payload, timeout=_TIMEOUT_S)
    response.raise_for_status()
    return response.json()


def _is_project_instruction(rel: str) -> bool:
    return Path(rel).name in ("CLAUDE.md", "AGENTS.md") or (Path(rel).parts[:1] == (".claude",) and rel.endswith(".md"))


def _committed_instructions(case: Case, base_commit: str | None) -> dict[str, str | None]:
    """The project's instruction files at the replay commit (CLAUDE.md and
    AGENTS.md files, .claude rules and skills): what a Claude replay's worktree
    would hold before the variant's files. A symlink reads as its target."""
    project = Path(case.project_path)
    if not (project / ".git").exists():
        return {}
    repo = Repo(project)
    sha = base_commit or commit_near(project, case.date)
    if sha is None:
        return {}
    files = {}
    for line in repo.out("ls-tree", "-r", "-z", sha, check=False).split("\0"):
        meta, _, rel = line.partition("\t")
        if not rel or not _is_project_instruction(rel):
            continue
        mode, _, blob = meta.split(" ")
        if mode == "120000":
            target = posixpath.normpath(posixpath.join(posixpath.dirname(rel), repo.read_blob(blob)))
            blob = None if target.startswith("..") else repo.blob(sha, target)
        if blob:
            files[rel] = repo.read_blob(blob)
    return files


def _run_local(case, claude_md, config_files, project_files, base_commit, model, harness) -> ReplayResult:
    if harness is not None and harness_version(model) != harness:
        return ReplayResult("", "", error="local endpoint does not match the frozen manifest")
    project = {**_committed_instructions(case, base_commit), **project_files}
    system = instructions(claude_md, config_files, project)
    messages = [*([{"role": "system", "content": system}] if system else []),
                {"role": "user", "content": build_prompt(case)}]
    payload = {"model": model.removeprefix(LOCAL_PREFIX), "messages": messages,
               "max_tokens": _LOCAL_MAX_TOKENS, "temperature": _LOCAL_TEMPERATURE}
    url = f"{local_base_url()}/chat/completions"
    try:
        data = _post_chat(url, payload)
    except httpx.TimeoutException:
        return ReplayResult("", "", error=f"local model timed out after {_TIMEOUT_S}s")
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return ReplayResult("", "", error=f"local model request to {url} failed: {type(exc).__name__}: {exc}")
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ReplayResult("", "", error="local model returned no message")
    if not isinstance(text, str) or not text.strip():
        return ReplayResult("", "", error="local model returned an empty message")
    usage = data.get("usage") or {}
    return ReplayResult(final_text=text, diff="",
                        tokens=usage.get("total_tokens") or _tokens(usage), cost_usd=None)


def _run_replay(case, claude_md, config_files, project_files, base_commit, model, harness) -> ReplayResult:
    project = Path(case.project_path)
    if not (project / ".git").exists():
        return ReplayResult("", "", error=f"{project} is not a git repo")
    if base_commit is not None and not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", base_commit):
        return ReplayResult("", "", error="replay requires a full base commit ID")
    if harness is not None and harness_version() != harness:
        return ReplayResult("", "", error="Claude Code version does not match the frozen manifest")
    sha = base_commit or commit_near(project, case.date)
    if sha is None:
        return ReplayResult("", "", error=f"no commit on/before {case.date} in {project}")

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "config"
        stage_config(claude_md, staging)
        _write_files(staging, config_files or {})
        worktree = Path(tmp) / "repo"
        repo = Repo(project)
        repo.run("worktree", "add", "--detach", str(worktree), sha)
        try:
            if project_files:
                _write_files(worktree, project_files)
                _git(worktree, "add", "-A")
                for path, content in project_files.items():
                    if content is not None:
                        _git(worktree, "add", "-f", "--", path)
            baseline = _git(worktree, "write-tree")
            r = subprocess.run(
                ["claude", "-p", build_prompt(case),
                 "--output-format", "json",
                 "--max-turns", str(_MAX_TURNS),
                 "--permission-mode", "acceptEdits",
                 "--tools", _ALLOWED_TOOLS,
                 "--allowedTools", _ALLOWED_TOOLS,
                 "--setting-sources", "user,project",
                 "--settings", '{"disableAllHooks":true}',
                 "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                 "--no-session-persistence",
                 *(["--model", model] if model else [])],
                cwd=worktree, capture_output=True, text=True, timeout=_TIMEOUT_S,
                env={**os.environ, "CLAUDE_CONFIG_DIR": str(staging)},
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
            if not isinstance(data, dict) or not data:
                return ReplayResult("", "", error="claude -p did not return a result")
            if r.returncode or data.get("is_error"):
                return ReplayResult("", "", error=f"claude -p failed: {str(data.get('result'))[:500]}")
            final_text = data.get("result", "")
            _git(worktree, "add", "-A")
            diff = _git(worktree, "diff", "--cached", "--no-ext-diff", baseline)
            return ReplayResult(final_text=final_text, diff=diff, tokens=_tokens(data.get("usage") or {}),
                                cost_usd=data.get("total_cost_usd"))
        except subprocess.TimeoutExpired:
            return ReplayResult("", "", error=f"replay timed out after {_TIMEOUT_S}s")
        finally:
            repo.run("worktree", "remove", "--force", str(worktree), check=False)
