from fastapi import APIRouter
from pydantic import BaseModel

from code_review_mcp.github import InboxName
from code_review_mcp.pr_service import PrService


class OpenPrRequest(BaseModel):
    ref: str


class ViewedRequest(BaseModel):
    path: str


def build_pr_router(prs: PrService) -> APIRouter:
    router = APIRouter(prefix="/api")

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
