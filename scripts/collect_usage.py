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
import subprocess
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path


def claude_usage() -> dict | None:
    try:
        raw = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        ).stdout
        token = json.loads(raw)["claudeAiOauth"]["accessToken"]
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
            return {
                "five_hour_pct": (rl.get("primary") or {}).get("used_percent"),
                "five_hour_resets_at": iso((rl.get("primary") or {}).get("resets_at")),
                "weekly_pct": (rl.get("secondary") or {}).get("used_percent"),
                "weekly_resets_at": iso((rl.get("secondary") or {}).get("resets_at")),
                "plan": rl.get("plan_type"),
                "as_of": ts,
            }
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
