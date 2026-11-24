"""Local blind human-label queues and reproducible calibration reports."""
import json
import random
from collections import Counter, defaultdict, deque
from contextlib import closing
from hashlib import sha256
from uuid import uuid4

from wisen.evals import miner
from wisen.evals.cases import Case
from wisen.evals.manifest import Manifest
from wisen.evals.statistics import bootstrap_ci
from wisen.store import SqliteStore, migrate

CLASSES = {
    "replay": ("pass", "fail", "abstain"),
    "turn": ("none", "tool-rejection", "interruption", "explicit-correction", "rephrase-storm"),
}
FAILURES = ("agent/model", "rule wording", "judge error", "replay environment")
MIGRATIONS = ["""
    CREATE TABLE eval_items (
        user_id TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, dataset TEXT NOT NULL,
        blind TEXT NOT NULL, private TEXT NOT NULL, slices TEXT NOT NULL,
        PRIMARY KEY (user_id, id)
    );
    CREATE TABLE eval_predictions (
        user_id TEXT NOT NULL, item_id TEXT NOT NULL, scorer TEXT NOT NULL,
        version TEXT NOT NULL, label TEXT NOT NULL,
        PRIMARY KEY (user_id, item_id, scorer, version)
    );
    CREATE TABLE eval_labels (
        user_id TEXT NOT NULL, item_id TEXT NOT NULL, rater TEXT NOT NULL,
        round INTEGER NOT NULL, label TEXT NOT NULL, failure TEXT,
        PRIMARY KEY (user_id, item_id, rater, round)
    );
"""]


def _id(*parts: str) -> str:
    return sha256(json.dumps(parts).encode()).hexdigest()[:24]


