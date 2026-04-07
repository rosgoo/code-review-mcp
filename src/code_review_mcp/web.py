"""FastAPI app, HTTP routes, uvicorn management, and static file serving."""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from code_review_mcp.models import Comment, CommentRequest, Reply, ReplyRequest
from code_review_mcp.serialize import serialize_comment, serialize_file, serialize_reply
from code_review_mcp.state import broadcast, find_free_port, state

STATIC_DIR = Path(__file__).parent / "static"

api = FastAPI(title="Code Review MCP UI", docs_url=None, redoc_url=None)
api.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_server_thread: threading.Thread | None = None
_server_port: int | None = None
_uvicorn_server: uvicorn.Server | None = None


@api.get("/")
def get_ui() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@api.get("/view")
def get_view() -> JSONResponse:
    """Return the current view state (mode, title, and content)."""
    with state.lock:
        result: dict[str, object] = {"mode": state.mode, "title": state.title}
        if state.mode == "diff":
            result["diff"] = state.diff_text
        elif state.mode == "files":
            result["files"] = [serialize_file(f) for f in state.files]
        return JSONResponse(result)


@api.get("/diff")
def get_diff() -> JSONResponse:
    with state.lock:
        return JSONResponse({"diff": state.diff_text, "title": state.title})


@api.get("/comments/all")
def get_all_comments() -> JSONResponse:
    with state.lock:
        return JSONResponse([serialize_comment(c) for c in state.comments])


@api.post("/comments")
def submit_comment(body: CommentRequest) -> JSONResponse:
    comment = Comment(
        id=str(uuid.uuid4()),
        file_path=body.file_path,
        line_number=body.line_number,
        line_type=body.line_type,
        line_content=body.line_content,
        user_message=body.user_message,
        timestamp=datetime.now(UTC).isoformat(),
    )
    with state.lock:
        state.comments.append(comment)
    return JSONResponse({"id": comment.id})


@api.post("/comments/submit-all")
def submit_all_drafts() -> JSONResponse:
    count = 0
    with state.lock:
        for comment in state.comments:
            if comment.status == "draft":
                comment.status = "submitted"
                count += 1
    return JSONResponse({"submitted": count})


@api.post("/comments/{comment_id}/reply")
def add_reply(comment_id: str, body: ReplyRequest) -> JSONResponse:
    reply = Reply(
        id=str(uuid.uuid4()),
        comment_id=comment_id,
        author="user",
        message=body.message,
        timestamp=datetime.now(UTC).isoformat(),
    )
    found = False
    reopened = False
    with state.lock:
        for comment in state.comments:
            if comment.id == comment_id:
                comment.replies.append(reply)
                found = True
                if comment.status == "resolved":
                    comment.status = "submitted"
                    reopened = True
                break
    if found:
        broadcast(
            "reply_added",
            {
                "comment_id": comment_id,
                "reply": serialize_reply(reply),
                "reopened": reopened,
            },
        )
        return JSONResponse({"id": reply.id, "reopened": reopened})
    return JSONResponse({"error": "not found"}, status_code=404)


@api.get("/events")
def sse_stream() -> StreamingResponse:
    def generate() -> Generator[str]:
        q: queue.SimpleQueue[str] = queue.SimpleQueue()
        with state.lock:
            state.sse_subscribers.append(q)
        try:
            yield 'data: {"type": "connected"}\n\n'
            while True:
                try:
                    msg = q.get(timeout=30)
                    yield f"data: {msg}\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with state.lock:
                if q in state.sse_subscribers:
                    state.sse_subscribers.remove(q)

    return StreamingResponse(generate(), media_type="text/event-stream")


def start_web_server() -> int:
    global _server_thread, _server_port, _uvicorn_server

    if _server_thread is not None and _server_thread.is_alive():
        assert _server_port is not None
        return _server_port

    port = find_free_port()
    _server_port = port

    config = uvicorn.Config(app=api, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    _uvicorn_server = server

    _server_thread = threading.Thread(target=server.run, daemon=True, name="code-review-mcp-web")
    _server_thread.start()

    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)

    return port
