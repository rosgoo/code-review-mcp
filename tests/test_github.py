import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from code_review_mcp import github as github_module
from code_review_mcp.github import (
    INBOX_PAGE_SIZE,
    INBOX_QUERIES,
    BranchRef,
    GitHubClient,
    GitHubError,
    NumberRef,
    ParsedRef,
    PrNotFoundError,
    RefError,
    ResolvedRef,
    ShaRef,
    StatusCheck,
    parse_inbox_page,
    parse_ref,
)
from code_review_mcp.procs import CommandResult
from code_review_mcp.repo_config import ConfigError, RepoConfig, load_repo_config

from .pr_fixtures import FakeGh, gql_page, gql_pr, inbox_rule, write_config

SHA40 = "cdc893ad69a0543c601bf2a39641fa6f16c90af1"


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("https://github.com/Maybern/maybern/pull/23946", NumberRef(23946, "Maybern/maybern")),
        (
            "https://github.com/Maybern/maybern/pull/23946/files",
            NumberRef(23946, "Maybern/maybern"),
        ),
        ("https://github.com/o/r/pull/7/files#diff-abc", NumberRef(7, "o/r")),
        ("http://www.github.com/o/r.js/pull/7/", NumberRef(7, "o/r.js")),
        ("github.com/o/r/pull/7?w=1", NumberRef(7, "o/r")),
        ("  Maybern/maybern#12  ", NumberRef(12, "Maybern/maybern")),
        ("#123", NumberRef(123)),
        ("123", NumberRef(123)),
        ("#1234567", NumberRef(1234567)),
        ("1234567", ShaRef("1234567")),
        ("cdc893a", ShaRef("cdc893a")),
        ("CDC893AD69A0543C601BF2A39641FA6F16C90AF1", ShaRef(SHA40)),
        ("ryan/event-dsl-9-translator", BranchRef("ryan/event-dsl-9-translator")),
        ("main", BranchRef("main")),
        ("cdc893", BranchRef("cdc893")),
    ],
)
def test_parse_ref(ref: str, expected: ParsedRef) -> None:
    assert parse_ref(ref) == expected


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("", "empty"),
        ("   ", "empty"),
        ("https://github.com/o/r/issues/3", "not a GitHub pull request URL"),
        ("https://gitlab.com/o/r/pull/3", "not a GitHub pull request URL"),
        ("o/r#abc", "Cannot read"),
        ("two words", "Cannot read"),
        ("-delete", "Cannot read"),
        ("a..b", "Cannot read"),
        ("x" * 41 + "#", "Cannot read"),
    ],
)
def test_parse_ref_errors(ref: str, message: str) -> None:
    with pytest.raises(RefError, match=message):
        parse_ref(ref)


def _search_item(number: int, state: str, updated_at: str, repo: str = "o/r") -> dict[str, object]:
    return {
        "number": number,
        "repository": {"name": repo.split("/")[1], "nameWithOwner": repo},
        "url": f"https://github.com/{repo}/pull/{number}",
        "state": state,
        "updatedAt": updated_at,
    }


async def test_resolve_number_refs_without_calling_gh() -> None:
    fake = FakeGh()
    client = GitHubClient(fake)

    with_default = await client.resolve_ref("#5", default_repo="o/r", search_repos=[])
    explicit = await client.resolve_ref("a/b#6", default_repo="o/r", search_repos=[])

    assert with_default == ResolvedRef("o/r", 5)
    assert explicit == ResolvedRef("a/b", 6)
    assert fake.calls == []
    with pytest.raises(RefError, match="default_repo"):
        await client.resolve_ref("5", default_repo=None, search_repos=[])


async def test_resolve_sha_single_match_scoped_to_repos() -> None:
    fake = FakeGh()
    fake.on(
        "search", "prs", SHA40, stdout=json.dumps([_search_item(9, "open", "2026-01-01")]).encode()
    )
    client = GitHubClient(fake)

    resolved = await client.resolve_ref(SHA40, default_repo="o/r", search_repos=["o/r", "a/b"])

    assert resolved == ResolvedRef("o/r", 9)
    [call] = fake.calls
    assert "--repo=o/r" in call and "--repo=a/b" in call


