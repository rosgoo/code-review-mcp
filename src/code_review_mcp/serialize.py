"""Serialization helpers for models."""

from __future__ import annotations

from code_review_mcp.models import Comment, FileView, Reply


def serialize_file(f: FileView) -> dict[str, object]:
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


def serialize_reply(r: Reply) -> dict[str, object]:
    return {
        "id": r.id,
        "comment_id": r.comment_id,
        "author": r.author,
        "message": r.message,
        "timestamp": r.timestamp,
    }


def serialize_comment(c: Comment) -> dict[str, object]:
    return {
        "id": c.id,
        "file_path": c.file_path,
        "line_number": c.line_number,
        "line_type": c.line_type,
        "line_content": c.line_content,
        "user_message": c.user_message,
        "timestamp": c.timestamp,
        "status": c.status,
        "replies": [serialize_reply(r) for r in c.replies],
    }
