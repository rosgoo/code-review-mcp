import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from code_review_mcp.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ReviewError,
    StaleThreadsError,
)
from code_review_mcp.github import GitHubClient
from code_review_mcp.github_reviews import DraftComment, GitHubReviewWriter, match_postings
from code_review_mcp.hub import ReviewHub
from code_review_mcp.pr_service import PrService, ReadyPr
from code_review_mcp.review_rules import (
    Anchor,
    allowed_events,
    check_commentable,
    check_review_body,
    normalize_anchor,
)
from code_review_mcp.store import (
    Author,
    MessageRow,
    ReviewRow,
    Side,
    Store,
    SubmissionEvent,
    ThreadKind,
    ThreadRow,
)

logger = logging.getLogger(__name__)

REVIEW_COMMENT: ThreadKind = "review_comment"


@dataclass(frozen=True)
class AnchorRequest:
    side: Side | None
    line: int
    start_line: int | None
    start_side: Side | None


def serialize_thread(thread: ThreadRow, messages: Sequence[MessageRow]) -> dict[str, object]:
    return {
        "id": thread.id,
        "kind": thread.kind,
        "status": thread.status,
        "path": thread.path,
        "side": thread.side,
        "line": thread.line,
        "start_line": thread.start_line,
        "start_side": thread.start_side,
        "anchor_sha": thread.anchor_sha,
        "created_by": thread.created_by,
        "created_at": thread.created_at,
        "updated_at": thread.updated_at,
        "github_url": thread.github_url,
        "agent_error": thread.agent_error,
        "messages": [
            {
                "id": m.id,
                "author": m.author,
                "body": m.body,
                "created_at": m.created_at,
                "status": m.status,
            }
            for m in messages
        ],
    }


def _clean_body(body: str) -> str:
    cleaned = body.strip()
    if not cleaned:
        raise ReviewError("The comment body is empty")
    return cleaned


