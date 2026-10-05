import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from code_review_mcp.errors import ReviewError
from code_review_mcp.github import GH_TIMEOUT_SECONDS
from code_review_mcp.procs import CommandResult, CommandRunner, run_command
from code_review_mcp.store import Side, SubmissionEvent, ThreadPosting

logger = logging.getLogger(__name__)

GITHUB_SIDE: dict[Side, str] = {"additions": "RIGHT", "deletions": "LEFT"}

ADD_FILE_THREAD = """
mutation($review: ID!, $path: String!, $body: String!) {
  addPullRequestReviewThread(input: {
    pullRequestReviewId: $review, path: $path, body: $body, subjectType: FILE
  }) { thread { id } }
}
"""

_GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")


class GitHubRequestError(ReviewError):
    def __init__(self, message: str, status: int | None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class DraftComment:
    thread_id: str
    path: str
    body: str
    line: int
    side: Side
    start_line: int | None
    start_side: Side | None

    @property
    def is_file_level(self) -> bool:
        return self.line == 0


@dataclass(frozen=True)
class SubmittedReview:
    review_id: int
    html_url: str | None
    state: str


@dataclass(frozen=True)
class PostedComment:
    comment_id: int
    path: str
    body: str
    line: int | None
    side: str | None
    start_line: int | None
    start_side: str | None
    subject_type: str | None
    html_url: str | None


class _GhModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class _GhReview(_GhModel):
    id: int
    node_id: str = ""
    state: str = ""
    html_url: str | None = None


class _GhReviewComment(_GhModel):
    id: int
    pull_request_review_id: int | None = None
    path: str
    body: str = ""
    line: int | None = None
    original_line: int | None = None
    side: str | None = None
    start_line: int | None = None
    original_start_line: int | None = None
    start_side: str | None = None
    subject_type: str | None = None
    html_url: str | None = None


_REVIEW = TypeAdapter(_GhReview)
_COMMENT_PAGES = TypeAdapter(list[list[_GhReviewComment]])


def line_comment_payload(comment: DraftComment) -> dict[str, object]:
    """One `comments[]` item of GitHub's create-review request (line or range comments)."""
    payload: dict[str, object] = {
        "path": comment.path,
        "body": comment.body,
        "line": comment.line,
        "side": GITHUB_SIDE[comment.side],
    }
    if comment.start_line is not None:
        payload["start_line"] = comment.start_line
        payload["start_side"] = GITHUB_SIDE[comment.start_side or comment.side]
    return payload


def pending_review_payload(commit_id: str, comments: Sequence[DraftComment]) -> dict[str, object]:
    """The request that creates a pending review holding every line and range comment.

    File comments are not in it: GitHub's create-review `comments[]` has no subject_type,
    so they are added to the pending review through GraphQL (see file_thread_request).
    """
    return {
        "commit_id": commit_id,
        "comments": [line_comment_payload(c) for c in comments if not c.is_file_level],
    }


def file_thread_request(review_node_id: str, comment: DraftComment) -> dict[str, object]:
    """The GraphQL request that adds one file comment to a pending review."""
    return {
        "query": ADD_FILE_THREAD,
        "variables": {"review": review_node_id, "path": comment.path, "body": comment.body},
    }


def failure_message(result: CommandResult) -> str:
    """GitHub's error text from a failed `gh api` call, with the HTTP status if gh gave one."""
    status = _HTTP_STATUS.search(result.stderr_text)
    parts: list[str] = []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if isinstance(data.get("message"), str):
            parts.append(data["message"])
        for error in data.get("errors") or []:
            if isinstance(error, str):
                parts.append(error)
            elif isinstance(error, dict) and isinstance(error.get("message"), str):
                parts.append(error["message"])
    if not parts:
        parts.append(result.stderr_text or f"exit status {result.returncode}")
    prefix = f"GitHub returned HTTP {status[1]}" if status else "GitHub request failed"
    return f"{prefix}: {'; '.join(parts)}"


def _normalized(body: str) -> str:
    return body.replace("\r\n", "\n").strip()


def _matches(draft: DraftComment, posted: PostedComment) -> bool:
    if posted.path != draft.path or _normalized(posted.body) != _normalized(draft.body):
        return False
    if draft.is_file_level:
        return posted.subject_type == "file"
    if posted.subject_type == "file":
        return False
    start_side = GITHUB_SIDE[draft.start_side] if draft.start_side else None
    return (
        posted.line == draft.line
        and posted.side == GITHUB_SIDE[draft.side]
        and posted.start_line == draft.start_line
        and (draft.start_line is None or posted.start_side == start_side)
    )


def match_postings(
    drafts: Sequence[DraftComment], posted: Sequence[PostedComment]
) -> list[ThreadPosting]:
    """Pair each draft with the GitHub comment that has its path, position, and body.

    A draft with no match is still returned, with no GitHub ids.
    """
    remaining = list(posted)
    postings: list[ThreadPosting] = []
    for draft in drafts:
        match = next((p for p in remaining if _matches(draft, p)), None)
        if match is None:
            postings.append(ThreadPosting(draft.thread_id, None, None))
            continue
        remaining.remove(match)
        postings.append(ThreadPosting(draft.thread_id, match.comment_id, match.html_url))
    return postings


class GitHubReviewWriter:
    """Submits PR reviews through `gh api`. The only code that writes to GitHub."""

    def __init__(self, runner: CommandRunner = run_command) -> None:
        self._runner = runner

    async def _api(self, args: Sequence[str], payload: Mapping[str, object] | None) -> bytes:
        result = await self._runner(
            ["gh", "api", *args, *(["--input", "-"] if payload is not None else [])],
            timeout=GH_TIMEOUT_SECONDS,
            env=_GH_ENV,
            stdin=json.dumps(payload).encode() if payload is not None else None,
        )
        if not result.ok:
            status = _HTTP_STATUS.search(result.stderr_text)
            raise GitHubRequestError(failure_message(result), int(status[1]) if status else None)
        return result.stdout

    async def submit_review(
        self,
        repo: str,
        number: int,
        *,
        commit_id: str,
        event: SubmissionEvent,
        body: str,
        comments: Sequence[DraftComment],
    ) -> SubmittedReview:
        """Post one review of `commit_id` with every comment, then submit it as `event`.

        Creates a pending review with the line comments, adds each file comment to it, and
        submits it. If a step after the first fails, the pending review is deleted, so
        nothing stays on GitHub. Raises GitHubRequestError with GitHub's message.
        """
        reviews = f"repos/{repo}/pulls/{number}/reviews"
        pending = _validate(
            _REVIEW,
            await self._api(["-X", "POST", reviews], pending_review_payload(commit_id, comments)),
        )
        try:
            for comment in comments:
                if comment.is_file_level:
                    await self._api(["graphql"], file_thread_request(pending.node_id, comment))
            submitted = _validate(
                _REVIEW,
                await self._api(
                    ["-X", "POST", f"{reviews}/{pending.id}/events"],
                    {"event": event, "body": body},
                ),
            )
        except ReviewError as error:
            await self._delete_pending(reviews, pending.id, error)
            raise
        return SubmittedReview(
            review_id=submitted.id, html_url=submitted.html_url, state=submitted.state
        )

    async def _delete_pending(self, reviews: str, review_id: int, cause: ReviewError) -> None:
        try:
            await self._api(["-X", "DELETE", f"{reviews}/{review_id}"], None)
        except ReviewError as e:
            logger.warning("could not delete pending review %d after %s: %s", review_id, cause, e)

    async def review_comments(self, repo: str, number: int, review_id: int) -> list[PostedComment]:
        """The comments of one submitted review, from every page of the PR's review comments.

        GitHub's per-review comments endpoint returns no line, side, or subject_type, so this
        reads the PR's comments and keeps those of `review_id`.
        """
        raw = await self._api(
            ["--paginate", "--slurp", f"repos/{repo}/pulls/{number}/comments?per_page=100"],
            None,
        )
        return [
            PostedComment(
                comment_id=c.id,
                path=c.path,
                body=c.body,
                line=c.line if c.line is not None else c.original_line,
                side=c.side,
                start_line=c.start_line if c.start_line is not None else c.original_start_line,
                start_side=c.start_side,
                subject_type=c.subject_type,
                html_url=c.html_url,
            )
            for page in _validate(_COMMENT_PAGES, raw)
            for c in page
            if c.pull_request_review_id == review_id
        ]


def _validate[T](adapter: TypeAdapter[T], raw: bytes) -> T:
    try:
        return adapter.validate_json(raw)
    except ValidationError as e:
        raise GitHubRequestError(f"Unexpected output from gh: {e}", None) from e
