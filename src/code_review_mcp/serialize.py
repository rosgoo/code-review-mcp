from collections.abc import Mapping, Sequence, Set
from typing import Literal

from code_review_mcp.models import LineType
from code_review_mcp.store import Author, MessageRow, ReviewFile, ReviewRow, ThreadRow

_REPLY_AUTHOR: dict[Author, Literal["user", "claude"]] = {"user": "user", "agent": "claude"}


def line_type_for(thread: ThreadRow, added_lines: Mapping[str, Set[int]]) -> LineType:
    """Derive the legacy line type: deletions are "delete"; additions are "add" only if the
    line is an added line of the review's current diff, otherwise "context"."""
    if thread.side == "deletions":
        return "delete"
    if thread.line in added_lines.get(thread.path, ()):
        return "add"
    return "context"


def serialize_file(f: ReviewFile) -> dict[str, object]:
    result: dict[str, object] = {
        "path": f.path,
        "content": f.content,
        "language": f.language,
    }
    if f.added_lines:
        result["added_lines"] = f.added_lines
    if f.deleted_lines:
        result["deleted_lines"] = f.deleted_lines
    if f.deleted_content:
        result["deleted_content"] = {str(k): v for k, v in f.deleted_content.items()}
    return result


def serialize_reply(m: MessageRow) -> dict[str, object]:
    return {
        "id": m.id,
        "comment_id": m.thread_id,
        "author": _REPLY_AUTHOR[m.author],
        "message": m.body,
        "timestamp": m.created_at,
    }


def serialize_comment(
    thread: ThreadRow,
    messages: Sequence[MessageRow],
    added_lines: Mapping[str, Set[int]],
) -> dict[str, object]:
    """Serialize a local thread in the comment shape that agents read.

    The first message is the comment body; the rest are replies. A multi-line
    comment adds `start_line` and `start_side`.
    """
    first, *replies = messages
    result: dict[str, object] = {
        "id": thread.id,
        "file_path": thread.path,
        "line_number": thread.line,
        "line_type": line_type_for(thread, added_lines),
        "line_content": thread.line_content,
        "user_message": first.body,
        "timestamp": thread.created_at,
        "status": thread.status,
        "replies": [serialize_reply(r) for r in replies],
    }
    if thread.start_line is not None:
        result["start_line"] = thread.start_line
        result["start_side"] = thread.start_side
    return result


def serialize_review_summary(review: ReviewRow, url: str) -> dict[str, object]:
    return {
        "id": review.id,
        "kind": review.kind,
        "title": review.title,
        "status": review.status,
        "mode": review.mode,
        "url": url,
        "created_at": review.created_at,
        "updated_at": review.updated_at,
    }
