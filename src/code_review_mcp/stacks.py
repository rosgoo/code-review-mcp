import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel

from code_review_mcp.errors import NotFoundError, ReviewError
from code_review_mcp.github import (
    GH_TIMEOUT_SECONDS,
    CheckState,
    GitHubClient,
    GitHubError,
    rollup_ci_state,
)
from code_review_mcp.store import PrKey, Store

STACK_TTL_SECONDS = 60.0
STACK_ENTRY_LIMIT = 50
CHILD_LIMIT = 5
HOP_CAP = 20

StackSource = Literal["github", "branches"]

_GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}

_PR_FIELDS = """
fragment StackPr on PullRequest {
  number title state isDraft headRefName baseRefName url
  commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
}
"""

STACK_QUERY = (
    """
query($owner: String!, $name: String!, $number: Int!, $entries: Int!) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { name }
    pullRequest(number: $number) {
      ...StackPr
      stackEntry { position }
      stack {
        number size baseRefName
        entries(first: $entries) { nodes { position pullRequest { ...StackPr } } }
      }
    }
  }
}
"""
    + _PR_FIELDS
)

PARENT_QUERY = (
    """
query($owner: String!, $name: String!, $ref: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $ref, states: OPEN, first: $first) {
      nodes { ...StackPr stack { number } }
    }
  }
}
"""
    + _PR_FIELDS
)

CHILDREN_QUERY = (
    """
query($owner: String!, $name: String!, $ref: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequests(baseRefName: $ref, states: OPEN, first: $first) { nodes { ...StackPr } }
  }
}
"""
    + _PR_FIELDS
)


def extensions_query(count: int) -> str:
    """One request for the open PRs based on each of `count` head refs ($r0, $r1, ...)."""
    variables = ", ".join(f"$r{i}: String!" for i in range(count))
    fields = " ".join(
        f"e{i}: pullRequests(baseRefName: $r{i}, states: OPEN, first: {CHILD_LIMIT}) "
        "{ nodes { ...StackPr } }"
        for i in range(count)
    )
    return (
        f"query($owner: String!, $name: String!, {variables}) "
        f"{{ repository(owner: $owner, name: $name) {{ {fields} }} }}" + _PR_FIELDS
    )


@dataclass(frozen=True)
class StackPr:
    number: int
    title: str
    state: str
    is_draft: bool
    head_ref: str
    base_ref: str
    url: str
    ci_state: CheckState | None


@dataclass(frozen=True)
class StackEntry:
    position: int
    pr: StackPr


@dataclass(frozen=True)
class StackExtension:
    pr: StackPr
    based_on: int


@dataclass(frozen=True)
class PrStack:
    source: StackSource
    number: int | None
    size: int
    base_ref: str
    position: int | None
    entries: tuple[StackEntry, ...]
    extensions: tuple[StackExtension, ...]


class _GqlModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, frozen=True)


class _GqlRollup(_GqlModel):
    state: str


class _GqlCommit(_GqlModel):
    status_check_rollup: _GqlRollup | None = None


class _GqlCommitNode(_GqlModel):
    commit: _GqlCommit


class _GqlCommits(_GqlModel):
    nodes: list[_GqlCommitNode] = []


class _GqlStackRef(_GqlModel):
    number: int


class _GqlPr(_GqlModel):
    number: int
    title: str = ""
    state: str = ""
    is_draft: bool = False
    head_ref_name: str = ""
    base_ref_name: str = ""
    url: str = ""
    commits: _GqlCommits | None = None


class _GqlParentPr(_GqlPr):
    stack: _GqlStackRef | None = None


class _GqlPrList(_GqlModel):
    nodes: list[_GqlPr] = []


class _GqlParentList(_GqlModel):
    nodes: list[_GqlParentPr] = []


class _GqlEntry(_GqlModel):
    position: int
    pull_request: _GqlPr


class _GqlEntries(_GqlModel):
    nodes: list[_GqlEntry] = []


class _GqlStack(_GqlModel):
    number: int
    size: int
    base_ref_name: str
    entries: _GqlEntries


class _GqlPosition(_GqlModel):
    position: int


class _GqlStackPr(_GqlPr):
    stack_entry: _GqlPosition | None = None
    stack: _GqlStack | None = None


class _GqlBranch(_GqlModel):
    name: str


class _GqlStackRepo(_GqlModel):
    default_branch_ref: _GqlBranch | None = None
    pull_request: _GqlStackPr | None = None


class _GqlStackData(_GqlModel):
    repository: _GqlStackRepo | None


class _GqlStackResponse(_GqlModel):
    data: _GqlStackData


class _GqlListRepo(_GqlModel):
    pull_requests: _GqlParentList


class _GqlListData(_GqlModel):
    repository: _GqlListRepo


class _GqlListResponse(_GqlModel):
    data: _GqlListData


class _GqlAliasData(_GqlModel):
    repository: dict[str, _GqlPrList]


