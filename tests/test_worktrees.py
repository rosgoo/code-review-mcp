import shutil
from pathlib import Path

import pytest

from code_review_mcp import worktrees as worktrees_module
from code_review_mcp.worktrees import (
    ChangedFile,
    GitError,
    WorktreeManager,
    pull_refs,
)

from .pr_fixtures import (
    APP_V2,
    APP_V3,
    BINARY_BODY,
    PR_NUMBER,
    RENAMED_BODY,
    REPO,
    PrRepo,
    git,
    make_pr_repo,
)


@pytest.fixture
def pr_repo(tmp_path: Path) -> PrRepo:
    return make_pr_repo(tmp_path / "git")


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def manager(home: Path) -> WorktreeManager:
    return WorktreeManager(home, credential_helper=None)


async def _prepare(manager: WorktreeManager, repo: PrRepo, mapped: Path | None) -> Path:
    prepared = await manager.prepare(
        REPO,
        PR_NUMBER,
        mapped_clone=mapped,
        base_ref="main",
        base_sha=repo.base_sha,
        head_sha=repo.head_sha,
    )
    assert prepared.head_sha == repo.head_sha
    assert prepared.merge_base_sha == repo.fork_sha
    return prepared.path


def _worktree_paths(clone: Path) -> list[Path]:
    output = git(clone, "worktree", "list", "--porcelain")
    return [
        Path(line.removeprefix("worktree ")).resolve()
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]


def test_fixture_hooks_fire_for_plain_git(pr_repo: PrRepo) -> None:
    git(pr_repo.clone, "checkout", "-q", "--detach", "HEAD")
    assert "post-checkout" in pr_repo.hook_marker.read_text()


async def test_worktree_lifecycle_in_mapped_clone(
    manager: WorktreeManager, pr_repo: PrRepo, home: Path
) -> None:
    clone = pr_repo.clone
    origin_main = git(clone, "rev-parse", "refs/remotes/origin/main")
    branches = git(clone, "for-each-ref", "refs/heads", "refs/remotes")
    pr_repo.advance_base()
    git(pr_repo.work, "tag", "v-later", "main")
    git(pr_repo.work, "push", "-q", "origin", "v-later")

    path = await _prepare(manager, pr_repo, clone)

    assert path == home / "worktrees" / "acme-widgets-1"
    assert git(path, "rev-parse", "HEAD") == pr_repo.head_sha
    assert (path / "app.py").read_text() == APP_V2
    assert path.resolve() in _worktree_paths(clone)
    refs = pull_refs(PR_NUMBER)
    assert git(clone, "rev-parse", refs.head) == pr_repo.head_sha
    assert git(clone, "rev-parse", refs.base) == pr_repo.base_sha
    assert git(clone, "rev-parse", "refs/remotes/origin/main") == origin_main
    assert git(clone, "for-each-ref", "refs/heads", "refs/remotes") == branches
    assert git(clone, "tag", "--list") == ""
    assert not (clone / ".git" / "FETCH_HEAD").exists()

    new_head = pr_repo.push_new_head(APP_V3)
    moved = await manager.prepare(
        REPO,
        PR_NUMBER,
        mapped_clone=clone,
        base_ref="main",
        base_sha=pr_repo.base_sha,
        head_sha=new_head,
    )

    assert moved.path == path
    assert git(path, "rev-parse", "HEAD") == new_head
    assert (path / "app.py").read_text() == APP_V3
    assert moved.merge_base_sha == pr_repo.fork_sha

    await manager.remove(REPO, PR_NUMBER, mapped_clone=clone)

    assert not path.exists()
    assert _worktree_paths(clone) == [clone.resolve()]
    assert git(clone, "for-each-ref", "refs/code-review-mcp") == ""
    assert not pr_repo.hook_marker.exists()


async def test_prepare_skips_fetch_when_head_is_current(
    manager: WorktreeManager, pr_repo: PrRepo
) -> None:
    path = await _prepare(manager, pr_repo, pr_repo.clone)
    git(pr_repo.clone, "update-ref", "-d", pull_refs(PR_NUMBER).head)

    again = await _prepare(manager, pr_repo, pr_repo.clone)

    assert again == path
    assert git(pr_repo.clone, "for-each-ref", pull_refs(PR_NUMBER).head) == ""


async def test_changed_files_rename_binary_and_delete(
    manager: WorktreeManager, pr_repo: PrRepo
) -> None:
    path = await _prepare(manager, pr_repo, pr_repo.clone)

    files = await manager.changed_files(path, pr_repo.fork_sha, pr_repo.head_sha)

    assert sorted(files, key=lambda f: f.path) == [
        ChangedFile(path="app.py", status="modified", old_path=None, additions=1, deletions=1),
        ChangedFile(path="data.bin", status="added", old_path=None, additions=None, deletions=None),
        ChangedFile(path="gone.txt", status="deleted", old_path=None, additions=0, deletions=1),
        ChangedFile(
            path="new_name.py", status="renamed", old_path="old_name.py", additions=1, deletions=0
        ),
    ]
    assert [f.path for f in files if f.binary] == ["data.bin"]