class ThreadService:
    """Draft review comments on a PR review, and the submit that posts them to GitHub."""

    def __init__(
        self,
        store: Store,
        hub: ReviewHub,
        prs: PrService,
        github: GitHubClient,
        writer: GitHubReviewWriter,
    ) -> None:
        self._store = store
        self._hub = hub
        self._prs = prs
        self._github = github
        self._writer = writer
        self._staged_listeners: list[Callable[[str], None]] = []

    def on_staged_change(self, listener: Callable[[str], None]) -> None:
        """Call `listener(review_id)` after a question or follow-up is staged or removed."""
        self._staged_listeners.append(listener)

    def _staged_changed(self, review_id: str) -> None:
        for listener in self._staged_listeners:
            listener(review_id)

    def _thread_json(self, thread_id: str) -> dict[str, object]:
        thread = self._require_thread(thread_id)
        return serialize_thread(thread, self._store.list_messages(thread_id))

    def _require_thread(self, thread_id: str) -> ThreadRow:
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise NotFoundError(f"Thread {thread_id!r} not found")
        return thread

    def list_threads(self, review_id: str) -> list[dict[str, object]]:
        """Every thread of the review, oldest first, with its messages."""
        if self._store.get_review(review_id) is None:
            raise NotFoundError(f"Review {review_id!r} not found")
        messages = self._store.messages_for_review(review_id)
        return [
            serialize_thread(t, messages.get(t.id, [])) for t in self._store.list_threads(review_id)
        ]

    async def _checked_anchor(self, pr: ReadyPr, path: str, request: AnchorRequest) -> Anchor:
        changed = await self._prs.changed_file(pr, path)
        if changed is None:
            raise ReviewError(
                f"{path!r} is not a changed file of this PR. A review comment must be on a "
                "file in the PR diff."
            )
        anchor = normalize_anchor(
            request.side, request.line, request.start_line, request.start_side
        )
        check_commentable(await self._prs.hunks(pr, changed), anchor, path)
        return anchor

    def _require_same_head(self, pr: ReadyPr) -> ReviewRow:
        current = self._prs.require_pr_review(pr.review.id)
        if current.head_sha != pr.head_sha:
            raise ConflictError(
                "The PR head moved while the comment was being saved. Reload the review and "
                "try again."
            )
        return current

    async def create_thread(
        self,
        review_id: str,
        path: str,
        request: AnchorRequest,
        body: str,
        *,
        created_by: Author = "user",
    ) -> dict[str, object]:
        """Create a draft review comment at the review's current head.

        Line 0 is a file comment. A line comment must be inside the diff (check_commentable).
        Raises ReviewError (400) for a local review, a bad position, or an empty body.
        """
        review = self._prs.require_pr_review(review_id)
        assert review.repo is not None and review.pr_number is not None
        cleaned = _clean_body(body)
        pr = await self._prs.ready_pr(review_id)
        async with self._prs.lock_for(review.repo, review.pr_number):
            self._require_same_head(pr)
            anchor = await self._checked_anchor(pr, path, request)
            thread = self._store.create_thread(
                review_id=review_id,
                kind=REVIEW_COMMENT,
                path=path,
                side=anchor.side,
                line=anchor.line,
                start_line=anchor.start_line,
                start_side=anchor.start_side,
                status="draft",
                author=created_by,
                body=cleaned,
                anchor_sha=pr.head_sha,
            )
        result = self._thread_json(thread.id)
        self._hub.publish(review_id, "thread_added", {"thread": result})
        return result

    def create_question(
        self, review_id: str, path: str, request: AnchorRequest, body: str
    ) -> dict[str, object]:
        """Stage a question for the review agent: a draft thread whose message is staged.

        Nothing is sent until the review agent's send. Any line may take a question. Line 0
        is about the file, and path "" with line 0 is about the whole PR. Raises ReviewError
        (400) for a local or closed review, a bad position or path, or an empty body.
        """
        review = self._prs.require_pr_review(review_id)
        if review.status == "closed":
            raise ReviewError(f"Review {review_id!r} is closed. Open the PR again to ask.")
        cleaned = _clean_body(body)
        if path.startswith("/") or ".." in path.split("/"):
            raise ReviewError(f"{path!r} must be a path inside the repository")
        if not path and request.line != 0:
            raise ReviewError('A question about the whole PR (path "") must use line 0')
        anchor = normalize_anchor(
            request.side, request.line, request.start_line, request.start_side
        )
        thread = self._store.create_thread(
            review_id=review_id,
            kind="question",
            path=path,
            side=anchor.side,
            line=anchor.line,
            start_line=anchor.start_line,
            start_side=anchor.start_side,
            status="draft",
            author="user",
            body=cleaned,
            anchor_sha=review.head_sha,
            message_status="staged",
        )
        result = self._thread_json(thread.id)
        self._hub.publish(review_id, "thread_added", {"thread": result})
        self._staged_changed(review_id)
        return result

    def _publish_updated(self, thread_id: str) -> dict[str, object]:
        thread = self._require_thread(thread_id)
        result = self._thread_json(thread_id)
        self._hub.publish(thread.review_id, "thread_updated", {"thread": result})
        return result

    def stage_reply(self, thread_id: str, body: str) -> MessageRow:
        """Stage a follow-up on a question thread. It is sent with the next agent send."""
        thread = self._require_thread(thread_id)
        if thread.kind != "question":
            raise ReviewError(f"Thread {thread_id!r} is not a question thread")
        review = self._prs.require_pr_review(thread.review_id)
        if review.status == "closed":
            raise ReviewError(f"Review {review.id!r} is closed. Open the PR again to ask.")
        message = self._store.add_message(
            thread_id, author="user", body=_clean_body(body), status="staged"
        )
        self._publish_updated(thread_id)
        self._staged_changed(thread.review_id)
        return message

    def _require_staged_message(self, message_id: str) -> MessageRow:
        message = self._store.get_message(message_id)
        if message is None:
            raise NotFoundError(f"Message {message_id!r} not found")
        if message.status != "staged":
            raise ConflictError(f"Message {message_id!r} was sent; only a staged one can change")
        return message

    def update_message(self, message_id: str, body: str) -> dict[str, object]:
        """Edit a staged message. Returns the thread. Raises ConflictError if it was sent."""
        message = self._require_staged_message(message_id)
        self._store.update_message_body(message_id, _clean_body(body))
        return self._publish_updated(message.thread_id)

    def delete_message(self, message_id: str) -> dict[str, object]:
        """Delete a staged follow-up. Returns the thread. A staged question is removed by
        deleting its thread. Raises ConflictError if the message was sent or is the first."""
        message = self._require_staged_message(message_id)
        first = self._store.list_messages(message.thread_id)[0]
        if first.id == message_id:
            raise ConflictError("This message is the question itself; delete the thread instead")
        self._store.delete_message(message_id)
        result = self._publish_updated(message.thread_id)
        self._staged_changed(self._require_thread(message.thread_id).review_id)
        return result

    def thread_json(self, thread_id: str) -> dict[str, object]:
        return self._thread_json(thread_id)

    async def update_thread(
        self, thread_id: str, *, body: str | None, anchor: AnchorRequest | None
    ) -> dict[str, object]:
        """Edit a draft's body, or re-anchor a draft or stale comment at the current head.

        Re-anchoring sets the status back to draft. Raises ConflictError (409) when the
        status does not allow the change.
        """
        thread = self._require_thread(thread_id)
        if thread.kind == "question":
            return self._update_question(thread, body=body, anchor=anchor)
        if thread.kind != REVIEW_COMMENT:
            raise ReviewError(f"Thread {thread_id!r} is not a review comment")
        if body is None and anchor is None:
            raise ReviewError("Send a body, a new position (side and line), or both")
        cleaned = _clean_body(body) if body is not None else None
        if anchor is not None and thread.status not in ("draft", "stale"):
            raise ConflictError(
                f"Thread {thread_id!r} is {thread.status}; only a draft or stale comment can move"
            )
        if anchor is None and thread.status != "draft":
            raise ConflictError(
                f"Thread {thread_id!r} is {thread.status}; only a draft can be edited"
            )
        if anchor is not None:
            review = self._prs.require_pr_review(thread.review_id)
            assert review.repo is not None and review.pr_number is not None
            pr = await self._prs.ready_pr(review.id)
            async with self._prs.lock_for(review.repo, review.pr_number):
                self._require_same_head(pr)
                if self._require_thread(thread_id).status not in ("draft", "stale"):
                    raise ConflictError(f"Thread {thread_id!r} changed status; reload it")
                checked = await self._checked_anchor(pr, thread.path, anchor)
                self._store.reanchor_thread(
                    thread_id,
                    side=checked.side,
                    line=checked.line,
                    start_line=checked.start_line,
                    start_side=checked.start_side,
                    anchor_sha=pr.head_sha,
                )
        if cleaned is not None:
            self._store.update_first_message(thread_id, cleaned)
        result = self._thread_json(thread_id)
        self._hub.publish(thread.review_id, "thread_updated", {"thread": result})
        return result

    def _update_question(
        self, thread: ThreadRow, *, body: str | None, anchor: AnchorRequest | None
    ) -> dict[str, object]:
        if anchor is not None or body is None:
            raise ReviewError("Only the body of a staged question can change")
        if self._store.list_messages(thread.id)[0].status != "staged":
            raise ConflictError(
                f"Thread {thread.id!r} was sent; only a staged question can be edited"
            )
        self._store.update_first_message(thread.id, _clean_body(body))
        return self._publish_updated(thread.id)

    async def submit_review(
        self, review_id: str, event: SubmissionEvent, body: str
    ) -> dict[str, object]:
        """Post every draft review comment to GitHub as one review of the current head.

        Raises ForbiddenError (403) for an event GitHub does not allow the viewer,
        StaleThreadsError (409) while a review comment is stale, and ReviewError (400) for a
        missing body or a GitHub rejection. On any error nothing is marked posted.
        """
        review = self._prs.require_pr_review(review_id)
        assert review.repo is not None and review.pr_number is not None
        if review.status == "closed":
            raise ReviewError(f"Review {review_id!r} is closed. Open the PR again to submit.")
        viewer = await self._github.viewer_login()
        allowed = allowed_events(viewer, review.author)
        if event not in allowed:
            raise ForbiddenError(
                f"{event} is not allowed here: the author of a PR can only COMMENT on it. "
                f"Allowed: {', '.join(allowed)}."
            )
        async with self._prs.lock_for(review.repo, review.pr_number):
            current = self._prs.require_pr_review(review_id)
            head = current.head_sha
            if head is None:
                raise ReviewError(f"Review {review_id!r} has no head commit. Open the PR again.")
            threads = self._store.list_threads(review_id, kind=REVIEW_COMMENT)
            moved = [t.id for t in threads if t.status == "draft" and t.anchor_sha != head]
            if moved:
                self._store.mark_threads_stale(moved)
                self._hub.publish(review_id, "threads_stale", {"thread_ids": moved})
            stale = [t.id for t in threads if t.status == "stale" or t.id in moved]
            if stale:
                raise StaleThreadsError(
                    f"{len(stale)} review comment(s) are stale after the PR head moved. "
                    "Move or delete them, then submit again.",
                    stale,
                )
            messages = self._store.messages_for_review(review_id)
            drafts = [
                DraftComment(
                    thread_id=t.id,
                    path=t.path,
                    body=messages[t.id][0].body,
                    line=t.line,
                    side=t.side,
                    start_line=t.start_line,
                    start_side=t.start_side,
                )
                for t in threads
                if t.status == "draft"
            ]
            check_review_body(event, body, comment_count=len(drafts))
            submitted = await self._writer.submit_review(
                review.repo,
                review.pr_number,
                commit_id=head,
                event=event,
                body=body,
                comments=drafts,
            )
            try:
                posted = await self._writer.review_comments(
                    review.repo, review.pr_number, submitted.review_id
                )
            except ReviewError as e:
                logger.warning(
                    "review %d on %s#%d was posted, but its comments could not be read: %s",
                    submitted.review_id,
                    review.repo,
                    review.pr_number,
                    e,
                )
                posted = []
            postings = match_postings(drafts, posted)
            unmatched = [p.thread_id for p in postings if p.github_comment_id is None]
            if unmatched:
                logger.warning(
                    "review %d on %s#%d: no GitHub comment matched threads %s",
                    submitted.review_id,
                    review.repo,
                    review.pr_number,
                    ", ".join(unmatched),
                )
            self._store.record_submission(
                review_id,
                event=event,
                body=body,
                github_review_id=submitted.review_id,
                html_url=submitted.html_url,
                commit_id=head,
                postings=postings,
            )
        logger.info(
            "submitted %s review %d on %s#%d with %d comment(s)",
            event,
            submitted.review_id,
            review.repo,
            review.pr_number,
            len(drafts),
        )
        result: dict[str, object] = {
            "github_review_id": submitted.review_id,
            "html_url": submitted.html_url,
            "posted": len(drafts),
        }
        self._hub.publish(
            review_id,
            "review_submitted",
            {**result, "event": event, "thread_ids": [d.thread_id for d in drafts]},
        )
        return result
