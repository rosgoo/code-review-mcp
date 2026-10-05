import asyncio
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel

from code_review_mcp.errors import NotFoundError, ReviewError
from code_review_mcp.procs import CommandRunner, run_command

GH_TIMEOUT_SECONDS = 30.0
INBOX_TTL_SECONDS = 60.0
INBOX_LIMIT = 100
INBOX_PAGE_SIZE = 50
SEARCH_LIMIT = 20

InboxName = Literal["direct", "mine", "team"]
INBOX_NAMES: tuple[InboxName, ...] = ("direct", "mine", "team")
INBOX_QUERIES: dict[InboxName, str] = {
    "direct": "is:pr is:open user-review-requested:@me sort:updated-desc",
    "mine": "is:pr is:open author:@me sort:updated-desc",
    "team": "is:pr is:open review-requested:@me -user-review-requested:@me sort:updated-desc",
}
INBOX_GRAPHQL = """
query($q: String!, $first: Int!, $after: String) {
  search(query: $q, type: ISSUE, first: $first, after: $after) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on PullRequest {
        number title url
        repository { nameWithOwner }
        author { __typename login }
        isDraft createdAt updatedAt baseRefName headRefName
        additions deletions changedFiles reviewDecision
        viewerLatestReview { state }
        labels(first: 10) { nodes { name } }
        commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
      }
    }
  }
}
"""
PR_VIEW_FIELDS = (
    "number,title,body,author,url,state,isDraft,baseRefName,baseRefOid,headRefName,"
    "headRefOid,files,additions,deletions,reviewDecision,reviewRequests,statusCheckRollup"
)
SHA_SEARCH_FIELDS = "number,repository,url,state,updatedAt"
BRANCH_LIST_FIELDS = "number,url,state,updatedAt"

_GH_ENV = {"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1", "NO_COLOR": "1"}
_NOT_FOUND = re.compile(r"Could not resolve to a (PullRequest|Repository)|no pull requests? found")
_RETRYABLE = re.compile(r"HTTP 50[234]")

_NAME = r"[A-Za-z0-9_.-]+"
_PR_URL = re.compile(
    rf"^(?:https?://)?(?:www\.)?github\.com/({_NAME}/{_NAME})/pull/(\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_REPO_NUMBER = re.compile(rf"^({_NAME}/{_NAME})#(\d+)$")
_HASH_NUMBER = re.compile(r"^#(\d+)$")
_BARE_NUMBER = re.compile(r"^\d{1,6}$")
_SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
_BRANCH = re.compile(r"^[^\s~^:?*\[\\#]+$")

CheckState = Literal["success", "failure", "pending", "skipped"]


class GitHubError(ReviewError):
    pass


class PrNotFoundError(NotFoundError):
    pass


class RefError(ReviewError):
    pass


@dataclass(frozen=True)
class NumberRef:
    number: int
    repo: str | None = None


@dataclass(frozen=True)
class ShaRef:
    sha: str


@dataclass(frozen=True)
class BranchRef:
    branch: str


ParsedRef = NumberRef | ShaRef | BranchRef


@dataclass(frozen=True)
class ResolvedRef:
    repo: str
    number: int
    note: str | None = None


@dataclass(frozen=True)
class InboxPr:
    repo: str
    number: int
    title: str
    url: str
    author: str | None
    author_is_bot: bool
    is_draft: bool
    created_at: str
    updated_at: str
    base_ref: str
    head_ref: str
    additions: int
    deletions: int
    changed_files: int
    review_decision: str | None
    viewer_review: str | None
    labels: tuple[str, ...]
    ci_state: CheckState | None


@dataclass(frozen=True)
class InboxList:
    total: int
    items: tuple[InboxPr, ...]
    fetched_at: str


class InboxListState(NamedTuple):
    inbox_list: InboxList
    refreshing: bool


@dataclass(frozen=True)
class PrFileStat:
    path: str
    additions: int
    deletions: int
    change_type: str


@dataclass(frozen=True)
class StatusCheck:
    name: str
    state: CheckState
    url: str | None
    workflow: str | None


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    body: str
    author: str | None
    url: str
    state: str
    is_draft: bool
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str
    additions: int
    deletions: int
    files: tuple[PrFileStat, ...]
    review_decision: str | None
    review_requests: tuple[str, ...]
    checks: tuple[StatusCheck, ...]

    @property
    def checks_state(self) -> CheckState | None:
        """The CI rollup: failure beats pending beats success beats skipped; None with no checks."""
        states = {check.state for check in self.checks}
        for state in ("failure", "pending", "success", "skipped"):
            if state in states:
                return state
        return None


class _GhModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, frozen=True)


