import asyncio
import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from code_review_mcp.errors import ConflictError, InvalidPathError, NotFoundError
from code_review_mcp.hub import ReviewHub
from code_review_mcp.models import CommentRequest
from code_review_mcp.service import ReviewService
from code_review_mcp.store import ReviewFile, ReviewRow, Side, Store

from .conftest import SAMPLE_DIFF, SAMPLE_NEW_FILE

COMMENT_KEYS = {
    "id",
    "file_path",
    "line_number",
    "line_type",
    "line_content",
    "user_message",
    "timestamp",
    "status",
    "replies",
}


def _comment(
    review: ReviewRow,
    service: ReviewService,
    *,
    line: int = 2,
    side: Side = "additions",
    path: str = "app.py",
    message: str = "why 2?",
) -> str:
    request = CommentRequest(path=path, side=side, line=line, line_content="x = 2", body=message)
    return service.add_user_comment(review.id, request).id


def _view_files(view: Mapping[str, object]) -> list[dict[str, object]]:
    files = view["files"]
    assert isinstance(files, list)
    return files


def _statuses(service: ReviewService, review_id: str) -> dict[str, object]:
    return {str(c["id"]): c["status"] for c in service.comments(review_id)}


def test_local_comment_lifecycle(service: ReviewService) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Lifecycle", "")
    thread_id = _comment(review, service)

    assert _statuses(service, review.id) == {thread_id: "draft"}
    assert service.submitted_comments(review.id) == []

    assert service.submit(review.id) == 1
    submitted = service.submitted_comments(review.id)
    assert [c["id"] for c in submitted] == [thread_id]
    assert set(submitted[0]) == COMMENT_KEYS

    agent_reply = service.reply(thread_id, "agent", "fixed")
    assert agent_reply.reopened is False
    service.resolve(thread_id)
    assert _statuses(service, review.id) == {thread_id: "resolved"}
    assert service.submitted_comments(review.id) == []

    user_reply = service.reply(thread_id, "user", "not quite")
    assert user_reply.reopened is True
    assert _statuses(service, review.id) == {thread_id: "submitted"}

    second_reply = service.reply(thread_id, "user", "still there?")
    assert second_reply.reopened is False

    [comment] = service.submitted_comments(review.id)
    assert comment["user_message"] == "why 2?"
    assert [(r["author"], r["message"]) for r in comment["replies"]] == [  # type: ignore[attr-defined]
        ("claude", "fixed"),
        ("user", "not quite"),
        ("user", "still there?"),
    ]


def test_line_type_is_derived_from_side_and_current_diff(service: ReviewService) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Line types", "")
    added = _comment(review, service, line=2)
    context = _comment(review, service, line=1)
    deleted = _comment(review, service, line=2, side="deletions")
    overall = _comment(review, service, line=0, path="(overall)")

    line_types = {c["id"]: c["line_type"] for c in service.comments(review.id)}
    assert line_types == {added: "add", context: "context", deleted: "delete", overall: "context"}


async def test_wait_for_comments_wakes_on_submit(service: ReviewService) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Wait", "")
    thread_id = _comment(review, service)

    waiter = asyncio.create_task(service.wait_for_comments(review.id, timeout=5))
    await asyncio.sleep(0.05)
    assert not waiter.done()

    service.submit(review.id)
    result = await asyncio.wait_for(waiter, 1)

    assert result["status"] == "submitted"
    assert [c["id"] for c in result["comments"]] == [thread_id]  # type: ignore[attr-defined]


async def test_wait_for_comments_times_out(service: ReviewService) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Timeout", "")
    assert await service.wait_for_comments(review.id, timeout=0.05) == {"status": "timeout"}


async def test_submit_before_wait_is_kept_then_rearmed(service: ReviewService) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Early submit", "")
    _comment(review, service)
    service.submit(review.id)

    first = await service.wait_for_comments(review.id, timeout=0.05)
    second = await service.wait_for_comments(review.id, timeout=0.05)

    assert first["status"] == "submitted"
    assert second == {"status": "timeout"}


async def test_submit_on_one_review_does_not_wake_another(service: ReviewService) -> None:
    review_a = service.open_diff(SAMPLE_DIFF, "A", "")
    review_b = service.open_diff(SAMPLE_DIFF, "B", "")
    _comment(review_a, service, message="for A")
    _comment(review_b, service, message="for B")

    wait_a = asyncio.create_task(service.wait_for_comments(review_a.id, timeout=2))
    wait_b = asyncio.create_task(service.wait_for_comments(review_b.id, timeout=0.3))
    await asyncio.sleep(0.05)
    service.submit(review_a.id)

    result_a = await wait_a
    result_b = await wait_b

    assert result_a["status"] == "submitted"
    assert [c["user_message"] for c in result_a["comments"]] == ["for A"]  # type: ignore[attr-defined]
    assert result_b == {"status": "timeout"}
    assert service.submitted_comments(review_b.id) == []


