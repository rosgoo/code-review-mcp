import asyncio
import re
import shlex
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from code_review_mcp.errors import ReviewError
from code_review_mcp.procs import CommandResult, CommandRunner, run_command

GIT_TIMEOUT_SECONDS = 60.0
FETCH_TIMEOUT_SECONDS = 600.0
CHECKOUT_TIMEOUT_SECONDS = 600.0
CLONE_TIMEOUT_SECONDS = 1800.0
MAX_TEXT_BYTES = 2 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192
REF_NAMESPACE = "refs/code-review-mcp"
REFS_PREFIX = f"{REF_NAMESPACE}/pull"

_GIT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GIT_LITERAL_PATHSPECS": "1"}
_NO_LAZY_FETCH_ENV = {"GIT_NO_LAZY_FETCH": "1"}

FileStatus = Literal[
    "added", "modified", "deleted", "renamed", "copied", "type_changed", "unmerged", "unknown"
]
_STATUS_BY_LETTER: dict[str, FileStatus] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
    "T": "type_changed",
    "U": "unmerged",
}


class GitError(ReviewError):
    pass


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: FileStatus
    old_path: str | None
    additions: int | None
    deletions: int | None

    @property
    def binary(self) -> bool:
        return self.additions is None or self.deletions is None


@dataclass(frozen=True)
class BlobContent:
    text: str | None
    size: int
    binary: bool
    too_large: bool


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def old_lines(self) -> range:
        return range(self.old_start, self.old_start + self.old_count)

    @property
    def new_lines(self) -> range:
        return range(self.new_start, self.new_start + self.new_count)


_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text: str) -> list[Hunk]:
    """The hunks of a unified diff, from its `@@ -a,b +c,d @@` headers (a missing count is 1)."""
    hunks: list[Hunk] = []
    for line in diff_text.splitlines():
        match = _HUNK_HEADER.match(line)
        if match:
            hunks.append(
                Hunk(
                    old_start=int(match[1]),
                    old_count=int(match[2]) if match[2] is not None else 1,
                    new_start=int(match[3]),
                    new_count=int(match[4]) if match[4] is not None else 1,
                )
            )
    return hunks


@dataclass(frozen=True)
class PreparedWorktree:
    path: Path
    head_sha: str
    merge_base_sha: str


class PullRefs(NamedTuple):
    head: str
    base: str


class _LineCounts(NamedTuple):
    additions: int | None
    deletions: int | None


class _NameStatus(NamedTuple):
    letter: str
    path: str
    old_path: str | None


def github_https_url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def gh_credential_helper() -> str | None:
    """A git credential helper that asks `gh` for GitHub credentials, if `gh` is installed."""
    gh = shutil.which("gh")
    return f"!{shlex.quote(gh)} auth git-credential" if gh else None


def pull_refs(number: int) -> PullRefs:
    prefix = f"{REFS_PREFIX}/{number}"
    return PullRefs(head=f"{prefix}/head", base=f"{prefix}/base")


class Git:
    """Runs git with repository hooks disabled, terminal prompts off, and literal pathspecs."""

    def __init__(self, runner: CommandRunner = run_command) -> None:
        self._runner = runner

    async def run(
        self,
        cwd: Path,
        *args: str,
        timeout: float = GIT_TIMEOUT_SECONDS,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        """Run `git -C cwd args`. Raises GitError on a non-zero exit when `check` is set."""
        result = await self._runner(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(cwd), *args],
            timeout=timeout,
            env={**_GIT_ENV, **(env or {})},
        )
        if check and not result.ok:
            raise GitError(result.describe_failure())
        return result

    async def output(self, cwd: Path, *args: str) -> str:
        return (await self.run(cwd, *args)).stdout_text.strip()


def _parse_name_status(output: str) -> list[_NameStatus]:
    tokens = output.split("\0")
    entries: list[_NameStatus] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        letter = tokens[i][0]
        if letter in "RC":
            entries.append(_NameStatus(letter=letter, path=tokens[i + 2], old_path=tokens[i + 1]))
            i += 3
        else:
            entries.append(_NameStatus(letter=letter, path=tokens[i + 1], old_path=None))
            i += 2
    return entries


def _parse_numstat(output: str) -> dict[str, _LineCounts]:
    tokens = output.split("\0")
    counts: dict[str, _LineCounts] = {}
    i = 0
    while i < len(tokens) and tokens[i]:
        added, deleted, path = tokens[i].split("\t", 2)
        i += 1
        if not path:
            path = tokens[i + 1]
            i += 2
        counts[path] = _LineCounts(
            additions=None if added == "-" else int(added),
            deletions=None if deleted == "-" else int(deleted),
        )
    return counts