class _GqlAliasResponse(_GqlModel):
    data: _GqlAliasData


_STACK = TypeAdapter(_GqlStackResponse)
_LIST = TypeAdapter(_GqlListResponse)
_ALIASES = TypeAdapter(_GqlAliasResponse)


def _stack_pr(pr: _GqlPr) -> StackPr:
    commits = pr.commits.nodes if pr.commits else []
    rollup = commits[-1].commit.status_check_rollup if commits else None
    return StackPr(
        number=pr.number,
        title=pr.title,
        state=pr.state.lower(),
        is_draft=pr.is_draft,
        head_ref=pr.head_ref_name,
        base_ref=pr.base_ref_name,
        url=pr.url,
        ci_state=rollup_ci_state(rollup.state) if rollup else None,
    )


def _validate[T](adapter: TypeAdapter[T], raw: bytes) -> T:
    try:
        return adapter.validate_json(raw)
    except ValidationError as e:
        raise GitHubError(f"Unexpected output from gh: {e}") from e


@dataclass(frozen=True)
class _Cached:
    stack: PrStack | None
    loaded_at: float


class StackFinder:
    """Finds the stack of a PR: GitHub's native stack, or else a chain of open PRs whose base
    branch is the head branch of the PR below.

    Results are cached per PR. A cached result returns at once; when it is older than the
    TTL, one background refresh starts. The first read waits for fresh data.
    """

    def __init__(
        self,
        github: GitHubClient,
        *,
        clock: Callable[[], float] = time.monotonic,
        ttl: float = STACK_TTL_SECONDS,
    ) -> None:
        self._runner = github.runner
        self._clock = clock
        self._ttl = ttl
        self._cache: dict[PrKey, _Cached] = {}
        self._fetches: dict[PrKey, asyncio.Task[PrStack | None]] = {}

    async def stack(self, repo: str, number: int) -> PrStack | None:
        """The PR's stack, or None when it is in no stack. Raises GitHubError."""
        key = PrKey(repo, number)
        cached = self._cache.get(key)
        if cached is None:
            return await asyncio.shield(self._start_fetch(key))
        if self._clock() - cached.loaded_at >= self._ttl:
            self._start_fetch(key)
        return cached.stack

    def _start_fetch(self, key: PrKey) -> asyncio.Task[PrStack | None]:
        task = self._fetches.get(key)
        if task is None:
            task = asyncio.create_task(self._fetch(key))
            self._fetches[key] = task
            task.add_done_callback(lambda done: self._fetch_finished(key, done))
        return task

    def _fetch_finished(self, key: PrKey, task: asyncio.Task[PrStack | None]) -> None:
        if self._fetches.get(key) is task:
            del self._fetches[key]
        if not task.cancelled():
            # A failed background refresh keeps the previous stack; the next stale read retries.
            task.exception()

    async def _fetch(self, key: PrKey) -> PrStack | None:
        stack = await self.find(key.repo, key.pr_number)
        self._cache[key] = _Cached(stack=stack, loaded_at=self._clock())
        return stack

    async def _graphql(self, query: str, variables: Mapping[str, str | int]) -> bytes:
        args = ["gh", "api", "graphql", "-f", f"query={query}"]
        for name, value in variables.items():
            args += (
                ["-F", f"{name}={value}"] if isinstance(value, int) else ["-f", f"{name}={value}"]
            )
        result = await self._runner(args, timeout=GH_TIMEOUT_SECONDS, env=_GH_ENV)
        if not result.ok:
            raise GitHubError(f"GitHub stack lookup failed: {result.describe_failure()}")
        return result.stdout

    async def find(self, repo: str, number: int) -> PrStack | None:
        """Look the stack up on GitHub, without the cache."""
        owner, name = repo.split("/", 1)
        repo_vars = {"owner": owner, "name": name}
        raw = await self._graphql(
            STACK_QUERY, {**repo_vars, "number": number, "entries": STACK_ENTRY_LIMIT}
        )
        repository = _validate(_STACK, raw).data.repository
        if repository is None or repository.pull_request is None:
            raise NotFoundError(f"PR {repo}#{number} not found")
        pr = repository.pull_request
        if pr.stack is not None:
            return await self._native(repo_vars, pr.stack, pr.stack_entry)
        default_branch = repository.default_branch_ref.name if repository.default_branch_ref else ""
        return await self._branch_chain(repo_vars, _stack_pr(pr), default_branch)

    async def _native(
        self,
        repo_vars: Mapping[str, str],
        stack: _GqlStack,
        stack_entry: _GqlPosition | None,
    ) -> PrStack:
        entries = sorted(
            (
                StackEntry(position=e.position, pr=_stack_pr(e.pull_request))
                for e in stack.entries.nodes
            ),
            key=lambda e: e.position,
        )
        return PrStack(
            source="github",
            number=stack.number,
            size=stack.size,
            base_ref=stack.base_ref_name,
            position=stack_entry.position if stack_entry else None,
            entries=tuple(entries),
            extensions=tuple(await self._extensions(repo_vars, entries)),
        )

    async def _extensions(
        self, repo_vars: Mapping[str, str], entries: Sequence[StackEntry]
    ) -> list[StackExtension]:
        if not entries:
            return []
        variables: dict[str, str | int] = {**repo_vars}
        for i, entry in enumerate(entries):
            variables[f"r{i}"] = entry.pr.head_ref
        raw = await self._graphql(extensions_query(len(entries)), variables)
        children = _validate(_ALIASES, raw).data.repository
        in_stack = {entry.pr.number for entry in entries}
        extensions: list[StackExtension] = []
        for i, entry in enumerate(entries):
            for child in children.get(f"e{i}", _GqlPrList()).nodes:
                if child.number not in in_stack:
                    extensions.append(StackExtension(pr=_stack_pr(child), based_on=entry.pr.number))
        return extensions

    async def _open_prs(
        self, query: str, repo_vars: Mapping[str, str], ref: str
    ) -> list[_GqlParentPr]:
        raw = await self._graphql(query, {**repo_vars, "ref": ref, "first": CHILD_LIMIT})
        return _validate(_LIST, raw).data.repository.pull_requests.nodes

    async def _branch_chain(
        self, repo_vars: Mapping[str, str], pr: StackPr, default_branch: str
    ) -> PrStack | None:
        """Walk the branch chain: up through the open PR whose head is each base, until the
        default branch, then down through the single open PR based on each head. A fork (two
        or more children) stops the walk and lists the children as extensions. The walk
        takes at most HOP_CAP lookups. A parent in a native stack returns that stack, with
        this PR as an extension."""
        hops = HOP_CAP
        seen = {pr.number}
        below: list[StackPr] = []
        current = pr
        while hops > 0 and current.base_ref and current.base_ref != default_branch:
            hops -= 1
            parents = await self._open_prs(PARENT_QUERY, repo_vars, current.base_ref)
            if len(parents) != 1 or parents[0].number in seen:
                break
            if parents[0].stack is not None:
                native = await self.find(
                    f"{repo_vars['owner']}/{repo_vars['name']}", parents[0].number
                )
                return replace(native, position=None) if native is not None else None
            current = _stack_pr(parents[0])
            seen.add(current.number)
            below.append(current)
        above: list[StackPr] = []
        extensions: list[StackExtension] = []
        current = pr
        while hops > 0:
            hops -= 1
            children = [
                c
                for c in await self._open_prs(CHILDREN_QUERY, repo_vars, current.head_ref)
                if c.number not in seen
            ]
            if len(children) > 1:
                extensions = [
                    StackExtension(pr=_stack_pr(c), based_on=current.number) for c in children
                ]
            if len(children) != 1:
                break
            current = _stack_pr(children[0])
            seen.add(current.number)
            above.append(current)
        chain = [*reversed(below), pr, *above]
        if len(chain) == 1 and not extensions:
            return None
        return PrStack(
            source="branches",
            number=None,
            size=len(chain),
            base_ref=chain[0].base_ref,
            position=len(below) + 1,
            entries=tuple(StackEntry(position=i, pr=p) for i, p in enumerate(chain, start=1)),
            extensions=tuple(extensions),
        )