async def test_read_blob(
    manager: WorktreeManager, pr_repo: PrRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = await _prepare(manager, pr_repo, pr_repo.clone)

    old = await manager.read_blob(path, pr_repo.fork_sha, "old_name.py")
    binary = await manager.read_blob(path, pr_repo.head_sha, "data.bin")
    missing = await manager.read_blob(path, pr_repo.head_sha, "old_name.py")
    nested_missing = await manager.read_blob(path, pr_repo.head_sha, "no/such/file.py")
    monkeypatch.setattr(worktrees_module, "MAX_TEXT_BYTES", 10)
    large = await manager.read_blob(path, pr_repo.head_sha, "app.py")

    assert old is not None and old.text == RENAMED_BODY and not old.binary
    assert binary is not None and binary.binary and binary.text is None
    assert binary.size == len(BINARY_BODY)
    assert missing is None
    assert nested_missing is None
    assert large is not None and large.too_large and large.text is None


async def test_blobless_clone_for_unmapped_repo(pr_repo: PrRepo, home: Path) -> None:
    manager = WorktreeManager(
        home, remote_url=lambda _repo: pr_repo.remote.as_uri(), credential_helper=None
    )

    path = await _prepare(manager, pr_repo, None)

    clone = home / "clones" / "acme" / "widgets"
    assert git(clone, "config", "remote.origin.partialclonefilter") == "blob:none"
    assert git(path, "rev-parse", "HEAD") == pr_repo.head_sha
    assert (path / "app.py").read_text() == APP_V2
    old = await manager.read_blob(path, pr_repo.fork_sha, "gone.txt")
    assert old is not None and old.text == "bye\n"

    await manager.remove(REPO, PR_NUMBER, mapped_clone=None)

    assert not path.exists()
    assert _worktree_paths(clone) == [clone.resolve()]


async def test_stale_worktree_directory_is_replaced(
    manager: WorktreeManager, pr_repo: PrRepo
) -> None:
    path = manager.worktree_path(REPO, PR_NUMBER)
    path.mkdir(parents=True)
    (path / "junk.txt").write_text("left over")

    prepared = await _prepare(manager, pr_repo, pr_repo.clone)

    assert prepared == path
    assert not (path / "junk.txt").exists()
    assert git(path, "rev-parse", "HEAD") == pr_repo.head_sha


async def test_errors(manager: WorktreeManager, pr_repo: PrRepo, tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()

    with pytest.raises(GitError, match="is not a git repository"):
        await _prepare(manager, pr_repo, not_a_repo)
    with pytest.raises(GitError, match="missing after the fetch"):
        await manager.prepare(
            REPO,
            PR_NUMBER,
            mapped_clone=pr_repo.clone,
            base_ref="main",
            base_sha=pr_repo.base_sha,
            head_sha="0" * 40,
        )
    with pytest.raises(GitError, match="refs/heads/no-such-branch"):
        await manager.prepare(
            REPO,
            PR_NUMBER,
            mapped_clone=pr_repo.clone,
            base_ref="no-such-branch",
            base_sha=pr_repo.base_sha,
            head_sha=pr_repo.head_sha,
        )


async def test_remove_without_worktree_is_a_no_op(
    manager: WorktreeManager, pr_repo: PrRepo
) -> None:
    await manager.remove(REPO, PR_NUMBER, mapped_clone=pr_repo.clone)
    await manager.remove(REPO, PR_NUMBER, mapped_clone=None)

    assert _worktree_paths(pr_repo.clone) == [pr_repo.clone.resolve()]


def _add_stale_worktree(clone: Path, path: Path) -> None:
    git(clone, "worktree", "add", "-q", "--detach", str(path), "HEAD")
    shutil.rmtree(path)
    assert path.resolve() in _worktree_paths(clone)


async def test_mapped_clone_keeps_unrelated_missing_worktrees(
    manager: WorktreeManager, pr_repo: PrRepo
) -> None:
    clone = pr_repo.clone
    unrelated = pr_repo.root / "someone-elses-worktree"
    _add_stale_worktree(clone, unrelated)

    path = await _prepare(manager, pr_repo, clone)

    assert unrelated.resolve() in _worktree_paths(clone)
    assert path.resolve() in _worktree_paths(clone)

    await manager.remove(REPO, PR_NUMBER, mapped_clone=clone)

    assert _worktree_paths(clone) == [clone.resolve(), unrelated.resolve()]


async def test_prepare_recovers_own_missing_worktree(
    manager: WorktreeManager, pr_repo: PrRepo
) -> None:
    path = await _prepare(manager, pr_repo, pr_repo.clone)
    shutil.rmtree(path)
    assert path.resolve() in _worktree_paths(pr_repo.clone)

    again = await _prepare(manager, pr_repo, pr_repo.clone)

    assert again == path
    assert git(path, "rev-parse", "HEAD") == pr_repo.head_sha
    assert (path / "app.py").read_text() == APP_V2
    assert _worktree_paths(pr_repo.clone) == [pr_repo.clone.resolve(), path.resolve()]


async def test_remove_clears_own_missing_entry(manager: WorktreeManager, pr_repo: PrRepo) -> None:
    path = await _prepare(manager, pr_repo, pr_repo.clone)
    shutil.rmtree(path)

    await manager.remove(REPO, PR_NUMBER, mapped_clone=pr_repo.clone)

    assert _worktree_paths(pr_repo.clone) == [pr_repo.clone.resolve()]
    assert git(pr_repo.clone, "for-each-ref", "refs/code-review-mcp") == ""
