import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp.shared.memory import create_connected_server_and_client_session

from code_review_mcp.config import Settings
from code_review_mcp.github import GitHubClient
from code_review_mcp.store import Store
from code_review_mcp.web import create_app
from code_review_mcp.worktrees import WorktreeManager

from .conftest import BASE_URL, SAMPLE_DIFF
from .pr_fixtures import (
    APP_V1,
    APP_V2,
    PR_NUMBER,
    REPO,
    FakeGh,
    PrRepo,
    gql_page,
    gql_pr,
    inbox_rule,
    make_pr_repo,
    mapped_config,
    pr_view_json,
)

PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"


@pytest.fixture
def pr_repo(tmp_path: Path) -> PrRepo:
    return make_pr_repo(tmp_path / "git")


@pytest.fixture
def fake_gh(pr_repo: PrRepo) -> FakeGh:
    fake = FakeGh()
    fake.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    fake.on(
        "pr",
        "view",
        "404",
        code=1,
        stderr=b"GraphQL: Could not resolve to a PullRequest with the number of 404.",
    )
    inbox_rule(fake, "direct", gql_page([gql_pr(PR_NUMBER, isDraft=True, title="Make x two")]))
    inbox_rule(fake, "mine", gql_page([]))
    inbox_rule(fake, "team", gql_page([gql_pr(2)]))
    fake.on(
        "search",
        "prs",
        pr_repo.head_sha,
        stdout=json.dumps(
            [
                {
                    "number": PR_NUMBER,
                    "repository": {"name": "widgets", "nameWithOwner": REPO},
                    "url": PR_URL,
                    "state": "open",
                    "updatedAt": "2026-10-05T00:00:00Z",
                }
            ]
        ).encode(),
    )
    return fake


@pytest.fixture
def pr_app(settings: Settings, store: Store, pr_repo: PrRepo, fake_gh: FakeGh) -> FastAPI:
    mapped_config(settings.home, pr_repo)
    return create_app(
        settings,
        store,
        github=GitHubClient(fake_gh),
        worktrees=WorktreeManager(settings.home, credential_helper=None),
    )


@pytest.fixture
async def pr_client(pr_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=pr_app), base_url=BASE_URL
    ) as http_client:
        yield http_client


async def _open(client: httpx.AsyncClient, ref: str) -> str:
    response = await client.post("/api/prs/open", json={"ref": ref})
    assert response.status_code == 200, response.text
    review_id: str = response.json()["review_id"]
    return review_id


async def test_open_by_url_number_and_sha(pr_client: httpx.AsyncClient, pr_repo: PrRepo) -> None:
    response = await pr_client.post("/api/prs/open", json={"ref": PR_URL})
    review_id = response.json()["review_id"]

    assert response.json() == {
        "review_id": review_id,
        "url": f"{BASE_URL}/r/{review_id}",
    }
    assert await _open(pr_client, f"#{PR_NUMBER}") == review_id
    assert await _open(pr_client, pr_repo.head_sha) == review_id
    reviews = (await pr_client.get("/api/reviews")).json()
    assert [(r["id"], r["kind"], r["repo"], r["pr_number"]) for r in reviews] == [
        (review_id, "pr", REPO, PR_NUMBER)
    ]


async def test_inbox_view_file_viewed_refresh_close(pr_client: httpx.AsyncClient) -> None:
    review_id = await _open(pr_client, PR_URL)
    base = f"/api/reviews/{review_id}"

    inbox = (await pr_client.get("/api/inbox")).json()
    direct_only = (await pr_client.get("/api/inbox/direct")).json()
    unknown_list = await pr_client.get("/api/inbox/everything")
    view = (await pr_client.get(f"{base}/pr")).json()
    file = (await pr_client.get(f"{base}/file", params={"path": "app.py"})).json()
    marked = await pr_client.put(f"{base}/viewed", json={"path": "app.py"})
    viewed_after_mark = (await pr_client.get(f"{base}/pr")).json()
    unmarked = await pr_client.request("DELETE", f"{base}/viewed", json={"path": "app.py"})
    refreshed = (await pr_client.post(f"{base}/refresh")).json()
    closed = await pr_client.post(f"{base}/close")
    after_close = (await pr_client.get(f"{base}/pr")).json()

    assert [(i["number"], i["review_id"], i["is_draft"]) for i in inbox["direct"]["items"]] == [
        (PR_NUMBER, review_id, True)
    ]
    assert [(i["number"], i["review_id"]) for i in inbox["team"]["items"]] == [(2, None)]
    assert (inbox["mine"]["total"], inbox["mine"]["items"]) == (0, [])
    assert (direct_only["name"], direct_only["refreshing"]) == ("direct", False)
    assert [i["number"] for i in direct_only["items"]] == [PR_NUMBER]
    assert unknown_list.status_code == 422
    assert sorted(f["path"] for f in view["files"]) == [
        "app.py",
        "data.bin",
        "gone.txt",
        "new_name.py",
    ]
    assert view["github_url"] == PR_URL
    assert (file["old_content"], file["new_content"]) == (APP_V1, APP_V2)
    assert marked.status_code == 200
    assert {f["path"]: f["viewed"] for f in viewed_after_mark["files"]}["app.py"] is True
    assert unmarked.json()["viewed"] is False
    assert refreshed["head_moved"] is False
    assert refreshed["pr"]["review_id"] == review_id
    assert closed.json() == {"ok": True}
    assert (after_close["status"], after_close["files"]) == ("closed", [])