def serialize_stack(stack: PrStack, review_ids: Mapping[int, str]) -> dict[str, object]:
    def pr_json(pr: StackPr) -> dict[str, object]:
        return {
            "number": pr.number,
            "title": pr.title,
            "state": pr.state,
            "is_draft": pr.is_draft,
            "head_ref": pr.head_ref,
            "base_ref": pr.base_ref,
            "url": pr.url,
            "ci_state": pr.ci_state,
            "review_id": review_ids.get(pr.number),
        }

    return {
        "source": stack.source,
        "number": stack.number,
        "size": stack.size,
        "base_ref": stack.base_ref,
        "position": stack.position,
        "entries": [{"position": e.position, **pr_json(e.pr)} for e in stack.entries],
        "extensions": [{**pr_json(x.pr), "based_on": x.based_on} for x in stack.extensions],
    }


class StackService:
    def __init__(self, store: Store, finder: StackFinder) -> None:
        self._store = store
        self._finder = finder

    async def review_stack(self, review_id: str) -> dict[str, object] | None:
        """The stack of a PR review's PR, or None when it is in no stack."""
        review = self._store.get_review(review_id)
        if review is None:
            raise NotFoundError(f"Review {review_id!r} not found")
        if review.kind != "pr" or review.repo is None or review.pr_number is None:
            raise ReviewError(f"Review {review_id!r} is not a PR review")
        stack = await self._finder.stack(review.repo, review.pr_number)
        if stack is None:
            return None
        review_ids = {
            key.pr_number: rid
            for key, rid in self._store.pr_review_ids().items()
            if key.repo == review.repo
        }
        return serialize_stack(stack, review_ids)
