from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from code_review_mcp.github import InboxName
from code_review_mcp.pr_service import PrService
from code_review_mcp.review_threads import AnchorRequest, ThreadService
from code_review_mcp.stacks import StackService
from code_review_mcp.store import Side, SubmissionEvent


class OpenPrRequest(BaseModel):
    ref: str


class ViewedRequest(BaseModel):
    path: str


class CreateThreadRequest(BaseModel):
    kind: Literal["review_comment"]
    path: str
    side: Side | None = None
    line: int
    start_line: int | None = None
    start_side: Side | None = None
    body: str


class UpdateThreadRequest(BaseModel):
    body: str | None = None
    side: Side | None = None
    line: int | None = None
    start_line: int | None = None
    start_side: Side | None = None


class SubmitReviewRequest(BaseModel):
    event: SubmissionEvent
    body: str = ""


def build_pr_router(prs: PrService, threads: ThreadService, stacks: StackService) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/reviews/{review_id}/stack")
    async def review_stack(review_id: str) -> dict[str, object] | None:
        return await stacks.review_stack(review_id)

    @router.get("/reviews/{review_id}/threads")
    async def list_threads(review_id: str) -> list[dict[str, object]]:
        return threads.list_threads(review_id)

    @router.post("/reviews/{review_id}/threads")
    async def create_thread(review_id: str, body: CreateThreadRequest) -> dict[str, object]:
        return await threads.create_thread(
            review_id,
            body.path,
            AnchorRequest(body.side, body.line, body.start_line, body.start_side),
            body.body,
        )

    @router.patch("/threads/{thread_id}")
    async def update_thread(thread_id: str, body: UpdateThreadRequest) -> dict[str, object]:
        anchor = (
            AnchorRequest(body.side, body.line, body.start_line, body.start_side)
            if body.line is not None
            else None
        )
        return await threads.update_thread(thread_id, body=body.body, anchor=anchor)

    @router.post("/reviews/{review_id}/submit-review")
    async def submit_review(review_id: str, body: SubmitReviewRequest) -> dict[str, object]:
        return await threads.submit_review(review_id, body.event, body.body)

    @router.get("/inbox")
    async def inbox(refresh: bool = False) -> dict[str, object]:
        return await prs.inbox(refresh=refresh)

    @router.get("/inbox/{name}")
    async def inbox_list(name: InboxName, refresh: bool = False) -> dict[str, object]:
        return await prs.inbox_list(name, refresh=refresh)

    @router.post("/prs/open")
    async def open_pr(body: OpenPrRequest) -> dict[str, object]:
        opened = await prs.open_pr(body.ref)
        result: dict[str, object] = {"review_id": opened.review.id, "url": opened.url}
        if opened.note:
            result["note"] = opened.note
        return result

    @router.get("/reviews/{review_id}/pr")
    async def get_pr(review_id: str) -> dict[str, object]:
        return await prs.get_pr_view(review_id)

    @router.get("/reviews/{review_id}/file")
    async def get_file(review_id: str, path: str) -> dict[str, object]:
        return await prs.get_file(review_id, path)

    @router.post("/reviews/{review_id}/refresh")
    async def refresh(review_id: str) -> dict[str, object]:
        result = await prs.refresh(review_id)
        return {
            "head_moved": result.head_moved,
            "old_head_sha": result.old_head_sha,
            "new_head_sha": result.new_head_sha,
            "pr": await prs.get_pr_view(review_id),
        }

    @router.put("/reviews/{review_id}/viewed")
    async def mark_viewed(review_id: str, body: ViewedRequest) -> dict[str, object]:
        return await prs.set_viewed(review_id, body.path, viewed=True)

    @router.delete("/reviews/{review_id}/viewed")
    async def unmark_viewed(review_id: str, body: ViewedRequest) -> dict[str, object]:
        return await prs.set_viewed(review_id, body.path, viewed=False)

    @router.post("/reviews/{review_id}/close")
    async def close(review_id: str) -> dict[str, object]:
        await prs.close(review_id)
        return {"ok": True}

    return router
