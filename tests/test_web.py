import re

import httpx
import pytest

from code_review_mcp.service import ReviewService

from .conftest import SAMPLE_DIFF

COMMENT = {
    "path": "app.py",
    "side": "additions",
    "line": 2,
    "line_content": "x = 2",
    "body": "why 2?",
}


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "schema_version": 6,
        "worktrees": {"count": 0, "released": 0},
    }


async def test_list_reviews_newest_first(
    client: httpx.AsyncClient, app_service: ReviewService
) -> None:
    assert (await client.get("/api/reviews")).json() == []
    older = app_service.open_diff(SAMPLE_DIFF, "Older", "")
    newer = app_service.open_diff(SAMPLE_DIFF, "Newer", "")

    reviews = (await client.get("/api/reviews")).json()

    assert [r["id"] for r in reviews] == [newer.id, older.id]
    assert reviews[0]["url"] == f"http://127.0.0.1:7791/r/{newer.id}"
    assert reviews[0]["kind"] == "local"
    assert (reviews[0]["repo"], reviews[0]["pr_number"]) == (None, None)


async def test_view_and_not_found(client: httpx.AsyncClient, app_service: ReviewService) -> None:
    review = app_service.open_diff(SAMPLE_DIFF, "View", "")

    view = await client.get(f"/api/reviews/{review.id}/view")
    missing = await client.get("/api/reviews/nope/view")

    assert view.json() == {
        "review_id": review.id,
        "kind": "local",
        "mode": "diff",
        "title": "View",
        "diff": SAMPLE_DIFF,
    }
    assert missing.status_code == 404
    assert "nope" in missing.json()["error"]


async def test_comment_submit_reply_flow(
    client: httpx.AsyncClient, app_service: ReviewService
) -> None:
    service = app_service
    review = service.open_diff(SAMPLE_DIFF, "Flow", "")
    base = f"/api/reviews/{review.id}"

    created = await client.post(f"{base}/comments", json=COMMENT)
    thread_id = created.json()["id"]
    drafts = (await client.get(f"{base}/comments")).json()
    submitted = await client.post(f"{base}/submit")

    assert created.status_code == 200
    assert [(c["id"], c["status"]) for c in drafts] == [(thread_id, "draft")]
    assert submitted.json() == {"submitted": 1}
    assert [c["id"] for c in service.submitted_comments(review.id)] == [thread_id]

    service.resolve(thread_id)
    reply = await client.post(f"/api/threads/{thread_id}/reply", json={"message": "again"})

    assert reply.json()["reopened"] is True
    [comment] = (await client.get(f"{base}/comments")).json()
    assert comment["status"] == "submitted"
    assert comment["replies"][0]["message"] == "again"
    assert comment["replies"][0]["author"] == "user"


async def test_unknown_ids_return_404(client: httpx.AsyncClient) -> None:
    responses = [
        await client.post("/api/reviews/nope/comments", json=COMMENT),
        await client.post("/api/reviews/nope/submit"),
        await client.get("/api/reviews/nope/comments"),
        await client.post("/api/threads/nope/reply", json={"message": "hi"}),
        await client.get("/api/events", params={"review": "nope"}),
    ]
    assert [r.status_code for r in responses] == [404] * len(responses)


async def test_invalid_comment_is_rejected(
    client: httpx.AsyncClient, app_service: ReviewService
) -> None:
    review = app_service.open_diff(SAMPLE_DIFF, "Bad", "")
    bad_side = await client.post(
        f"/api/reviews/{review.id}/comments", json={**COMMENT, "side": "sideways"}
    )
    legacy_shape = await client.post(
        f"/api/reviews/{review.id}/comments",
        json={"file_path": "app.py", "line_number": 2, "user_message": "old shape"},
    )
    negative_line = await client.post(
        f"/api/reviews/{review.id}/comments", json={**COMMENT, "line": -1}
    )
    assert [r.status_code for r in (bad_side, legacy_shape, negative_line)] == [422, 422, 422]


async def test_foreign_host_header_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health", headers={"Host": "evil.example:7791"})
    assert response.status_code == 400


