"""
`wisen eval report`: one Markdown scorecard from a `paired`, `weekly` or `demo`
report. By default it holds only aggregates and case IDs, so it can be shared;
`--evidence` adds judge quotes, which can contain private transcript text.
"""
from collections import Counter

_COHORTS = {"eval": "Targeted (gate)", "clean": "Clean (gate)", "holdout": "Holdout (transfer)",
            "edge": "Edge (diagnostic)", "adversarial": "Adversarial (diagnostic)"}
_DECISIONS = {True: "Promote", False: "Hold", None: "Insufficient evidence"}


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:.0f}%"


def _pp(value: float | None) -> str:
    return "–" if value is None else f"{value * 100:+.1f} pp"


def _ci(ci: list[float] | None) -> str:
    return "–" if not ci else f"[{ci[0] * 100:+.1f}, {ci[1] * 100:+.1f}]"


def _rate(counts: dict) -> float | None:
    total = sum(counts.values())
    return counts["pass"] / total if total else None


def gate_checks(report: dict) -> list[tuple[str, str, str, bool | None, str]]:
    """(check, required, observed, met, why it blocks) for each part of the
    promotion rule."""
    margin = report.get("clean_margin", 0.05)
    targeted = report["paired"].get("eval", {})
    clean = report["paired"].get("clean", {})
    rows = []
    for name, cohort, required, ok, miss in (
            ("Targeted gain", targeted, "95% CI lower bound > 0", lambda low: low > 0,
             "a targeted gain is not shown"),
            ("Clean set held", clean, f"95% CI lower bound ≥ {-margin * 100:.0f} pp",
             lambda low: low >= -margin, f"a clean regression beyond {margin * 100:.0f} pp is not ruled out")):
        ci = cohort.get("ci95")
        rows.append((name, required, _ci(ci), ok(ci[0]) if ci else None, miss))
    unscored = sum(counts["error"] + counts["abstain"]
                   for cohort in (targeted, clean) for counts in cohort.get("outcomes", {}).values())
    rows.append(("Every gate replay scored", "0 errors or abstentions", str(unscored),
                 None if not (targeted or clean) else unscored == 0, "some gate replays errored or abstained"))
    return rows


def _cohort_rows(report: dict) -> list[str]:
    lines = ["| Cohort | Cases | Current pass | Proposed pass | Difference | 95% CI | Wins / losses / ties | Sign p | Errors / abstains |",
             "|---|---|---|---|---|---|---|---|---|"]
    for kind, paired in report["paired"].items():
        outcomes = paired["outcomes"]
        unscored = " / ".join(str(sum(outcomes[arm][key] for arm in outcomes)) for key in ("error", "abstain"))
        p = paired.get("sign_p")
        lines.append(f"| {_COHORTS.get(kind, kind)} | {paired['cases']} | {_pct(_rate(outcomes['current']))} "
                     f"| {_pct(_rate(outcomes['proposed']))} | {_pp(paired['difference'])} | {_ci(paired['ci95'])} "
                     f"| {paired['wins']} / {paired['losses']} / {paired['ties']} | {'–' if p is None else f'{p:.3f}'} "
                     f"| {unscored} |")
    return lines


def _case_lines(report: dict, evidence: bool, top: int) -> list[str]:
    scored = [c for c in report.get("case_results", []) if c.get("difference") is not None]
    rule_types = report.get("rule_types", {})
    lines = []
    for title, picked in (("Largest regressions", sorted((c for c in scored if c["difference"] < 0),
                                                          key=lambda c: c["difference"])[:top]),
                          ("Largest gains", sorted((c for c in scored if c["difference"] > 0),
                                                   key=lambda c: -c["difference"])[:top])):
        if not picked:
            continue
        lines += ["", f"### {title}", ""]
        for c in picked:
            rule = f", {rule_types[c['case']]}" if c["case"] in rule_types else ""
            rates = c.get("matched_pass_rates") or c["pass_rates"]
            lines.append(f"- `{c['case']}` ({_COHORTS.get(c['cohort'], c['cohort'])}{rule}): "
                         f"{_pct(rates['current'])} → {_pct(rates['proposed'])}")
            if evidence:
                arm = "proposed" if c["difference"] < 0 else "current"
                quote = next((a[arm].get("evidence") for a in c.get("attempts", [])
                              if a[arm]["outcome"] == "fail" and a[arm].get("evidence")), None)
                if quote:
                    lines.append(f"  - {arm} failure: “{quote.strip()[:300]}”")
    if rule_types and scored:
        by_rule: dict[str, list[float]] = {}
        for c in scored:
            if c["case"] in rule_types and c["cohort"] != "clean":
                by_rule.setdefault(rule_types[c["case"]], []).append(c["difference"])
        if by_rule:
            lines += ["", "### By rule (targeted and holdout)", "", "| Rule | Cases | Mean difference |", "|---|---|---|"]
            lines += [f"| {rule} | {len(d)} | {_pp(sum(d) / len(d))} |" for rule, d in sorted(by_rule.items())]
    return lines


