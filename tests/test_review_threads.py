import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from code_review_mcp.config import Settings
from code_review_mcp.errors import ReviewError
from code_review_mcp.github import GitHubClient
from code_review_mcp.hub import ReviewHub
from code_review_mcp.pr_service import PrService
from code_review_mcp.review_rules import (
    Anchor,
    allowed_events,
    check_commentable,
    check_review_body,
    normalize_anchor,
)
from code_review_mcp.store import Store
from code_review_mcp.web import create_app
from code_review_mcp.worktrees import GitError, Hunk, WorktreeManager

from .conftest import BASE_URL, SAMPLE_DIFF
from .pr_fixtures import (
    APP_V3,
    PR_NUMBER,
    RENAMED_BODY,
    REPO,
    VIEWER_LOGIN,
    FakeGh,
    PrRepo,
    git,
    make_pr_repo,
    mapped_config,
    pr_view_json,
)

PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
REVIEWS = f"repos/{REPO}/pulls/{PR_NUMBER}/reviews"
THREAD_KEYS = {
    "id",
    "kind",
    "status",
    "path",
    "side",
    "line",
    "start_line",
    "start_side",
    "anchor_sha",
    "created_by",
    "created_at",
    "updated_at",
    "github_url",
    "messages",
}
SINGLE = {"path": "app.py", "side": "additions", "line": 2, "body": "why two?"}
LEFT = {"path": "app.py", "side": "deletions", "line": 2, "body": "why drop one?"}
RANGE = {
    "path": "new_name.py",
    "side": "additions",
    "start_line": 29,
    "line": 31,
    "body": "these three",
}
FILE = {"path": "data.bin", "line": 0, "side": "deletions", "start_line": 3, "body": "binary?"}


@pytest.fixture
def pr_repo(tmp_path: Path) -> PrRepo:
    return make_pr_repo(tmp_path / "git")


@pytest.fixture
def fake_gh(pr_repo: PrRepo) -> FakeGh:
    fake = FakeGh()
    fake.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    return fake


@pytest.fixture
def app(settings: Settings, store: Store, fake_gh: FakeGh, pr_repo: PrRepo) -> FastAPI:
    mapped_config(settings.home, pr_repo)
    return create_app(
        settings,
        store,
        github=GitHubClient(fake_gh),
        worktrees=WorktreeManager(settings.home, credential_helper=None),
    )


