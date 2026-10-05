import asyncio
import json
from collections.abc import AsyncIterator, Collection
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from code_review_mcp.config import Settings
from code_review_mcp.errors import ConflictError, NotFoundError, ReviewError
from code_review_mcp.hub import ReviewHub
from code_review_mcp.models import CommentRequest, ReplyRequest
from code_review_mcp.service import ReviewService
from code_review_mcp.store import Store
from code_review_mcp.tools import build_mcp, transport_security

STATIC_DIR = Path(__file__).parent / "static"
INDEX_HTML = STATIC_DIR / "index.html"
SSE_KEEPALIVE_SECONDS = 30.0
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ApiOriginGuard:
    """Reject a state-changing /api/ request that carries an Origin header not in the allowed set.

    Requests without an Origin header pass. Paths outside /api/ pass.
    """

    def __init__(self, app: ASGIApp, allowed_origins: Collection[str]) -> None:
        self._app = app
        self._allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope["path"].startswith("/api/")
            and scope["method"] not in SAFE_METHODS
        ):
            origin = Headers(scope=scope).get("origin")
            if origin is not None and origin not in self._allowed_origins:
                response = JSONResponse(
                    {"error": f"Origin {origin!r} is not allowed"}, status_code=403
                )
                await response(scope, receive, send)
                return
        await self._app(scope, receive, send)


def build_api_router(service: ReviewService, hub: ReviewHub, store: Store) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/health")
    async def health() -> dict[str, object]:
        return {"status": "ok", "schema_version": store.schema_version}

    @router.get("/reviews")
    async def list_reviews() -> list[dict[str, object]]:
        return service.list_reviews()

    @router.get("/reviews/{review_id}/view")
    async def get_view(review_id: str) -> dict[str, object]:
        return service.view(review_id)

    @router.get("/reviews/{review_id}/comments")
    async def get_comments(review_id: str) -> list[dict[str, object]]:
        return service.comments(review_id)

    @router.post("/reviews/{review_id}/comments")
    async def add_comment(review_id: str, body: CommentRequest) -> dict[str, object]:
        thread = service.add_user_comment(review_id, body)
        return {"id": thread.id}

    @router.post("/reviews/{review_id}/submit")
    async def submit(review_id: str) -> dict[str, object]:
        return {"submitted": service.submit(review_id)}

    @router.delete("/threads/{thread_id}")
    async def delete_thread(thread_id: str) -> dict[str, object]:
        service.delete_thread(thread_id)
        return {"deleted": True}

    @router.post("/threads/{thread_id}/reply")
    async def reply(thread_id: str, body: ReplyRequest) -> dict[str, object]:
        result = service.reply(thread_id, "user", body.message)
        return {"id": result.message.id, "reopened": result.reopened}

    @router.get("/events")
    async def events(review: str) -> StreamingResponse:
        service.require_review(review)

        async def stream() -> AsyncIterator[str]:
            queue = hub.subscribe(review)
            try:
                yield f"data: {json.dumps({'type': 'connected', 'review_id': review})}\n\n"
                while True:
                    try:
                        message = await asyncio.wait_for(queue.get(), SSE_KEEPALIVE_SECONDS)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"data: {message}\n\n"
            finally:
                hub.unsubscribe(review, queue)

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
        )

    return router


def create_app(settings: Settings, store: Store) -> FastAPI:
    """Build the daemon app: REST + SSE under /api, MCP (streamable HTTP) at /mcp, the UI at
    / and /r/{review_id}, and its built assets under /static.

    The app closes `store` when its lifespan ends.
    """
    hub = ReviewHub()
    service = ReviewService(store, hub, settings)
    security = transport_security(settings)
    mcp = build_mcp(service, security)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            store.close()

    app = FastAPI(
        title="code-review-mcp",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.service = service
    app.state.mcp = mcp

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=sorted({"127.0.0.1", "localhost", settings.host}),
    )
    app.add_middleware(ApiOriginGuard, allowed_origins=security.allowed_origins)

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(ReviewError)
    async def review_error(_: Request, exc: ReviewError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    app.include_router(build_api_router(service, hub, store))
    # Mount("/mcp") would answer POST /mcp with a 307 to /mcp/, so add the route itself.
    app.router.routes.extend(mcp_app.routes)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    @app.get("/r/{review_id}", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(
            INDEX_HTML, media_type="text/html", headers={"Cache-Control": "no-cache"}
        )

    return app
