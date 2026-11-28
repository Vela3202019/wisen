"""
Case-bank CLI.

  python -m evals mine [--since DATE] [--no-enrich]   scan transcripts, save new cases
                                                       (corrections, and the clean set)
  python -m evals list                                 show the case bank
  python -m evals review <id> --confirm|--reject       curate a case
  python -m evals metrics [--since DATE]               Tier-1 outcome metrics (JSON)
  python -m evals replay <id> [--claude-md PATH]       replay one case, print verdict
  python -m evals compare [--a PATH] [--b PATH]        run the suite per config, report
  python -m evals report REPORT [--evidence]           Markdown scorecard of a paired/weekly/demo report
  python -m evals demo [--model local:NAME]            the protocol on the public synthetic case bank
  python -m evals export-sft --out DIR                 mlx-lm training data from the case bank
  python -m evals distill-demo --out DIR               mlx-lm training data that teaches the demo rules
"""
import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

from wisen.evals import metrics as metrics_mod
from wisen.evals import miner
from wisen.evals.cases import CASES_DIR, Case, case_path, load_cases, save_case


async def _mine(since: str | None, no_enrich: bool) -> None:
    new = []
    for scan in miner.scan_all(since=since):
        for case in [*scan.cases, *([scan.clean] if scan.clean else [])]:
            if case_path(case.id).exists():
                continue
            new.append(case)
    print(f"{len(new)} new cases detected")
    for case in new:
        if not no_enrich and case.kind == "correction":
            try:
                case = await miner.enrich(case)
            except (ValueError, RuntimeError) as e:
                print(f"  enrich failed for {case.id}: {e}")
        save_case(case)
        print(f"  [{case.detector or case.kind}] {case.id} ({case.split})")


def _list() -> None:
    cases = load_cases()
    if not cases:
        print(f"case bank empty ({CASES_DIR})")
        return
    for c in cases:
        check = c.behavior_check or c.correction.replace("\n", " ")[:80]
        print(f"{c.id}  {c.status:9}  {c.split:7}  [{c.detector}]  {check}")
    print(f"\n{len(cases)} cases")


def _review(case_id: str, confirm: bool) -> None:
    path = case_path(case_id)
    if not path.exists():
        raise SystemExit(f"no case {case_id}")
    case = Case.model_validate_json(path.read_text())
    case.status = "confirmed" if confirm else "rejected"
    save_case(case)
    print(f"{case_id} → {case.status}")


async def _replay(case_id: str, claude_md: Path | None, k: int) -> None:
    from wisen.evals.judge import judge
    from wisen.evals.replay import run_replay
    from wisen.llm import eval_model

    model = eval_model()
    path = case_path(case_id)
    if not path.exists():
        raise SystemExit(f"no case {case_id}")
    case = Case.model_validate_json(path.read_text())
    if not case.behavior_check:
        raise SystemExit(f"{case_id} has no behavior_check — run mine with enrichment first")
    print(f"replaying {case_id} in {case.project_path} @ {case.date}…")
    replay = await asyncio.to_thread(run_replay, case, claude_md)
    if replay.error:
        raise SystemExit(f"replay error: {replay.error}")
    verdict = await judge(case, replay, k, model)
    print(f"check:    {case.behavior_check}")
    print(f"verdict:  {'ABSTAIN' if verdict.no_signal else 'FAIL' if verdict.repeated else 'PASS'}")
    print(f"evidence: {verdict.evidence}")