@pytest.fixture
async def api(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        yield client


@pytest.fixture
async def review_id(api: httpx.AsyncClient) -> str:
    response = await api.post("/api/prs/open", json={"ref": PR_URL})
    assert response.status_code == 200, response.text
    opened: str = response.json()["review_id"]
    return opened


async def _create(api: httpx.AsyncClient, review_id: str, fields: dict[str, Any]) -> Any:
    response = await api.post(
        f"/api/reviews/{review_id}/threads", json={"kind": "review_comment", **fields}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _review_json(review_id: int, state: str) -> bytes:
    return json.dumps(
        {
            "id": review_id,
            "node_id": f"PRR_{review_id}",
            "state": state,
            "html_url": f"https://github.com/{REPO}/pull/1#pullrequestreview-{review_id}",
        }
    ).encode()


def _comment_json(comment_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    side = {"additions": "RIGHT", "deletions": "LEFT"}
    file_level = fields["line"] == 0
    return {
        "id": comment_id,
        "pull_request_review_id": 900,
        "path": fields["path"],
        "body": fields["body"],
        "line": 1 if file_level else fields["line"],
        "side": "RIGHT" if file_level else side[fields["side"]],
        "start_line": fields.get("start_line") if not file_level else None,
        "start_side": side[fields["side"]] if fields.get("start_line") and not file_level else None,
        "subject_type": "file" if file_level else "line",
        "html_url": f"https://github.com/{REPO}/pull/1#discussion_r{comment_id}",
    }


def _github_accepts(fake: FakeGh, posted: list[dict[str, Any]]) -> None:
    fake.on("POST", REVIEWS, stdout=_review_json(900, "PENDING"))
    fake.on("graphql", "--input", stdout=b'{"data": {"addPullRequestReviewThread": {}}}')
    fake.on(f"{REVIEWS}/900/events", stdout=_review_json(900, "COMMENTED"))
    comments = [_comment_json(1000 + i, fields) for i, fields in enumerate(posted)]
    older = {**_comment_json(999, posted[0]), "pull_request_review_id": 800}
    fake.on(
        "--paginate",
        f"repos/{REPO}/pulls/{PR_NUMBER}/comments?per_page=100",
        stdout=json.dumps([[older], comments]).encode(),
    )


def _hunks(*spans: tuple[int, int, int, int]) -> list[Hunk]:
    return [Hunk(*span) for span in spans]


def test_rules() -> None:
    assert allowed_events("octocat", "OctoCat") == ["COMMENT"]
    assert allowed_events("reviewer", "octocat") == ["COMMENT", "APPROVE", "REQUEST_CHANGES"]
    assert allowed_events("reviewer", None) == ["COMMENT", "APPROVE", "REQUEST_CHANGES"]
    for event in ("COMMENT", "REQUEST_CHANGES"):
        with pytest.raises(ReviewError, match=f"A {event} review needs a body"):
            check_review_body(event, "  \n")
    check_review_body("APPROVE", "")
    check_review_body("COMMENT", "fine")

    assert normalize_anchor("deletions", 0, 4, "additions") == Anchor("additions", 0, None, None)
    assert normalize_anchor("additions", 5, 5, None) == Anchor("additions", 5, None, None)
    assert normalize_anchor("additions", 5, 3, None) == Anchor("additions", 5, 3, "additions")
    assert normalize_anchor("additions", 5, 3, "deletions") == Anchor(
        "additions", 5, 3, "deletions"
    )
    for args, message in [
        ((None, 5, None, None), "side is required"),
        (("additions", 5, 7, None), "must come before"),
        (("additions", -1, None, None), "line must be 0"),
        (("additions", 5, 0, None), "start_line must be a positive"),
    ]:
        with pytest.raises(ReviewError, match=message):
            normalize_anchor(*args)  # type: ignore[arg-type]

    hunks = _hunks((1, 5, 1, 6), (40, 4, 41, 7))
    check_commentable(hunks, Anchor("additions", 6, 2, "additions"), "a.py")
    check_commentable(hunks, Anchor("deletions", 43, None, None), "a.py")
    check_commentable(hunks, Anchor("additions", 45, 40, "deletions"), "a.py")
    check_commentable([], Anchor("additions", 0, None, None), "a.py")
    with pytest.raises(ReviewError, match="Line 7 on the additions side of a.py is outside"):
        check_commentable(hunks, Anchor("additions", 7, None, None), "a.py")
    with pytest.raises(ReviewError, match="not inside one diff hunk"):
        check_commentable(hunks, Anchor("additions", 42, 5, "additions"), "a.py")


async def test_commentable_ranges_from_the_real_diff(
    api: httpx.AsyncClient, review_id: str
) -> None:
    async def commentable(path: str) -> Any:
        response = await api.get(f"/api/reviews/{review_id}/file", params={"path": path})
        return response.json()["commentable"]

    assert await commentable("app.py") == {"additions": [[1, 3]], "deletions": [[1, 3]]}
    assert await commentable("new_name.py") == {"additions": [[28, 31]], "deletions": [[28, 30]]}
    assert await commentable("gone.txt") == {"additions": [], "deletions": [[1, 1]]}
    assert await commentable("data.bin") == {"additions": [], "deletions": []}


async def test_two_hunk_file_ranges_and_cross_hunk_range(
    api: httpx.AsyncClient, review_id: str, pr_repo: PrRepo, fake_gh: FakeGh
) -> None:
    git(pr_repo.work, "checkout", "-q", "pr")
    lines = (RENAMED_BODY + "line 30\n").splitlines(keepends=True)
    lines[1] = "line one changed\n"
    (pr_repo.work / "new_name.py").write_text("".join(lines))
    git(pr_repo.work, "commit", "-q", "-am", "edit near the top")
    git(pr_repo.work, "push", "-q", "--force", "origin", "pr:refs/pull/1/head")
    pr_repo.head_sha = git(pr_repo.work, "rev-parse", "HEAD")
    fake_gh.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    assert (await api.post(f"/api/reviews/{review_id}/refresh")).status_code == 200

    file = (await api.get(f"/api/reviews/{review_id}/file", params={"path": "new_name.py"})).json()
    across = await api.post(
        f"/api/reviews/{review_id}/threads",
        json={"kind": "review_comment", **RANGE, "start_line": 3, "line": 29},
    )

    assert file["commentable"] == {
        "additions": [[1, 5], [28, 31]],
        "deletions": [[1, 5], [28, 30]],
    }
    assert across.status_code == 400
    assert "not inside one diff hunk" in across.json()["error"]


async def test_create_and_list_threads(
    api: httpx.AsyncClient, review_id: str, pr_repo: PrRepo
) -> None:
    created = [await _create(api, review_id, fields) for fields in (SINGLE, LEFT, RANGE, FILE)]
    listed = (await api.get(f"/api/reviews/{review_id}/threads")).json()

    assert [set(t) for t in listed] == [THREAD_KEYS] * 4
    assert listed == created
    first = created[0]
    assert (first["kind"], first["status"], first["created_by"]) == (
        "review_comment",
        "draft",
        "user",
    )
    assert first["anchor_sha"] == pr_repo.head_sha
    assert first["github_url"] is None
    assert [m["body"] for m in first["messages"]] == ["why two?"]
    assert (created[2]["start_line"], created[2]["start_side"]) == (29, "additions")
    assert (created[3]["line"], created[3]["start_line"], created[3]["start_side"]) == (
        0,
        None,
        None,
    )
    view = (await api.get(f"/api/reviews/{review_id}/pr")).json()
    assert (view["draft_count"], view["stale_count"]) == (4, 0)


async def test_create_rejects_bad_comments(
    api: httpx.AsyncClient, app: FastAPI, review_id: str, store: Store
) -> None:
    local = app.state.service.open_diff(SAMPLE_DIFF, "Local", "")
    base = f"/api/reviews/{review_id}/threads"
    cases = {
        "outside the diff": (base, {**SINGLE, "path": "new_name.py", "line": 10}),
        "range outside": (base, {**RANGE, "start_line": 20}),
        "not in the PR": (base, {**SINGLE, "path": "base_only.txt"}),
        "no side": (base, {"path": "app.py", "line": 2, "body": "x"}),
        "empty body": (base, {**SINGLE, "body": "   "}),
        "local review": (f"/api/reviews/{local.id}/threads", SINGLE),
    }
    responses = {
        name: await api.post(url, json={"kind": "review_comment", **fields})
        for name, (url, fields) in cases.items()
    }
    wrong_kind = await api.post(base, json={"kind": "question", **SINGLE})

    assert {name: r.status_code for name, r in responses.items()} == dict.fromkeys(cases, 400)
    assert (
        "Line 10 on the additions side of new_name.py is outside the diff"
        in (responses["outside the diff"].json()["error"])
    )
    assert wrong_kind.status_code == 422
    assert store.list_threads(review_id) == []


async def test_viewer_and_allowed_events(
    api: httpx.AsyncClient, review_id: str, fake_gh: FakeGh
) -> None:
    view = (await api.get(f"/api/reviews/{review_id}/pr")).json()

    assert view["viewer"] == {"login": VIEWER_LOGIN, "is_author": False}
    assert view["allowed_events"] == ["COMMENT", "APPROVE", "REQUEST_CHANGES"]
    assert len(fake_gh.calls_with("api", "user")) == 1


async def test_own_pr_allows_only_comment(
    settings: Settings, store: Store, pr_repo: PrRepo, fake_gh: FakeGh
) -> None:
    fake_gh.on("api", "user", stdout=b'{"login": "OctoCat"}')
    mapped_config(settings.home, pr_repo)
    app = create_app(
        settings,
        store,
        github=GitHubClient(fake_gh),
        worktrees=WorktreeManager(settings.home, credential_helper=None),
    )
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as api:
        review_id = (await api.post("/api/prs/open", json={"ref": PR_URL})).json()["review_id"]
        view = (await api.get(f"/api/reviews/{review_id}/pr")).json()
        approve = await api.post(
            f"/api/reviews/{review_id}/submit-review", json={"event": "APPROVE", "body": "ok"}
        )
        changes = await api.post(
            f"/api/reviews/{review_id}/submit-review",
            json={"event": "REQUEST_CHANGES", "body": "no"},
        )

    assert view["viewer"] == {"login": "OctoCat", "is_author": True}
    assert view["allowed_events"] == ["COMMENT"]
    assert (approve.status_code, changes.status_code) == (403, 403)
    assert "only COMMENT" in approve.json()["error"]
    assert not fake_gh.calls_with("POST", REVIEWS)


async def test_submit_posts_every_draft_once(
    api: httpx.AsyncClient,
    app: FastAPI,
    review_id: str,
    store: Store,
    fake_gh: FakeGh,
    pr_repo: PrRepo,
) -> None:
    created = [await _create(api, review_id, fields) for fields in (SINGLE, LEFT, RANGE, FILE)]
    _github_accepts(fake_gh, [SINGLE, LEFT, RANGE, FILE])
    hub: ReviewHub = app.state.prs._hub
    queue = hub.subscribe(review_id)

    response = await api.post(
        f"/api/reviews/{review_id}/submit-review", json={"event": "COMMENT", "body": "Looks fine"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "github_review_id": 900,
        "html_url": f"https://github.com/{REPO}/pull/1#pullrequestreview-900",
        "posted": 4,
    }
    [pending_stdin] = fake_gh.stdins_with("POST", REVIEWS)
    assert json.loads(pending_stdin or b"") == {
        "commit_id": pr_repo.head_sha,
        "comments": [
            {"path": "app.py", "body": "why two?", "line": 2, "side": "RIGHT"},
            {"path": "app.py", "body": "why drop one?", "line": 2, "side": "LEFT"},
            {
                "path": "new_name.py",
                "body": "these three",
                "line": 31,
                "side": "RIGHT",
                "start_line": 29,
                "start_side": "RIGHT",
            },
        ],
    }
    [file_stdin] = fake_gh.stdins_with("graphql", "--input")
    assert json.loads(file_stdin or b"")["variables"] == {
        "review": "PRR_900",
        "path": "data.bin",
        "body": "binary?",
    }
    [events_stdin] = fake_gh.stdins_with(f"{REVIEWS}/900/events")
    assert json.loads(events_stdin or b"") == {"event": "COMMENT", "body": "Looks fine"}

    threads = {t.id: t for t in store.list_threads(review_id)}
    assert [threads[c["id"]].status for c in created] == ["posted"] * 4
    assert [threads[c["id"]].github_comment_id for c in created] == [1000, 1001, 1002, 1003]
    assert threads[created[0]["id"]].github_url == (
        f"https://github.com/{REPO}/pull/1#discussion_r1000"
    )
    [submission] = store.list_submissions(review_id)
    assert (submission.event, submission.github_review_id, submission.commit_id) == (
        "COMMENT",
        900,
        pr_repo.head_sha,
    )
    review = store.get_review(review_id)
    assert review is not None and review.last_reviewed_sha == pr_repo.head_sha
    events = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    assert events[-1]["type"] == "review_submitted"
    assert events[-1]["posted"] == 4
    listed = (await api.get(f"/api/reviews/{review_id}/threads")).json()
    assert listed[0]["github_url"].endswith("discussion_r1000")
    view = (await api.get(f"/api/reviews/{review_id}/pr")).json()
    assert view["draft_count"] == 0


async def test_github_422_maps_to_400_and_posts_nothing(
    api: httpx.AsyncClient, review_id: str, store: Store, fake_gh: FakeGh
) -> None:
    await _create(api, review_id, SINGLE)
    fake_gh.on(
        "POST",
        REVIEWS,
        code=1,
        stdout=b'{"message": "Unprocessable Entity", "errors": ["User can only have one '
        b'pending review per pull request"], "status": "422"}',
        stderr=b"gh: Unprocessable Entity (HTTP 422)",
    )

    response = await api.post(
        f"/api/reviews/{review_id}/submit-review", json={"event": "COMMENT", "body": "x"}
    )

    assert response.status_code == 400
    assert response.json()["error"] == (
        "GitHub returned HTTP 422: Unprocessable Entity; "
        "User can only have one pending review per pull request"
    )
    assert [t.status for t in store.list_threads(review_id)] == ["draft"]
    assert store.list_submissions(review_id) == []


async def test_body_rules_are_enforced_before_github(
    api: httpx.AsyncClient, review_id: str, fake_gh: FakeGh
) -> None:
    await _create(api, review_id, SINGLE)
    url = f"/api/reviews/{review_id}/submit-review"

    comment = await api.post(url, json={"event": "COMMENT", "body": ""})
    changes = await api.post(url, json={"event": "REQUEST_CHANGES", "body": " "})
    unknown = await api.post(url, json={"event": "MERGE", "body": "x"})

    assert (comment.status_code, changes.status_code, unknown.status_code) == (400, 400, 422)
    assert not fake_gh.calls_with("POST", REVIEWS)


async def test_stale_thread_blocks_submit(
    api: httpx.AsyncClient, review_id: str, store: Store, fake_gh: FakeGh
) -> None:
    stale = await _create(api, review_id, SINGLE)
    await _create(api, review_id, LEFT)
    store.mark_threads_stale([stale["id"]])

    response = await api.post(
        f"/api/reviews/{review_id}/submit-review", json={"event": "COMMENT", "body": "x"}
    )

    assert response.status_code == 409
    assert response.json()["stale_thread_ids"] == [stale["id"]]
    assert "stale" in response.json()["error"]
    assert not fake_gh.calls_with("POST", REVIEWS)


async def test_draft_on_an_old_head_is_marked_stale_at_submit(
    api: httpx.AsyncClient, review_id: str, store: Store
) -> None:
    thread = await _create(api, review_id, SINGLE)
    store.move_thread_anchor(thread["id"], "0" * 40)

    response = await api.post(
        f"/api/reviews/{review_id}/submit-review", json={"event": "COMMENT", "body": "x"}
    )

    assert response.status_code == 409
    assert response.json()["stale_thread_ids"] == [thread["id"]]
    row = store.get_thread(thread["id"])
    assert row is not None and row.status == "stale"


async def test_patch_body_rules(api: httpx.AsyncClient, review_id: str, store: Store) -> None:
    draft = await _create(api, review_id, SINGLE)
    stale = await _create(api, review_id, LEFT)
    posted = await _create(api, review_id, RANGE)
    store.mark_threads_stale([stale["id"]])
    store._conn.execute("UPDATE threads SET status = 'posted' WHERE id = ?", (posted["id"],))

    edited = await api.patch(f"/api/threads/{draft['id']}", json={"body": " better "})
    on_stale = await api.patch(f"/api/threads/{stale['id']}", json={"body": "x"})
    on_posted = await api.patch(f"/api/threads/{posted['id']}", json={"body": "x"})
    empty = await api.patch(f"/api/threads/{draft['id']}", json={})
    missing = await api.patch("/api/threads/nope", json={"body": "x"})

    assert edited.status_code == 200
    assert [m["body"] for m in edited.json()["messages"]] == ["better"]
    assert (on_stale.status_code, on_posted.status_code) == (409, 409)
    assert (empty.status_code, missing.status_code) == (400, 404)


async def test_patch_reanchor_rules(
    api: httpx.AsyncClient, review_id: str, store: Store, pr_repo: PrRepo
) -> None:
    stale = await _create(api, review_id, SINGLE)
    posted = await _create(api, review_id, LEFT)
    store.mark_threads_stale([stale["id"]])
    store.move_thread_anchor(stale["id"], "0" * 40)
    store._conn.execute("UPDATE threads SET status = 'posted' WHERE id = ?", (posted["id"],))
    url = f"/api/threads/{stale['id']}"

    outside = await api.patch(url, json={"side": "additions", "line": 9})
    after_outside = store.get_thread(stale["id"])
    moved = await api.patch(
        url, json={"side": "additions", "start_line": 1, "line": 3, "body": "now a range"}
    )
    on_posted = await api.patch(
        f"/api/threads/{posted['id']}", json={"side": "additions", "line": 1}
    )
    to_file = await api.patch(url, json={"line": 0})

    assert outside.status_code == 400
    assert after_outside is not None and after_outside.status == "stale"
    assert moved.status_code == 200
    body = moved.json()
    assert (body["status"], body["anchor_sha"]) == ("draft", pr_repo.head_sha)
    assert (body["side"], body["start_line"], body["line"]) == ("additions", 1, 3)
    assert body["messages"][0]["body"] == "now a range"
    assert on_posted.status_code == 409
    assert (to_file.json()["line"], to_file.json()["start_line"]) == (0, None)


async def test_delete_allows_draft_and_stale_only(
    api: httpx.AsyncClient, review_id: str, store: Store
) -> None:
    draft = await _create(api, review_id, SINGLE)
    stale = await _create(api, review_id, LEFT)
    posted = await _create(api, review_id, RANGE)
    store.mark_threads_stale([stale["id"]])
    store._conn.execute("UPDATE threads SET status = 'posted' WHERE id = ?", (posted["id"],))

    statuses = [
        (await api.delete(f"/api/threads/{t['id']}")).status_code for t in (draft, stale, posted)
    ]

    assert statuses == [200, 200, 409]
    assert [t.id for t in store.list_threads(review_id)] == [posted["id"]]


async def test_head_move_reanchors_unchanged_files_and_stales_the_rest(
    api: httpx.AsyncClient,
    app: FastAPI,
    review_id: str,
    store: Store,
    fake_gh: FakeGh,
    pr_repo: PrRepo,
) -> None:
    on_changed = await _create(api, review_id, SINGLE)
    on_unchanged = await _create(api, review_id, RANGE)
    file_level = await _create(api, review_id, FILE)
    posted = await _create(api, review_id, LEFT)
    store._conn.execute("UPDATE threads SET status = 'posted' WHERE id = ?", (posted["id"],))
    old_head = pr_repo.head_sha
    queue = app.state.prs._hub.subscribe(review_id)

    new_head = pr_repo.push_new_head(APP_V3)
    fake_gh.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    refreshed = (await api.post(f"/api/reviews/{review_id}/refresh")).json()

    rows = {t.id: t for t in store.list_threads(review_id)}
    assert refreshed["head_moved"] is True
    assert (rows[on_changed["id"]].status, rows[on_changed["id"]].anchor_sha) == (
        "stale",
        old_head,
    )
    for kept in (on_unchanged, file_level):
        assert (rows[kept["id"]].status, rows[kept["id"]].anchor_sha) == ("draft", new_head)
    assert (rows[posted["id"]].status, rows[posted["id"]].anchor_sha) == ("posted", old_head)
    events = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
    assert {"type": "threads_stale", "review_id": review_id, "thread_ids": [on_changed["id"]]} in (
        events
    )
    view = (await api.get(f"/api/reviews/{review_id}/pr")).json()
    assert (view["draft_count"], view["stale_count"]) == (2, 1)


async def test_head_move_stales_every_draft_when_the_old_head_is_unreadable(
    store: Store,
    hub: ReviewHub,
    settings: Settings,
    fake_gh: FakeGh,
    pr_repo: PrRepo,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mapped_config(settings.home, pr_repo)
    worktrees = WorktreeManager(settings.home, credential_helper=None)
    prs = PrService(store, hub, settings, GitHubClient(fake_gh), worktrees)
    review_id = (await prs.open_pr(PR_URL)).review.id
    thread = store.create_thread(
        review_id=review_id,
        kind="review_comment",
        path="new_name.py",
        side="additions",
        line=31,
        status="draft",
        author="user",
        body="x",
        anchor_sha=pr_repo.head_sha,
    )

    async def unreadable(*_args: object) -> dict[str, str]:
        raise GitError("bad object")

    monkeypatch.setattr(worktrees, "tree_entries", unreadable)
    pr_repo.push_new_head(APP_V3)
    fake_gh.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    await prs.refresh(review_id)

    row = store.get_thread(thread.id)
    assert row is not None and row.status == "stale"
