"""FastMCP server and MCP tool definitions."""

from __future__ import annotations

import threading
import uuid
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from code_review_mcp.models import Reply
from code_review_mcp.serialize import serialize_comment, serialize_reply
from code_review_mcp.state import broadcast, state
from code_review_mcp.web import start_web_server

mcp = FastMCP(
    name="code-review-mcp",
    instructions=(
        "Interactive code review tool with GitHub-style diff UI. "
        "Publish a diff with open_diff, then poll get_comments for user "
        "annotations on specific lines. Use reply_to_comment to respond "
        "to specific comments in-thread. Use mark_comment_resolved to "
        "mark comments as handled. Use update_diff to show updated code "
        "after making changes.\n\n"
        "IMPORTANT: When calling open_diff or update_diff, prefer writing the "
        "diff to a temporary file and passing diff_file instead of inlining "
        "the full content in diff. This avoids bloating the tool call payload. "
        "Example: write diff to /tmp/review.diff, then call "
        'open_diff(diff_file="/tmp/review.diff", title="Feature Review").'
    ),
)


@mcp.tool()
def open_diff(
    diff: str = "",
    diff_file: str = "",
    title: str = "Code Review",
) -> dict[str, object]:
    """Open a unified diff in the browser for interactive code review.

    PREFERRED: Write the diff to a temp file and pass diff_file="/tmp/review.diff"
    instead of inlining content in diff. This keeps the tool call small.

    Accepts standard unified diff format (output of `git diff`).
    If both are given, diff_file takes precedence.
    Starts the web server on first call. Opens the browser automatically.
    Returns {"port": int, "url": str}.
    """
    diff_text = diff
    if diff_file:
        path = Path(diff_file).expanduser()
        diff_text = path.read_text(encoding="utf-8")

    if not diff_text:
        return {"error": "Provide either diff or diff_file"}

    port = start_web_server()
    url = f"http://127.0.0.1:{port}"

    with state.lock:
        state.diff_text = diff_text
        state.title = title
        state.comments.clear()

    broadcast("diff_updated")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    return {"port": port, "url": url}


@mcp.tool()
def get_comments() -> list[dict[str, object]]:
    """Return all submitted (not draft/resolved) comments from the browser.

    Each item: id, file_path, line_number, line_type, line_content,
    user_message, timestamp, replies[].
    """
    with state.lock:
        return [serialize_comment(c) for c in state.comments if c.status == "submitted"]


@mcp.tool()
def mark_comment_resolved(comment_id: str) -> dict[str, object]:
    """Mark a comment as resolved so it won't reappear in get_comments."""
    found = False
    with state.lock:
        for comment in state.comments:
            if comment.id == comment_id:
                comment.status = "resolved"
                found = True
                break
    if found:
        broadcast("comment_resolved", {"comment_id": comment_id})
        return {"ok": True}
    return {"ok": False, "error": f"Comment {comment_id!r} not found"}


@mcp.tool()
def reply_to_comment(
    comment_id: str,
    message: str,
) -> dict[str, object]:
    """Reply to a user's code review comment. Appears as a threaded reply inline."""
    reply = Reply(
        id=str(uuid.uuid4()),
        comment_id=comment_id,
        author="claude",
        message=message,
        timestamp=datetime.now(UTC).isoformat(),
    )
    found = False
    with state.lock:
        for comment in state.comments:
            if comment.id == comment_id:
                comment.replies.append(reply)
                found = True
                break
    if found:
        broadcast("reply_added", {"comment_id": comment_id, "reply": serialize_reply(reply)})
        return {"ok": True, "reply_id": reply.id}
    return {"ok": False, "error": f"Comment {comment_id!r} not found"}


@mcp.tool()
def update_diff(
    diff: str = "",
    diff_file: str = "",
) -> dict[str, object]:
    """Replace the diff with an updated version. Browser auto-refreshes via SSE.

    PREFERRED: Write diff to a temp file and pass diff_file instead of inlining.
    Comments are preserved but may need re-anchoring by the user.
    """
    diff_text = diff
    if diff_file:
        path = Path(diff_file).expanduser()
        diff_text = path.read_text(encoding="utf-8")

    if not diff_text:
        return {"error": "Provide either diff or diff_file"}

    with state.lock:
        state.diff_text = diff_text

    broadcast("diff_updated")
    return {"ok": True}