class WorktreeManager:
    """One detached git worktree per PR, under `<home>/worktrees/<owner>-<name>-<number>`.

    A repo uses its mapped clone, or a blobless clone under `<home>/clones/<owner>/<name>`.
    Fetches write only to `refs/code-review-mcp/pull/<n>/{head,base}`; branches, tags, and
    remote-tracking refs are not touched. Git writes to one clone are serialized.
    """

    def __init__(
        self,
        home: Path,
        *,
        runner: CommandRunner = run_command,
        remote_url: Callable[[str], str] = github_https_url,
        credential_helper: str | None = None,
    ) -> None:
        self._home = home
        self._git = Git(runner)
        self._remote_url = remote_url
        self._credential_helper = credential_helper
        self._locks: dict[Path, asyncio.Lock] = {}

    def worktree_path(self, repo: str, number: int) -> Path:
        owner, name = repo.split("/", 1)
        return self._home / "worktrees" / f"{owner}-{name}-{number}"

    def clone_path(self, repo: str, mapped_clone: Path | None) -> Path:
        if mapped_clone is not None:
            return mapped_clone
        owner, name = repo.split("/", 1)
        return self._home / "clones" / owner / name

    def _lock(self, clone: Path) -> asyncio.Lock:
        return self._locks.setdefault(clone.resolve(), asyncio.Lock())

    async def _is_repository(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        return (await self._git.run(path, "rev-parse", "--git-dir", check=False)).ok

    async def _ensure_clone(self, repo: str, mapped_clone: Path | None) -> Path:
        clone = self.clone_path(repo, mapped_clone)
        if mapped_clone is not None:
            if not await self._is_repository(clone):
                raise GitError(f"The [repos] clone for {repo}, {clone}, is not a git repository")
            return clone
        async with self._lock(clone):
            if await self._is_repository(clone):
                return clone
            partial = clone.with_name(f"{clone.name}.partial")
            for leftover in (partial, clone):
                if leftover.exists():
                    shutil.rmtree(leftover)
            clone.parent.mkdir(parents=True, exist_ok=True)
            helper_config = (
                [
                    "--config",
                    "credential.helper=",
                    "--config",
                    f"credential.helper={self._credential_helper}",
                ]
                if self._credential_helper
                else []
            )
            await self._git.run(
                clone.parent,
                "clone",
                "--filter=blob:none",
                "--no-checkout",
                *helper_config,
                "--",
                self._remote_url(repo),
                str(partial),
                timeout=CLONE_TIMEOUT_SECONDS,
            )
            partial.rename(clone)
        return clone

    async def _registered_worktrees(self, clone: Path) -> set[Path]:
        output = await self._git.output(clone, "worktree", "list", "--porcelain", "-z")
        return {
            Path(token.removeprefix("worktree ")).resolve()
            for token in output.split("\0")
            if token.startswith("worktree ")
        }

    async def _has_commit(self, repo_dir: Path, sha: str) -> bool:
        result = await self._git.run(
            repo_dir, "cat-file", "-e", f"{sha}^{{commit}}", check=False, env=_NO_LAZY_FETCH_ENV
        )
        return result.ok

    async def _fetch(self, clone: Path, number: int, base_ref: str) -> None:
        refs = pull_refs(number)
        await self._git.run(
            clone,
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "--no-auto-maintenance",
            "--refmap=",
            "origin",
            f"+refs/pull/{number}/head:{refs.head}",
            f"+refs/heads/{base_ref}:{refs.base}",
            timeout=FETCH_TIMEOUT_SECONDS,
        )

    async def merge_base(self, repo_dir: Path, base_sha: str, head_sha: str) -> str:
        result = await self._git.run(repo_dir, "merge-base", base_sha, head_sha, check=False)
        sha = result.stdout_text.strip()
        if not result.ok or not sha:
            raise GitError(
                f"No merge base between base {base_sha} and head {head_sha}: "
                f"{result.stderr_text or 'unrelated histories'}"
            )
        return sha

    async def prepare(
        self,
        repo: str,
        number: int,
        *,
        mapped_clone: Path | None,
        base_ref: str,
        base_sha: str,
        head_sha: str,
    ) -> PreparedWorktree:
        """Make the PR's worktree exist at `head_sha` and compute the merge base.

        Fetches the PR head and base branch unless the worktree is already at `head_sha` and
        `base_sha` is present. Moves an existing worktree with `checkout --detach --force`.
        Re-creates the worktree if its directory is gone. Other worktrees of the clone are
        never touched. Raises GitError if a git command fails or a commit is missing after
        the fetch.
        """
        clone = await self._ensure_clone(repo, mapped_clone)
        path = self.worktree_path(repo, number)
        async with self._lock(clone):
            registered = path.resolve() in await self._registered_worktrees(clone)
            present = registered and path.is_dir()
            current = await self._git.output(path, "rev-parse", "HEAD") if present else None
            if current != head_sha or not await self._has_commit(clone, base_sha):
                await self._fetch(clone, number, base_ref)
                for sha, role in ((head_sha, "head"), (base_sha, "base")):
                    if not await self._has_commit(clone, sha):
                        raise GitError(
                            f"PR {role} commit {sha} is missing after the fetch. "
                            "The branch may have been force-pushed; refresh to retry."
                        )
            if present:
                if current != head_sha:
                    await self._git.run(
                        path,
                        "checkout",
                        "--detach",
                        "--force",
                        head_sha,
                        timeout=CHECKOUT_TIMEOUT_SECONDS,
                    )
            else:
                if path.exists():
                    shutil.rmtree(path)
                path.parent.mkdir(parents=True, exist_ok=True)
                await self._git.run(
                    clone,
                    "worktree",
                    "add",
                    *(["--force"] if registered else []),
                    "--detach",
                    str(path),
                    head_sha,
                    timeout=CHECKOUT_TIMEOUT_SECONDS,
                )
            merge_base_sha = await self.merge_base(clone, base_sha, head_sha)
        return PreparedWorktree(path=path, head_sha=head_sha, merge_base_sha=merge_base_sha)

    async def changed_files(self, repo_dir: Path, base: str, head: str) -> list[ChangedFile]:
        """Files changed from `base` to `head`, with renames detected, in git's path order."""
        diff = ("diff", "--no-ext-diff", "--no-textconv", "-M", "-z")
        name_status, numstat = await asyncio.gather(
            self._git.run(repo_dir, *diff, "--name-status", base, head),
            self._git.run(repo_dir, *diff, "--numstat", base, head),
        )
        counts = _parse_numstat(numstat.stdout_text)
        files: list[ChangedFile] = []
        for letter, path, old_path in _parse_name_status(name_status.stdout_text):
            count = counts.get(path, _LineCounts(additions=0, deletions=0))
            files.append(
                ChangedFile(
                    path=path,
                    status=_STATUS_BY_LETTER.get(letter, "unknown"),
                    old_path=old_path,
                    additions=count.additions,
                    deletions=count.deletions,
                )
            )
        return files

    async def diff_hunks(
        self, repo_dir: Path, base: str, head: str, paths: Sequence[str]
    ) -> list[Hunk]:
        """The hunks of `git diff -U3 -M base head -- paths`, with 3 lines of context as in a
        GitHub PR diff. Pass both paths of a renamed file so git pairs them."""
        result = await self._git.run(
            repo_dir,
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "-U3",
            "-M",
            base,
            head,
            "--",
            *paths,
        )
        return parse_hunks(result.stdout_text)

    async def tree_entries(self, repo_dir: Path, sha: str, paths: Sequence[str]) -> dict[str, str]:
        """Map each of `paths` that exists at commit `sha` to its object id."""
        if not paths:
            return {}
        listing = await self._git.output(repo_dir, "ls-tree", "-z", sha, "--", *paths)
        entries: dict[str, str] = {}
        for token in listing.split("\0"):
            if not token:
                continue
            meta, _, entry_path = token.partition("\t")
            entries[entry_path] = meta.split()[2]
        return entries

    async def read_blob(self, repo_dir: Path, sha: str, path: str) -> BlobContent | None:
        """Read `path` at commit `sha`. Returns None if the path is not a file at that commit.

        A file over MAX_TEXT_BYTES is reported too large and not read. A file with a NUL byte
        in its first BINARY_SNIFF_BYTES is reported binary. Other bytes decode as UTF-8, with
        invalid sequences replaced. A submodule reads as "Subproject commit <sha>".
        """
        listing = await self._git.output(repo_dir, "ls-tree", "-l", "-z", sha, "--", path)
        entry = listing.split("\0", 1)[0]
        if not entry:
            return None
        meta, _, entry_path = entry.partition("\t")
        _mode, object_type, object_id, size_text = meta.split()
        if entry_path != path:
            return None
        if object_type == "commit":
            text = f"Subproject commit {object_id}\n"
            return BlobContent(text=text, size=len(text), binary=False, too_large=False)
        if object_type != "blob":
            return None
        size = int(size_text)
        if size > MAX_TEXT_BYTES:
            return BlobContent(text=None, size=size, binary=False, too_large=True)
        data = (await self._git.run(repo_dir, "cat-file", "blob", object_id)).stdout
        if b"\0" in data[:BINARY_SNIFF_BYTES]:
            return BlobContent(text=None, size=size, binary=True, too_large=False)
        return BlobContent(
            text=data.decode("utf-8", errors="replace"), size=size, binary=False, too_large=False
        )

    async def remove(self, repo: str, number: int, *, mapped_clone: Path | None) -> None:
        """Remove the PR's worktree, its registration, and its namespaced refs.

        A registration whose directory is gone is cleared too. A missing worktree is not an
        error. Other worktrees of the clone are never touched.
        """
        clone = self.clone_path(repo, mapped_clone)
        path = self.worktree_path(repo, number)
        if not await self._is_repository(clone):
            if path.exists():
                shutil.rmtree(path)
            return
        async with self._lock(clone):
            if path.resolve() in await self._registered_worktrees(clone):
                await self._git.run(clone, "worktree", "remove", "--force", "--force", str(path))
            if path.exists():
                shutil.rmtree(path)
            for ref in pull_refs(number):
                await self._git.run(clone, "update-ref", "-d", ref)