def _protocol_commands(sub) -> None:
    freeze = sub.add_parser("freeze", help="pin the case inventory and an applied proposal's Git blobs")
    freeze.add_argument("proposal_id")
    freeze.add_argument("inventory", type=Path)
    freeze.add_argument("--out", type=Path, required=True)
    freeze.add_argument("--model", required=True, help="pinned Claude model, or local:NAME for a local server")
    freeze.add_argument("--harness", help="Claude Code version, or the local endpoint (default: detect)")
    freeze.add_argument("--judge-model")
    freeze.add_argument("--config", type=Path, help="JSON mapping of shared frozen home instruction files")
    for name in ("paired", "weekly"):
        run = sub.add_parser(name, help="run the frozen suite and write a private report")
        run.add_argument("manifest", type=Path)
        run.add_argument("--out", type=Path, required=True)
        run.add_argument("--repeats", type=int, default=5)
        run.add_argument("--votes", type=int, default=3)
        run.add_argument("--clean-margin", type=float, default=0.05)
        if name == "paired":
            mode = run.add_mutually_exclusive_group()
            mode.add_argument("--holdout", action="store_true")
            mode.add_argument("--record-gate", action="store_true")
        else:
            run.add_argument("--retain", type=Path, action="append", default=[])
            run.add_argument("--dataset", help="labeled replay dataset for judge drift")
            run.add_argument("--rater", default="owner")
            run.add_argument("--baseline-version", help="judge version to compare on the same human labels")
    report = sub.add_parser("report", help="print a Markdown scorecard of a paired, weekly or demo report")
    report.add_argument("report", type=Path)
    report.add_argument("--evidence", action="store_true", help="add judge quotes (may contain private text)")
    report.add_argument("--top", type=int, default=5, help="cases listed per gains and regressions")
    demo = sub.add_parser("demo", help="run the protocol on the public synthetic case bank")
    demo.add_argument("--model", help="local:NAME or a pinned Claude model (default: simulated, no model call)")
    demo.add_argument("--proposed-model", help="the proposed arm's model, e.g. a fine-tuned local:NAME@URL "
                      "(default: --model)")
    demo.add_argument("--rules", choices=("proposed", "both", "current", "none"), default="proposed",
                      help="which arms get the demo rules (default: proposed)")
    demo.add_argument("--judge-model")
    demo.add_argument("--repeats", type=int, default=3)
    demo.add_argument("--votes", type=int, default=3)
    demo.add_argument("--clean-margin", type=float, default=0.05)
    demo.add_argument("--out", type=Path, help="also write the full JSON report here")
    sft = sub.add_parser("export-sft", help="write mlx-lm train/valid JSONL from the case bank (see docs/training.md)")
    sft.add_argument("--out", type=Path, required=True)
    sft.add_argument("--exclude", type=Path, action="append", default=[],
                     help="frozen manifest whose cases stay out of training (repeatable)")
    targets = sft.add_mutually_exclusive_group()
    targets.add_argument("--raw", action="store_true", help="use the agent's own post-correction reply locally (default)")
    targets.add_argument("--rewrite", action="store_true", help="send private case context and replies to the configured "
                         "eval model for a rewritten target")
    sft.add_argument("--include-mined", action="store_true", help="also use cases not yet confirmed")
    sft.add_argument("--valid-fraction", type=float, default=0.1)
    distill = sub.add_parser("distill-demo", help="write mlx-lm train/valid JSONL that teaches the demo rules")
    distill.add_argument("--out", type=Path, required=True)
    distill.add_argument("--per-rule", type=int, default=40, help="training requests per demo rule")
    distill.add_argument("--clean", type=int, default=40, help="requests no rule bears on")
    distill.add_argument("--valid-fraction", type=float, default=0.1)
    field = sub.add_parser("field", help="show replay gate and observational field evidence side by side")
    field.add_argument("proposal_id")
    sub.add_parser("record-exposure", help="read a Claude Code SessionStart payload from stdin")