class Labels:
    def __init__(self, store: SqliteStore):
        self.store = store
        self.user = store.user_id
        with closing(store._connect()) as conn:
            migrate(conn, "eval_labels", MIGRATIONS)

    def items(self, kind: str, dataset: str) -> list[dict]:
        with closing(self.store._connect()) as conn:
            rows = conn.execute("SELECT * FROM eval_items WHERE user_id=? AND kind=? AND dataset=? ORDER BY id",
                                (self.user, kind, dataset)).fetchall()
        return [{**row, **{key: json.loads(row[key]) for key in ("blind", "private", "slices")}} for row in rows]

    def add(self, kind: str, dataset: str, items: list[dict]) -> None:
        with self.store.transaction() as conn:
            for item in items:
                conn.execute("INSERT OR IGNORE INTO eval_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (self.user, item["id"], kind, dataset,
                              *(json.dumps(item.get(key, {})) for key in ("blind", "private", "slices"))))
                for scorer, version, label in item["predictions"]:
                    conn.execute("INSERT OR IGNORE INTO eval_predictions VALUES (?, ?, ?, ?, ?)",
                                 (self.user, item["id"], scorer, version, label))

    def predict(self, item_id: str, scorer: str, version: str, label: str) -> None:
        with self.store.transaction() as conn:
            conn.execute("INSERT OR REPLACE INTO eval_predictions VALUES (?, ?, ?, ?, ?)",
                         (self.user, item_id, scorer, version, label))

    def blind(self, kind: str, dataset: str, rater: str, round_: int) -> list[dict]:
        with closing(self.store._connect()) as conn:
            done = {row[0] for row in conn.execute(
                "SELECT item_id FROM eval_labels WHERE user_id=? AND rater=? AND round=?", (self.user, rater, round_))}
        items = self.items(kind, dataset)
        if round_ > 1:
            with closing(self.store._connect()) as conn:
                first = {row[0] for row in conn.execute(
                    "SELECT item_id FROM eval_labels WHERE user_id=? AND rater=? AND round=1", (self.user, rater))}
            items = [item for item in items if item["id"] in first and int(item["id"], 16) % 5 == 0]
        return [{"id": item["id"], **item["blind"]} for item in items if item["id"] not in done]

    def label(self, item_id: str, rater: str, round_: int, label: str, failure: str | None = None) -> None:
        if not rater or round_ < 1 or (failure is not None and failure not in FAILURES):
            raise ValueError("invalid rater, round or failure category")
        with self.store.transaction() as conn:
            item = conn.execute("SELECT kind FROM eval_items WHERE user_id=? AND id=?",
                                (self.user, item_id)).fetchone()
            if not item or label not in CLASSES[item["kind"]]:
                raise ValueError("unknown item or invalid label")
            conn.execute("INSERT INTO eval_labels VALUES (?, ?, ?, ?, ?, ?)",
                         (self.user, item_id, rater, round_, label, failure))

    def comparisons(self, kind: str, dataset: str, rater: str, scorer: str, version: str) -> list[dict]:
        with closing(self.store._connect()) as conn:
            rows = conn.execute("""
                SELECT i.id, i.slices, i.private, h.label AS human, p.label AS predicted
                FROM eval_items i JOIN eval_labels h
                    ON h.user_id=i.user_id AND h.item_id=i.id AND h.rater=? AND h.round=1
                JOIN eval_predictions p
                    ON p.user_id=i.user_id AND p.item_id=i.id AND p.scorer=? AND p.version=?
                WHERE i.user_id=? AND i.kind=? AND i.dataset=? ORDER BY i.id
            """, (rater, scorer, version, self.user, kind, dataset)).fetchall()
        return [_comparison(row) for row in rows]

    def retest(self, kind: str, dataset: str, rater: str) -> list[dict]:
        with closing(self.store._connect()) as conn:
            rows = conn.execute("""
                SELECT i.id, i.slices, i.private, a.label AS human, b.label AS predicted
                FROM eval_items i JOIN eval_labels a
                    ON a.user_id=i.user_id AND a.item_id=i.id AND a.rater=? AND a.round=1
                JOIN eval_labels b
                    ON b.user_id=a.user_id AND b.item_id=a.item_id AND b.rater=a.rater AND b.round=2
                WHERE i.user_id=? AND i.kind=? AND i.dataset=? ORDER BY i.id
            """, (rater, self.user, kind, dataset)).fetchall()
        return [_comparison(row) for row in rows]

    def diagnoses(self, kind: str, dataset: str, rater: str) -> dict[str, str]:
        with closing(self.store._connect()) as conn:
            return dict(conn.execute("""
                SELECT h.item_id, h.failure FROM eval_labels h JOIN eval_items i
                    ON h.user_id=i.user_id AND h.item_id=i.id
                WHERE h.user_id=? AND h.rater=? AND h.round=1 AND i.kind=? AND i.dataset=? AND h.failure IS NOT NULL
            """, (self.user, rater, kind, dataset)).fetchall())


def _comparison(row) -> dict:
    return {**row, "slices": json.loads(row["slices"]),
            "cluster": json.loads(row["private"]).get("cluster", row["id"])}


def _stratified(items: list[dict], keys: tuple[str, ...], n: int, seed: int) -> list[dict]:
    strata: dict[tuple, deque] = defaultdict(deque)
    rng = random.Random(seed)
    pool = sorted(items, key=lambda item: item["id"])
    rng.shuffle(pool)
    for item in pool:
        strata[tuple(item["slices"].get(key) for key in keys)].append(item)
    selected = []
    groups = list(strata.values())
    while groups and len(selected) < n:
        for group in groups:
            selected.append(group.popleft())
            if len(selected) == n:
                break
        groups = [group for group in groups if group]
    return selected


def judge_version(model: str, prompt: str, votes: int) -> str:
    return f"{model}/{prompt[:16]}/votes={votes}"


