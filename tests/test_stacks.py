import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from code_review_mcp.config import Settings
from code_review_mcp.github import GitHubClient, parse_inbox_page
from code_review_mcp.procs import CommandResult
from code_review_mcp.stacks import (
    CHILDREN_QUERY,
    HOP_CAP,
    PARENT_QUERY,
    STACK_QUERY,
    StackFinder,
)
from code_review_mcp.store import PrReviewFields, Store
from code_review_mcp.web import create_app

from .conftest import BASE_URL, SAMPLE_DIFF
from .pr_fixtures import gql_page, gql_pr

REPO = "acme/widgets"


@dataclass
class FakePr:
    number: int
    head: str
    base: str
    state: str = "OPEN"
    stack: int | None = None
    position: int | None = None


@dataclass
class FakeGitHub:
    """Answers the stack GraphQL queries from a set of PRs, the way GitHub would."""

    prs: list[FakePr]
    default_branch: str = "main"
    stack_bases: dict[int, str] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)

    def _node(self, pr: FakePr) -> dict[str, Any]:
        return {
            "number": pr.number,
            "title": f"PR {pr.number}",
            "state": pr.state,
            "isDraft": pr.number % 2 == 0,
            "headRefName": pr.head,
            "baseRefName": pr.base,
            "url": f"https://github.com/{REPO}/pull/{pr.number}",
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        }

    def _open(self, **match: str) -> list[FakePr]:
        key, value = next(iter(match.items()))
        return [p for p in self.prs if p.state == "OPEN" and getattr(p, key) == value][:5]

    def _stack_json(self, number: int) -> dict[str, Any]:
        entries = sorted((p for p in self.prs if p.stack == number), key=lambda p: p.position or 0)
        return {
            "number": number,
            "size": len(entries),
            "baseRefName": self.stack_bases.get(number, self.default_branch),
            "entries": {
                "nodes": [{"position": p.position, "pullRequest": self._node(p)} for p in entries]
            },
        }

    def _answer(self, query: str, variables: Mapping[str, str]) -> dict[str, Any]:
        if query == STACK_QUERY:
            self.queries.append("stack")
            pr = next(p for p in self.prs if p.number == int(variables["number"]))
            node = {
                **self._node(pr),
                "stackEntry": {"position": pr.position} if pr.stack else None,
                "stack": self._stack_json(pr.stack) if pr.stack else None,
            }
            return {
                "repository": {
                    "defaultBranchRef": {"name": self.default_branch},
                    "pullRequest": node,
                }
            }
        if query == PARENT_QUERY:
            self.queries.append("parent")
            nodes = [
                {**self._node(p), "stack": {"number": p.stack} if p.stack else None}
                for p in self._open(head=variables["ref"])
            ]
            return {"repository": {"pullRequests": {"nodes": nodes}}}
        if query == CHILDREN_QUERY:
            self.queries.append("children")
            nodes = [self._node(p) for p in self._open(base=variables["ref"])]
            return {"repository": {"pullRequests": {"nodes": nodes}}}
        self.queries.append("extensions")
        refs = {k: v for k, v in variables.items() if k.startswith("r") and k[1:].isdigit()}
        return {
            "repository": {
                f"e{k[1:]}": {"nodes": [self._node(p) for p in self._open(base=ref)]}
                for k, ref in refs.items()
            }
        }

    async def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        fields: dict[str, str] = {}
        for flag, value in zip(args, args[1:], strict=False):
            if flag in ("-f", "-F"):
                name, _, raw = value.partition("=")
                fields[name] = raw
        data = self._answer(fields.pop("query"), fields)
        return CommandResult(tuple(args), 0, json.dumps({"data": data}).encode(), b"")


def _chain(count: int, *, base: str = "main", start: int = 1) -> list[FakePr]:
    prs: list[FakePr] = []
    for i in range(start, start + count):
        prs.append(FakePr(number=i, head=f"b{i}", base=base if i == start else f"b{i - 1}"))
    return prs


