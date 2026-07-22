# Eval Loop: measuring whether wisen recommendations actually improve the harness

**Goal.** Close the loop wisen currently leaves open: it observes friction and
recommends config changes, but never measures whether an applied change reduced friction.
This plan adds ground truth (mined correction cases), a counterfactual replay eval, and a
gate so config changes are accepted only when they demonstrably help.

**North star metric.** Pass rate on the personal case bank, per config version — plus
friction half-life (days until a friction type recurs after its fix is applied).

---

## Phase 1 — Case miner (ground truth)

Corrections in real transcripts are labeled failures with full context. Mine them.

### New module: `evals/`

```
evals/
  miner.py        # detect correction moments in raw claude-code JSONL transcripts
  cases.py        # Case model + case-bank load/save (evals/cases/*.json, git-tracked)
  metrics.py      # Tier-1 deterministic outcome metrics per day
  cli.py          # python -m evals mine|list|review|metrics
  cases/          # the case bank (one JSON per case)
```

### Deterministic detectors (code, not LLM) — `miner.py`

Scan raw claude-code transcripts (`raw_source_path` from ingested sessions; fall back to
globbing `~/.claude/projects/*/*.jsonl`):

1. **Tool rejection** — tool_result containing the harness marker
   `"The user doesn't want to proceed with this tool use"`.
2. **Interruption** — user message containing `[Request interrupted by user`.
3. **Explicit correction** — user message starting with a correction marker:
   `no`, `stop`, `wait`, `don't`, `revert`, `undo`, `that's not`, `not what i`, `wrong`.
4. **Rephrase storm** — reuse the existing `user_rephrases` signal: ≥2 user messages
   within 60s windows (from `ingestion/normalizer.py` logic, applied at the moment level).

### Case model — `cases.py`

```python
class Case(BaseModel):
    id: str                      # "<date>-<session_id[:8]>-<turn_index>"
    date: str
    session_id: str
    source_path: str             # raw transcript
    project_path: str
    detector: Literal["tool-rejection", "interruption", "explicit-correction", "rephrase-storm"]
    context: list[dict]          # conversation prefix up to the failure (role/content, truncated)
    agent_action: str            # what the agent did/was about to do
    correction: str              # the user's actual words (verbatim ground truth)
    behavior_check: str          # yes/no question for the judge, e.g. "Did the agent add a fallback default?"
    split: Literal["eval", "holdout"]   # assigned by date hash — holdout never used for accepting changes
    status: Literal["mined", "confirmed", "rejected"]  # user review state
```

`agent_action` and `behavior_check` are filled by a single cheap LLM pass over each
detected moment (the only LLM step in mining). Everything else is mechanical.

### Tier-1 metrics — `metrics.py`

Per day, computed deterministically from transcripts, written to Mongo (`eval_metrics`
collection) and exported alongside reflections:

- corrections per 100 user turns (by detector type)
- tool-rejection rate
- friction half-life per friction type (from reflection `friction_deltas` history)
- correlation of reflection `alignment_score` vs measured correction rate
  (this evals the synthesizer itself)

### CLI

- `python -m evals mine [--since DATE]` — scan archive, append new cases (idempotent by case id)
- `python -m evals list / review <id> --confirm|--reject` — human curation
- `python -m evals metrics [--since DATE]` — compute/print Tier-1 metrics

**Verify:** unit tests with synthetic JSONL fixtures per detector; run miner on the real
archive and confirm it finds the known corrections from recent sessions (e.g. the July 5
dashboard tool-use rejection).

---

## Phase 2 — Counterfactual replay (the eval)

For each confirmed case, replay the moment headlessly under two configs and ask: did the
corrected behavior recur?

```
evals/
  replay.py       # run one case: sandbox + claude -p + capture output
  judge.py        # yes/no judgment against behavior_check, k runs, majority vote
  runner.py       # run suite across case bank for config A vs B, report
```

- **Sandbox:** temp git worktree of the project at (or nearest to) the case's date;
  `CLAUDE_CONFIG_DIR` pointed at a staged config dir containing the CLAUDE.md variant
  under test, so the real `~/.claude` is never touched.
- **Replay:** `claude -p` with the case's context prefix as the prompt, capped turns,
  `--permission-mode` deny-by-default (we only need the transcript, not applied edits).
- **Judge:** LLM asked only the case's `behavior_check` (yes/no + quote as evidence);
  k=3 runs per case per config, majority vote. Judge model ≠ synthesis model version
  where possible.
- **Clean set:** sample N smooth moments (sessions with zero detections) as regression
  cases with inverted checks — a config change must not break these.
- **Report:** per-config pass rate on eval split, clean-set pass rate, per-case flips,
  cost/token overhead of the config (CLAUDE.md token count is tracked as a cost metric).

**Verify:** smoke-run one confirmed case end-to-end under current config; assert the
runner produces a diffable report for two identical configs (pass rates equal).

---

## Phase 3 — Gated loop + dashboard

1. Put `~/.claude/CLAUDE.md` under git (separate repo or symlink into dotfiles).
2. Nightly (extends existing launchd synthesis job): recommendations targeting
   `claude-md` → applied on a branch → Phase-2 suite runs → **accept** iff eval-split
   pass rate improves and clean-set rate does not drop; otherwise revert and record the
   rejection in the next reflection's context (`yesterday's rec was tested and rejected: …`).
3. Dashboard additions: Tier-1 trend sparkline, case review queue (👍/👎 doubles as
   judge calibration), config version history with pass rates.
4. Holdout split is scored monthly only — never used in accept/reject decisions.

**Verify:** one full dry-run cycle on a real recommendation with `--no-apply`, reviewed
manually before enabling the gate.

---

## Non-goals (for now)

- Optimizing CLAUDE.md automatically (DSPy/GEPA search) — the eval is the fitness
  function; optimization is a later layer.
- Sources other than claude-code (cursor/codex transcripts lack the rejection markers).
- Cross-user generalization — this is deliberately a personal benchmark.

## Order of work

Phase 1 first; it's useful standalone (metrics + case bank) and everything stacks on it.
Branch: `feature/eval-case-miner` off `main`.