def import_replays(labels: Labels, manifest: Manifest, report: dict, dataset: str, *,
                   n: int = 150, seed: int = 0) -> int:
    if report.get("manifest") != manifest.digest() or report.get("holdout"):
        raise ValueError("report was not produced from this manifest")
    if n < 1:
        raise ValueError("sample size must be positive")
    cases = {entry.case.id: entry for entry in manifest.cases}
    version = judge_version(manifest.judge_model, manifest.judge_prompt, report["judge_votes"])
    items = []
    for result in report["case_results"]:
        entry = cases[result["case"]]
        case = entry.case
        for i, pair in enumerate(result["attempts"]):
            for arm, attempt in pair.items():
                if "output" not in attempt:
                    continue
                output = attempt["output"]
                length = sum(len(turn.content) for turn in case.context)
                item = {
                    "id": _id(dataset, report["run_id"], case.id, str(i), arm),
                    "blind": {"check": case.behavior_check, "reference": case.correction,
                              "case_kind": case.kind, **output},
                    "private": {"cluster": case.id, "case": case.model_dump(mode="json")},
                    "slices": {"rule_type": entry.rule_type, "model": manifest.model,
                               "transcript_length": "short" if length < 2000 else "long", "arm": arm,
                               "verdict": attempt["outcome"]},
                    "predictions": [],
                }
                if attempt["outcome"] in CLASSES["replay"]:
                    item["predictions"].append(("judge", version, attempt["outcome"]))
                items.append(item)
    sample = _stratified(items, ("rule_type", "arm", "verdict"), n, seed)
    labels.add("replay", dataset, sample)
    return len(sample)


async def rejudge(labels: Labels, dataset: str, model: str, votes: int) -> str:
    from wisen.evals.judge import judge, prompt_version
    from wisen.evals.replay import ReplayResult

    if votes < 1:
        raise ValueError("judge votes must be positive")
    version = f"{judge_version(model, prompt_version(), votes)}/{uuid4().hex[:8]}"
    for item in labels.items("replay", dataset):
        case = Case.model_validate(item["private"]["case"])
        payload = item["blind"]
        verdict = await judge(case, ReplayResult(payload["final_text"], payload["diff"]), votes, model)
        prediction = "abstain" if verdict.no_signal else "fail" if verdict.repeated else "pass"
        labels.predict(item["id"], "judge", version, prediction)
    return version


async def sample_turns(labels: Labels, dates: list[str], dataset: str, *, n: int = 300,
                       seed: int = 0) -> dict:
    from wisen.ingestion.pipeline import ingest_all
    from wisen.llm import get_model
    from wisen.synthesis.agent import observe, turn_id
    from wisen.synthesis.claude_cli import is_cli_model, synth_model
    from wisen.synthesis.prompt import OBSERVER_PROMPT

    if n < 1:
        raise ValueError("sample size must be positive")
    model = get_model()
    observer_model = synth_model() if is_cli_model() else model
    observer_version = f"{observer_model}/{_id(OBSERVER_PROMPT)}"
    hits, negatives = [], []
    for day in sorted(set(dates)):
        data = await ingest_all(day)
        history = await labels.store.list_reflections(limit=1, before=day)
        observation = await observe(data, day, history[0] if history else None, model)
        predictions = {c.turn_id: c.kind for c in observation.turn_corrections}
        for session in data.sessions:
            if session.source == "git" or session.date != day:
                continue
            for i, heuristic in miner.normalized_predictions(session):
                item = {
                    "id": _id(dataset, turn_id(session, i)),
                    "blind": {"turn": session.turns[i].content,
                              "context": [{"role": t.role, "content": t.content}
                                          for t in session.turns[max(0, i - 4):i]]},
                    "private": {"cluster": session.session_id},
                    "slices": {"connector": session.source},
                    "predictions": [
                        ("miner", miner.normalized_detector_version(), heuristic or "none"),
                        ("observer", observer_version, predictions.get(turn_id(session, i), "none")),
                    ],
                }
                (hits if heuristic else negatives).append(item)
    sample = hits + _stratified(negatives, ("connector",), max(0, n - len(hits)), seed)
    labels.add("turn", dataset, sample)
    return {"items": len(sample), "heuristic_hits": len(hits), "observer_version": observer_version,
            "miner_version": miner.normalized_detector_version()}