async def test_error_statuses(
    pr_client: httpx.AsyncClient, pr_app: FastAPI, settings: Settings
) -> None:
    review_id = await _open(pr_client, PR_URL)
    local = pr_app.state.service.open_diff(SAMPLE_DIFF, "Local", "")

    responses = {
        "bad ref": await pr_client.post("/api/prs/open", json={"ref": "two words"}),
        "missing pr": await pr_client.post("/api/prs/open", json={"ref": "#404"}),
        "unknown review": await pr_client.get("/api/reviews/nope/pr"),
        "unknown file review": await pr_client.get("/api/reviews/nope/file", params={"path": "a"}),
        "unknown refresh": await pr_client.post("/api/reviews/nope/refresh"),
        "unknown close": await pr_client.post("/api/reviews/nope/close"),
        "file not in pr": await pr_client.get(
            f"/api/reviews/{review_id}/file", params={"path": "base_only.txt"}
        ),
        "viewed not in pr": await pr_client.put(
            f"/api/reviews/{review_id}/viewed", json={"path": "nope"}
        ),
        "local review": await pr_client.get(f"/api/reviews/{local.id}/pr"),
        "missing body": await pr_client.post("/api/prs/open", json={}),
    }
    statuses = {name: r.status_code for name, r in responses.items()}

    assert statuses == {
        "bad ref": 400,
        "missing pr": 404,
        "unknown review": 404,
        "unknown file review": 404,
        "unknown refresh": 404,
        "unknown close": 404,
        "file not in pr": 404,
        "viewed not in pr": 404,
        "local review": 400,
        "missing body": 422,
    }
    assert "Cannot read 'two words'" in responses["bad ref"].json()["error"]

    (settings.home / "config.toml").write_text("default_repo = 'broken'\n")
    broken = await pr_client.post("/api/prs/open", json={"ref": "#1"})
    assert broken.status_code == 400
    assert "default_repo" in broken.json()["error"]


async def test_foreign_origin_cannot_open_mark_or_close(pr_client: httpx.AsyncClient) -> None:
    review_id = await _open(pr_client, PR_URL)
    headers = {"Origin": "http://evil.example"}
    base = f"/api/reviews/{review_id}"

    responses = [
        await pr_client.post("/api/prs/open", json={"ref": PR_URL}, headers=headers),
        await pr_client.put(f"{base}/viewed", json={"path": "app.py"}, headers=headers),
        await pr_client.post(f"{base}/refresh", headers=headers),
        await pr_client.post(f"{base}/close", headers=headers),
    ]

    assert [r.status_code for r in responses] == [403] * 4
    assert (await pr_client.get(f"{base}/pr")).json()["status"] == "open"


def _tool_result(result: Any) -> Any:
    assert not result.isError, result.content
    return result.structuredContent


async def test_pr_tools(pr_app: FastAPI, pr_repo: PrRepo) -> None:
    async with create_connected_server_and_client_session(pr_app.state.mcp) as session:
        inbox = _tool_result(await session.call_tool("list_review_requests", {}))
        opened = _tool_result(await session.call_tool("open_pr", {"ref": PR_URL}))
        again = _tool_result(await session.call_tool("open_pr", {"ref": pr_repo.head_sha}))
        review = _tool_result(
            await session.call_tool("get_review", {"review_id": opened["review_id"]})
        )
        bad = _tool_result(await session.call_tool("open_pr", {"ref": "#404"}))
        missing = _tool_result(await session.call_tool("get_review", {"review_id": "nope"}))

    assert [i["number"] for i in inbox["direct"]["items"]] == [PR_NUMBER]
    assert [i["number"] for i in inbox["team"]["items"]] == [2]
    assert opened["url"] == f"{BASE_URL}/r/{opened['review_id']}"
    assert again["review_id"] == opened["review_id"]
    assert review["head_sha"] == pr_repo.head_sha
    assert review["merge_base_sha"] == pr_repo.fork_sha
    assert review["worktree_path"].endswith("acme-widgets-1")
    assert {f["path"] for f in review["files"]} >= {"app.py", "new_name.py"}
    assert review["threads"] == []
    assert "Could not resolve" in bad["error"]
    assert "nope" in missing["error"]
