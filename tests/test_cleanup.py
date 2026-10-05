import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from mcp.shared.memory import create_connected_server_and_client_session

from code_review_mcp import cleanup as cleanup_module
from code_review_mcp.cleanup import WorktreeSweeper
from code_review_mcp.config import Settings, load_settings
from code_review_mcp.github import GitHubClient, GitHubError, fetch_pr_states
from code_review_mcp.hub import ReviewHub
from code_review_mcp.pr_service import PrService
from code_review_mcp.repo_config import CleanupConfig, ConfigError, load_repo_config
from code_review_mcp.store import Store, timestamp
from code_review_mcp.web import create_app
from code_review_mcp.worktrees import GitError, PreparedWorktree, WorktreeManager

from .conftest import BASE_URL
from .pr_fixtures import (
    APP_V2,
    PR_NUMBER,
    REPO,
    FakeGh,
    PrRepo,
    git,
    make_pr_repo,
    pr_view_json,
    write_config,
)

OTHER_REPO = "acme/gadgets"
OTHER_NUMBER = 2
PR_URL = f"https://github.com/{REPO}/pull/{PR_NUMBER}"
OTHER_URL = f"https://github.com/{OTHER_REPO}/pull/{OTHER_NUMBER}"


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class CountingWorktrees(WorktreeManager):
    """Counts prepare calls and holds each one briefly, so parallel callers overlap."""

    prepare_calls = 0

    async def prepare(
        self,
        repo: str,
        number: int,
        *,
        mapped_clone: Path | None,
        base_ref: str,
        base_sha: str,
        head_sha: str,
    ) -> PreparedWorktree:
        self.prepare_calls += 1
        await asyncio.sleep(0.05)
        return await super().prepare(
            repo,
            number,
            mapped_clone=mapped_clone,
            base_ref=base_ref,
            base_sha=base_sha,
            head_sha=head_sha,
        )


def _states(**states: str) -> bytes:
    return json.dumps(
        {
            "data": {
                "repository": {f"pr{n.removeprefix('n')}": {"state": s} for n, s in states.items()}
            }
        }
    ).encode()


def _set_state(fake: FakeGh, repo: str, **states: str) -> None:
    fake.on("api", "graphql", f"name={repo.split('/')[1]}", stdout=_states(**states))


def _worktree(home: Path, repo: str, number: int) -> Path:
    return home / "worktrees" / f"{repo.replace('/', '-')}-{number}"


@pytest.fixture
def pr_repo(tmp_path: Path) -> PrRepo:
    repo = make_pr_repo(tmp_path / "git")
    git(repo.work, "push", "-q", "origin", f"pr:refs/pull/{OTHER_NUMBER}/head")
    return repo


@pytest.fixture
def fake_gh(pr_repo: PrRepo) -> FakeGh:
    fake = FakeGh()
    fake.on("pr", "view", str(PR_NUMBER), stdout=pr_view_json(pr_repo))
    fake.on(
        "pr",
        "view",
        str(OTHER_NUMBER),
        stdout=pr_view_json(pr_repo, number=OTHER_NUMBER, repo_name=OTHER_REPO),
    )
    _set_state(fake, REPO, n1="OPEN")
    _set_state(fake, OTHER_REPO, n2="OPEN")
    return fake


@pytest.fixture
def config_home(settings: Settings, pr_repo: PrRepo) -> Path:
    write_config(
        settings.home,
        f'default_repo = "{REPO}"\n\n[repos]\n"{REPO}" = "{pr_repo.clone}"\n'
        f'"{OTHER_REPO}" = "{pr_repo.clone}"\n',
    )
    return settings.home


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def worktrees(settings: Settings) -> CountingWorktrees:
    return CountingWorktrees(settings.home, credential_helper=None)


@pytest.fixture
def prs(
    store: Store,
    hub: ReviewHub,
    settings: Settings,
    fake_gh: FakeGh,
    worktrees: CountingWorktrees,
    clock: Clock,
    config_home: Path,
) -> PrService:
    return PrService(store, hub, settings, GitHubClient(fake_gh), worktrees, clock=clock)


