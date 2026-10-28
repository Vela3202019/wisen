"""
Git plumbing for the Gardener. Nothing here checks out a branch or touches a
work tree: diffs and commits are built in a throwaway index file
(GIT_INDEX_FILE), and branches are created by `update-ref` only if they do not
exist yet. Your checkout, your index and your uncommitted work stay as they are.
"""
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ZERO = "0" * 40
IDENTITY = {"name": "wisen gardener", "email": "gardener@wisen.invalid"}
# Variables a caller's environment may set that would point git elsewhere.
_SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR")


class GitError(Exception):
    pass


@dataclass(frozen=True)
class Repo:
    """A repository: a normal clone (`git_dir` None), or wisen's own repo for
    files outside any repo, whose git dir sits apart from its work tree."""
    work_tree: Path
    git_dir: Path | None = None

    def _cmd(self, args: tuple[str, ...]) -> list[str]:
        if self.git_dir is not None:
            return ["git", "--git-dir", str(self.git_dir), "--work-tree", str(self.work_tree), *args]
        return ["git", "-C", str(self.work_tree), *args]

    def run(self, *args: str, env: dict | None = None, input: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
        full_env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
        full_env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})})
        r = subprocess.run(self._cmd(args), capture_output=True, text=True, input=input, env=full_env,
                           check=False)
        if check and r.returncode != 0:
            raise GitError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
        return r

    def out(self, *args: str, **kwargs) -> str:
        return self.run(*args, **kwargs).stdout.strip()

    def ok(self, *args: str, **kwargs) -> bool:
        return self.run(*args, check=False, **kwargs).returncode == 0

    def resolve(self, rev: str) -> str | None:
        r = self.run("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}", check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def blob(self, commit: str, path: str) -> str | None:
        """The blob id of `path` in `commit`, or None if it has no such file."""
        r = self.run("rev-parse", "--verify", "--quiet", f"{commit}:{path}", check=False)
        return r.stdout.strip() if r.returncode == 0 else None

    def read_blob(self, blob: str) -> str:
        return self.run("cat-file", "blob", blob).stdout

    def default_branch(self) -> str:
        """The branch changes land on: origin's HEAD, else main or master."""
        head = self.run("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD", check=False).stdout.strip()
        if head.startswith("refs/remotes/origin/"):
            return head.removeprefix("refs/remotes/origin/")
        for name in ("main", "master"):
            if self.resolve(f"refs/heads/{name}"):
                return name
        return self.out("symbolic-ref", "--short", "HEAD")

    def remote(self) -> str | None:
        """The remote pull requests go to: origin, else the first one."""
        remotes = self.out("remote").split()
        return "origin" if "origin" in remotes else (remotes[0] if remotes else None)

    def default_refs(self) -> list[str]:
        """Where the default branch can be seen: the remote's copy and the local one."""
        name = self.default_branch()
        return [ref for ref in (f"refs/remotes/{self.remote()}/{name}", f"refs/heads/{name}") if self.resolve(ref)]

    @contextmanager
    def scratch_index(self) -> Iterator[dict]:
        """Env for git commands that work on a temporary index file."""
        with tempfile.TemporaryDirectory(prefix="wisen-index-") as tmp:
            yield {"GIT_INDEX_FILE": str(Path(tmp) / "index")}

    def commit_tree(self, tree: str, parent: str | None, message: str, when: str) -> str:
        """A commit whose every input is given, so the same call makes the
        same commit id: fixed identity, fixed time `when` (ISO 8601)."""
        env = {"GIT_AUTHOR_NAME": IDENTITY["name"], "GIT_AUTHOR_EMAIL": IDENTITY["email"],
               "GIT_COMMITTER_NAME": IDENTITY["name"], "GIT_COMMITTER_EMAIL": IDENTITY["email"],
               "GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
        args = ["commit-tree", tree, *(["-p", parent] if parent else []), "-F", "-"]
        return self.out(*args, env=env, input=message)

    def create_ref(self, ref: str, commit: str) -> bool:
        """Create `ref` at `commit` only if it does not exist. True if it now
        points there (created now or before); False if it points elsewhere."""
        if self.ok("update-ref", ref, commit, ZERO):
            return True
        return self.resolve(ref) == commit


def one_file_diff(repo: Repo, path: str, old: str | None, new: str) -> str:
    """A unified diff (git format) that turns `old` into `new` at `path`;
    `old` None means a new file. Built from two one-file trees, so it is
    exactly what `git apply` takes back."""
    trees = []
    with repo.scratch_index() as env:
        for text in (old, new):
            repo.run("read-tree", "--empty", env=env)
            if text is not None:
                blob = repo.out("hash-object", "-w", "--stdin", input=text)
                repo.run("update-index", "--add", "--cacheinfo", f"100644,{blob},{path}", env=env)
            trees.append(repo.out("write-tree", env=env))
    return repo.run("diff-tree", "-p", "--no-color", "--no-ext-diff", trees[0], trees[1]).stdout


def diff_paths(repo: Repo, diff: str) -> list[str]:
    """Every path a diff touches, as git reads it (renames count both sides)."""
    r = repo.run("apply", "--numstat", "-z", "--cached", "-", input=diff, check=False)
    if r.returncode != 0:
        raise GitError(f"not a diff git can read: {r.stderr.strip()[:200]}")
    paths = []
    for field in r.stdout.split("\0"):
        parts = field.split("\t")
        if len(parts) == 3:
            if parts[2]:
                paths.append(parts[2])
        elif field and "\t" not in field:
            paths.append(field)  # the second path of a rename
    return paths