async def test_resolve_sha_prefers_open_then_newest_and_says_so() -> None:
    fake = FakeGh()
    items = [
        _search_item(1, "merged", "2026-03-01"),
        _search_item(2, "open", "2026-01-01"),
        _search_item(3, "closed", "2026-02-01"),
    ]
    fake.on("search", "prs", "abcdef1", stdout=json.dumps(items).encode())
    client = GitHubClient(fake)

    resolved = await client.resolve_ref("abcdef1", default_repo=None, search_repos=[])

    assert (resolved.repo, resolved.number) == ("o/r", 2)
    assert resolved.note is not None
    assert "matches 3 PRs" in resolved.note
    assert "Opened o/r#2, the open one." in resolved.note
    assert not any(arg.startswith("--repo") for arg in fake.calls[0])

    fake.on("search", "prs", "abcdef2", stdout=json.dumps(items[::2]).encode())
    newest = await client.resolve_ref("abcdef2", default_repo=None, search_repos=[])
    assert newest.number == 1
    assert newest.note is not None and "most recently updated one" in newest.note


async def test_resolve_sha_and_branch_not_found() -> None:
    fake = FakeGh()
    fake.on("search", "prs", stdout=b"[]")
    fake.on("pr", "list", stdout=b"[]")
    client = GitHubClient(fake)

    with pytest.raises(PrNotFoundError, match="No PR found for commit abcdef1"):
        await client.resolve_ref("abcdef1", default_repo="o/r", search_repos=["o/r"])
    with pytest.raises(PrNotFoundError, match="No PR found for branch 'feature' in o/r"):
        await client.resolve_ref("feature", default_repo="o/r", search_repos=["o/r"])


async def test_resolve_branch() -> None:
    fake = FakeGh()
    fake.on(
        "pr",
        "list",
        "--head=ryan/feature",
        stdout=json.dumps(
            [
                {
                    "number": 4,
                    "url": "https://github.com/O/R/pull/4",
                    "state": "MERGED",
                    "updatedAt": "2026-01-01",
                },
                {
                    "number": 8,
                    "url": "https://github.com/O/R/pull/8",
                    "state": "OPEN",
                    "updatedAt": "2025-01-01",
                },
            ]
        ).encode(),
    )
    client = GitHubClient(fake)

    resolved = await client.resolve_ref("ryan/feature", default_repo="o/r", search_repos=[])

    assert (resolved.repo, resolved.number) == ("O/R", 8)
    assert "--repo=o/r" in fake.calls[0] and "--state=all" in fake.calls[0]
    with pytest.raises(RefError, match="needs default_repo"):
        await client.resolve_ref("ryan/feature", default_repo=None, search_repos=[])


async def test_pull_request_parsing() -> None:
    fake = FakeGh()
    payload = {
        "number": 23946,
        "title": "AI-2086: Add pyarrow",
        "body": "## Summary",
        "author": {"is_bot": True, "login": "app/bendermaybern"},
        "url": "https://github.com/Maybern/maybern/pull/23946",
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "master",
        "baseRefOid": "e" * 40,
        "headRefName": "opencode/ai-2086",
        "headRefOid": "c" * 40,
        "files": [
            {"path": "a.py", "additions": 1, "deletions": 1, "changeType": "MODIFIED"},
        ],
        "additions": 10,
        "deletions": 3,
        "reviewDecision": "CHANGES_REQUESTED",
        "reviewRequests": [
            {"__typename": "Team", "name": "eng", "slug": "Maybern/eng"},
            {"__typename": "User", "login": "izaak-baker"},
        ],
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "Lint",
                "status": "IN_PROGRESS",
                "conclusion": "",
                "detailsUrl": "https://x/1",
                "workflowName": "Backend PR Lint",
            },
            {
                "__typename": "CheckRun",
                "name": "Tests",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
                "detailsUrl": "https://x/2",
                "workflowName": "W",
            },
            {
                "__typename": "StatusContext",
                "context": "Bender Approval Gate",
                "state": "FAILURE",
                "targetUrl": "https://x/3",
            },
        ],
    }
    fake.on("pr", "view", "23946", stdout=json.dumps(payload).encode())
    client = GitHubClient(fake)

    pr = await client.pull_request("maybern/maybern", 23946)

    assert pr.repo == "Maybern/maybern"
    assert pr.author == "app/bendermaybern"
    assert pr.state == "open"
    assert (pr.base_ref, pr.base_sha, pr.head_ref, pr.head_sha) == (
        "master",
        "e" * 40,
        "opencode/ai-2086",
        "c" * 40,
    )
    assert pr.review_decision == "CHANGES_REQUESTED"
    assert pr.review_requests == ("Maybern/eng", "izaak-baker")
    assert pr.checks == (
        StatusCheck("Lint", "pending", "https://x/1", "Backend PR Lint"),
        StatusCheck("Tests", "skipped", "https://x/2", "W"),
        StatusCheck("Bender Approval Gate", "failure", "https://x/3", None),
    )
    assert pr.checks_state == "failure"
    assert [f.path for f in pr.files] == ["a.py"]
    assert "--repo=maybern/maybern" in fake.calls[0]