@pytest.fixture
def sweeper(
    store: Store, prs: PrService, fake_gh: FakeGh, settings: Settings, clock: Clock
) -> WorktreeSweeper:
    return WorktreeSweeper(
        store,
        prs,
        GitHubClient(fake_gh),
        lambda: load_repo_config(settings.home),
        clock=clock,
    )


async def test_merged_pr_is_closed_by_the_sweep(
    prs: PrService,
    sweeper: WorktreeSweeper,
    store: Store,
    hub: ReviewHub,
    fake_gh: FakeGh,
    clock: Clock,
    pr_repo: PrRepo,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="code_review_mcp")
    review_id = (await prs.open_pr(PR_URL)).review.id
    await prs.set_viewed(review_id, "app.py", True)
    store.create_thread(
        review_id=review_id,
        kind="question",
        path="app.py",
        side="additions",
        line=2,
        status="submitted",
        author="user",
        body="why?",
    )
    queue = hub.subscribe(review_id)
    _set_state(fake_gh, REPO, n1="MERGED")
    clock.advance(timedelta(hours=2))

    report = await sweeper.sweep_once()

    assert report.closed == [f"{REPO}#{PR_NUMBER}"]
    review = store.get_review(review_id)
    assert review is not None
    assert (review.status, review.worktree_path, review.pr_state) == ("closed", None, "merged")
    assert not _worktree(settings.home, REPO, PR_NUMBER).exists()
    assert git(pr_repo.clone, "for-each-ref", "refs/code-review-mcp") == ""
    assert json.loads(queue.get_nowait())["type"] == "review_closed"
    assert len(store.list_threads(review_id)) == 1
    assert store.viewed_paths(review_id, pr_repo.head_sha) == {"app.py"}
    assert f"removed worktree of {REPO}#{PR_NUMBER}: the PR is merged" in caplog.text
    [graphql_call] = fake_gh.calls_with("api", "graphql")
    assert "pr1: pullRequest(number: 1) { state }" in " ".join(graphql_call)


async def test_open_pr_is_kept(
    prs: PrService, sweeper: WorktreeSweeper, store: Store, clock: Clock, settings: Settings
) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id
    clock.advance(timedelta(days=6, hours=23))

    report = await sweeper.sweep_once()

    assert (report.closed, report.released, report.failures) == ([], [], [])
    review = store.get_review(review_id)
    assert review is not None
    assert (review.status, review.worktree_path) == (
        "open",
        str(_worktree(settings.home, REPO, PR_NUMBER)),
    )
    assert _worktree(settings.home, REPO, PR_NUMBER).is_dir()


async def test_merged_pr_with_recent_activity_is_kept(
    prs: PrService, sweeper: WorktreeSweeper, fake_gh: FakeGh, clock: Clock, store: Store
) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id
    _set_state(fake_gh, REPO, n1="MERGED")
    clock.advance(timedelta(hours=2))
    await prs.get_file(review_id, "app.py")
    clock.advance(timedelta(minutes=59))

    kept = await sweeper.sweep_once()
    clock.advance(timedelta(minutes=2))
    closed = await sweeper.sweep_once()

    assert kept.kept_recent == [f"{REPO}#{PR_NUMBER}"]
    assert kept.closed == []
    assert closed.closed == [f"{REPO}#{PR_NUMBER}"]


async def test_thread_change_counts_as_activity(
    prs: PrService, sweeper: WorktreeSweeper, fake_gh: FakeGh, clock: Clock, store: Store
) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id
    _set_state(fake_gh, REPO, n1="MERGED")
    clock.advance(timedelta(hours=2))
    store.create_thread(
        review_id=review_id,
        kind="question",
        path="app.py",
        side="additions",
        line=2,
        status="submitted",
        author="user",
        body="later question",
    )
    store._conn.execute(
        "UPDATE threads SET updated_at = ? WHERE review_id = ?",
        (timestamp(clock.now - timedelta(minutes=5)), review_id),
    )

    report = await sweeper.sweep_once()

    assert report.kept_recent == [f"{REPO}#{PR_NUMBER}"]