def _label_commands(sub) -> None:
    from wisen.evals.calibration import FAILURES

    sample = sub.add_parser("sample-replays", help="import a stratified sample for blind replay labeling")
    sample.add_argument("manifest", type=Path)
    sample.add_argument("report", type=Path)
    sample.add_argument("dataset")
    sample.add_argument("--n", type=int, default=150)
    sample.add_argument("--seed", type=int, default=0)
    turns = sub.add_parser("sample-turns", help="sample real transcript turns and record both detector predictions")
    turns.add_argument("dataset")
    turns.add_argument("dates", nargs="+", type=date.fromisoformat)
    turns.add_argument("--n", type=int, default=300)
    turns.add_argument("--seed", type=int, default=0)
    queue = sub.add_parser("label-queue", help="show blind items without model decisions or arm labels")
    queue.add_argument("kind", choices=("replay", "turn"))
    queue.add_argument("dataset")
    queue.add_argument("--rater", default="owner")
    queue.add_argument("--round", type=int, default=1, choices=(1, 2))
    queue.add_argument("--limit", type=int, default=10)
    label = sub.add_parser("label", help="record one blind human judgment")
    label.add_argument("id")
    label.add_argument("label")
    label.add_argument("--rater", default="owner")
    label.add_argument("--round", type=int, default=1, choices=(1, 2))
    label.add_argument("--failure", choices=FAILURES)
    cal = sub.add_parser("calibrate", help="measure agreement and confidence intervals")
    cal.add_argument("scorer", choices=("judge", "miner", "observer", "retest"))
    cal.add_argument("dataset")
    cal.add_argument("--version")
    cal.add_argument("--kind", choices=("replay", "turn"), default="replay", help="label kind for retest")
    cal.add_argument("--rater", default="owner")
    rerun = sub.add_parser("rejudge", help="score the same blinded outputs with a new judge")
    rerun.add_argument("dataset")
    rerun.add_argument("--model")
    rerun.add_argument("--votes", type=int, default=3)
    failures = sub.add_parser("failures", help="tally labeled causes and replay errors")
    failures.add_argument("dataset")
    failures.add_argument("--rater", default="owner")
    failures.add_argument("--report", type=Path)


async def _protocol(args) -> dict:
    from wisen.evals.exposure import record_start
    from wisen.evals.manifest import freeze_proposal, load_manifest, save_manifest
    from wisen.evals.protocol import (
        evidence,
        record_gate,
        retention,
        summary,
        write_report,
    )
    from wisen.evals.replay import harness_version
    from wisen.evals.runner import run_manifest
    from wisen.llm import eval_model
    from wisen.store import get_store

    store = get_store()
    if args.cmd == "record-exposure":
        return {"records": record_start(store, json.load(sys.stdin))}
    if args.cmd == "field":
        return evidence(store, args.proposal_id)
    if args.cmd == "freeze":
        manifest = freeze_proposal(store, args.proposal_id, _read_json(args.inventory), model=args.model,
                                   harness=args.harness or harness_version(args.model), judge_model=args.judge_model or eval_model(),
                                   config=_read_json(args.config) if args.config else None)
        save_manifest(args.out, manifest)
        return {"manifest": manifest.digest(), "path": str(args.out)}
    manifest = load_manifest(args.manifest)
    report = await run_manifest(manifest, k=args.repeats, judge_votes=args.votes, clean_margin=args.clean_margin,
                                holdout=getattr(args, "holdout", False))
    if args.cmd == "weekly":
        if args.retain:
            report["retention"] = await retention(store, [load_manifest(path) for path in args.retain], manifest,
                                                  k=args.repeats, votes=args.votes, margin=args.clean_margin)
        if args.dataset:
            from wisen.evals.calibration import Labels, judge_drift

            report["judge_drift"] = await judge_drift(Labels(store), args.dataset, args.rater, manifest.judge_model,
                                                      args.votes, args.baseline_version)
    write_report(args.out, report)
    result = {**summary(report), "path": str(args.out)}
    if getattr(args, "record_gate", False):
        result["promotion"] = record_gate(store, manifest, report)["decision"]
    if args.cmd == "weekly":
        result["evidence"] = evidence(store, manifest.proposal_id)
    return result


async def _scorecard(args) -> str:
    from wisen.evals.scorecard import render

    if args.cmd == "report":
        return render(_read_json(args.report), evidence=args.evidence, top=args.top)
    from wisen.evals.demo import run_demo
    from wisen.evals.protocol import write_report

    report = await run_demo(args.model, proposed_model=args.proposed_model, rules_in=args.rules,
                            repeats=args.repeats, votes=args.votes, clean_margin=args.clean_margin,
                            judge_model=args.judge_model)
    if args.out:
        write_report(args.out, report)
    return render(report)


async def _training(args) -> dict:
    from wisen.evals import training

    if args.cmd == "distill-demo":
        return await training.distill_demo(args.out, per_rule=args.per_rule, clean=args.clean,
                                           valid_fraction=args.valid_fraction)
    from wisen.evals.manifest import load_manifest

    frozen = [entry.case for path in args.exclude for entry in load_manifest(path).cases]
    return await training.export_cases(load_cases(), args.out, exclude={c.id for c in frozen},
                                       exclude_sessions=frozenset(c.session_id for c in frozen),
                                       ask=training.teacher() if args.rewrite else None,
                                       include_mined=args.include_mined, valid_fraction=args.valid_fraction)


