#!/bin/sh
# SessionStart hook: one line of context about pending Gardener proposals.
# Prints nothing when wisen is not installed or has nothing to say, so a
# missing install never adds noise to a session.
command -v wisen >/dev/null 2>&1 || exit 0
out=$(wisen gardener list --json 2>/dev/null) || exit 0
python3 - "$out" <<'PY' 2>/dev/null || exit 0
import json, sys
try:
    rows = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
proposed = [r for r in rows if r.get("state") == "proposed"]
applied = [r for r in rows if r.get("state") == "applied" and r.get("branch")]
parts = []
if proposed:
    targets = ", ".join(sorted({r["target"] for r in proposed}))
    parts.append(f"{len(proposed)} Gardener proposal{'s' if len(proposed) != 1 else ''} waiting ({targets})")
if applied:
    parts.append(f"{len(applied)} approved branch{'es' if len(applied) != 1 else ''} not merged yet")
if parts:
    print("wisen: " + "; ".join(parts) + ". Run /wisen:proposals to review.")
PY
