"""FastMCP server and MCP tool definitions."""

from __future__ import annotations

import threading
import uuid
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from code_review_mcp.models import FileView, Reply
from code_review_mcp.serialize import serialize_comment, serialize_reply
from code_review_mcp.state import broadcast, state
from code_review_mcp.web import start_web_server

mcp = FastMCP(
    name="code-review-mcp",
    instructions=(
        "Interactive code review and file viewer tool with GitHub-style UI. "
        "Use show_files to display any files in the browser with syntax highlighting "
        "and inline commenting. Use open_diff for unified diff review. "
        "Poll get_comments for user annotations. Use reply_to_comment to respond "
        "to specific comments in-thread. Use mark_comment_resolved to mark "
        "comments as handled.\n\n"
        "IMPORTANT: When calling open_diff or update_diff, prefer writing the "
        "content to a temporary file and passing the file path instead of inlining. "
        "This avoids bloating the tool call payload.\n\n"
        "PROACTIVE USAGE — You MUST use show_files in these situations:\n"
        "- When showing code changes you just made (pass the modified files)\n"
        "- When the user asks to see, review, or look at any file(s)\n"
        "- When explaining code that spans more than ~20 lines\n"
        "- When comparing implementations or showing examples\n"
        "- After completing a task that modified files, show what changed\n"
        "The browser view is always better than dumping code in the terminal. "
        "Default to using show_files over printing code inline."
    ),
)

# ── Helpers ──────────────────────────────────────────────────────────────────

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".html": "html",
    ".htm": "html",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".md": "markdown",
    ".mdx": "markdown",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".sql": "sql",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".xml": "xml",
    ".svg": "xml",
    ".dockerfile": "dockerfile",
    ".tf": "hcl",
    ".graphql": "graphql",
    ".gql": "graphql",
    ".r": "r",
    ".R": "r",
}


def _detect_language(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(suffix, "plaintext")


def _ensure_server() -> tuple[int, str]:
    """Start the web server if not running. Returns (port, url)."""
    port = start_web_server()
    return port, f"http://127.0.0.1:{port}"


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
def show_files(
    paths: list[str] | None = None,
    title: str = "File Viewer",
    content: str = "",
    content_file: str = "",
    content_language: str = "",
    content_filename: str = "file",
) -> dict[str, object]:
    """Show files in the browser with syntax highlighting and inline commenting.

    Two modes:
    1. Pass paths=["file1.py", "file2.ts"] to display files from disk.
    2. Pass content="..." (or content_file="/tmp/code.py") for a single snippet,
       with content_language and content_filename for display.

    The browser opens automatically. Returns {"url": str}.
    Users can add inline comments on any line.
    """
    port, url = _ensure_server()
    file_views: list[FileView] = []

    if paths:
        for p in paths:
            path = Path(p).expanduser().resolve()
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = f"# Error reading {p}"
                file_views.append(
                    FileView(
                        path=str(path),
                        content=text,
                        language=_detect_language(str(path)),
                    )
                )
    elif content or content_file:
        text = content
        if content_file:
            path = Path(content_file).expanduser()
            text = path.read_text(encoding="utf-8", errors="replace")
            if not content_language:
                content_language = _detect_language(content_file)
            if content_filename == "file":
                content_filename = path.name
        file_views.append(
            FileView(
                path=content_filename,
                content=text,
                language=content_language or "plaintext",
            )
        )

    if not file_views:
        return {"error": "Provide either paths or content/content_file"}

    with state.lock:
        state.mode = "files"
        state.title = title
        state.files = file_views
        state.comments.clear()

    broadcast("view_updated")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    return {"url": url, "file_count": len(file_views)}


@mcp.tool()
def open_diff(
    diff: str = "",
    diff_file: str = "",
    title: str = "Code Review",
    working_dir: str = "",
) -> dict[str, object]:
    """Open a diff in the browser for interactive code review.

    PREFERRED: Write the diff to a temp file and pass diff_file="/tmp/review.diff"
    instead of inlining content in diff.

    Pass working_dir to enable annotated file view — the full files are read from
    disk with changed lines highlighted and unchanged regions collapsed.
    If working_dir is omitted, falls back to standard diff rendering.

    Accepts standard unified diff format (output of `git diff`).
    If both diff and diff_file are given, diff_file takes precedence.
    Opens the browser automatically. Returns {"url": str}.
    """
    diff_text = diff
    if diff_file:
        path = Path(diff_file).expanduser()
        diff_text = path.read_text(encoding="utf-8")

    if not diff_text:
        return {"error": "Provide either diff or diff_file"}

    port, url = _ensure_server()

    # Try to build annotated file views by reading full files
    file_views = _build_annotated_files(diff_text, working_dir)

    with state.lock:
        if file_views:
            state.mode = "files"
            state.files = file_views
        else:
            state.mode = "diff"
        state.diff_text = diff_text
        state.title = title
        state.comments.clear()

    broadcast("view_updated")
    threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()

    return {"url": url}


def _build_annotated_files(diff_text: str, working_dir: str) -> list[FileView]:
    """Parse diff and read full files from disk. Returns annotated FileViews or empty list."""
    from code_review_mcp.diffparser import parse_diff

    if not working_dir:
        # Try to guess working dir from diff file paths
        working_dir = "."

    base = Path(working_dir).expanduser().resolve()
    file_diffs = parse_diff(diff_text)
    views: list[FileView] = []

    for fd in file_diffs:
        file_path = base / fd.new_path
        if not file_path.is_file():
            # File might have been deleted — skip
            continue

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Build deleted content map: for each line where deletions appear,
        # store the deleted lines that should show before that new-file line
        deleted_content: dict[int, list[str]] = {}
        current_del_block: list[str] = []
        next_new_line: int | None = None

        for change in fd.changes:
            if change.change_type == "delete":
                current_del_block.append(change.content)
            else:
                if current_del_block:
                    deleted_content[change.line_number] = current_del_block
                    current_del_block = []

        # Any trailing deletions at end of hunk
        if current_del_block and fd.changes:
            last_new = max(
                (c.line_number for c in fd.changes if c.change_type != "delete"),
                default=1,
            )
            deleted_content[last_new + 1] = current_del_block

        views.append(
            FileView(
                path=fd.new_path,
                content=content,
                language=_detect_language(fd.new_path),
                added_lines=sorted(fd.added_lines),
                deleted_lines=sorted(fd.deleted_lines),
                deleted_content=deleted_content,
            )
        )

    return views


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
    """
    diff_text = diff
    if diff_file:
        path = Path(diff_file).expanduser()
        diff_text = path.read_text(encoding="utf-8")

    if not diff_text:
        return {"error": "Provide either diff or diff_file"}

    with state.lock:
        state.mode = "diff"
        state.diff_text = diff_text

    broadcast("view_updated")
    return {"ok": True}