def _finder(fake: FakeGitHub, **kwargs: Any) -> StackFinder:
    return StackFinder(GitHubClient(fake), **kwargs)


async def test_native_stack_with_extensions() -> None:
    stack = [
        FakePr(i, f"s{i}", "main" if i == 1 else f"s{i - 1}", stack=900, position=i)
        for i in (1, 2, 3)
    ]
    fake = FakeGitHub(
        prs=[
            *stack,
            FakePr(4, "on-top", "s3"),
            FakePr(5, "side", "s1"),
            FakePr(6, "closed-child", "s2", state="CLOSED"),
        ]
    )

    found = await _finder(fake).find(REPO, 2)

    assert found is not None
    assert (found.source, found.number, found.size, found.base_ref, found.position) == (
        "github",
        900,
        3,
        "main",
        2,
    )
    assert [(e.position, e.pr.number) for e in found.entries] == [(1, 1), (2, 2), (3, 3)]
    assert [(x.pr.number, x.based_on) for x in found.extensions] == [(5, 1), (4, 3)]
    assert found.entries[1].pr.ci_state == "success"
    assert found.entries[1].pr.state == "open"
    assert fake.queries == ["stack", "extensions"]


async def test_pr_on_top_of_a_native_stack_gets_that_stack_with_no_position() -> None:
    stack = [
        FakePr(i, f"s{i}", "main" if i == 1 else f"s{i - 1}", stack=900, position=i) for i in (1, 2)
    ]
    fake = FakeGitHub(prs=[*stack, FakePr(7, "top", "s2")])

    found = await _finder(fake).find(REPO, 7)

    assert found is not None
    assert (found.source, found.number, found.position) == ("github", 900, None)
    assert [x.pr.number for x in found.extensions] == [7]


async def test_branch_chain_with_a_fork() -> None:
    fake = FakeGitHub(
        prs=[*_chain(3, start=10), FakePr(13, "fork-a", "b12"), FakePr(14, "fork-b", "b12")]
    )

    found = await _finder(fake).find(REPO, 11)

    assert found is not None
    assert (found.source, found.number, found.size, found.base_ref, found.position) == (
        "branches",
        None,
        3,
        "main",
        2,
    )
    assert [(e.position, e.pr.number) for e in found.entries] == [(1, 10), (2, 11), (3, 12)]
    assert [(x.pr.number, x.based_on) for x in found.extensions] == [(13, 12), (14, 12)]
    assert fake.queries == ["stack", "parent", "children", "children"]


async def test_branch_chain_stops_at_an_ambiguous_parent() -> None:
    fake = FakeGitHub(
        prs=[
            FakePr(1, "x", "main"),
            FakePr(2, "x", "other"),
            FakePr(3, "y", "x"),
            FakePr(4, "z", "y"),
        ]
    )

    found = await _finder(fake).find(REPO, 3)
    alone = await _finder(FakeGitHub(prs=fake.prs[:3])).find(REPO, 3)

    assert found is not None
    assert [(e.position, e.pr.number) for e in found.entries] == [(1, 3), (2, 4)]
    assert (found.position, found.base_ref) == (1, "x")
    assert alone is None


async def test_branch_walk_stops_at_the_hop_cap() -> None:
    fake = FakeGitHub(prs=_chain(40))

    found = await _finder(fake).find(REPO, 1)

    assert found is not None
    assert found.size == HOP_CAP + 1
    assert [e.pr.number for e in found.entries] == list(range(1, HOP_CAP + 2))
    assert fake.queries.count("children") == HOP_CAP


async def test_pr_in_no_stack() -> None:
    fake = FakeGitHub(prs=[FakePr(1, "solo", "main"), FakePr(2, "other", "main")])

    assert await _finder(fake).find(REPO, 1) is None
    assert fake.queries == ["stack", "children"]