def _read_json(path: Path):
    return json.loads(path.read_text())


async def _labels(args):
    from wisen.evals.calibration import (
        Labels,
        calibration,
        failure_report,
        import_replays,
        rejudge,
        sample_turns,
    )
    from wisen.evals.manifest import load_manifest
    from wisen.llm import eval_model
    from wisen.store import get_store

    labels = Labels(get_store())
    if args.cmd == "label":
        labels.label(args.id, args.rater, args.round, args.label, args.failure)
        return {"labeled": args.id}
    if args.cmd == "label-queue":
        return labels.blind(args.kind, args.dataset, args.rater, args.round)[:args.limit]
    if args.cmd == "sample-replays":
        count = import_replays(labels, load_manifest(args.manifest), _read_json(args.report), args.dataset,
                               n=args.n, seed=args.seed)
        return {"items": count}
    if args.cmd == "sample-turns":
        return await sample_turns(labels, [day.isoformat() for day in args.dates], args.dataset, n=args.n, seed=args.seed)
    if args.cmd == "rejudge":
        return {"judge_version": await rejudge(labels, args.dataset, args.model or eval_model(), args.votes)}
    if args.cmd == "failures":
        return failure_report(labels, args.dataset, args.rater, _read_json(args.report) if args.report else None)
    if args.scorer == "retest":
        return calibration(labels.retest(args.kind, args.dataset, args.rater), args.kind)
    if not args.version:
        raise ValueError("calibrate requires --version for model or miner scores")
    kind = "replay" if args.scorer == "judge" else "turn"
    rows = labels.comparisons(kind, args.dataset, args.rater, args.scorer, args.version)
    return calibration(rows, kind)


def main() -> None:
    p = argparse.ArgumentParser(prog="evals")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("mine")
    m.add_argument("--since", help="only sessions on/after this date (YYYY-MM-DD)")
    m.add_argument("--no-enrich", action="store_true", help="skip the LLM labeling pass")

    sub.add_parser("list")

    r = sub.add_parser("review")
    r.add_argument("id")
    g = r.add_mutually_exclusive_group(required=True)
    g.add_argument("--confirm", action="store_true")
    g.add_argument("--reject", action="store_true")

    mt = sub.add_parser("metrics")
    mt.add_argument("--since")

    rp = sub.add_parser("replay")
    rp.add_argument("id")
    rp.add_argument("--claude-md", type=Path)
    rp.add_argument("--k", type=int, default=3)

    cp = sub.add_parser("compare")
    cp.add_argument("--a", type=Path, help="CLAUDE.md variant A (default: no home instructions)")
    cp.add_argument("--b", type=Path, help="CLAUDE.md variant B (omit for single-config run)")
    cp.add_argument("--k", type=int, default=3)
    cp.add_argument("--limit", type=int)
    _protocol_commands(sub)
    _label_commands(sub)

    args = p.parse_args()
    if args.cmd == "mine":
        asyncio.run(_mine(args.since, args.no_enrich))
    elif args.cmd == "list":
        _list()
    elif args.cmd == "review":
        _review(args.id, args.confirm)
    elif args.cmd == "metrics":
        print(json.dumps(metrics_mod.report(since=args.since), indent=2))
    elif args.cmd == "replay":
        asyncio.run(_replay(args.id, args.claude_md, args.k))
    elif args.cmd == "compare":
        from wisen.evals.runner import run_suite
        report = asyncio.run(run_suite(args.a, args.b, args.k, args.limit))
        print(json.dumps(report, indent=2))
    elif args.cmd in ("export-sft", "distill-demo"):
        try:
            print(json.dumps(asyncio.run(_training(args)), indent=2))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"eval: {exc}") from None
    elif args.cmd in ("report", "demo"):
        try:
            print(asyncio.run(_scorecard(args)), end="")
        except (OSError, ValueError) as exc:
            raise SystemExit(f"eval: {exc}") from None
    else:
        handler = _protocol if args.cmd in ("freeze", "paired", "weekly", "field", "record-exposure") else _labels
        try:
            print(json.dumps(asyncio.run(handler(args)), indent=2))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"eval: {exc}") from None