async def test_events_reach_only_their_review(service: ReviewService, hub: ReviewHub) -> None:
    review_a = service.open_diff(SAMPLE_DIFF, "A", "")
    review_b = service.open_diff(SAMPLE_DIFF, "B", "")
    queue_a = hub.subscribe(review_a.id)
    queue_b = hub.subscribe(review_b.id)
    thread_id = _comment(review_a, service)
    draft_id = _comment(review_a, service, line=1)

    service.reply(thread_id, "agent", "hello")
    service.resolve(thread_id)
    service.delete_thread(draft_id)
    service.submit(review_a.id)
    service.update_diff(review_a.id, SAMPLE_DIFF)

    events = [json.loads(queue_a.get_nowait()) for _ in range(queue_a.qsize())]
    assert [e["type"] for e in events] == [
        "comment_added",
        "comment_added",
        "reply_added",
        "comment_resolved",
        "thread_deleted",
        "comments_submitted",
        "view_updated",
    ]
    assert {e["review_id"] for e in events} == {review_a.id}
    added = events[0]["comment"]
    assert (added["id"], added["status"], added["line_type"]) == (thread_id, "draft", "add")
    assert added["user_message"] == "why 2?"
    assert events[2]["comment_id"] == thread_id
    assert events[2]["reply"]["author"] == "claude"
    assert events[4]["comment_id"] == draft_id
    assert events[5]["count"] == 0
    assert queue_b.empty()

    hub.unsubscribe(review_a.id, queue_a)
    assert hub.subscriber_count(review_a.id) == 0


def test_range_comment_keeps_start_line_and_side(service: ReviewService) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Range", "")
    across = service.add_user_comment(
        review.id,
        CommentRequest(
            path="app.py", side="additions", line=2, start_line=2, start_side="deletions", body="a"
        ),
    )
    same_side = service.add_user_comment(
        review.id,
        CommentRequest(path="app.py", side="additions", line=3, start_line=1, body="b"),
    )
    no_range = service.add_user_comment(
        review.id,
        CommentRequest(path="app.py", side="additions", line=2, start_side="deletions", body="c"),
    )

    comments = {c["id"]: c for c in service.comments(review.id)}

    assert (comments[across.id]["start_line"], comments[across.id]["start_side"]) == (
        2,
        "deletions",
    )
    assert (comments[same_side.id]["start_line"], comments[same_side.id]["start_side"]) == (
        1,
        "additions",
    )
    assert set(comments[no_range.id]) == COMMENT_KEYS
    assert no_range.start_side is None


def test_delete_thread_only_while_draft(service: ReviewService) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Delete", "")
    submitted_id = _comment(review, service, message="sent")
    service.submit(review.id)
    draft_id = _comment(review, service, message="draft")
    service.reply(draft_id, "user", "a reply goes with it")

    service.delete_thread(draft_id)

    assert _statuses(service, review.id) == {submitted_id: "submitted"}
    with pytest.raises(ConflictError, match="submitted"):
        service.delete_thread(submitted_id)
    with pytest.raises(NotFoundError):
        service.delete_thread(draft_id)
    service.resolve(submitted_id)
    with pytest.raises(ConflictError, match="resolved"):
        service.delete_thread(submitted_id)


def test_open_diff_with_working_dir_builds_annotated_files(
    service: ReviewService, store: Store, repo_dir: Path
) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Annotated", str(repo_dir))

    assert review.mode == "files"
    assert review.working_dir == str(repo_dir.resolve())
    [file] = store.list_review_files(review.id)
    assert file.path == "app.py"
    assert file.added_lines == [2]
    assert file.deleted_content == {2: ["x = 1"]}
    view = service.view(review.id)
    assert view["mode"] == "files"
    assert view["diff"] == SAMPLE_DIFF
    [file_view] = _view_files(view)
    assert file_view["content"] == SAMPLE_NEW_FILE
    assert file_view["old_content"] == "import os\nx = 1\nprint(x)\n"


def test_annotated_view_returns_null_old_content_on_mismatch(
    service: ReviewService, repo_dir: Path
) -> None:
    (repo_dir / "app.py").write_text("import os\nx = 3\nprint(x)\n")
    review = service.open_diff(SAMPLE_DIFF, "Edited after diff", str(repo_dir))

    [file_view] = _view_files(service.view(review.id))

    assert file_view["old_content"] is None


def test_show_files_view_has_no_old_content(service: ReviewService) -> None:
    review = service.show_files(
        [ReviewFile(path="notes.md", content="# hi\n", language="markdown")], "Files"
    )

    view = service.view(review.id)

    assert "diff" not in view
    assert view["files"] == [{"path": "notes.md", "content": "# hi\n", "language": "markdown"}]


def test_open_diff_without_working_dir_uses_diff_mode(service: ReviewService) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Plain", "")

    assert review.mode == "diff"
    assert review.working_dir is None
    assert service.view(review.id) == {
        "review_id": review.id,
        "mode": "diff",
        "title": "Plain",
        "diff": SAMPLE_DIFF,
    }


def test_update_diff_keeps_threads(service: ReviewService, repo_dir: Path) -> None:
    review = service.open_diff(SAMPLE_DIFF, "Update", str(repo_dir))
    thread_id = _comment(review, service)
    (repo_dir / "app.py").write_text("import os\nx = 3\nprint(x)\n")
    new_diff = SAMPLE_DIFF.replace("+x = 2", "+x = 3")

    service.update_diff(review.id, new_diff)

    view = service.view(review.id)
    assert view["mode"] == "files"
    assert "x = 3" in view["files"][0]["content"]  # type: ignore[index]
    assert [c["id"] for c in service.comments(review.id)] == [thread_id]


def test_unknown_ids_raise_not_found(service: ReviewService) -> None:
    with pytest.raises(NotFoundError):
        service.view("missing")
    with pytest.raises(NotFoundError):
        service.submit("missing")
    with pytest.raises(NotFoundError):
        service.reply("missing", "agent", "hi")
    with pytest.raises(NotFoundError):
        service.resolve("missing")
    with pytest.raises(NotFoundError):
        service.delete_thread("missing")


def test_open_diff_rejects_relative_working_dir(service: ReviewService, store: Store) -> None:
    with pytest.raises(InvalidPathError, match="working_dir must be an absolute path"):
        service.open_diff(SAMPLE_DIFF, "Relative", "repo")
    assert store.list_reviews() == []
