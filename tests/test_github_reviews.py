import json

import pytest

from code_review_mcp.github_reviews import (
    ADD_FILE_THREAD,
    DraftComment,
    GitHubRequestError,
    GitHubReviewWriter,
    PostedComment,
    failure_message,
    file_thread_request,
    line_comment_payload,
    match_postings,
    pending_review_payload,
)
from code_review_mcp.procs import CommandResult
from code_review_mcp.store import ThreadPosting
from code_review_mcp.worktrees import Hunk, parse_hunks

from .pr_fixtures import FakeGh

REVIEWS = "repos/o/r/pulls/7/reviews"

SINGLE = DraftComment("t1", "app.py", "one line", 12, "additions", None, None)
LEFT = DraftComment("t2", "app.py", "deleted line", 4, "deletions", None, None)
RANGE = DraftComment("t3", "lib/x.py", "a range", 9, "additions", 7, "additions")
CROSS = DraftComment("t4", "lib/x.py", "both sides", 5, "additions", 3, "deletions")
FILE = DraftComment("t5", "data.bin", "whole file", 0, "additions", None, None)


def test_line_comment_payloads() -> None:
    assert line_comment_payload(SINGLE) == {
        "path": "app.py",
        "body": "one line",
        "line": 12,
        "side": "RIGHT",
    }
    assert line_comment_payload(LEFT)["side"] == "LEFT"
    assert line_comment_payload(RANGE) == {
        "path": "lib/x.py",
        "body": "a range",
        "line": 9,
        "side": "RIGHT",
        "start_line": 7,
        "start_side": "RIGHT",
    }
    assert line_comment_payload(CROSS) == {
        "path": "lib/x.py",
        "body": "both sides",
        "line": 5,
        "side": "RIGHT",
        "start_line": 3,
        "start_side": "LEFT",
    }


def test_pending_payload_leaves_file_comments_to_graphql() -> None:
    payload = pending_review_payload("abc123", [SINGLE, FILE, LEFT])

    assert payload == {
        "commit_id": "abc123",
        "comments": [line_comment_payload(SINGLE), line_comment_payload(LEFT)],
    }
    assert "event" not in payload
    assert file_thread_request("PRR_9", FILE) == {
        "query": ADD_FILE_THREAD,
        "variables": {"review": "PRR_9", "path": "data.bin", "body": "whole file"},
    }
    assert "subjectType: FILE" in ADD_FILE_THREAD


def test_parse_hunks() -> None:
    diff = (
        "diff --git a/a b/a\n--- a/a\n+++ b/a\n"
        "@@ -1,3 +1,4 @@ def f():\n context\n-old\n+new\n+more\n context\n"
        "@@ -20 +21 @@\n-x\n+y\n"
        "@@ -0,0 +1,2 @@\n+a\n+b\n"
        " @@ not a header\n"
    )

    assert parse_hunks(diff) == [Hunk(1, 3, 1, 4), Hunk(20, 1, 21, 1), Hunk(0, 0, 1, 2)]
    assert list(Hunk(0, 0, 1, 2).old_lines) == []


def test_failure_message() -> None:
    unprocessable = CommandResult(
        (),
        1,
        b'{"message": "Unprocessable Entity", "errors": ["pull_request_review_thread.line '
        b'must be part of the diff", {"message": "second"}], "status": "422"}',
        b"gh: Unprocessable Entity (HTTP 422)",
    )
    graphql = CommandResult((), 1, b'{"errors": [{"message": "Path not found"}]}', b"gh: oops")
    plain = CommandResult((), 1, b"", b"gh: network down")

    assert failure_message(unprocessable) == (
        "GitHub returned HTTP 422: Unprocessable Entity; "
        "pull_request_review_thread.line must be part of the diff; second"
    )
    assert failure_message(graphql) == "GitHub request failed: Path not found"
    assert failure_message(plain) == "GitHub request failed: gh: network down"


def _posted(comment_id: int, **fields: object) -> PostedComment:
    values: dict[str, object] = {
        "path": "app.py",
        "body": "one line",
        "line": 12,
        "side": "RIGHT",
        "start_line": None,
        "start_side": None,
        "subject_type": "line",
        "html_url": f"https://github.com/o/r/pull/7#discussion_r{comment_id}",
    }
    values.update(fields)
    return PostedComment(comment_id=comment_id, **values)  # type: ignore[arg-type]


def test_match_postings_pairs_by_path_position_and_body() -> None:
    posted = [
        _posted(4, subject_type="file"),
        _posted(3, path="data.bin", body="whole file", line=1, side="RIGHT", subject_type="file"),
        _posted(2, path="lib/x.py", body="a range\r\n", line=9, start_line=7, start_side="RIGHT"),
        _posted(1),
    ]

    postings = match_postings([SINGLE, RANGE, FILE, LEFT], posted)

    assert postings == [
        ThreadPosting("t1", 1, "https://github.com/o/r/pull/7#discussion_r1"),
        ThreadPosting("t3", 2, "https://github.com/o/r/pull/7#discussion_r2"),
        ThreadPosting("t5", 3, "https://github.com/o/r/pull/7#discussion_r3"),
        ThreadPosting("t2", None, None),
    ]


