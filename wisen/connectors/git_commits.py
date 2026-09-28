"""
Fetches git commits from GitHub via the Events API + compare API.

Strategy:
  1. GET /users/{username}/events → PushEvents (includes head/before SHAs)
  2. For each PushEvent → GET /repos/{repo}/compare/{before}...{head}
     to get the actual commit list (works for private repos)
  3. Filter commits by author email, group by (repo, local-date)

Requires env vars:
  GITHUB_TOKEN    — personal access token (repo scope for private repos)
  GITHUB_USERNAME — optional, auto-detected from token if not set
  GITHUB_EMAILS   — comma-separated emails to match
                    e.g.  you@work.com,you@personal.com

Example API payloads:

  # /users/{me}/events → PushEvent (head/before SHAs, no commit messages yet)
  {"type": "PushEvent", "repo": {"name": "Vela3202019/wisen"},
   "created_at": "2026-07-21T22:14:05Z",
   "payload": {"before": "39a5dbd…", "head": "9ba7535…", "size": 1}}

  # /repos/{repo}/compare/{before}...{head} → the actual commits
  {"commits": [{"sha": "9ba7535…",
    "commit": {"author": {"email": "velapod@gmail.com",
                          "date": "2026-07-21T22:13:58Z"},
               "message": "feat: package wisen as installable CLI"}}]}

  # → one NormalizedSession per (repo, local-date); each commit becomes a
  #   turn pair (message → user Turn, changed-files summary → assistant Turn)
  #   — commits are the "what actually shipped" signal.
"""
import json
import os
import ssl
import urllib.error
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.request import Request, urlopen

import certifi
from dotenv import load_dotenv

load_dotenv()

from wisen.ingestion.normalizer import (
    NormalizedSession,
    Turn,
    compute_metadata,
    truncate_content,
)

_API = "https://api.github.com"
_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _get(url: str, token: str) -> dict | list | None:
    try:
        req = Request(url, headers=_headers(token))
        with urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            return json.loads(resp.read().decode())
    except (OSError, json.JSONDecodeError, urllib.error.URLError):
        return None


def _get_username(token: str) -> str | None:
    data = _get(f"{_API}/user", token)
    return data.get("login") if isinstance(data, dict) else None


def _parse_ts(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return datetime.now(UTC)


def _fetch_push_events(username: str, token: str) -> list[dict]:
    events = []
    for page in range(1, 11):
        data = _get(f"{_API}/users/{username}/events?per_page=100&page={page}", token)
        if not isinstance(data, list) or not data:
            break
        events.extend(e for e in data if e.get("type") == "PushEvent")
        if len(data) < 100:
            break
    return events


def _get_push_commits(repo_full_name: str, before: str, head: str, token: str) -> list[dict]:
    """Use compare API to get commits in a push (works for private repos)."""
    # Zero SHA means this is a new branch — get commits on head instead
    if before == "0000000000000000000000000000000000000000":
        data = _get(f"{_API}/repos/{repo_full_name}/commits/{head}", token)
        return [data] if isinstance(data, dict) and "commit" in data else []

    data = _get(f"{_API}/repos/{repo_full_name}/compare/{before}...{head}", token)
    if not isinstance(data, dict):
        return []
    return data.get("commits", [])


def _get_files(repo_full_name: str, sha: str, token: str) -> list[str]:
    data = _get(f"{_API}/repos/{repo_full_name}/commits/{sha}", token)
    if not isinstance(data, dict):
        return []
    return [f["filename"] for f in data.get("files", [])]


async def ingest(date: str | None = None) -> AsyncIterator[NormalizedSession]:
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return

    username = os.getenv("GITHUB_USERNAME") or _get_username(token)
    if not username:
        return

    extra_emails = [e.strip().lower() for e in os.getenv("GITHUB_EMAILS", "").split(",") if e.strip()]
    author_emails = set(extra_emails)

    # Collect all relevant commits: {(repo, date): [commit_dict]}
    groups: dict[tuple[str, str], list[dict]] = {}
    seen_shas: set[str] = set()

    for event in _fetch_push_events(username, token):
        payload = event.get("payload", {})
        repo_full_name = event.get("repo", {}).get("name", "")
        before = payload.get("before", "")
        head = payload.get("head", "")

        if not repo_full_name or not head:
            continue

        commits = _get_push_commits(repo_full_name, before, head, token)
        for c in commits:
            sha = c.get("sha", "")
            if not sha or sha in seen_shas:
                continue
            seen_shas.add(sha)

            commit_data = c.get("commit", {})
            email = commit_data.get("author", {}).get("email", "").lower()
            if author_emails and email not in author_emails:
                continue

            message = commit_data.get("message", "").split("\n")[0].strip()
            if not message:
                continue

            # Use local commit timestamp so date grouping matches git log
            ts_raw = commit_data.get("author", {}).get("date", "")
            ts = _parse_ts(ts_raw)
            local_date = ts.date().isoformat()

            if date and local_date != date:
                continue

            groups.setdefault((repo_full_name, local_date), []).append({
                "sha": sha,
                "message": message,
                "timestamp": ts,
                "repo": repo_full_name,
            })

    for (repo_full_name, local_date), commits in groups.items():
        turns: list[Turn] = []
        for c in commits:
            files = _get_files(repo_full_name, c["sha"], token)
            files_summary = (
                f"Changed {len(files)} file(s): {', '.join(files[:8])}"
                + (f" (+{len(files) - 8} more)" if len(files) > 8 else "")
            ) if files else "No files recorded"

            ts = c["timestamp"]
            turns.append(Turn(role="user", content=truncate_content(c["message"]), timestamp=ts))
            turns.append(Turn(role="assistant", content=files_summary, timestamp=ts))

        if not turns:
            continue

        turns.sort(key=lambda t: t.timestamp)
        repo_short = repo_full_name.split("/")[-1]

        yield NormalizedSession(
            session_id=f"git:{repo_full_name}:{local_date}",
            source="git",
            project_path=f"https://github.com/{repo_full_name}",
            project_name=f"{repo_short} (git)",
            date=local_date,
            started_at=turns[0].timestamp,
            ended_at=turns[-1].timestamp,
            turns=turns,
            metadata=compute_metadata(turns),
            raw_source_path=f"https://github.com/{repo_full_name}",
        )