class _GhActor(_GhModel):
    login: str = ""


class _GhRepository(_GhModel):
    name_with_owner: str


class _GhSearchItem(_GhModel):
    number: int
    url: str
    repository: _GhRepository
    title: str = ""
    author: _GhActor | None = None
    updated_at: str = ""
    is_draft: bool = False
    state: str = ""


class _GhListItem(_GhModel):
    number: int
    url: str
    state: str = ""
    updated_at: str = ""


class _GhFile(_GhModel):
    path: str
    additions: int = 0
    deletions: int = 0
    change_type: str = ""


class _GhReviewRequest(_GhModel):
    login: str | None = None
    slug: str | None = None
    name: str | None = None


class _GhCheck(_GhModel):
    typename: str = Field(default="", alias="__typename")
    name: str | None = None
    context: str | None = None
    status: str | None = None
    conclusion: str | None = None
    state: str | None = None
    details_url: str | None = None
    target_url: str | None = None
    workflow_name: str | None = None


class _GhPullRequest(_GhModel):
    number: int
    title: str
    url: str
    state: str
    base_ref_name: str
    base_ref_oid: str
    head_ref_name: str
    head_ref_oid: str
    body: str = ""
    author: _GhActor | None = None
    is_draft: bool = False
    additions: int = 0
    deletions: int = 0
    files: list[_GhFile] | None = None
    review_decision: str | None = None
    review_requests: list[_GhReviewRequest] | None = None
    status_check_rollup: list[_GhCheck] | None = None


class _GqlAuthor(_GhModel):
    typename: str = Field(default="", alias="__typename")
    login: str = ""


class _GqlRepository(_GhModel):
    name_with_owner: str


class _GqlState(_GhModel):
    state: str


class _GqlLabel(_GhModel):
    name: str


class _GqlLabels(_GhModel):
    nodes: list[_GqlLabel] = []


class _GqlCommit(_GhModel):
    status_check_rollup: _GqlState | None = None


class _GqlCommitNode(_GhModel):
    commit: _GqlCommit


class _GqlCommits(_GhModel):
    nodes: list[_GqlCommitNode] = []


class _GqlPullRequest(_GhModel):
    number: int
    title: str
    url: str
    repository: _GqlRepository
    created_at: str
    updated_at: str
    author: _GqlAuthor | None = None
    is_draft: bool = False
    base_ref_name: str = ""
    head_ref_name: str = ""
    additions: int = 0
    deletions: int = 0
    changed_files: int = 0
    review_decision: str | None = None
    viewer_latest_review: _GqlState | None = None
    labels: _GqlLabels | None = None
    commits: _GqlCommits | None = None


class _GqlOtherNode(_GhModel):
    pass


class _GqlPageInfo(_GhModel):
    has_next_page: bool = False
    end_cursor: str | None = None


class _GqlSearch(_GhModel):
    issue_count: int
    page_info: _GqlPageInfo = _GqlPageInfo()
    nodes: list[Annotated[_GqlPullRequest | _GqlOtherNode, Field(union_mode="left_to_right")]] = []


class _GqlSearchData(_GhModel):
    search: _GqlSearch


class _GqlSearchResponse(_GhModel):
    data: _GqlSearchData