async def test_parallel_reads_after_release_share_one_restore(
    prs: PrService,
    sweeper: WorktreeSweeper,
    worktrees: CountingWorktrees,
    clock: Clock,
    settings: Settings,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="code_review_mcp")
    review_id = (await prs.open_pr(PR_URL)).review.id
    clock.advance(timedelta(days=8))
    assert (await sweeper.sweep_once()).released == [f"{REPO}#{PR_NUMBER}"]
    assert not _worktree(settings.home, REPO, PR_NUMBER).exists()
    calls_before = worktrees.prepare_calls

    view, file = await asyncio.gather(prs.get_pr_view(review_id), prs.get_file(review_id, "app.py"))

    assert worktrees.prepare_calls == calls_before + 1
    files = view["files"]
    assert isinstance(files, list) and len(files) == 4
    assert file["new_content"] == APP_V2
    assert _worktree(settings.home, REPO, PR_NUMBER).is_dir()
    assert f"released worktree of {REPO}#{PR_NUMBER}: no activity for 7 days" in caplog.text
    assert f"restored worktree of {REPO}#{PR_NUMBER}" in caplog.text


async def test_gh_failure_for_one_repo_does_not_stop_the_others(
    prs: PrService,
    sweeper: WorktreeSweeper,
    fake_gh: FakeGh,
    clock: Clock,
    store: Store,
    settings: Settings,
) -> None:
    widgets = (await prs.open_pr(PR_URL)).review.id
    gadgets = (await prs.open_pr(OTHER_URL)).review.id
    fake_gh.on("api", "graphql", "name=widgets", code=1, stderr=b"HTTP 502: Bad Gateway")
    _set_state(fake_gh, OTHER_REPO, n2="CLOSED")
    clock.advance(timedelta(days=8))

    report = await sweeper.sweep_once()

    assert report.closed == [f"{OTHER_REPO}#{OTHER_NUMBER}"]
    assert report.released == [f"{REPO}#{PR_NUMBER}"]
    assert len(report.failures) == 1 and "Bad Gateway" in report.failures[0]
    statuses = {r.id: (r.status, r.worktree_path) for r in store.list_reviews()}
    assert statuses == {widgets: ("open", None), gadgets: ("closed", None)}