def _review(review_id: int, state: str) -> bytes:
    return json.dumps(
        {
            "id": review_id,
            "node_id": f"PRR_{review_id}",
            "state": state,
            "html_url": f"https://github.com/o/r/pull/7#pullrequestreview-{review_id}",
        }
    ).encode()


async def test_writer_creates_pending_adds_file_comments_and_submits() -> None:
    fake = FakeGh()
    fake.on("POST", REVIEWS, stdout=_review(41, "PENDING"))
    fake.on("graphql", stdout=b'{"data": {"addPullRequestReviewThread": {"thread": {}}}}')
    fake.on(f"{REVIEWS}/41/events", stdout=_review(41, "COMMENTED"))
    writer = GitHubReviewWriter(fake)

    submitted = await writer.submit_review(
        "o/r", 7, commit_id="abc", event="COMMENT", body="summary", comments=[SINGLE, FILE]
    )

    assert (submitted.review_id, submitted.state) == (41, "COMMENTED")
    assert [call[:4] for call in fake.calls] == [
        ("gh", "api", "-X", "POST"),
        ("gh", "api", "graphql", "--input"),
        ("gh", "api", "-X", "POST"),
    ]
    pending, file_thread, events = (json.loads(s or b"") for s in fake.stdins)
    assert pending == pending_review_payload("abc", [SINGLE, FILE])
    assert file_thread["variables"] == {
        "review": "PRR_41",
        "path": "data.bin",
        "body": "whole file",
    }
    assert events == {"event": "COMMENT", "body": "summary"}


async def test_writer_leaves_out_a_blank_body() -> None:
    fake = FakeGh()
    fake.on("POST", REVIEWS, stdout=_review(43, "PENDING"))
    fake.on(f"{REVIEWS}/43/events", stdout=_review(43, "APPROVED"))

    await GitHubReviewWriter(fake).submit_review(
        "o/r", 7, commit_id="abc", event="APPROVE", body=" ", comments=[]
    )

    assert json.loads(fake.stdins_with(f"{REVIEWS}/43/events")[0] or b"") == {"event": "APPROVE"}


async def test_writer_deletes_the_pending_review_when_a_later_step_fails() -> None:
    fake = FakeGh()
    fake.on("POST", REVIEWS, stdout=_review(42, "PENDING"))
    fake.on(
        "graphql",
        code=1,
        stdout=b'{"errors": [{"message": "Path could not be resolved"}]}',
        stderr=b"gh: Path could not be resolved",
    )
    fake.on("DELETE", f"{REVIEWS}/42", stdout=_review(42, "PENDING"))
    writer = GitHubReviewWriter(fake)

    with pytest.raises(GitHubRequestError, match="Path could not be resolved"):
        await writer.submit_review(
            "o/r", 7, commit_id="abc", event="COMMENT", body="x", comments=[FILE]
        )

    assert fake.calls_with("DELETE", f"{REVIEWS}/42")
    assert not fake.calls_with(f"{REVIEWS}/42/events")


async def test_writer_reports_a_422_without_creating_anything() -> None:
    fake = FakeGh()
    fake.on(
        "POST",
        REVIEWS,
        code=1,
        stdout=b'{"message": "Unprocessable Entity", "errors": ["Line could not be resolved"]}',
        stderr=b"gh: Unprocessable Entity (HTTP 422)",
    )
    writer = GitHubReviewWriter(fake)

    with pytest.raises(GitHubRequestError) as caught:
        await writer.submit_review(
            "o/r", 7, commit_id="abc", event="COMMENT", body="x", comments=[SINGLE]
        )

    assert caught.value.status == 422
    assert "Line could not be resolved" in str(caught.value)
    assert len(fake.calls) == 1


async def test_review_comments_reads_every_page_and_keeps_one_review() -> None:
    fake = FakeGh()
    page1 = [
        {"id": 1, "pull_request_review_id": 41, "path": "a.py", "body": "x", "line": 3},
        {"id": 9, "pull_request_review_id": 40, "path": "a.py", "body": "older review"},
    ]
    page2 = [
        {
            "id": 2,
            "pull_request_review_id": 41,
            "path": "b.py",
            "body": "y",
            "line": None,
            "original_line": 8,
            "side": "LEFT",
        },
        {
            "id": 3,
            "pull_request_review_id": 41,
            "path": "c.bin",
            "body": "z",
            "subject_type": "file",
            "line": 1,
        },
    ]
    fake.on("--paginate", "--slurp", stdout=json.dumps([page1, page2]).encode())

    comments = await GitHubReviewWriter(fake).review_comments("o/r", 7, 41)

    assert [(c.comment_id, c.path, c.line, c.subject_type) for c in comments] == [
        (1, "a.py", 3, None),
        (2, "b.py", 8, None),
        (3, "c.bin", 1, "file"),
    ]
    assert "repos/o/r/pulls/7/comments?per_page=100" in fake.calls[0]