_SEARCH_ITEMS = TypeAdapter(list[_GhSearchItem])
_SEARCH_RESPONSE = TypeAdapter(_GqlSearchResponse)
_LIST_ITEMS = TypeAdapter(list[_GhListItem])
_PULL_REQUEST = TypeAdapter(_GhPullRequest)


def parse_ref(ref: str) -> ParsedRef:
    """Classify a PR reference without calling GitHub.

    Accepted forms: a PR URL (any trailing sub-page such as /files), `owner/name#123`,
    `#123`, `123` (up to 6 digits), a 7 to 40 character hex commit SHA, or a branch name.
    A 7+ digit number is read as a SHA; write `#1234567` for a PR number.
    Raises RefError for an empty ref, a non-PR GitHub URL, or text that is none of these.
    """
    text = ref.strip()
    if not text:
        raise RefError("The PR reference is empty")
    if match := _PR_URL.match(text):
        return NumberRef(number=int(match[2]), repo=match[1])
    if text.lower().startswith(("http://", "https://")) or "github.com/" in text.lower():
        raise RefError(f"{ref!r} is not a GitHub pull request URL")
    if match := _REPO_NUMBER.match(text):
        return NumberRef(number=int(match[2]), repo=match[1])
    if match := _HASH_NUMBER.match(text):
        return NumberRef(number=int(match[1]))
    if _BARE_NUMBER.match(text):
        return NumberRef(number=int(text))
    if _SHA.match(text):
        return ShaRef(sha=text.lower())
    if text.startswith("-") or ".." in text or not _BRANCH.match(text):
        raise RefError(
            f"Cannot read {ref!r} as a PR reference. Use a PR URL, owner/name#123, #123, "
            "a commit SHA, or a branch name."
        )
    return BranchRef(branch=text)


def repo_from_pr_url(url: str) -> str | None:
    match = _PR_URL.match(url)
    return match[1] if match else None


def _check_state(check: _GhCheck) -> CheckState:
    if check.typename == "StatusContext":
        state = (check.state or "").upper()
        if state == "SUCCESS":
            return "success"
        return "pending" if state in ("PENDING", "EXPECTED") else "failure"
    if (check.status or "").upper() != "COMPLETED":
        return "pending"
    conclusion = (check.conclusion or "").upper()
    if conclusion == "SUCCESS":
        return "success"
    return "skipped" if conclusion in ("NEUTRAL", "SKIPPED") else "failure"


def _to_status_check(check: _GhCheck) -> StatusCheck:
    return StatusCheck(
        name=check.name or check.context or "",
        state=_check_state(check),
        url=check.details_url or check.target_url,
        workflow=check.workflow_name,
    )


_ROLLUP_STATES: dict[str, CheckState] = {
    "SUCCESS": "success",
    "FAILURE": "failure",
    "ERROR": "failure",
    "PENDING": "pending",
    "EXPECTED": "pending",
}


def _inbox_pr(pr: _GqlPullRequest) -> InboxPr:
    commits = pr.commits.nodes if pr.commits else []
    rollup = commits[-1].commit.status_check_rollup if commits else None
    return InboxPr(
        repo=pr.repository.name_with_owner,
        number=pr.number,
        title=pr.title,
        url=pr.url,
        author=pr.author.login if pr.author else None,
        author_is_bot=pr.author is not None and pr.author.typename == "Bot",
        is_draft=pr.is_draft,
        created_at=pr.created_at,
        updated_at=pr.updated_at,
        base_ref=pr.base_ref_name,
        head_ref=pr.head_ref_name,
        additions=pr.additions,
        deletions=pr.deletions,
        changed_files=pr.changed_files,
        review_decision=pr.review_decision or None,
        viewer_review=pr.viewer_latest_review.state if pr.viewer_latest_review else None,
        labels=tuple(label.name for label in pr.labels.nodes) if pr.labels else (),
        ci_state=_ROLLUP_STATES.get(rollup.state.upper()) if rollup else None,
    )


