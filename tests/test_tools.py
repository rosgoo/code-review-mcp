import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.memory import create_connected_server_and_client_session

from code_review_mcp.models import CommentRequest
from code_review_mcp.service import ReviewService

from .conftest import BASE_URL

TOOL_NAMES = {
    "show_files",
    "open_diff",
    "update_diff",
    "get_comments",
    "wait_for_comments",
    "resolve_thread",
    "reply_to_thread",
    "list_review_requests",
    "open_pr",
    "get_review",
}


@asynccontextmanager
async def _memory_session(app: FastAPI) -> AsyncIterator[ClientSession]:
    async with create_connected_server_and_client_session(app.state.mcp) as session:
        yield session


async def _call(session: ClientSession, name: str, **arguments: Any) -> Any:
    result = await session.call_tool(name, arguments)
    assert not result.isError, result.content
    return result.structuredContent


async def test_tools_are_registered(app: FastAPI) -> None:
    async with _memory_session(app) as session:
        tools = await session.list_tools()
    assert {tool.name for tool in tools.tools} == TOOL_NAMES


async def test_open_diff_wait_reply_resolve(
    app: FastAPI, app_service: ReviewService, diff_file: Path
) -> None:
    service = app_service
    async with _memory_session(app) as session:
        opened = await _call(session, "open_diff", diff_file=str(diff_file), title="Tools")
        review_id = opened["review_id"]
        assert opened["url"] == f"http://127.0.0.1:7791/r/{review_id}"

        waiter = asyncio.create_task(
            _call(session, "wait_for_comments", review_id=review_id, timeout_seconds=5)
        )
        await asyncio.sleep(0.1)
        thread = service.add_user_comment(
            review_id,
            CommentRequest(path="app.py", side="additions", line=2, body="why?"),
        )
        service.submit(review_id)
        waited = await asyncio.wait_for(waiter, 2)

        assert waited["status"] == "submitted"
        [comment] = waited["comments"]
        assert comment["id"] == thread.id
        assert comment["line_type"] == "add"

        replied = await _call(session, "reply_to_thread", thread_id=thread.id, message="ok")
        resolved = await _call(session, "resolve_thread", thread_id=thread.id)
        remaining = await _call(session, "get_comments", review_id=review_id)

    assert replied["ok"] is True
    assert resolved == {"ok": True}
    assert remaining == {"result": []}


async def test_tool_errors(app: FastAPI) -> None:
    async with _memory_session(app) as session:
        missing_comments = await session.call_tool("get_comments", {"review_id": "nope"})
        missing_wait = await _call(session, "wait_for_comments", review_id="nope")
        missing_reply = await _call(session, "reply_to_thread", thread_id="nope", message="x")
        empty_diff = await _call(session, "open_diff")

    assert missing_comments.isError
    assert "nope" in missing_wait["error"]
    assert missing_reply["ok"] is False
    assert empty_diff == {"error": "Provide either diff or diff_file"}


async def test_show_files_paths_and_content(
    app: FastAPI, app_service: ReviewService, repo_dir: Path
) -> None:
    service = app_service
    async with _memory_session(app) as session:
        from_paths = await _call(
            session, "show_files", paths=[str(repo_dir / "app.py"), "relative/missing.py"]
        )
        from_content = await _call(
            session, "show_files", content="print(1)", content_language="python"
        )

    assert from_paths["file_count"] == 2
    files = service.view(from_paths["review_id"])["files"]
    assert files[0]["language"] == "python"  # type: ignore[index]
    assert "pass an absolute path" in files[1]["content"]  # type: ignore[index]
    assert from_content["file_count"] == 1


async def test_mcp_over_streamable_http(app: FastAPI, diff_file: Path) -> None:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
        streamable_http_client(f"{BASE_URL}/mcp", http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        opened = await _call(session, "open_diff", diff_file=str(diff_file))
        comments = await _call(session, "get_comments", review_id=opened["review_id"])

    assert init.serverInfo.name == "code"
    assert "review_id" in (init.instructions or "")
    assert comments == {"result": []}


async def test_relative_paths_are_rejected(
    app: FastAPI, app_service: ReviewService, diff_file: Path
) -> None:
    async with _memory_session(app) as session:
        relative_dir = await _call(session, "open_diff", diff_file=str(diff_file), working_dir=".")
        relative_diff = await _call(session, "open_diff", diff_file="review.diff")
        relative_content = await _call(session, "show_files", content_file="notes.md")
        opened = await _call(session, "open_diff", diff_file=str(diff_file))
        relative_update = await _call(
            session, "update_diff", review_id=opened["review_id"], diff_file="review.diff"
        )

    assert relative_dir == {"error": "working_dir must be an absolute path, got '.'"}
    assert relative_diff == {"error": "diff_file must be an absolute path, got 'review.diff'"}
    assert relative_content == {"error": "content_file must be an absolute path, got 'notes.md'"}
    assert relative_update == {"error": "diff_file must be an absolute path, got 'review.diff'"}
    assert [r["id"] for r in app_service.list_reviews()] == [opened["review_id"]]
    assert app_service.view(opened["review_id"])["diff"] == diff_file.read_text()


async def test_home_relative_paths_are_expanded(
    app: FastAPI,
    app_service: ReviewService,
    diff_file: Path,
    repo_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(diff_file.parent))
    async with _memory_session(app) as session:
        opened = await _call(
            session, "open_diff", diff_file=f"~/{diff_file.name}", working_dir=f"~/{repo_dir.name}"
        )

    review = app_service.require_review(opened["review_id"])
    assert review.working_dir == str(repo_dir.resolve())
    assert review.mode == "files"


async def test_unreadable_absolute_diff_file_is_reported(app: FastAPI, tmp_path: Path) -> None:
    async with _memory_session(app) as session:
        missing = await _call(session, "open_diff", diff_file=str(tmp_path / "missing.diff"))

    assert missing["error"].startswith("Cannot read diff_file:")


async def test_mcp_route_keeps_fastmcp_origin_check(app: FastAPI) -> None:
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as http,
    ):
        response = await http.post(
            "/mcp",
            headers={
                "Origin": "http://evil.example",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 403
    assert "Origin" in response.text