async def test_stack_cache_returns_stale_data_and_refreshes_once() -> None:
    fake = FakeGitHub(prs=_chain(2))
    now = [100.0]
    finder = _finder(fake, clock=lambda: now[0], ttl=60)

    first, second = await asyncio.gather(finder.stack(REPO, 1), finder.stack(REPO, 1))
    calls_after_first = len(fake.queries)
    now[0] += 59
    cached = await finder.stack(REPO, 1)
    fake.prs.append(FakePr(3, "b3", "b2"))
    now[0] += 2
    stale = await finder.stack(REPO, 1)
    await asyncio.sleep(0)
    for _ in range(20):
        await asyncio.sleep(0)
    fresh = await finder.stack(REPO, 1)

    assert first is second
    assert first is not None and first.size == 2
    assert cached is first
    assert len(fake.queries) > calls_after_first
    assert stale is first
    assert fresh is not None and fresh.size == 3


def test_inbox_items_carry_their_stack() -> None:
    page = gql_page(
        [
            gql_pr(1, stackEntry={"position": 3}, stack={"number": 900, "size": 10}),
            gql_pr(2, stackEntry=None, stack=None),
            gql_pr(3),
        ]
    )

    _, prs, _ = parse_inbox_page(page)

    assert [(p.number, p.stack) for p in prs][1:] == [(2, None), (3, None)]
    stacked = prs[0].stack
    assert stacked is not None
    assert (stacked.number, stacked.size, stacked.position) == (900, 10, 3)


def _pr_review(store: Store, number: int, head: str, base: str) -> str:
    return store.upsert_pr_review(
        PrReviewFields(
            repo=REPO,
            pr_number=number,
            title=f"PR {number}",
            author="octocat",
            url=f"https://github.com/{REPO}/pull/{number}",
            pr_body="",
            pr_state="open",
            is_draft=False,
            base_ref=base,
            head_ref=head,
            base_sha="b" * 40,
            head_sha="h" * 40,
            merge_base_sha="m" * 40,
            worktree_path=f"/wt/{number}",
        )
    ).id


async def test_stack_route(settings: Settings, store: Store) -> None:
    stack = [
        FakePr(i, f"s{i}", "main" if i == 1 else f"s{i - 1}", stack=900, position=i) for i in (1, 2)
    ]
    fake = FakeGitHub(prs=[*stack, FakePr(3, "top", "s2"), FakePr(8, "solo", "main")])
    app = create_app(settings, store, github=GitHubClient(fake))
    review_2 = _pr_review(store, 2, "s2", "s1")
    review_3 = _pr_review(store, 3, "top", "s2")
    solo = _pr_review(store, 8, "solo", "main")
    local = app.state.service.open_diff(SAMPLE_DIFF, "Local", "")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as api:
        body = (await api.get(f"/api/reviews/{review_2}/stack")).json()
        none = await api.get(f"/api/reviews/{solo}/stack")
        missing = await api.get("/api/reviews/nope/stack")
        not_pr = await api.get(f"/api/reviews/{local.id}/stack")

    assert body == {
        "source": "github",
        "number": 900,
        "size": 2,
        "base_ref": "main",
        "position": 2,
        "entries": [
            {
                "position": 1,
                "number": 1,
                "title": "PR 1",
                "state": "open",
                "is_draft": False,
                "head_ref": "s1",
                "base_ref": "main",
                "url": f"https://github.com/{REPO}/pull/1",
                "ci_state": "success",
                "review_id": None,
            },
            {
                "position": 2,
                "number": 2,
                "title": "PR 2",
                "state": "open",
                "is_draft": True,
                "head_ref": "s2",
                "base_ref": "s1",
                "url": f"https://github.com/{REPO}/pull/2",
                "ci_state": "success",
                "review_id": review_2,
            },
        ],
        "extensions": [
            {
                "number": 3,
                "title": "PR 3",
                "state": "open",
                "is_draft": False,
                "head_ref": "top",
                "base_ref": "s2",
                "url": f"https://github.com/{REPO}/pull/3",
                "ci_state": "success",
                "review_id": review_3,
                "based_on": 2,
            }
        ],
    }
    assert (none.status_code, none.json()) == (200, None)
    assert (missing.status_code, not_pr.status_code) == (404, 400)