def _retention_lines(items: list[dict]) -> list[str]:
    lines = ["", "## Retention of earlier rules", "",
             "| Earlier proposal | Retention difference | 95% CI | Rollback | Holdout transfer |", "|---|---|---|---|---|"]
    for item in items:
        kept = item["retention"]["paired"].get("eval", {})
        transfer = (item.get("holdout_transfer") or {}).get("paired", {}).get("holdout", {})
        rollback = {True: "recommended", False: "no", None: "inconclusive"}[item.get("rollback_recommended")]
        lines.append(f"| {item['earlier_proposal']} | {_pp(kept.get('difference'))} | {_ci(kept.get('ci95'))} "
                     f"| {rollback} | {_pp(transfer.get('difference')) if transfer else '–'} |")
    return lines


def render(report: dict, *, evidence: bool = False, top: int = 5) -> str:
    decision = _DECISIONS[report.get("meets_rule")]
    lines = [f"# Replay scorecard: {report.get('proposal_id', 'unknown proposal')}", ""]
    if report.get("simulated"):
        lines += [("> **Simulated.** No model was called; the pass rates are made up. "
                  "The statistics and the gate are real."), ""]
    if report.get("holdout"):
        lines += ["> Holdout run: transfer evidence only, never a promotion decision.", ""]
    failed = [miss for *_, met, miss in gate_checks(report) if met is False]
    reason = {"Promote": "targeted gain is above zero and the clean set held.",
              "Hold": "; ".join(failed) + "." if failed else "the gate rule was not met.",
              "Insufficient evidence": "no scored pairs in a gate cohort."}[decision]
    lines += [f"**Verdict: {decision}** ({reason})", ""]
    meta = [("Agent model", report.get("model")), ("Judge model", report.get("judge_model")),
            ("Replays", (f"{report['replays']} ({report['agent_repeats']} per arm per case, "
                        f"{report['judge_votes']} judge votes each)")),
            ("Tokens", f"{report.get('tokens', 0):,}" + (" (lower bound)" if report.get("tokens_unmeasured") else "")),
            ("Cost", "–" if report.get("cost_usd") is None else f"${report['cost_usd']:.2f}"),
            ("Manifest", (report.get("manifest") or "–")[:12]), ("Run", (report.get("run_id") or "–")[:12])]
    lines += [" · ".join(f"{k}: {v}" for k, v in meta if v not in (None, "–")), "", "## Gate", "",
              "| Check | Required | Observed | Met |", "|---|---|---|---|"]
    lines += [f"| {name} | {required} | {observed} | {'–' if met is None else 'yes' if met else 'no'} |"
              for name, required, observed, met, _ in gate_checks(report)]
    lines += ["", "## Cohorts", "", ("Difference is the proposed arm's pass rate minus the current arm's, "
              "averaged per case; the CI resamples cases."), "", *_cohort_rows(report)]
    case_lines = _case_lines(report, evidence, top)
    if case_lines:
        lines += ["", "## Cases", *case_lines]
    if report.get("retention"):
        lines += _retention_lines(report["retention"])
    errors = Counter(a[arm].get("error", "")[:80] for c in report.get("case_results", []) for a in c.get("attempts", [])
                     for arm in a if a[arm]["outcome"] == "error")
    if errors:
        lines += ["", "## Replay errors", ""]
        lines += ([f"- {n}× {msg or 'unknown'}" for msg, n in errors.most_common(5)] if evidence else
                  [(f"{sum(errors.values())} replays failed to run; `--evidence` lists the messages, "
                   "which can name local paths.")])
    return "\n".join(lines) + "\n"
