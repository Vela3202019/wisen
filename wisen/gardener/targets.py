"""
What the Gardener may change, and where it lives.

A target is `<project alias>:<path relative to the project>`, the form that
leaves the machine in a proposal packet. Two kinds of project:

  - a git repo found the way the config connector finds them
    (`[paths].git_scan_dirs`, and Claude Code's known projects); its alias is
    the repo directory's name. Two repos with the same name are both left
    out, rather than guessing which one a proposal means.
  - `home`: files outside any repo, such as ~/.claude/CLAUDE.md and personal
    skills. wisen keeps its own git repo for them (`[gardener].home_repo`,
    default ~/.wisen/gardener/home.git) whose work tree is your home dir
    (`[paths].root_dir`). Its main branch records the files as they are on
    disk; a proposal is a branch you merge like any other
    (docs/plan/cloud-architecture.md §5.5).

`allowed` is the allow-list. The applier checks it after approval, so it is
the authority, not the model's instructions.

The Gardener reads and drafts through one of two views of these files:
`LocalFiles`, the repos on this machine, or `SnapshotFiles`, the text a
laptop uploaded as config snapshots (the server's Gardener, stage B), which
it drafts against without any repo.
"""
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wisen import config
from wisen.gardener.git import Repo, one_file_diff, text_diff

HOME = "home"
ALIAS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md")
SKILL_ROOTS = (".claude", ".agents")
# Where wisen looks for editable files outside any repo, relative to home.
HOME_FILES = (".claude/CLAUDE.md", ".codex/AGENTS.md", ".config/opencode/AGENTS.md")
HOME_SKILL_DIRS = (".claude/skills", ".agents/skills")
FILE_MODES = ("100644", "100755")   # regular files in a git tree; not symlinks (120000) or submodules


class Refused(Exception):
    """The target is not one the Gardener may change; nothing was written."""


def allowed(path: str) -> bool:
    """True for a CLAUDE.md or AGENTS.md file, or a file inside a skill
    directory (`.claude/skills/<name>/…`, `.agents/skills/<name>/…`), given as a
    plain relative path. Everything else (repo code, settings, MCP config) is
    refused."""
    if not path or "\\" in path or "\0" in path:
        return False
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).is_absolute() or any(p in ("", ".", "..", ".git") for p in parts):
        return False
    if parts[-1] in INSTRUCTION_FILES:
        return True
    for i in range(1, len(parts) - 2):
        if parts[i] == "skills" and parts[i - 1] in SKILL_ROOTS:
            return True
    return False


def home_repo_dir() -> Path:
    return Path(config.get("gardener", "home_repo", "WISEN_GARDENER_HOME_REPO", "~/.wisen/gardener/home.git")
                ).expanduser()


def home_repo(create: bool = True) -> Repo | None:
    """wisen's repo for files outside any repo; made on first use, with an
    empty first commit on main and untracked files hidden from `git status`."""
    git_dir, root = home_repo_dir(), config.root()
    repo = Repo(work_tree=root, git_dir=git_dir)
    if (git_dir / "HEAD").exists():
        return repo
    if not create:
        return None
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    Repo(work_tree=git_dir.parent).run("init", "--quiet", "--bare", "--initial-branch=main", str(git_dir))
    for key, value in (("core.bare", "false"), ("core.worktree", str(root)), ("status.showUntrackedFiles", "no")):
        repo.run("config", key, value)
    with repo.scratch_index() as env:
        repo.run("read-tree", "--empty", env=env)
        tree = repo.out("write-tree", env=env)
    first = repo.commit_tree(tree, None, "wisen: start the home repo\n", "946684800 +0000")
    repo.create_ref("refs/heads/main", first)
    return repo


def _scan_dirs() -> list[Path]:
    raw = config.get("paths", "git_scan_dirs", "GIT_SCAN_DIRS", str(Path.home() / "Developer"))
    return [Path(d.strip()).expanduser() for d in raw.split(",") if d.strip()]


def project_repos() -> dict[str, Path]:
    """alias -> repo root, for every repo the config connector would scan.
    Ambiguous aliases (two repos, one name) are dropped."""
    found: dict[str, set[Path]] = {}

    def add(path: Path) -> None:
        if (path / ".git").exists() and ALIAS.match(path.name) and path.name != HOME:
            found.setdefault(path.name, set()).add(path.resolve())

    for base in _scan_dirs():
        if base.is_dir():
            add(base)
            for pattern in ("*", "*/*"):
                for path in base.glob(pattern):
                    if path.is_dir():
                        add(path)
    projects = config.root() / ".claude" / "projects"
    if projects.is_dir():
        for d in projects.iterdir():
            add(Path("/" + d.name.lstrip("-").replace("-", "/")))
    return {alias: next(iter(paths)) for alias, paths in found.items() if len(paths) == 1}


@dataclass(frozen=True)
class Target:
    alias: str
    path: str
    repo: Repo

    @property
    def name(self) -> str:
        return f"{self.alias}:{self.path}"

    @property
    def is_home(self) -> bool:
        return self.alias == HOME

    def base(self) -> str | None:
        """The commit a branch for this target starts from: the default
        branch (origin's copy if there is one); for home, main."""
        if self.is_home:
            return self.repo.resolve("refs/heads/main")
        refs = self.repo.default_refs()
        return self.repo.resolve(refs[0]) if refs else None

    def live_file(self) -> Path:
        return self.repo.work_tree / self.path

    def current(self) -> str | None:
        """The text a change is drafted against: for home, the file on disk;
        for a repo, the file on its default branch."""
        if self.is_home:
            f = self.live_file()
            return f.read_text() if f.is_file() else None
        base = self.base()
        blob = self.repo.blob(base, self.path) if base else None
        return self.repo.read_blob(blob) if blob else None


