from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from code_review_mcp.agents import AgentRunner
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
    kind: Literal["review_comment", "question"]
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


class SendRequest(BaseModel):
    thread_ids: list[str] | None = None


class MessageRequest(BaseModel):
    body: str


class SubmitReviewRequest(BaseModel):
    event: SubmissionEvent
    body: str = ""


def build_pr_router(
    prs: PrService, threads: ThreadService, stacks: StackService, agents: AgentRunner
) -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/reviews/{review_id}/agent")
    async def agent_status(review_id: str) -> dict[str, object]:
        return agents.status(review_id)

    @router.post("/reviews/{review_id}/agent/warmup")
    async def agent_warmup(review_id: str) -> dict[str, object]:
        return await agents.start_warmup(review_id)

    @router.post("/reviews/{review_id}/agent/send")
    async def agent_send(review_id: str, body: SendRequest | None = None) -> dict[str, object]:
        return agents.send(review_id, body.thread_ids if body is not None else None)

    @router.post("/reviews/{review_id}/agent/stop")
    async def agent_stop(review_id: str) -> dict[str, object]:
        return await agents.stop(review_id)

    @router.patch("/messages/{message_id}")
    async def update_message(message_id: str, body: MessageRequest) -> dict[str, object]:
        return threads.update_message(message_id, body.body)

    @router.delete("/messages/{message_id}")
    async def delete_message(message_id: str) -> dict[str, object]:
        return threads.delete_message(message_id)

    @router.get("/reviews/{review_id}/stack")
    async def review_stack(review_id: str) -> dict[str, object] | None:
        return await stacks.review_stack(review_id)

    @router.get("/reviews/{review_id}/threads")
    async def list_threads(review_id: str) -> list[dict[str, object]]:
        return threads.list_threads(review_id)

    @router.post("/reviews/{review_id}/threads")
    async def create_thread(review_id: str, body: CreateThreadRequest) -> dict[str, object]:
        anchor = AnchorRequest(body.side, body.line, body.start_line, body.start_side)
        if body.kind == "question":
            return threads.create_question(review_id, body.path, anchor, body.body)
        return await threads.create_thread(review_id, body.path, anchor, body.body)

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
