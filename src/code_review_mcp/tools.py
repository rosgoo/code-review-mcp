from collections.abc import Sequence
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from code_review_mcp.config import Settings
from code_review_mcp.errors import ReviewError
from code_review_mcp.local_files import (
    detect_language,
    read_files,
    read_text_file,
    require_absolute,
)
from code_review_mcp.pr_service import PrService
from code_review_mcp.service import ReviewService
from code_review_mcp.store import ReviewFile

INSTRUCTIONS = (
    "Interactive code review and file viewer tool with GitHub-style UI, served by a "
    "local daemon. Use show_files to display any files in the browser with syntax "
    "highlighting and inline commenting. Use open_diff for unified diff review. "
    "Each open_diff or show_files call creates a new review and returns its review_id. "
    "Pass that review_id to update_diff, get_comments, and wait_for_comments. Other "
    "sessions can hold their own reviews at the same time.\n\n"
    "After opening, call wait_for_comments(review_id) to block until the user clicks "
    "Submit in the browser. It returns their comments as soon as they submit; no polling "
    "is needed. If it returns status=timeout, call it again to keep waiting (or stop if "
    "the user told you to). Each comment's id is a thread_id. Use "
    "reply_to_thread(thread_id, message) to respond in-thread. Use "
    "resolve_thread(thread_id) to mark a comment as handled.\n\n"
    "IMPORTANT: The daemon does not run in your working directory. Pass absolute paths "
    "for paths, diff_file, and content_file. Pass working_dir (the absolute repo root) "
    "to open_diff to get the annotated full-file view.\n\n"
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
    "Default to using show_files over printing code inline.\n\n"
    "PR MODE (read-only on GitHub): list_review_requests() lists the open PRs that request "
    "the user's review. open_pr(ref) opens any GitHub PR in the browser; ref is a PR URL, "
    "owner/name#123, #123, a commit SHA, or a branch name. It returns a review_id, and "
    "opening the same PR again returns the same review_id. get_review(review_id) returns "
    "the PR's metadata, changed files, threads, and worktree_path, a local checkout of the "
    "PR head that you can read. No tool posts anything to GitHub."
)

MAX_WAIT_SECONDS = 3600


def transport_security(settings: Settings) -> TransportSecuritySettings:
    hosts = {"127.0.0.1", "localhost", settings.host}
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(f"{host}:{settings.port}" for host in hosts),
        allowed_origins=sorted(f"http://{host}:{settings.port}" for host in hosts),
    )


def _read_input_file(value: str, name: str) -> str:
    """Read the file the agent named in argument `name`.

    Raises InvalidPathError if the path is relative, ReviewError if it cannot be read.
    """
    path = require_absolute(value, name)
    try:
        return read_text_file(path)
    except OSError as e:
        raise ReviewError(f"Cannot read {name}: {e}") from e


def _read_diff(diff: str, diff_file: str) -> str:
    return _read_input_file(diff_file, "diff_file") if diff_file else diff


