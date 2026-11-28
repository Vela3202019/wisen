"""Immutable, local-only inputs to a replay comparison."""
import json
import os
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from wisen.evals.cases import Case

Files = dict[str, str | None]


def require_claude_target(path: str, *, home: bool, local: bool = False) -> None:
    """`local`: a local model reads AGENTS.md too (see replay.instructions)."""
    if home and not path.startswith(".claude/"):
        raise ValueError("Claude replay supports home proposals under .claude only")
    if Path(path).name == "AGENTS.md" and not local:
        raise ValueError("Claude replay does not load standalone AGENTS.md targets")


class ReplayFiles(BaseModel):
    config: Files = Field(default_factory=dict)
    project: Files = Field(default_factory=dict)
    blob: str | None = None

    @model_validator(mode="after")
    def instructions_only(self):
        for rel in self.config:
            path = Path(rel)
            if (path.is_absolute() or ".." in path.parts or
                    (rel != "CLAUDE.md" and not (path.parts and path.parts[0] in ("rules", "skills")
                                                  and path.suffix == ".md"))):
                raise ValueError(f"not a supported home instruction path: {rel}")
        return self


class FrozenCase(BaseModel):
    case: Case
    base_commit: str = Field(pattern=r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
    cohort: Literal["targeted", "clean", "edge", "adversarial", "holdout"]
    rule_type: str = "unspecified"
    cumulative_project_files: Files = Field(default_factory=dict)


class Manifest(BaseModel):
    version: Literal[1] = 1
    proposal_id: str
    model: str = Field(min_length=1)
    harness: str = Field(min_length=1)
    judge_model: str = Field(min_length=1)
    judge_prompt: str
    current: ReplayFiles
    proposed: ReplayFiles
    cases: list[FrozenCase] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_cases(self):
        if self.model.lower() in {"sonnet", "opus", "haiku", "default", "opusplan", "best"}:
            raise ValueError("use a pinned Claude model name instead of an alias")
        ids = [entry.case.id for entry in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate case IDs in manifest")
        for entry in self.cases:
            case = entry.case
            if case.status != "confirmed" or not case.context or not case.behavior_check:
                raise ValueError(f"{case.id} must have a confirmed prefix and behavior check")
            if case.split == "holdout" and entry.cohort != "holdout":
                raise ValueError(f"{case.id} is reserved for holdout")
            if entry.cohort in ("targeted", "clean") and (case.kind == "correction") != (entry.cohort == "targeted"):
                raise ValueError(f"{case.id} does not match its cohort")
        return self

    def digest(self) -> str:
        data = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256(data.encode()).hexdigest()


def save_manifest(path: Path, manifest: Manifest) -> None:
    with open(path, "x", encoding="utf-8", opener=_private_open) as output:
        json.dump({"sha256": manifest.digest(), "manifest": manifest.model_dump(mode="json")}, output, indent=2)


def _private_open(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def load_manifest(path: Path) -> Manifest:
    envelope = json.loads(path.read_text())
    manifest = Manifest.model_validate(envelope["manifest"])
    if manifest.digest() != envelope["sha256"]:
        raise ValueError("manifest content hash does not match")
    return manifest


def freeze_proposal(store, proposal_id: str, inventory: list[dict], *, model: str, harness: str,
                    judge_model: str, config: Files | None = None) -> Manifest:
    from wisen.evals.cases import load_cases
    from wisen.evals.judge import prompt_version
    from wisen.gardener.applier import git_row
    from wisen.gardener.git import Repo

    row = git_row(store, proposal_id)
    if row is None:
        raise ValueError("apply the proposal before freezing its Git artifacts")
    repo = Repo(Path(row["work_tree"]), Path(row["git_dir"]) if row["git_dir"] else None)
    path = row["path"]
    require_claude_target(path, home=bool(row["git_dir"]), local=model.startswith("local:"))
    old_blob = repo.blob(row["base_commit"], path)
    new_blob = row["new_blob"]
    cases = {case.id: case for case in load_cases()}
    entries = []
    for item in inventory:
        case_id = item["case_id"]
        if case_id not in cases:
            raise ValueError(f"unknown case: {case_id}")
        case = cases[case_id]
        work_tree = Path(case.project_path).resolve()
        if not row["git_dir"] and not work_tree.is_relative_to(repo.work_tree.resolve()):
            raise ValueError(f"{case_id} is outside the proposal's project")
        if not (work_tree / ".git").exists() or Repo(work_tree).resolve(item["base_commit"]) != item["base_commit"]:
            raise ValueError(f"{case_id} needs a full commit ID in its project clone")
        entries.append(FrozenCase(case=case, base_commit=item["base_commit"], cohort=item["cohort"],
                                  rule_type=item.get("rule_type", "unspecified")))

    def files(blob: str | None) -> ReplayFiles:
        text = repo.run("cat-file", "blob", blob, text=False).stdout.decode("utf-8") if blob else None
        if row["git_dir"]:
            return ReplayFiles(config={**(config or {}), path.removeprefix(".claude/"): text}, blob=blob)
        return ReplayFiles(config=config or {}, project={path: text}, blob=blob)

    return Manifest(proposal_id=proposal_id, model=model, harness=harness, judge_model=judge_model,
                    judge_prompt=prompt_version(), current=files(old_blob), proposed=files(new_blob), cases=entries)