async def test_pull_request_empty_decision_and_no_checks() -> None:
    fake = FakeGh()
    payload = {
        "number": 1,
        "title": "t",
        "url": "https://github.com/o/r/pull/1",
        "state": "MERGED",
        "baseRefName": "main",
        "baseRefOid": "a" * 40,
        "headRefName": "x",
        "headRefOid": "b" * 40,
        "author": None,
        "reviewDecision": "",
        "statusCheckRollup": [],
    }
    fake.on("pr", "view", stdout=json.dumps(payload).encode())

    pr = await GitHubClient(fake).pull_request("o/r", 1)

    assert pr.review_decision is None
    assert pr.author is None
    assert pr.checks_state is None
    assert pr.state == "merged"


async def test_gh_errors() -> None:
    fake = FakeGh()
    fake.on(
        "pr",
        "view",
        "999",
        code=1,
        stderr=b"GraphQL: Could not resolve to a PullRequest with the number of 999.",
    )
    fake.on("pr", "view", "5", code=1, stderr=b"HTTP 502: Bad Gateway")
    fake.on("pr", "view", "6", stdout=b'{"number": "not json for a PR"}')
    client = GitHubClient(fake)

    with pytest.raises(PrNotFoundError, match="Could not resolve"):
        await client.pull_request("o/r", 999)
    with pytest.raises(GitHubError, match="Bad Gateway"):
        await client.pull_request("o/r", 5)
    with pytest.raises(GitHubError, match="Unexpected output from gh"):
        await client.pull_request("o/r", 6)


INBOX_FIXTURE = gql_page(
    [
        gql_pr(
            10,
            title="Bot PR, no checks",
            author={"__typename": "Bot", "login": "dependabot"},
            commits={"nodes": [{"commit": {"statusCheckRollup": None}}]},
            labels={"nodes": [{"name": "deps"}, {"name": "ci"}]},
            updatedAt="2026-10-05T10:00:00Z",
        ),
        gql_pr(
            11,
            title="Reviewed, errored CI",
            viewerLatestReview={"state": "CHANGES_REQUESTED"},
            commits={"nodes": [{"commit": {"statusCheckRollup": {"state": "ERROR"}}}]},
            reviewDecision=None,
            isDraft=True,
            updatedAt="2026-10-05T12:00:00Z",
        ),
        gql_pr(12, title="No commits listed", commits={"nodes": []}, author=None),
        {},
    ],
    total=57,
    cursor="Y3Vyc29yOjQ=",
)


def test_parse_inbox_page() -> None:
    total, prs, cursor = parse_inbox_page(INBOX_FIXTURE)

    assert (total, cursor) == (57, "Y3Vyc29yOjQ=")
    assert [pr.number for pr in prs] == [10, 11, 12]
    bot, reviewed, bare = prs
    assert (bot.author, bot.author_is_bot, bot.ci_state) == ("dependabot", True, None)
    assert bot.labels == ("deps", "ci")
    assert (bot.viewer_review, bot.review_decision) == (None, "REVIEW_REQUIRED")
    assert (reviewed.author_is_bot, reviewed.ci_state, reviewed.viewer_review) == (
        False,
        "failure",
        "CHANGES_REQUESTED",
    )
    assert (reviewed.review_decision, reviewed.is_draft) == (None, True)
    assert (bare.author, bare.author_is_bot, bare.ci_state) == (None, False, None)
    assert (bare.base_ref, bare.head_ref, bare.changed_files) == ("main", "branch-12", 2)


