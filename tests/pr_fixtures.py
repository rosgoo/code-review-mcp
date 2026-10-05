import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from code_review_mcp.procs import CommandResult

REPO = "acme/widgets"
PR_NUMBER = 1

APP_V1 = "import os\nx = 1\nprint(x)\n"
APP_V2 = "import os\nx = 2\nprint(x)\n"
APP_V3 = "import os\nx = 3\nprint(x)\n"
RENAMED_BODY = "".join(f"line {i}\n" for i in range(30))
BINARY_BODY = b"\x89PNG\x00\x01\x02\x03"

HOOK_SCRIPT = '#!/bin/sh\necho "$0" >> "{marker}"\n'


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _commit(work: Path, message: str) -> str:
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", message)
    return git(work, "rev-parse", "HEAD")


@dataclass
class PrRepo:
    """A bare 'GitHub' remote with a base branch and refs/pull/1/head, plus a local clone.

    History: fork (on main) -> base (main tip) and fork -> head (refs/pull/1/head).
    The PR renames old_name.py to new_name.py, edits app.py, adds data.bin, and
    deletes gone.txt. The clone has hooks that append to `hook_marker` when they run.
    """

    root: Path
    work: Path
    remote: Path
    clone: Path
    hook_marker: Path
    fork_sha: str
    base_sha: str
    head_sha: str

    def push_new_head(self, content: str) -> str:
        git(self.work, "checkout", "-q", "pr")
        (self.work / "app.py").write_text(content)
        sha = _commit(self.work, "update PR")
        git(self.work, "push", "-q", "--force", "origin", f"pr:refs/pull/{PR_NUMBER}/head")
        self.head_sha = sha
        return sha

    def advance_base(self) -> str:
        git(self.work, "checkout", "-q", "main")
        (self.work / "later.txt").write_text("later\n")
        sha = _commit(self.work, "advance main")
        git(self.work, "push", "-q", "origin", "main")
        self.base_sha = sha
        return sha


def make_pr_repo(root: Path) -> PrRepo:
    work = root / "work"
    remote = root / "remote.git"
    clone = root / "clone"
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "--bare", "-b", "main", str(remote))
    git(remote, "config", "uploadpack.allowFilter", "true")
    git(root, "init", "-q", "-b", "main", str(work))
    git(work, "config", "user.email", "test@example.com")
    git(work, "config", "user.name", "Test")
    git(work, "config", "commit.gpgsign", "false")
    git(work, "remote", "add", "origin", str(remote))

    (work / "app.py").write_text(APP_V1)
    (work / "old_name.py").write_text(RENAMED_BODY)
    (work / "gone.txt").write_text("bye\n")
    fork_sha = _commit(work, "fork point")

    git(work, "checkout", "-q", "-b", "pr")
    (work / "app.py").write_text(APP_V2)
    (work / "old_name.py").rename(work / "new_name.py")
    (work / "new_name.py").write_text(RENAMED_BODY + "line 30\n")
    (work / "data.bin").write_bytes(BINARY_BODY)
    (work / "gone.txt").unlink()
    head_sha = _commit(work, "the PR")

    git(work, "checkout", "-q", "main")
    (work / "base_only.txt").write_text("base\n")
    base_sha = _commit(work, "main moves on")

    git(work, "push", "-q", "origin", "main")
    git(work, "push", "-q", "origin", f"pr:refs/pull/{PR_NUMBER}/head")
    git(root, "clone", "-q", str(remote), str(clone))

    hooks = root / "hooks"
    hooks.mkdir()
    marker = root / "hook-ran.log"
    for name in ("post-checkout", "reference-transaction", "pre-auto-gc"):
        hook = hooks / name
        hook.write_text(HOOK_SCRIPT.format(marker=marker))
        hook.chmod(0o755)
    git(clone, "config", "core.hooksPath", str(hooks))

    return PrRepo(
        root=root,
        work=work,
        remote=remote,
        clone=clone,
        hook_marker=marker,
        fork_sha=fork_sha,
        base_sha=base_sha,
        head_sha=head_sha,
    )


def pr_view_json(
    repo: PrRepo,
    *,
    number: int = PR_NUMBER,
    repo_name: str = REPO,
    state: str = "OPEN",
) -> bytes:
    return json.dumps(
        {
            "number": number,
            "title": "Make x two",
            "body": "Changes x.",
            "author": {"login": "octocat", "is_bot": False},
            "url": f"https://github.com/{repo_name}/pull/{number}",
            "state": state,
            "isDraft": False,
            "baseRefName": "main",
            "baseRefOid": repo.base_sha,
            "headRefName": "pr",
            "headRefOid": repo.head_sha,
            "files": [{"path": "app.py", "additions": 1, "deletions": 1, "changeType": "MODIFIED"}],
            "additions": 3,
            "deletions": 2,
            "reviewDecision": "",
            "reviewRequests": [
                {"__typename": "User", "login": "ryan"},
                {"__typename": "Team", "name": "eng", "slug": "acme/eng"},
            ],
            "statusCheckRollup": [
                {
                    "__typename": "CheckRun",
                    "name": "Lint",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                    "detailsUrl": "https://ci.example/lint",
                    "workflowName": "PR",
                },
                {
                    "__typename": "StatusContext",
                    "context": "deploy",
                    "state": "PENDING",
                    "targetUrl": "https://ci.example/deploy",
                },
            ],
        }
    ).encode()


class _Rule(NamedTuple):
    tokens: frozenset[str]
    result: CommandResult


@dataclass
class FakeGh:
    """A CommandRunner for `gh`. The newest rule whose tokens all appear in the args answers."""

    rules: list[_Rule] = field(default_factory=list)
    calls: list[tuple[str, ...]] = field(default_factory=list)

    def on(self, *tokens: str, stdout: bytes = b"[]", stderr: bytes = b"", code: int = 0) -> None:
        result = CommandResult(args=(), returncode=code, stdout=stdout, stderr=stderr)
        self.rules.insert(0, _Rule(frozenset(tokens), result))

    async def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        call = tuple(args)
        self.calls.append(call)
        for tokens, result in self.rules:
            if tokens <= set(call):
                return CommandResult(
                    args=call,
                    returncode=result.returncode,
                    stdout=result.stdout,
                    stderr=result.stderr,
                )
        raise AssertionError(f"unexpected gh call: {call}")

    def calls_with(self, *tokens: str) -> list[tuple[str, ...]]:
        return [call for call in self.calls if set(tokens) <= set(call)]


def write_config(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(body)


def mapped_config(home: Path, repo: PrRepo) -> None:
    write_config(home, f'default_repo = "{REPO}"\n\n[repos]\n"{REPO}" = "{repo.clone}"\n')
