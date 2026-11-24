# Evaluate a Gardener rule

**The controlled replay is the promotion evidence.** The two-week field window is observational, including when it suggests a rollback. The local pre-approval replay on a draft is advisory: it does not have the frozen artifacts required for promotion. A gate verdict does not merge a Git branch or bypass human approval.

## Freeze and run

Mine cases with `wisen eval mine`, then `wisen eval review CASE_ID --confirm` only after checking the conversation prefix, expected behavior and rubric. Reserve unseen cases before tuning rules. Start with ~40 cases across targeted failures, clean non-regressions, edge cases (boundary or too-broad rules) and adversarial cases (e.g. conflicting instructions in a benign task). Keep holdout cases out of rule design, gate decisions and judge tuning. Case banks, manifests, label databases and full reports can contain private transcript text and diffs; do not commit or share them.

After approving and applying a proposal to its **branch**, write a local inventory JSON list with one entry per case:

```json
[
  {"case_id": "CASE_ID", "base_commit": "FULL_PRE_TASK_GIT_SHA", "cohort": "targeted", "rule_type": "test workflow"}
]
```

Valid cohorts are `targeted`, `clean`, `edge`, `adversarial`, and `holdout`. Use the commit from **before** the task, not the original solution commit. For shared home instructions, pass `--config` with a JSON filename-to-text mapping (e.g. `{"CLAUDE.md":"..."}`); do not include settings or credentials. Use explicit model IDs rather than rolling aliases.

```sh
wisen eval freeze PROPOSAL_ID inventory.json --model claude-sonnet-4-6 --out manifest.json
wisen eval paired manifest.json --record-gate --out replay.json
wisen eval field PROPOSAL_ID
```

Freeze reads the old and new proposal blobs from local Git, records the output of `claude --version` and hashes the judge prompt and entire manifest. Replay stages those files in a clean config directory and detached worktree. It uses local Claude authorization when needed (not included in artifacts) and restricts tools, hooks and MCP. It does not reproduce uncommitted or external dependencies of the original session; use runnable cases in local clones. Each case runs five fresh agents per arm (override with `--repeats`); the judge votes separately three times. Use `--holdout` instead of `--record-gate` to run the reserved set.

The gate requires a case-bootstrap 95% CI with a targeted lower bound above zero and a clean lower bound at or above −5 percentage points (set beforehand with `--clean-margin`). It is inconclusive with no scored pairs in either primary cohort. Error or abstention in either primary cohort blocks promotion; both arm outcomes and matched scored counts remain visible. Sign tests compare the direction of the per-case differences; edge and adversarial results are diagnostic, not silently pooled with primary cohorts. Report sample sizes and inspect representative failures before making a decision.

## Calibrate and analyze

```sh
wisen eval sample-replays manifest.json replay.json replay-v1 --n 150
wisen eval label-queue replay replay-v1
wisen eval label ITEM_ID pass                         # or fail / abstain
wisen eval rejudge replay-v1 --model JUDGE_MODEL
wisen eval calibrate judge replay-v1 --version VERSION_FROM_REJUDGE
wisen eval sample-turns detector-v1 2026-07-01 2026-07-02 --n 300
wisen eval label-queue turn detector-v1
wisen eval label ITEM_ID explicit-correction
wisen eval calibrate miner detector-v1 --version MINER_VERSION_FROM_SAMPLE
wisen eval calibrate observer detector-v1 --version OBSERVER_VERSION_FROM_SAMPLE
wisen eval failures replay-v1 --report replay.json
```

For replay outputs, label only whether the behavior check failed, succeeded, or lacked evidence. The human sees the check and output without arm or predicted label. For real turns use `none`, `tool-rejection`, `interruption`, `explicit-correction`, or `rephrase-storm`; do not count ordinary follow-ups as corrections. Turn sampling includes all heuristic hits on the chosen days and fills the quota with negatives stratified by connector. Miner scoring outside Claude uses normalized text; some connectors lack tool or interruption markers. Use this enriched sample to diagnose error types, not to estimate natural correction prevalence. Blindness does not hide clues present in the text itself.

Add `--failure` during labeling to record a known cause (agent/model, rule wording, judge error, replay environment). Reports separate agreement (κ), class precision/recall and confusion from the per-slice sample sizes and cluster-bootstrap intervals. Repeat ~20% a week later with `label-queue ... --round 2`, label with `--round 2`, and run `calibrate retest ... --kind turn` (or `replay`). Use a new dataset name if changing the sampled corpus.

## Monitor retention and field exposure

On the laptop, schedule a weekly command with a unique output path. Add every earlier merged rule manifest via repeated `--retain` flags so cumulative replay includes their file changes; list the later manifest as the primary input. The report flags statistically supported regressions of prior gains and reports holdout transfer separately.

```sh
wisen eval weekly manifest.json --out "weekly-$(date +%F).json" \
  --retain earlier.json --dataset replay-v1 --baseline-version PREVIOUS_JUDGE_VERSION
```

The Claude plugin records the exact recognized rule-file blob at session startup for proposals already applied. Unobserved sessions and ambiguous blobs are not imputed. The field summary compares day −7…−1 to +7…+13 using GitHub's merge time where available and local Git evidence or detection time otherwise. It reports a difference-in-differences across enrolled project/branch cohorts that consistently did or did not receive the rule, using only matching detector-version records; a missing cell produces no estimate. Exposure is a file-at-startup proxy (not proof that the model followed the rule). Selection, task mix, model changes and lack of parallel trends can still confound the result. The field estimate never substitutes for the replay gate.
