import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from code_review_mcp.config import Settings
from code_review_mcp.errors import NotFoundError, ReviewError
from code_review_mcp.github import GitHubClient
from code_review_mcp.hub import ReviewHub
from code_review_mcp.pr_service import PrService
from code_review_mcp.service import ReviewService
from code_review_mcp.store import Store
from code_review_mcp.worktrees import WorktreeManager

from .conftest import SAMPLE_DIFF
from .pr_fixtures import (
    APP_V1,
    APP_V2,
    APP_V3,
    PR_NUMBER,
    RENAMED_BODY,
    REPO,
    FakeGh,
    PrRepo,
    git,
    make_pr_repo,
    mapped_config,
    pr_view_json,
)

PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"


def _plain(value: object) -> Any:
    return json.loads(json.dumps(value))


async def _viewed_paths(prs: PrService, review_id: str) -> set[str]:
    view = _plain(await prs.get_pr_view(review_id))
    return {f["path"] for f in view["files"] if f["viewed"]}


@pytest.fixture
def pr_repo(tmp_path: Path) -> PrRepo:
    return make_pr_repo(tmp_path / "git")


@pytest.fixture
def fake_gh(pr_repo: PrRepo) -> FakeGh:
    fake = FakeGh()
    fake.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    fake.on(
        "search",
        "prs",
        pr_repo.head_sha,
        stdout=json.dumps(
            [
                {
                    "number": PR_NUMBER,
                    "repository": {"name": "widgets", "nameWithOwner": REPO},
                    "url": PR_URL,
                    "state": "open",
                    "updatedAt": "2026-10-05T00:00:00Z",
                }
            ]
        ).encode(),
    )
    return fake


@pytest.fixture
def prs(
    store: Store, hub: ReviewHub, settings: Settings, pr_repo: PrRepo, fake_gh: FakeGh
) -> PrService:
    mapped_config(settings.home, pr_repo)
    return PrService(
        store,
        hub,
        settings,
        GitHubClient(fake_gh),
        WorktreeManager(settings.home, credential_helper=None),
    )


async def test_open_pr_returns_the_same_review_for_every_ref_form(
    prs: PrService, store: Store, settings: Settings, pr_repo: PrRepo
) -> None:
    by_url = await prs.open_pr(PR_URL)
    by_number = await prs.open_pr(f"#{PR_NUMBER}")
    by_repo_number = await prs.open_pr(f"ACME/widgets#{PR_NUMBER}")
    by_sha = await prs.open_pr(pr_repo.head_sha)

    review_ids = {o.review.id for o in (by_url, by_number, by_repo_number, by_sha)}
    assert len(review_ids) == 1
    assert by_url.url == settings.review_url(by_url.review.id)
    assert by_sha.note is None
    assert len(store.list_reviews()) == 1

    review = by_sha.review
    assert review.kind == "pr"
    assert (review.repo, review.pr_number) == (REPO, PR_NUMBER)
    assert review.title == "Make x two"
    assert review.author == "octocat"
    assert review.url == PR_URL
    assert (review.base_ref, review.head_ref) == ("main", "pr")
    assert (review.base_sha, review.head_sha) == (pr_repo.base_sha, pr_repo.head_sha)
    assert review.merge_base_sha == pr_repo.fork_sha
    assert (review.pr_body, review.pr_state, review.is_draft) == ("Changes x.", "open", False)
    assert review.worktree_path == str(settings.home / "worktrees" / "acme-widgets-1")
    assert git(Path(review.worktree_path), "rev-parse", "HEAD") == pr_repo.head_sha
    assert not pr_repo.hook_marker.exists()


async def test_pr_view_lists_files_and_github_metadata(prs: PrService) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id

    view = _plain(await prs.get_pr_view(review_id))

    files = {f["path"]: f for f in view["files"]}
    assert sorted(files) == ["app.py", "data.bin", "gone.txt", "new_name.py"]
    assert files["new_name.py"]["status"] == "renamed"
    assert files["new_name.py"]["old_path"] == "old_name.py"
    assert files["data.bin"]["binary"] is True
    assert files["app.py"]["viewed"] is False
    github = view["github"]
    assert isinstance(github, dict)
    assert github["checks_state"] == "pending"
    assert github["review_requests"] == ["ryan", "acme/eng"]
    assert view["status"] == "open"
    assert view["is_draft"] is False


async def test_get_file_contents(prs: PrService) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id

    modified = await prs.get_file(review_id, "app.py")
    renamed = await prs.get_file(review_id, "new_name.py")
    deleted = await prs.get_file(review_id, "gone.txt")
    binary = await prs.get_file(review_id, "data.bin")

    assert modified["old_content"] == APP_V1
    assert modified["new_content"] == APP_V2
    assert modified["language"] == "python"
    assert (modified["binary"], modified["too_large"]) == (False, False)
    assert renamed["old_path"] == "old_name.py"
    assert renamed["old_content"] == RENAMED_BODY
    assert renamed["new_content"] == RENAMED_BODY + "line 30\n"
    assert (deleted["old_content"], deleted["new_content"]) == ("bye\n", None)
    assert binary["binary"] is True
    assert (binary["old_content"], binary["new_content"]) == (None, None)
    with pytest.raises(NotFoundError, match="not a changed file"):
        await prs.get_file(review_id, "base_only.txt")