async def test_ui_is_served(client: httpx.AsyncClient, app_service: ReviewService) -> None:
    review = app_service.open_diff(SAMPLE_DIFF, "UI", "")
    index = await client.get("/")
    review_page = await client.get(f"/r/{review.id}")
    match = re.search(r'src="(/static/assets/[^"]+\.js)"', index.text)
    assert match is not None
    script = await client.get(match.group(1))

    assert index.status_code == 200
    assert review_page.status_code == 200
    assert review_page.text == index.text
    assert script.status_code == 200
    assert "/api/reviews" in script.text


async def test_static_assets_must_be_revalidated(client: httpx.AsyncClient) -> None:
    index = await client.get("/")
    match = re.search(r'src="(/static/assets/[^"]+\.js)"', index.text)
    assert match is not None

    first = await client.get(match.group(1))
    again = await client.get(match.group(1), headers={"If-None-Match": first.headers["etag"]})

    assert index.headers["cache-control"] == "no-cache"
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-cache"
    assert again.status_code == 304
    assert again.headers["cache-control"] == "no-cache"


async def test_delete_thread(client: httpx.AsyncClient, app_service: ReviewService) -> None:
    review = app_service.open_diff(SAMPLE_DIFF, "Delete", "")
    base = f"/api/reviews/{review.id}"
    submitted_id = (await client.post(f"{base}/comments", json=COMMENT)).json()["id"]
    await client.post(f"{base}/submit")
    draft_id = (await client.post(f"{base}/comments", json=COMMENT)).json()["id"]

    foreign = await client.delete(
        f"/api/threads/{draft_id}", headers={"Origin": "http://evil.example"}
    )
    deleted = await client.delete(f"/api/threads/{draft_id}")
    conflict = await client.delete(f"/api/threads/{submitted_id}")
    missing = await client.delete(f"/api/threads/{draft_id}")

    assert foreign.status_code == 403
    assert (deleted.status_code, deleted.json()) == (200, {"deleted": True})
    assert conflict.status_code == 409
    assert "only a draft or stale thread can be deleted" in conflict.json()["error"]
    assert missing.status_code == 404
    assert [c["id"] for c in (await client.get(f"{base}/comments")).json()] == [submitted_id]


@pytest.mark.parametrize("origin", ["http://evil.example", "null", "http://127.0.0.1:9999"])
async def test_foreign_origin_cannot_change_state(
    client: httpx.AsyncClient, app_service: ReviewService, origin: str
) -> None:
    review = app_service.open_diff(SAMPLE_DIFF, "Guarded", "")
    base = f"/api/reviews/{review.id}"
    thread_id = (await client.post(f"{base}/comments", json=COMMENT)).json()["id"]
    headers = {"Origin": origin}

    responses = [
        await client.post(f"{base}/comments", json=COMMENT, headers=headers),
        await client.post(f"{base}/submit", headers=headers),
        await client.post(
            f"/api/threads/{thread_id}/reply", json={"message": "x"}, headers=headers
        ),
    ]

    assert [r.status_code for r in responses] == [403, 403, 403]
    assert responses[1].json() == {"error": f"Origin {origin!r} is not allowed"}
    [comment] = (await client.get(f"{base}/comments")).json()
    assert comment["status"] == "draft"
    assert comment["replies"] == []


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "http://127.0.0.1:7791"}, {"Origin": "http://localhost:7791"}],
)
async def test_allowed_or_missing_origin_can_change_state(
    client: httpx.AsyncClient, app_service: ReviewService, headers: dict[str, str]
) -> None:
    review = app_service.open_diff(SAMPLE_DIFF, "Allowed", "")
    base = f"/api/reviews/{review.id}"

    created = await client.post(f"{base}/comments", json=COMMENT, headers=headers)
    submitted = await client.post(f"{base}/submit", headers=headers)

    assert created.status_code == 200
    assert submitted.json() == {"submitted": 1}


async def test_foreign_origin_can_still_read(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/reviews", headers={"Origin": "http://evil.example"})
    assert response.status_code == 200