def build_mcp(
    service: ReviewService, prs: PrService, security: TransportSecuritySettings
) -> FastMCP:
    mcp = FastMCP(
        name="code",
        instructions=INSTRUCTIONS,
        log_level="WARNING",
        streamable_http_path="/mcp",
        stateless_http=True,
        transport_security=security,
    )

    @mcp.tool()
    async def show_files(
        paths: Sequence[str] | None = None,
        title: str = "File Viewer",
        content: str = "",
        content_file: str = "",
        content_language: str = "",
        content_filename: str = "file",
    ) -> dict[str, object]:
        """Show files in the browser with syntax highlighting and inline commenting.

        Two modes:
        1. Pass paths=["/abs/file1.py", "/abs/file2.ts"] to display files from disk.
        2. Pass content="..." (or content_file="/tmp/code.py") for a single snippet,
           with content_language and content_filename for display.

        Use absolute paths: the daemon does not share your working directory.
        A relative content_file returns an error; a relative entry in paths shows a
        "File not found" placeholder.
        Creates a new review and opens the browser.
        Returns {"review_id": str, "url": str, "file_count": int}.
        """
        files: list[ReviewFile] = []
        if paths:
            files = read_files(paths)
        elif content or content_file:
            text = content
            if content_file:
                try:
                    text = _read_input_file(content_file, "content_file")
                except ReviewError as e:
                    return {"error": str(e)}
                content_language = content_language or detect_language(content_file)
                if content_filename == "file":
                    content_filename = Path(content_file).name
            files = [
                ReviewFile(
                    path=content_filename, content=text, language=content_language or "plaintext"
                )
            ]

        if not files:
            return {"error": "Provide either paths or content/content_file"}

        review = service.show_files(files, title)
        url = service.review_url(review.id)
        service.open_browser(url)
        return {"review_id": review.id, "url": url, "file_count": len(files)}

    @mcp.tool()
    async def open_diff(
        diff: str = "",
        diff_file: str = "",
        title: str = "Code Review",
        working_dir: str = "",
    ) -> dict[str, object]:
        """Open a diff in the browser for interactive code review.

        PREFERRED: Write the diff to a temp file and pass diff_file="/tmp/review.diff"
        instead of inlining content in diff.

        Pass working_dir (absolute path of the repo root) to enable the annotated file
        view: the full files are read from disk with changed lines highlighted and
        unchanged regions collapsed. Without working_dir, the standard diff view is used.

        diff_file and working_dir must be absolute paths; a relative path returns an error.
        Accepts standard unified diff format (output of `git diff`).
        If both diff and diff_file are given, diff_file takes precedence.
        Creates a new review and opens the browser. Returns {"review_id": str, "url": str}.
        """
        try:
            diff_text = _read_diff(diff, diff_file)
            if not diff_text:
                return {"error": "Provide either diff or diff_file"}
            review = service.open_diff(diff_text, title, working_dir)
        except ReviewError as e:
            return {"error": str(e)}
        url = service.review_url(review.id)
        service.open_browser(url)
        return {"review_id": review.id, "url": url}

    @mcp.tool()
    async def update_diff(
        review_id: str,
        diff: str = "",
        diff_file: str = "",
    ) -> dict[str, object]:
        """Replace a review's diff with an updated version. The browser refreshes via SSE.

        Existing comments are kept. PREFERRED: Write the diff to a temp file and pass
        diff_file (an absolute path) instead of inlining.
        """
        try:
            diff_text = _read_diff(diff, diff_file)
            if not diff_text:
                return {"error": "Provide either diff or diff_file"}
            service.update_diff(review_id, diff_text)
        except ReviewError as e:
            return {"error": str(e)}
        return {"ok": True}

    @mcp.tool()
    async def get_comments(review_id: str) -> list[dict[str, object]]:
        """Return the review's submitted (not draft, not resolved) comments.

        Each item: id (the thread_id), file_path, line_number, line_type, line_content,
        user_message, timestamp, status, replies[].
        """
        try:
            return service.submitted_comments(review_id)
        except ReviewError as e:
            raise ToolError(str(e)) from e

    @mcp.tool()
    async def wait_for_comments(review_id: str, timeout_seconds: int = 540) -> dict[str, object]:
        """Block until the user clicks "Submit Comments" on this review, then return them.

        Call this immediately after open_diff or show_files to pause until the user
        is done reviewing. Returns as soon as the user submits; no polling needed.
        Only a submit on this review_id wakes this call.

        Supports multiple rounds: after handling a batch (replying / resolving /
        pushing new code via update_diff), call wait_for_comments again to block
        until the user submits the NEXT round. Only unresolved comments are
        returned, so call resolve_thread after you address each one.

        If the user hasn't submitted within timeout_seconds, returns
        {"status": "timeout"} with no comments. You may call this tool again to
        keep waiting, or stop if the user indicated they're done by other means
        (e.g. they closed the browser or sent a chat message).

        Default timeout (540s = 9 min) stays under typical MCP client tool timeouts.

        Returns:
          {"status": "submitted", "comments": [...]}: the user submitted (only
            currently-unresolved submitted comments are returned)
          {"status": "timeout"}: no submission within timeout_seconds
        """
        timeout = max(1, min(timeout_seconds, MAX_WAIT_SECONDS))
        try:
            return await service.wait_for_comments(review_id, timeout)
        except ReviewError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def resolve_thread(thread_id: str) -> dict[str, object]:
        """Mark a comment thread as resolved so it won't reappear in get_comments."""
        try:
            service.resolve(thread_id)
        except ReviewError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    @mcp.tool()
    async def reply_to_thread(thread_id: str, message: str) -> dict[str, object]:
        """Reply to a comment thread. Appears as a threaded reply inline (markdown)."""
        try:
            result = service.reply(thread_id, "agent", message)
        except ReviewError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "reply_id": result.message.id}

    @mcp.tool()
    async def list_review_requests(refresh: bool = False) -> dict[str, object]:
        """List the open GitHub PRs that request the user's review, newest update first.

        Results are cached for 60 s; refresh=True fetches again.
        Returns {"fetched_at": str, "items": [{repo, number, title, author, url,
        updated_at, is_draft, review_id}]}. review_id is null until the PR is opened here.
        Read-only.
        """
        try:
            return await prs.inbox(refresh=refresh)
        except ReviewError as e:
            return {"error": str(e)}

    @mcp.tool()
    async def open_pr(ref: str) -> dict[str, object]:
        """Open a GitHub PR for review in the browser.

        ref is one of: a PR URL; owner/name#123; #123 or 123 (uses default_repo from the
        daemon's config.toml); a 7-40 character commit SHA; or a branch name (in
        default_repo). If a SHA or branch matches several PRs, the open one wins and "note"
        says which PR was picked.

        Fetches the PR into a local worktree at its head commit, which can take a while for
        a large repo on first open. Opening a PR that is already open here returns the same
        review_id and moves it to the PR's current head.
        Returns {"review_id": str, "url": str, "note"?: str}. Never posts to GitHub.
        """
        try:
            opened = await prs.open_pr(ref)
        except ReviewError as e:
            return {"error": str(e)}
        service.open_browser(opened.url)
        result: dict[str, object] = {"review_id": opened.review.id, "url": opened.url}
        if opened.note:
            result["note"] = opened.note
        return result

    @mcp.tool()
    async def get_review(review_id: str) -> dict[str, object]:
        """Return a PR review: metadata, changed files, and a summary of its threads.

        Includes title, author, body, state, base_ref/head_ref, base_sha, head_sha,
        merge_base_sha (the diff is merge_base_sha..head_sha), worktree_path (a local
        checkout of head_sha that you can read), github (CI checks and review decision as
        of the last open or refresh, or null), files [{path, old_path, status, additions,
        deletions, binary, viewed}], thread_counts, and threads. Does not call GitHub.
        """
        try:
            return await prs.get_review(review_id)
        except ReviewError as e:
            return {"error": str(e)}

    return mcp