async def test_viewed_state_is_per_head(prs: PrService, pr_repo: PrRepo, fake_gh: FakeGh) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id

    await prs.set_viewed(review_id, "app.py", True)
    await prs.set_viewed(review_id, "app.py", True)
    await prs.set_viewed(review_id, "gone.txt", True)
    await prs.set_viewed(review_id, "gone.txt", False)
    assert await _viewed_paths(prs, review_id) == {"app.py"}
    with pytest.raises(NotFoundError):
        await prs.set_viewed(review_id, "nope.py", True)

    pr_repo.push_new_head(APP_V3)
    fake_gh.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    await prs.refresh(review_id)

    assert await _viewed_paths(prs, review_id) == set()


async def test_refresh_detects_head_move_and_publishes(
    prs: PrService, hub: ReviewHub, pr_repo: PrRepo, fake_gh: FakeGh
) -> None:
    opened = await prs.open_pr(PR_URL)
    review_id = opened.review.id
    queue = hub.subscribe(review_id)
    old_head = pr_repo.head_sha

    unchanged = await prs.refresh(review_id)
    assert unchanged.head_moved is False
    assert queue.empty()

    new_head = pr_repo.push_new_head(APP_V3)
    fake_gh.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    moved = await prs.refresh(review_id)

    assert (moved.head_moved, moved.old_head_sha, moved.new_head_sha) == (True, old_head, new_head)
    assert moved.review.id == review_id
    assert moved.review.head_sha == new_head
    event = json.loads(await asyncio.wait_for(queue.get(), 1))
    assert event == {
        "type": "head_moved",
        "review_id": review_id,
        "old_head_sha": old_head,
        "new_head_sha": new_head,
    }
    assert (await prs.get_file(review_id, "app.py"))["new_content"] == APP_V3
    assert git(Path(opened.review.worktree_path or ""), "rev-parse", "HEAD") == new_head


async def test_close_then_reopen_keeps_review_id(
    prs: PrService, store: Store, pr_repo: PrRepo, settings: Settings
) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id
    worktree = settings.home / "worktrees" / "acme-widgets-1"

    await prs.close(review_id)
    await prs.close(review_id)

    closed = store.get_review(review_id)
    assert closed is not None
    assert (closed.status, closed.worktree_path) == ("closed", None)
    assert not worktree.exists()
    assert git(pr_repo.clone, "for-each-ref", "refs/code-review-mcp") == ""
    assert (await prs.get_pr_view(review_id))["files"] == []
    with pytest.raises(ReviewError, match="closed"):
        await prs.get_file(review_id, "app.py")
    with pytest.raises(ReviewError, match="closed"):
        await prs.refresh(review_id)

    reopened = await prs.open_pr(f"#{PR_NUMBER}")

    assert reopened.review.id == review_id
    assert reopened.review.status == "open"
    assert git(worktree, "rev-parse", "HEAD") == pr_repo.head_sha
    assert not pr_repo.hook_marker.exists()


async def test_inbox_links_opened_prs(prs: PrService, fake_gh: FakeGh) -> None:
    item = {
        "author": {"login": "octocat"},
        "isDraft": False,
        "number": PR_NUMBER,
        "repository": {"name": "widgets", "nameWithOwner": REPO},
        "title": "Make x two",
        "updatedAt": "2026-10-05T00:00:00Z",
        "url": PR_URL,
    }
    other = {**item, "number": 2, "url": f"https://github.com/{REPO}/pull/2"}
    fake_gh.on("--review-requested=@me", stdout=json.dumps([item, other]).encode())
    review_id = (await prs.open_pr(PR_URL)).review.id

    inbox = _plain(await prs.inbox())

    links = {i["number"]: i["review_id"] for i in inbox["items"]}
    assert links == {PR_NUMBER: review_id, 2: None}


async def test_local_and_unknown_reviews_are_rejected(
    prs: PrService, service: ReviewService
) -> None:
    local = service.open_diff(SAMPLE_DIFF, "Local", "")

    with pytest.raises(ReviewError, match="not a PR review"):
        await prs.get_pr_view(local.id)
    with pytest.raises(NotFoundError):
        await prs.get_review("nope")
    with pytest.raises(NotFoundError):
        await prs.close("nope")


async def test_get_review_includes_threads(prs: PrService, store: Store) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id
    store.create_thread(
        review_id=review_id,
        kind="question",
        path="app.py",
        side="additions",
        line=2,
        status="submitted",
        author="user",
        body="why two?",
    )

    summary = _plain(await prs.get_review(review_id))

    assert summary["thread_counts"] == {"submitted": 1}
    [thread] = summary["threads"]
    assert thread["first_message"] == "why two?"
    assert thread["message_count"] == 1
    assert summary["worktree_path"]


async def test_missing_worktree_is_reported_and_refresh_recreates_it(
    prs: PrService, settings: Settings
) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id
    worktree = settings.home / "worktrees" / "acme-widgets-1"
    shutil.rmtree(worktree)

    with pytest.raises(ReviewError, match="worktree .* is missing"):
        await prs.get_file(review_id, "app.py")
    refreshed = await prs.refresh(review_id)

    assert refreshed.head_moved is False
    assert (await prs.get_file(review_id, "app.py"))["new_content"] == APP_V2