@pytest.mark.parametrize(
    ("rollup", "expected"),
    [
        ("SUCCESS", "success"),
        ("FAILURE", "failure"),
        ("ERROR", "failure"),
        ("PENDING", "pending"),
        ("EXPECTED", "pending"),
    ],
)
def test_parse_inbox_rollup_states(rollup: str, expected: str) -> None:
    commits = {"nodes": [{"commit": {"statusCheckRollup": {"state": rollup}}}]}
    page = gql_page([gql_pr(1, commits=commits)])
    [pr] = parse_inbox_page(page)[1]
    assert pr.ci_state == expected


def test_parse_inbox_page_rejects_unexpected_output() -> None:
    with pytest.raises(GitHubError, match="Unexpected output from gh"):
        parse_inbox_page(b'{"data": {"search": {"nodes": []}}}')


async def test_inbox_list_pages_until_the_last_page() -> None:
    fake = FakeGh()
    inbox_rule(
        fake,
        "team",
        gql_page([gql_pr(6, updated_at="2026-10-04T10:00:00Z")], total=3, cursor="page2"),
    )
    inbox_rule(
        fake,
        "team",
        gql_page([gql_pr(7, updated_at="2026-10-05T12:00:00Z")], total=3, cursor="page3"),
        "after=page2",
    )
    inbox_rule(fake, "team", gql_page([gql_pr(8)], total=3), "after=page3")
    client = GitHubClient(fake)

    state = await client.inbox_list("team")

    assert state.refreshing is False
    assert state.inbox_list.total == 3
    assert [pr.number for pr in state.inbox_list.items] == [7, 8, 6]
    assert len(fake.calls) == 3
    for call in fake.calls:
        assert call[:3] == ("gh", "api", "graphql")
        assert f"first={INBOX_PAGE_SIZE}" in call
        assert f"q={INBOX_QUERIES['team']}" in call
    assert "-user-review-requested:@me" in INBOX_QUERIES["team"]


async def test_inbox_list_stops_at_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_module, "INBOX_LIMIT", 2)
    fake = FakeGh()
    inbox_rule(fake, "team", gql_page([gql_pr(1), gql_pr(2)], total=9, cursor="more"))
    client = GitHubClient(fake)

    state = await client.inbox_list("team")

    assert (state.inbox_list.total, len(state.inbox_list.items)) == (9, 2)
    assert fake.calls_with("after=more") == []


async def test_inbox_list_retries_a_gateway_error_once() -> None:
    fake = FakeGh()
    team_query = f"q={INBOX_QUERIES['team']}"
    fake.on("graphql", team_query, code=1, stderr=b"gh: HTTP 502")
    client = GitHubClient(fake)

    with pytest.raises(GitHubError, match="'team' inbox list failed: gh: HTTP 502"):
        await client.inbox_list("team")
    assert len(fake.calls) == 2

    fake.on("graphql", team_query, code=1, stderr=b"gh: HTTP 401: Bad credentials")
    with pytest.raises(GitHubError, match="Bad credentials"):
        await client.inbox_list("team")
    assert len(fake.calls) == 3


@dataclass
class GatedRunner:
    """Delegates to a FakeGh, holding each call until `gate` opens."""

    fake: FakeGh
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    started: int = 0

    async def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
    ) -> CommandResult:
        self.started += 1
        await self.gate.wait()
        return await self.fake(args, timeout=timeout, cwd=cwd, env=env, stdin=stdin)


def _gated_client(now: list[float]) -> tuple[GitHubClient, GatedRunner, FakeGh]:
    fake = FakeGh()
    inbox_rule(fake, "mine", gql_page([gql_pr(1, title="first")]))
    runner = GatedRunner(fake)
    return GitHubClient(runner, clock=lambda: now[0]), runner, fake


async def test_first_read_waits_and_fresh_reads_are_cached() -> None:
    now = [1000.0]
    client, runner, _ = _gated_client(now)
    runner.gate.set()

    first = await client.inbox_list("mine")
    now[0] += 59
    again = await client.inbox_list("mine")

    assert [pr.title for pr in first.inbox_list.items] == ["first"]
    assert again == (first.inbox_list, False)
    assert runner.started == 1