def parse_inbox_page(raw: bytes) -> tuple[int, list[InboxPr], str | None]:
    """Parse one page of the inbox GraphQL search.

    Returns the total match count, the page's PRs, and the cursor of the next page
    (None on the last page). Raises GitHubError for output that does not fit.
    """
    search = _validate(_SEARCH_RESPONSE, raw).data.search
    prs = [_inbox_pr(node) for node in search.nodes if isinstance(node, _GqlPullRequest)]
    cursor = search.page_info.end_cursor if search.page_info.has_next_page else None
    return search.issue_count, prs, cursor


def _reviewer_name(request: _GhReviewRequest) -> str:
    return request.login or request.slug or request.name or ""


@dataclass(frozen=True)
class _Candidate:
    repo: str
    number: int
    state: str
    updated_at: str


def _pick_candidate(candidates: Sequence[_Candidate], description: str) -> ResolvedRef:
    """Pick the open PR, else the most recently updated one, and explain the pick if ambiguous."""
    if not candidates:
        raise PrNotFoundError(f"No PR found for {description}")
    ordered = sorted(candidates, key=lambda c: (c.state == "open", c.updated_at), reverse=True)
    chosen = ordered[0]
    if len(ordered) == 1:
        return ResolvedRef(repo=chosen.repo, number=chosen.number)
    open_count = sum(c.state == "open" for c in ordered)
    if chosen.state != "open":
        reason = "the most recently updated one (none is open)"
    elif open_count == 1:
        reason = "the open one"
    else:
        reason = "the most recently updated open one"
    listing = ", ".join(f"{c.repo}#{c.number} ({c.state})" for c in ordered)
    note = (
        f"{description} matches {len(ordered)} PRs: {listing}. "
        f"Opened {chosen.repo}#{chosen.number}, {reason}."
    )
    return ResolvedRef(repo=chosen.repo, number=chosen.number, note=note)


