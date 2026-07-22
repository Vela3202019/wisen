"""
Case bank: mined correction moments, one JSON file per case in evals/cases/.

A Case is a labeled failure — the conversation context right before the user
corrected the agent, plus the correction itself as ground truth. Phase 2 replays
these under config variants and asks the behavior_check question.
"""
import hashlib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

CASES_DIR = Path(__file__).parent / "cases"

Detector = Literal["tool-rejection", "interruption", "explicit-correction", "rephrase-storm"]


class ContextTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class Case(BaseModel):
    id: str                      # "<date>-<session_id[:8]>-<event_index>"
    date: str                    # YYYY-MM-DD
    session_id: str
    source_path: str             # raw transcript the case was mined from
    project_path: str
    detector: Detector
    context: list[ContextTurn]   # conversation prefix up to the failure
    agent_action: str = ""       # what the agent did/was about to do (LLM-enriched)
    correction: str              # the user's actual words — verbatim ground truth
    behavior_check: str = ""     # yes/no judge question (LLM-enriched)
    split: Literal["eval", "holdout"]
    status: Literal["mined", "confirmed", "rejected"] = "mined"


def assign_split(date: str) -> Literal["eval", "holdout"]:
    """Deterministic ~20% holdout by date, so a whole day lands in one split."""
    digest = int(hashlib.sha1(date.encode()).hexdigest(), 16)
    return "holdout" if digest % 5 == 0 else "eval"


def case_path(case_id: str, cases_dir: Path = CASES_DIR) -> Path:
    return cases_dir / f"{case_id}.json"


def save_case(case: Case, cases_dir: Path = CASES_DIR) -> Path:
    cases_dir.mkdir(parents=True, exist_ok=True)
    path = case_path(case.id, cases_dir)
    path.write_text(case.model_dump_json(indent=2))
    return path


def load_cases(cases_dir: Path = CASES_DIR) -> list[Case]:
    if not cases_dir.exists():
        return []
    cases = []
    for f in sorted(cases_dir.glob("*.json")):
        cases.append(Case.model_validate_json(f.read_text()))
    return cases
