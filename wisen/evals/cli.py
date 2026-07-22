"""
Case-bank CLI.

  python -m evals mine [--since DATE] [--no-enrich]   scan transcripts, save new cases
  python -m evals list                                 show the case bank
  python -m evals review <id> --confirm|--reject       curate a case
  python -m evals metrics [--since DATE]               Tier-1 outcome metrics (JSON)
  python -m evals replay <id> [--claude-md PATH]       replay one case, print verdict
  python -m evals compare [--a PATH] [--b PATH]        run the suite per config, report
"""
import argparse
import asyncio
import json
from pathlib import Path

from wisen.evals.cases import CASES_DIR, Case, case_path, load_cases, save_case
from wisen.evals import metrics as metrics_mod
from wisen.evals import miner


async def _mine(since: str | None, no_enrich: bool) -> None:
    new = []
    for scan in miner.scan_all(since=since):
        for case in scan.cases:
            if case_path(case.id).exists():
                continue
            new.append(case)
    print(f"{len(new)} new cases detected")
    for case in new:
        if not no_enrich:
            try:
                case = await miner.enrich(case)
            except Exception as e:
                print(f"  enrich failed for {case.id}: {e}")
        save_case(case)
        print(f"  [{case.detector}] {case.id} ({case.split})")


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
    verdict = await judge(case, replay, k)
    print(f"check:    {case.behavior_check}")
    print(f"repeated: {verdict.repeated}  →  {'FAIL' if verdict.repeated else 'PASS'}")
    print(f"evidence: {verdict.evidence}")


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
    cp.add_argument("--a", type=Path, help="CLAUDE.md variant A (default: current config)")
    cp.add_argument("--b", type=Path, help="CLAUDE.md variant B (omit for single-config run)")
    cp.add_argument("--k", type=int, default=3)
    cp.add_argument("--limit", type=int)

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