def resolve(name: str, repos: dict[str, Path] | None = None) -> Target:
    """The Target for `alias:path`, or Refused."""
    alias, sep, path = name.partition(":")
    if not sep or not ALIAS.match(alias):
        raise Refused(f"{name!r} is not <project>:<path>")
    if not allowed(path):
        raise Refused(f"{path!r} is not a CLAUDE.md, AGENTS.md or skill file; the Gardener may not change it")
    if alias == HOME:
        repo = home_repo()
        live = repo.work_tree / path
        if live.is_symlink() or any(p.is_symlink() for p in live.parents if p != repo.work_tree
                                    and p.is_relative_to(repo.work_tree)):
            raise Refused(f"home:{path} is a symlink; change the file it points to instead")
        return Target(alias, path, repo)
    root = (repos if repos is not None else project_repos()).get(alias)
    if root is None:
        raise Refused(f"no project {alias!r} (or more than one repo has that name)")
    t = Target(alias, path, Repo(work_tree=root))
    base = t.base()
    if base:
        parts = PurePosixPath(path).parts
        prefixes = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
        for line in t.repo.out("ls-tree", base, "--", *prefixes).splitlines():
            if line.split(" ", 1)[0] not in (*FILE_MODES, "040000"):
                raise Refused(f"{name} is a symlink or submodule in the repo; change the file it points to instead")
    return t


def editable_files(repos: dict[str, Path] | None = None) -> list[str]:
    """Every existing file the Gardener may change, as targets."""
    names = []
    root = config.root()
    for rel in HOME_FILES:
        if (root / rel).is_file() and not (root / rel).is_symlink():
            names.append(f"{HOME}:{rel}")
    for skills in HOME_SKILL_DIRS:
        d = root / skills
        if d.is_dir():
            names += sorted(f"{HOME}:{f.relative_to(root).as_posix()}" for f in d.rglob("*")
                            if f.is_file() and not f.is_symlink() and allowed(f.relative_to(root).as_posix()))
    for alias, path in sorted((repos if repos is not None else project_repos()).items()):
        repo = Repo(work_tree=path)
        refs = repo.default_refs()
        if not refs:
            continue
        for line in repo.out("ls-tree", "-r", refs[0]).splitlines():
            meta, f = line.split("\t", 1)
            if meta.split(" ", 1)[0] in FILE_MODES and allowed(f):
                names.append(f"{alias}:{f}")
    return names


def base_blob(t: Target, current: str | None) -> str | None:
    """The blob a change to `t` is drafted against: for home, the file on
    disk (the applier records it on main before it branches); for a repo,
    the file on the default branch."""
    if t.is_home:
        return t.repo.out("hash-object", "--stdin", input=current) if current is not None else None
    base = t.base()
    return t.repo.blob(base, t.path) if base else None


@dataclass(frozen=True)
class LocalFile:
    target: Target
    device_id = None   # drafted where it is applied

    @property
    def name(self) -> str:
        return self.target.name

    def current(self) -> str | None:
        return self.target.current()

    def diff(self, current: str | None, proposed: str) -> str:
        return one_file_diff(self.target.repo, self.target.path, current, proposed)

    def base_blob(self, current: str | None) -> str | None:
        return base_blob(self.target, current)


class LocalFiles:
    """The files on this machine, found in its repos and home dir."""

    def __init__(self, repos: dict[str, Path] | None = None):
        self.repos = repos

    def list(self) -> list[str]:
        return editable_files(self.repos)

    def open(self, name: str) -> LocalFile:
        return LocalFile(resolve(name, self.repos))


@dataclass(frozen=True)
class SnapshotFile:
    name: str
    path: str
    text: str | None
    blob: str | None
    device_id: str   # the laptop whose snapshot it is: the one that applies a change to it

    def current(self) -> str | None:
        return self.text

    def diff(self, current: str | None, proposed: str) -> str:
        return text_diff(self.path, current, proposed)

    def base_blob(self, current: str | None) -> str | None:
        return self.blob


class SnapshotFiles:
    """The files laptops uploaded as config snapshots, keyed by target, newest
    upload first. Two laptops can hold the same target (each has its own home
    dir): the newest snapshot wins, and a change to it goes to that laptop. The
    text is secret-redacted; `base_blob` is the real file's blob, which the
    applier checks."""

    def __init__(self, files: dict[str, dict]):
        self.files = files   # target -> {"text", "base_blob", "device_id"}

    def list(self) -> list[str]:
        return sorted(self.files)

    def open(self, name: str) -> SnapshotFile:
        alias, sep, path = name.partition(":")
        if not sep or not ALIAS.match(alias):
            raise Refused(f"{name!r} is not <project>:<path>")
        if not allowed(path):
            raise Refused(f"{path!r} is not a CLAUDE.md, AGENTS.md or skill file; the Gardener may not change it")
        if entry := self.files.get(name):
            return SnapshotFile(name, path, entry["text"], entry["base_blob"], entry["device_id"])
        # A new file: in a project a laptop has shown, or in home.
        devices = [e["device_id"] for t, e in self.files.items() if t.partition(":")[0] == alias]
        if not devices and alias == HOME:
            devices = [e["device_id"] for e in self.files.values()]
        if not devices:
            raise Refused(f"no project {alias!r} in the files the laptops uploaded")
        return SnapshotFile(name, path, None, None, devices[0])
