"""
Collect real subscription usage for Claude Code and Codex.

  Claude — OAuth token from the macOS Keychain → api.anthropic.com/api/oauth/usage
           (server-side utilization of the 5-hour and 7-day windows)
  Codex  — last server-reported rate_limits snapshot in ~/.codex/sessions
           (fresh as of the last Codex activity)

Only percentages and reset times are written; tokens never leave this machine.

Usage: python3 scripts/collect_usage.py [output.json]
       (default: ~/.wisen/exports/reflections/usage.json)
"""
import json
import re
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

_CLAUDE_SVC = "Claude Code-credentials"


def _claude_access_token() -> str | None:
    """Find the Claude Code OAuth access token in the macOS Keychain.

    Multiple keychain entries can share the "Claude Code-credentials" service
    name (e.g. a per-user account plus a generic "unknown" one holding only
    MCP OAuth state). ``find-generic-password`` without an account returns an
    arbitrary entry, which may be the one without the token. Try the default
    lookup first, then enumerate every account for the service until one
    yields a ``claudeAiOauth.accessToken``.
    """

    def extract(raw: str) -> str | None:
        try:
            return json.loads(raw).get("claudeAiOauth", {}).get("accessToken")
        except (json.JSONDecodeError, AttributeError):
            return None

    raw = subprocess.run(
        ["security", "find-generic-password", "-s", _CLAUDE_SVC, "-w"],
        capture_output=True, text=True, timeout=10, check=False,
    ).stdout
    token = extract(raw)
    if token:
        return token

    # Enumerate accounts for the service via dump-keychain and try each.
    dump = subprocess.run(
        ["security", "dump-keychain"],
        capture_output=True, text=True, timeout=15, check=False,
    ).stdout
    accounts: list[str] = []
    for entry in dump.split("keychain:"):
        if _CLAUDE_SVC not in entry:
            continue
        m = re.search(r'"acct"<blob>="([^"]*)"', entry)
        if m:
            accounts.append(m.group(1))
    for acct in accounts:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", _CLAUDE_SVC, "-a", acct, "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
        token = extract(raw)
        if token:
            return token
    return None


def claude_usage() -> dict | None:
    try:
        token = _claude_access_token()
        if not token:
            return None
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return {
            "five_hour_pct": data["five_hour"]["utilization"],
            "five_hour_resets_at": data["five_hour"]["resets_at"],
            "seven_day_pct": data["seven_day"]["utilization"],
            "seven_day_resets_at": data["seven_day"]["resets_at"],
        }
    except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError, urllib.error.URLError):
        return None


def codex_usage() -> dict | None:
    sessions = sorted(
        Path.home().glob(".codex/sessions/*/*/*/*.jsonl"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for path in sessions[:5]:
        last = None
        try:
            with open(path, errors="replace") as f:
                for line in f:
                    if '"rate_limits"' not in line:
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    rl = (e.get("payload") or {}).get("rate_limits")
                    if rl:
                        last = (e.get("timestamp", ""), rl)
        except (OSError, json.JSONDecodeError):
            continue
        if last:
            ts, rl = last
            iso = lambda epoch: datetime.fromtimestamp(epoch, UTC).isoformat() if epoch else None
            # Codex reports `primary` and `secondary` rate-limit windows, but
            # which is the 5-hour vs weekly limit depends on the plan. Identify
            # each window by its `window_minutes` (300 = 5h, 10080 = 7d) rather
            # than assuming primary is always the short window.
            out: dict = {"plan": rl.get("plan_type"), "as_of": ts}
            for slot in ("primary", "secondary"):
                win = rl.get(slot)
                if not win:
                    continue
                mins = win.get("window_minutes")
                if mins is not None and mins <= 360:
                    out["five_hour_pct"] = win.get("used_percent")
                    out["five_hour_resets_at"] = iso(win.get("resets_at"))
                else:
                    out["weekly_pct"] = win.get("used_percent")
                    out["weekly_resets_at"] = iso(win.get("resets_at"))
            return out
    return None


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "~/.wisen/exports/reflections/usage.json").expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    usage = {
        "collected_at": datetime.now(UTC).isoformat(),
        "claude": claude_usage(),
        "codex": codex_usage(),
    }
    out.write_text(json.dumps(usage, indent=2))
    print(f"usage written to {out}")


if __name__ == "__main__":
    main()