async def test_git_failure_for_one_review_does_not_stop_the_others(
    prs: PrService,
    sweeper: WorktreeSweeper,
    fake_gh: FakeGh,
    worktrees: CountingWorktrees,
    clock: Clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await prs.open_pr(PR_URL)
    await prs.open_pr(OTHER_URL)
    _set_state(fake_gh, REPO, n1="MERGED")
    _set_state(fake_gh, OTHER_REPO, n2="MERGED")
    clock.advance(timedelta(hours=2))
    real_remove = worktrees.remove

    async def failing_remove(repo: str, number: int, *, mapped_clone: Path | None) -> None:
        if repo == OTHER_REPO:
            raise GitError("simulated git failure")
        await real_remove(repo, number, mapped_clone=mapped_clone)

    monkeypatch.setattr(worktrees, "remove", failing_remove)

    report = await sweeper.sweep_once()

    assert report.closed == [f"{REPO}#{PR_NUMBER}"]
    assert report.failures == [f"{OTHER_REPO}#{OTHER_NUMBER}: simulated git failure"]


async def test_disabled_cleanup_does_nothing(
    prs: PrService,
    sweeper: WorktreeSweeper,
    fake_gh: FakeGh,
    clock: Clock,
    settings: Settings,
    pr_repo: PrRepo,
) -> None:
    review_id = (await prs.open_pr(PR_URL)).review.id
    write_config(
        settings.home,
        f'[repos]\n"{REPO}" = "{pr_repo.clone}"\n\n[cleanup]\nenabled = false\n',
    )
    _set_state(fake_gh, REPO, n1="MERGED")
    clock.advance(timedelta(days=30))

    report = await sweeper.sweep_once()

    assert (report.closed, report.released, report.failures) == ([], [], [])
    assert fake_gh.calls_with("api", "graphql") == []
    assert _worktree(settings.home, REPO, PR_NUMBER).is_dir()
    assert (await prs.get_pr_view(review_id))["status"] == "open"


async def test_bad_config_skips_the_sweep(
    prs: PrService, sweeper: WorktreeSweeper, settings: Settings
) -> None:
    await prs.open_pr(PR_URL)
    write_config(settings.home, "[cleanup]\nidle_days = 0\n")

    report = await sweeper.sweep_once()

    assert report.closed == report.released == []
    assert "idle_days" in report.failures[0]


async def test_run_forever_survives_a_failing_sweep(
    sweeper: WorktreeSweeper, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(settings.home, "[cleanup]\ninterval_minutes = 0.0002\n")
    calls: list[int] = []

    async def flaky_sweep() -> cleanup_module.SweepReport:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("bug in one sweep")
        return cleanup_module.SweepReport()

    monkeypatch.setattr(sweeper, "sweep_once", flaky_sweep)
    task = asyncio.create_task(sweeper.run_forever(first_delay=0))
    for _ in range(200):
        if len(calls) >= 3:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(calls) >= 3


@pytest.mark.parametrize("enabled", [True, False])
async def test_lifespan_starts_and_stops_the_sweep(
    settings: Settings, store: Store, enabled: bool
) -> None:
    app = create_app(replace(settings, cleanup_enabled=enabled), store)

    async with app.router.lifespan_context(app):
        task = app.state.sweep_task
        assert (task is not None and not task.done()) if enabled else task is None

    if enabled:
        assert task.cancelled()


@pytest.fixture
def api_app(settings: Settings, store: Store, fake_gh: FakeGh, config_home: Path) -> FastAPI:
    return create_app(
        settings,
        store,
        github=GitHubClient(fake_gh),
        worktrees=WorktreeManager(settings.home, credential_helper=None),
    )


@pytest.fixture
async def api(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url=BASE_URL
    ) as client:
        yield client


def _backdate(store: Store, review_id: str, delta: timedelta) -> None:
    store._conn.execute(
        "UPDATE reviews SET last_activity_at = ? WHERE id = ?",
        (timestamp(datetime.now(UTC) - delta), review_id),
    )


async def _open(api: httpx.AsyncClient, ref: str) -> str:
    response = await api.post("/api/prs/open", json={"ref": ref})
    assert response.status_code == 200, response.text
    review_id: str = response.json()["review_id"]
    return review_id


def _tool_result(result: Any) -> Any:
    assert not result.isError, result.content
    return result.structuredContent


async def test_rest_and_mcp_reads_keep_a_merged_pr(
    api: httpx.AsyncClient, api_app: FastAPI, store: Store, fake_gh: FakeGh
) -> None:
    sweeper: WorktreeSweeper = api_app.state.sweeper
    review_id = await _open(api, PR_URL)
    _set_state(fake_gh, REPO, n1="MERGED")

    _backdate(store, review_id, timedelta(hours=2))
    assert (await api.get(f"/api/reviews/{review_id}/pr")).status_code == 200
    after_rest = await sweeper.sweep_once()

    _backdate(store, review_id, timedelta(hours=2))
    async with create_connected_server_and_client_session(api_app.state.mcp) as session:
        _tool_result(await session.call_tool("get_review", {"review_id": review_id}))
    after_mcp = await sweeper.sweep_once()

    _backdate(store, review_id, timedelta(hours=2))
    after_no_read = await sweeper.sweep_once()

    assert after_rest.kept_recent == [f"{REPO}#{PR_NUMBER}"]
    assert after_mcp.kept_recent == [f"{REPO}#{PR_NUMBER}"]
    assert after_no_read.closed == [f"{REPO}#{PR_NUMBER}"]


async def test_idle_release_then_restore_on_pr_read(
    api: httpx.AsyncClient, api_app: FastAPI, store: Store, settings: Settings, pr_repo: PrRepo
) -> None:
    sweeper: WorktreeSweeper = api_app.state.sweeper
    review_id = await _open(api, PR_URL)
    worktree = _worktree(settings.home, REPO, PR_NUMBER)
    assert (await api.get("/api/health")).json()["worktrees"] == {"count": 1, "released": 0}

    _backdate(store, review_id, timedelta(days=8))
    report = await sweeper.sweep_once()

    assert report.released == [f"{REPO}#{PR_NUMBER}"]
    assert not worktree.exists()
    assert git(pr_repo.clone, "for-each-ref", "refs/code-review-mcp") == ""
    released = store.get_review(review_id)
    assert released is not None
    assert (released.status, released.worktree_path) == ("open", None)
    assert (await api.get("/api/health")).json()["worktrees"] == {"count": 0, "released": 1}

    view = (await api.get(f"/api/reviews/{review_id}/pr")).json()

    assert view["status"] == "open"
    assert view["worktree_path"] == str(worktree)
    assert len(view["files"]) == 4
    assert git(worktree, "rev-parse", "HEAD") == pr_repo.head_sha
    assert (await api.get("/api/health")).json()["worktrees"] == {"count": 1, "released": 0}


async def test_fetch_pr_states() -> None:
    fake = FakeGh()
    fake.on(
        "api",
        "graphql",
        "owner=o",
        "name=r",
        stdout=b'{"data": {"repository": {"pr3": {"state": "MERGED"}, "pr7": {"state": "OPEN"}}}}',
    )
    client = GitHubClient(fake)

    states = await fetch_pr_states(client, "o/r", [7, 3, 7])
    empty = await fetch_pr_states(client, "o/r", [])

    assert states == {3: "merged", 7: "open"}
    assert empty == {}
    [call] = fake.calls
    query = next(arg for arg in call if arg.startswith("query="))
    assert query.count("pullRequest(") == 2


async def test_fetch_pr_states_keeps_partial_data_and_reports_failures() -> None:
    fake = FakeGh()
    fake.on(
        "name=partial",
        code=1,
        stdout=b'{"data": {"repository": {"pr1": {"state": "CLOSED"}, "pr9": null}},'
        b' "errors": [{"type": "NOT_FOUND"}]}',
        stderr=b"gh: Could not resolve to a PullRequest with the number of 9.",
    )
    fake.on("name=down", code=1, stderr=b"HTTP 502: Bad Gateway")
    fake.on("name=garbage", stdout=b'{"unexpected": true}')
    client = GitHubClient(fake)

    assert await fetch_pr_states(client, "o/partial", [1, 9]) == {1: "closed"}
    with pytest.raises(GitHubError, match="Bad Gateway"):
        await fetch_pr_states(client, "o/down", [1])
    with pytest.raises(GitHubError, match="Unexpected output from gh"):
        await fetch_pr_states(client, "o/garbage", [1])


def test_cleanup_config(tmp_path: Path) -> None:
    assert load_repo_config(tmp_path).cleanup == CleanupConfig(
        enabled=True, interval_minutes=15.0, idle_days=7.0
    )
    write_config(tmp_path, "[cleanup]\nenabled = false\ninterval_minutes = 1\nidle_days = 0.5\n")

    assert load_repo_config(tmp_path).cleanup == CleanupConfig(
        enabled=False, interval_minutes=1.0, idle_days=0.5
    )


@pytest.mark.parametrize(
    "body",
    [
        "cleanup = 3",
        "[cleanup]\nenabled = 'no'",
        "[cleanup]\ninterval_minutes = 0",
        "[cleanup]\nidle_days = -1",
        "[cleanup]\nidle_days = true",
        "[cleanup]\ninterval_minutes = 'soon'",
    ],
)
def test_cleanup_config_errors(tmp_path: Path, body: str) -> None:
    write_config(tmp_path, body)
    with pytest.raises(ConfigError):
        load_repo_config(tmp_path)


def test_cleanup_env_switch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CODE_REVIEW_MCP_HOME", str(tmp_path))
    monkeypatch.delenv("CODE_REVIEW_MCP_CLEANUP", raising=False)
    assert load_settings().cleanup_enabled is True
    monkeypatch.setenv("CODE_REVIEW_MCP_CLEANUP", "0")
    assert load_settings().cleanup_enabled is False