def _agreement(rows: list[dict], classes: tuple[str, ...]) -> dict:
    matrix = {label: dict.fromkeys(classes, 0) for label in classes}
    for row in rows:
        matrix[row["human"]][row["predicted"]] += 1
    n = len(rows)
    human = {label: sum(counts.values()) for label, counts in matrix.items()}
    pred = {label: sum(counts[label] for counts in matrix.values()) for label in classes}
    expected = sum(human[label] * pred[label] for label in classes)
    observed = sum(matrix[label][label] for label in classes)
    kappa = (observed * n - expected) / (n * n - expected) if n * n > expected else None
    per_class = {}
    for label in classes:
        hit = matrix[label][label]
        per_class[label] = {"precision": hit / pred[label] if pred[label] else None,
                            "recall": hit / human[label] if human[label] else None, "support": human[label]}
    return {"n": n, "kappa": kappa, "confusion": matrix, "per_class": per_class}


def calibration(rows: list[dict], kind: str) -> dict:
    classes = CLASSES[kind]

    def summary(subset: list[dict]) -> dict:
        clusters: dict[str, list] = defaultdict(list)
        for row in subset:
            clusters[row["cluster"]].append(row)
        estimates = []

        def estimate(sample):
            metrics = _agreement([row for group in sample for row in group], classes)
            estimates.append(metrics["per_class"])
            return metrics["kappa"]
        ci = bootstrap_ci(list(clusters.values()), estimate, samples=1000)
        result = {**_agreement(subset, classes), "kappa_ci95": ci, "clusters": len(clusters)}
        for label in classes:
            for metric in ("precision", "recall"):
                values = sorted(value for sample in estimates if (value := sample[label][metric]) is not None)
                result["per_class"][label][f"{metric}_ci95"] = (
                    [values[int((len(values) - 1) * 0.025)],
                     values[-1 - int((len(values) - 1) * 0.025)]] if values else None)
        return result

    slices: dict[str, dict] = defaultdict(dict)
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for row in rows:
        for key, value in row["slices"].items():
            groups[key, str(value)].append(row)
    for (key, value), group in groups.items():
        slices[key][value] = summary(group)
    return {"overall": summary(rows), "slices": dict(slices)}


async def judge_drift(labels: Labels, dataset: str, rater: str, model: str, votes: int,
                      baseline_version: str | None) -> dict:
    current_version = await rejudge(labels, dataset, model, votes)
    current = labels.comparisons("replay", dataset, rater, "judge", current_version)
    baseline = (labels.comparisons("replay", dataset, rater, "judge", baseline_version)
                if baseline_version else [])
    if baseline_version:
        matched = {row["id"] for row in current} & {row["id"] for row in baseline}
        current = [row for row in current if row["id"] in matched]
        baseline = [row for row in baseline if row["id"] in matched]
    result = {"current_version": current_version, "current": calibration(current, "replay")}
    if baseline_version:
        old = calibration(baseline, "replay")
        before, after = old["overall"]["kappa"], result["current"]["overall"]["kappa"]
        result.update(baseline_version=baseline_version, baseline=old,
                      kappa_change=after - before if before is not None and after is not None else None)
    return result


def failure_report(labels: Labels, dataset: str, rater: str, report: dict | None = None) -> dict:
    diagnoses = labels.diagnoses("replay", dataset, rater)
    counts = Counter(diagnoses.values())
    if report:
        for case in report["case_results"]:
            for index, pair in enumerate(case["attempts"]):
                for arm, attempt in pair.items():
                    item_id = _id(dataset, report["run_id"], case["case"], str(index), arm)
                    if attempt["outcome"] == "error" and item_id not in diagnoses:
                        counts["judge error" if "output" in attempt else "replay environment"] += 1
    total = sum(counts.values())
    guidance = dict(zip(FAILURES, (
        "Inspect repeated behavior and add a targeted capability case.",
        "Revise ambiguity or scope and retest against the clean set.",
        "Recalibrate the rubric or make the judge abstain on missing evidence.",
        "Check the pinned SHA, harness and required files before replaying.",
    ), strict=True))
    return {"categorized_incidents": total, "categories": {
        key: {"count": counts[key], "share": counts[key] / total if total else None,
              "recommendation": guidance[key]} for key in FAILURES}}
