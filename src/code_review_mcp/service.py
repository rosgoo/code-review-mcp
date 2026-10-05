import threading
import webbrowser
from collections.abc import Collection, Sequence
from dataclasses import dataclass

from code_review_mcp.config import Settings
from code_review_mcp.errors import ConflictError, NotFoundError, ReviewError
from code_review_mcp.hub import ReviewHub
from code_review_mcp.local_files import (
    added_lines_by_path,
    build_annotated_files,
    require_absolute,
)
from code_review_mcp.models import CommentRequest
from code_review_mcp.reverse_patch import reconstruct_old_content, split_file_patches
from code_review_mcp.serialize import (
    serialize_comment,
    serialize_file,
    serialize_reply,
    serialize_review_summary,
)
from code_review_mcp.store import (
    Author,
    MessageRow,
    ReviewFile,
    ReviewRow,
    Store,
    ThreadRow,
    ThreadStatus,
)

_SUBMITTED: tuple[ThreadStatus, ...] = ("submitted",)


@dataclass(frozen=True)
class ReplyResult:
    message: MessageRow
    reopened: bool


class ReviewService:
    def __init__(self, store: Store, hub: ReviewHub, settings: Settings) -> None:
        self._store = store
        self._hub = hub
        self._settings = settings

    def review_url(self, review_id: str) -> str:
        return self._settings.review_url(review_id)

    def open_browser(self, url: str) -> None:
        if self._settings.open_browser:
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    def require_review(self, review_id: str) -> ReviewRow:
        review = self._store.get_review(review_id)
        if review is None:
            raise NotFoundError(f"Review {review_id!r} not found")
        return review

    def _require_local_review(self, review_id: str) -> ReviewRow:
        review = self.require_review(review_id)
        if review.kind != "local":
            raise ReviewError(f"Review {review_id!r} is not a local review")
        return review

    def _require_thread(self, thread_id: str) -> ThreadRow:
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise NotFoundError(f"Thread {thread_id!r} not found")
        return thread

    def list_reviews(self) -> list[dict[str, object]]:
        return [
            serialize_review_summary(review, self.review_url(review.id))
            for review in self._store.list_reviews()
        ]

    def open_diff(self, diff_text: str, title: str, working_dir: str) -> ReviewRow:
        """Create a local review of `diff_text`.

        With a `working_dir`, the review shows the full new-side files from disk
        (mode "files"); without one, or if none of the files exist, it shows the diff.
        Raises InvalidPathError if `working_dir` is relative.
        """
        resolved_dir = (
            str(require_absolute(working_dir, "working_dir").resolve()) if working_dir else None
        )
        files = build_annotated_files(diff_text, resolved_dir) if resolved_dir else []
        return self._store.create_review(
            kind="local",
            title=title,
            mode="files" if files else "diff",
            patch_text=diff_text,
            working_dir=resolved_dir,
            files=files,
        )

    def show_files(self, files: Sequence[ReviewFile], title: str) -> ReviewRow:
        return self._store.create_review(kind="local", title=title, mode="files", files=files)

    def update_diff(self, review_id: str, diff_text: str) -> None:
        """Replace the review's diff, keeping its threads. Re-reads files from the review's
        working_dir when it has one."""
        review = self._require_local_review(review_id)
        files = build_annotated_files(diff_text, review.working_dir) if review.working_dir else []
        self._store.update_review_content(
            review_id, mode="files" if files else "diff", patch_text=diff_text, files=files
        )
        self._hub.publish(review_id, "view_updated")

    def view(self, review_id: str) -> dict[str, object]:
        """Return what the browser renders for the review.

        An annotated review (files from a diff) also carries the diff and, per file,
        `old_content` rebuilt from that diff, or None when the file does not match it.
        """
        review = self.require_review(review_id)
        result: dict[str, object] = {
            "review_id": review.id,
            "kind": review.kind,
            "mode": review.mode or "empty",
            "title": review.title,
        }
        if review.mode == "diff":
            result["diff"] = review.patch_text or ""
        elif review.mode == "files":
            files = self._store.list_review_files(review_id)
            if review.patch_text is None:
                result["files"] = [serialize_file(f) for f in files]
            else:
                patches = split_file_patches(review.patch_text)
                result["diff"] = review.patch_text
                result["files"] = [
                    {
                        **serialize_file(f),
                        "old_content": reconstruct_old_content(f.content, patches[f.path])
                        if f.path in patches
                        else None,
                    }
                    for f in files
                ]
        return result

    def comments(
        self, review_id: str, statuses: Collection[ThreadStatus] | None = None
    ) -> list[dict[str, object]]:
        review = self.require_review(review_id)
        threads = self._store.list_threads(review_id, kind="local", statuses=statuses)
        messages = self._store.messages_for_review(review_id)
        added = added_lines_by_path(review.patch_text)
        return [serialize_comment(t, messages[t.id], added) for t in threads]

    def submitted_comments(self, review_id: str) -> list[dict[str, object]]:
        return self.comments(review_id, statuses=_SUBMITTED)

    def add_user_comment(self, review_id: str, request: CommentRequest) -> ThreadRow:
        """Create a draft thread. A `start_side` without a `start_line` is dropped; a
        `start_line` without a `start_side` starts on the comment's own side."""
        review = self._require_local_review(review_id)
        has_range = request.start_line is not None
        thread = self._store.create_thread(
            review_id=review_id,
            kind="local",
            path=request.path,
            side=request.side,
            line=request.line,
            start_line=request.start_line,
            start_side=(request.start_side or request.side) if has_range else None,
            line_content=request.line_content,
            status="draft",
            author="user",
            body=request.body,
        )
        comment = serialize_comment(
            thread,
            self._store.list_messages(thread.id),
            added_lines_by_path(review.patch_text),
        )
        self._hub.publish(review_id, "comment_added", {"comment": comment})
        return thread

    def delete_thread(self, thread_id: str) -> None:
        """Delete a draft or stale thread. Raises ConflictError for any other status."""
        thread = self._require_thread(thread_id)
        if not self._store.delete_unposted_thread(thread_id):
            raise ConflictError(
                f"Thread {thread_id!r} is {thread.status}; only a draft or stale thread can be "
                "deleted"
            )
        self._hub.publish(
            thread.review_id,
            "thread_deleted",
            {"comment_id": thread_id, "thread_id": thread_id},
        )

    def submit(self, review_id: str) -> int:
        """Move the review's draft threads to submitted and wake its waiters.

        Wakes waiters even when there are no drafts, so they see reopened threads.
        """
        self._require_local_review(review_id)
        count = self._store.move_threads(
            review_id, kind="local", from_status="draft", to_status="submitted"
        )
        self._store.touch_review(review_id)
        self._hub.notify_submit(review_id)
        self._hub.publish(review_id, "comments_submitted", {"count": count})
        return count

    async def wait_for_comments(self, review_id: str, timeout: float) -> dict[str, object]:
        self._require_local_review(review_id)
        if not await self._hub.wait_for_submit(review_id, timeout):
            return {"status": "timeout"}
        return {
            "status": "submitted",
            "comments": self.submitted_comments(review_id),
        }

    def reply(self, thread_id: str, author: Author, body: str) -> ReplyResult:
        """Add a message to the thread. A user reply on a resolved thread reopens it."""
        thread = self._require_thread(thread_id)
        message = self._store.add_message(thread_id, author=author, body=body)
        reopened = author == "user" and thread.status == "resolved"
        if reopened:
            self._store.set_thread_status(thread_id, "submitted")
        self._hub.publish(
            thread.review_id,
            "reply_added",
            {"comment_id": thread_id, "reply": serialize_reply(message), "reopened": reopened},
        )
        return ReplyResult(message=message, reopened=reopened)

    def resolve(self, thread_id: str) -> None:
        thread = self._require_thread(thread_id)
        self._store.set_thread_status(thread_id, "resolved")
        self._hub.publish(thread.review_id, "comment_resolved", {"comment_id": thread_id})