class GitHubClient:
    """Read-only access to GitHub through the `gh` CLI, which owns authentication."""

    def __init__(
        self,
        runner: CommandRunner = run_command,
        *,
        clock: Callable[[], float] = time.monotonic,
        inbox_ttl: float = INBOX_TTL_SECONDS,
    ) -> None:
        self._runner = runner
        self._clock = clock
        self._inbox_ttl = inbox_ttl
        self._lists: dict[InboxName, InboxList] = {}
        self._loaded_at: dict[InboxName, float] = {}
        self._fetches: dict[InboxName, asyncio.Task[InboxList]] = {}

    async def _gh(self, args: Sequence[str]) -> bytes:
        result = await self._runner(["gh", *args], timeout=GH_TIMEOUT_SECONDS, env=_GH_ENV)
        if result.ok:
            return result.stdout
        if _NOT_FOUND.search(result.stderr_text):
            raise PrNotFoundError(result.describe_failure())
        raise GitHubError(result.describe_failure())

    async def _search_page(self, name: InboxName, cursor: str | None) -> bytes:
        args = [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={INBOX_GRAPHQL}",
            "-f",
            f"q={INBOX_QUERIES[name]}",
            "-F",
            f"first={INBOX_PAGE_SIZE}",
            *(["-f", f"after={cursor}"] if cursor else []),
        ]
        result = await self._runner(args, timeout=GH_TIMEOUT_SECONDS, env=_GH_ENV)
        if not result.ok and _RETRYABLE.search(result.stderr_text):
            result = await self._runner(args, timeout=GH_TIMEOUT_SECONDS, env=_GH_ENV)
        if not result.ok:
            detail = result.stderr_text or f"exit status {result.returncode}"
            raise GitHubError(f"GitHub search for the {name!r} inbox list failed: {detail}")
        return result.stdout

    async def _fetch_list(self, name: InboxName) -> InboxList:
        items: list[InboxPr] = []
        total = 0
        cursor: str | None = None
        while True:
            total, page, cursor = parse_inbox_page(await self._search_page(name, cursor))
            items.extend(page)
            if cursor is None or len(items) >= INBOX_LIMIT:
                break
        newest_first = sorted(items[:INBOX_LIMIT], key=lambda pr: pr.updated_at, reverse=True)
        fetched = InboxList(
            total=total, items=tuple(newest_first), fetched_at=datetime.now(UTC).isoformat()
        )
        self._lists[name] = fetched
        self._loaded_at[name] = self._clock()
        return fetched

    def _start_fetch(self, name: InboxName) -> asyncio.Task[InboxList]:
        """Return the list's in-flight fetch, starting one if none runs."""
        task = self._fetches.get(name)
        if task is None:
            task = asyncio.create_task(self._fetch_list(name))
            self._fetches[name] = task
            task.add_done_callback(lambda done: self._fetch_finished(name, done))
        return task

    def _fetch_finished(self, name: InboxName, task: asyncio.Task[InboxList]) -> None:
        if self._fetches.get(name) is task:
            del self._fetches[name]
        if not task.cancelled():
            # A failed background refresh keeps the previous list; the next stale read retries.
            task.exception()

    def cached_inbox_list(self, name: InboxName) -> InboxList | None:
        return self._lists.get(name)

    async def inbox_list(self, name: InboxName, *, refresh: bool = False) -> InboxListState:
        """One inbox list, newest update first, at most INBOX_LIMIT PRs.

        `direct`: PRs that request the user by name. `mine`: PRs the user wrote. `team`:
        PRs that request a team the user is in and not the user. `total` counts every match.
        Each list is fetched in pages of INBOX_PAGE_SIZE, because larger GraphQL searches
        exceed GitHub's request time limit.

        A cached list returns at once. When it is older than the inbox TTL, one background
        refresh starts and `refreshing` is True; the next read gets the new list. The first
        read of a list, and `refresh=True`, wait for fresh data. Concurrent callers share
        one fetch per list. Raises GitHubError when a fetch that the caller waits for fails.
        """
        cached = self._lists.get(name)
        if refresh or cached is None:
            return InboxListState(await asyncio.shield(self._start_fetch(name)), refreshing=False)
        if self._clock() - self._loaded_at[name] >= self._inbox_ttl:
            self._start_fetch(name)
        return InboxListState(cached, refreshing=name in self._fetches)

    async def pull_request(self, repo: str, number: int) -> PullRequest:
        """Fetch a PR's metadata. Raises PrNotFoundError if the PR or repo does not exist."""
        raw = await self._gh(
            ["pr", "view", str(number), f"--repo={repo}", f"--json={PR_VIEW_FIELDS}"]
        )
        pr = _validate(_PULL_REQUEST, raw)
        return PullRequest(
            repo=repo_from_pr_url(pr.url) or repo,
            number=pr.number,
            title=pr.title,
            body=pr.body,
            author=pr.author.login if pr.author else None,
            url=pr.url,
            state=pr.state.lower(),
            is_draft=pr.is_draft,
            base_ref=pr.base_ref_name,
            base_sha=pr.base_ref_oid,
            head_ref=pr.head_ref_name,
            head_sha=pr.head_ref_oid,
            additions=pr.additions,
            deletions=pr.deletions,
            files=tuple(
                PrFileStat(
                    path=f.path,
                    additions=f.additions,
                    deletions=f.deletions,
                    change_type=f.change_type,
                )
                for f in pr.files or ()
            ),
            review_decision=pr.review_decision or None,
            review_requests=tuple(_reviewer_name(r) for r in pr.review_requests or ()),
            checks=tuple(_to_status_check(c) for c in pr.status_check_rollup or ()),
        )

    async def resolve_ref(
        self, ref: str, *, default_repo: str | None, search_repos: Sequence[str]
    ) -> ResolvedRef:
        """Resolve a PR reference (see parse_ref) to a repo and PR number.

        A bare number or a branch uses `default_repo`. A SHA is searched in `search_repos`,
        or across GitHub when that is empty. When a SHA or branch matches several PRs, the
        open one wins, then the most recently updated, and `note` says which was picked.
        Raises RefError (bad ref, or no default_repo when one is needed) or PrNotFoundError.
        """
        parsed = parse_ref(ref)
        if isinstance(parsed, NumberRef):
            repo = parsed.repo or default_repo
            if repo is None:
                raise RefError(
                    f"{ref!r} names no repository and config.toml sets no default_repo. "
                    f"Use owner/name#{parsed.number} or a PR URL."
                )
            return ResolvedRef(repo=repo, number=parsed.number)
        if isinstance(parsed, ShaRef):
            raw = await self._gh(
                [
                    "search",
                    "prs",
                    parsed.sha,
                    *(f"--repo={repo}" for repo in search_repos),
                    f"--limit={SEARCH_LIMIT}",
                    f"--json={SHA_SEARCH_FIELDS}",
                ]
            )
            candidates = [
                _Candidate(
                    repo=item.repository.name_with_owner,
                    number=item.number,
                    state=item.state.lower(),
                    updated_at=item.updated_at,
                )
                for item in _validate(_SEARCH_ITEMS, raw)
            ]
            return _pick_candidate(candidates, f"commit {parsed.sha}")
        if default_repo is None:
            raise RefError(
                f"{ref!r} looks like a branch name, and branch lookup needs default_repo "
                "in config.toml. Use a PR URL or owner/name#123."
            )
        raw = await self._gh(
            [
                "pr",
                "list",
                f"--repo={default_repo}",
                f"--head={parsed.branch}",
                "--state=all",
                f"--json={BRANCH_LIST_FIELDS}",
            ]
        )
        candidates = [
            _Candidate(
                repo=repo_from_pr_url(item.url) or default_repo,
                number=item.number,
                state=item.state.lower(),
                updated_at=item.updated_at,
            )
            for item in _validate(_LIST_ITEMS, raw)
        ]
        return _pick_candidate(candidates, f"branch {parsed.branch!r} in {default_repo}")