async def test_stale_read_returns_at_once_with_one_background_refresh() -> None:
    now = [1000.0]
    client, runner, fake = _gated_client(now)
    runner.gate.set()
    first = (await client.inbox_list("mine")).inbox_list
    inbox_rule(fake, "mine", gql_page([gql_pr(1, title="second")]))
    runner.gate.clear()
    now[0] += 61

    stale = await client.inbox_list("mine")
    await asyncio.sleep(0)
    stale_again = await client.inbox_list("mine")

    assert stale == (first, True)
    assert stale_again == (first, True)
    assert runner.started == 2

    runner.gate.set()
    joined = await client.inbox_list("mine", refresh=True)
    after = await client.inbox_list("mine")

    assert [pr.title for pr in joined.inbox_list.items] == ["second"]
    assert after == (joined.inbox_list, False)
    assert runner.started == 2


async def test_concurrent_stale_reads_start_one_refresh() -> None:
    now = [1000.0]
    client, runner, fake = _gated_client(now)
    runner.gate.set()
    first = (await client.inbox_list("mine")).inbox_list
    inbox_rule(fake, "mine", gql_page([gql_pr(1, title="second")]))
    runner.gate.clear()
    now[0] += 61

    reads = await asyncio.gather(*(client.inbox_list("mine") for _ in range(2)))
    await asyncio.sleep(0)

    assert reads == [(first, True), (first, True)]
    assert runner.started == 2
    runner.gate.set()
    await client.inbox_list("mine", refresh=True)
    assert runner.started == 2


async def test_refresh_true_waits_for_new_data() -> None:
    now = [1000.0]
    client, runner, fake = _gated_client(now)
    runner.gate.set()
    first = (await client.inbox_list("mine")).inbox_list
    inbox_rule(fake, "mine", gql_page([gql_pr(1, title="forced")]))

    forced = await client.inbox_list("mine", refresh=True)

    assert forced.inbox_list is not first
    assert [pr.title for pr in forced.inbox_list.items] == ["forced"]
    assert client.cached_inbox_list("mine") is forced.inbox_list


async def test_failed_background_refresh_keeps_the_list_and_retries() -> None:
    now = [1000.0]
    client, runner, fake = _gated_client(now)
    runner.gate.set()
    first = (await client.inbox_list("mine")).inbox_list
    fake.on("graphql", f"q={INBOX_QUERIES['mine']}", code=1, stderr=b"gh: HTTP 401")
    now[0] += 61

    assert await client.inbox_list("mine") == (first, True)
    for _ in range(5):
        await asyncio.sleep(0)
    assert client.cached_inbox_list("mine") is first
    assert await client.inbox_list("mine") == (first, True)
    await asyncio.sleep(0)
    assert runner.started == 3


async def test_lists_refresh_independently() -> None:
    fake = FakeGh()
    for name in ("direct", "mine", "team"):
        inbox_rule(fake, name, gql_page([gql_pr(1)]))
    client = GitHubClient(fake)

    await client.inbox_list("direct")

    assert client.cached_inbox_list("direct") is not None
    assert client.cached_inbox_list("team") is None
    assert len(fake.calls) == 1


def test_repo_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_repo_config(tmp_path / "missing") == RepoConfig()

    write_config(
        tmp_path / "home",
        'default_repo = "Maybern/maybern"\nother = 1\n\n'
        '[repos]\n"Maybern/maybern" = "~/Dev/maybern"\n"a/b" = "/abs/b"\n',
    )
    config = load_repo_config(tmp_path / "home")

    assert config.default_repo == "Maybern/maybern"
    assert config.clone_path("maybern/MAYBERN") == tmp_path / "Dev" / "maybern"
    assert config.clone_path("x/y") is None
    assert config.known_repos == ["Maybern/maybern", "a/b"]


@pytest.mark.parametrize(
    "body",
    [
        "default_repo = 'not-a-repo'",
        "default_repo = 3",
        "repos = 'x'",
        "[repos]\n'bad key' = '/x'",
        "[repos]\n'a/b' = 'relative/path'",
        "[repos]\n'a/b' = 5",
        "this is not toml",
    ],
)
def test_repo_config_errors(tmp_path: Path, body: str) -> None:
    write_config(tmp_path, body)
    with pytest.raises(ConfigError):
        load_repo_config(tmp_path)