def _validate[T](adapter: TypeAdapter[T], raw: bytes) -> T:
    try:
        return adapter.validate_json(raw)
    except ValidationError as e:
        raise GitHubError(f"Unexpected output from gh: {e}") from e


class _GhStateNode(_GhModel):
    state: str


class _GhStatesData(_GhModel):
    repository: dict[str, _GhStateNode | None] | None


class _GhStatesResponse(_GhModel):
    data: _GhStatesData


_PR_STATES = TypeAdapter(_GhStatesResponse)


async def fetch_pr_states(
    client: GitHubClient, repo: str, numbers: Sequence[int]
) -> dict[int, str]:
    """Return the lowercase state ("open", "closed", "merged") of each PR of `repo`.

    Uses one GraphQL request for all `numbers`. A PR that GitHub cannot resolve is left out,
    and the other PRs still return. Raises GitHubError if the request returns no data.
    """
    if not numbers:
        return {}
    owner, name = repo.split("/", 1)
    fields = " ".join(f"pr{n}: pullRequest(number: {n}) {{ state }}" for n in sorted(set(numbers)))
    query = (
        "query($owner: String!, $name: String!) "
        f"{{ repository(owner: $owner, name: $name) {{ {fields} }} }}"
    )
    result = await client._runner(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
        ],
        timeout=GH_TIMEOUT_SECONDS,
        env=_GH_ENV,
    )
    try:
        response = _PR_STATES.validate_json(result.stdout)
    except ValidationError as e:
        detail = result.describe_failure() if not result.ok else f"Unexpected output from gh: {e}"
        raise GitHubError(detail) from e
    repository = response.data.repository or {}
    return {
        int(alias.removeprefix("pr")): node.state.lower()
        for alias, node in repository.items()
        if node is not None and alias.startswith("pr") and alias[2:].isdigit()
    }
