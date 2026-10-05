import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import httpx
import pytest
from fastapi import FastAPI

from code_review_mcp.agent_prompts import review_delta
from code_review_mcp.agents import AgentRunner, TurnEnd
from code_review_mcp.config import Settings
from code_review_mcp.github import GitHubClient
from code_review_mcp.github_reviews import GitHubReviewWriter
from code_review_mcp.hub import ReviewHub
from code_review_mcp.pr_service import PrService
from code_review_mcp.repo_config import AgentConfig, ConfigError, load_repo_config
from code_review_mcp.review_threads import AnchorRequest, ThreadService
from code_review_mcp.store import MessageRow, ReviewRow, Store, ThreadRow, timestamp
from code_review_mcp.web import create_app
from code_review_mcp.worktrees import ChangedFile, WorktreeManager

from .agent_fakes import FakeAgents, FakeSession, echo_answer, long_answer, question_of
from .conftest import BASE_URL
from .pr_fixtures import (
    PR_NUMBER,
    REPO,
    FakeGh,
    PrRepo,
    git,
    gql_page,
    gql_pr,
    inbox_rule,
    make_pr_repo,
    pr_view_json,
    write_config,
)

REPOS = {1: REPO, 2: "acme/gadgets", 3: "acme/gizmos"}
LOC = "app.py:2 (new file, additions side)"


def _url(number: int) -> str:
    return f"https://github.com/{REPOS[number]}/pull/{number}"


def _config(clone: Path, **agent: object) -> str:
    settings = {
        "model": '"haiku"',
        "max_live_clients": 2,
        "idle_minutes": 1,
        "max_client_budget_usd": 5,
        "warmup": '"never"',
        **{k: (f'"{v}"' if isinstance(v, str) else str(v).lower()) for k, v in agent.items()},
    }
    repos = "\n".join(f'"{repo}" = "{clone}"' for repo in REPOS.values())
    table = "\n".join(f"{k} = {v}" for k, v in settings.items())
    return f'default_repo = "{REPO}"\n\n[repos]\n{repos}\n\n[agent]\n{table}\n'


@pytest.fixture
def pr_repo(tmp_path: Path) -> PrRepo:
    repo = make_pr_repo(tmp_path / "git")
    for number in (2, 3):
        git(repo.work, "push", "-q", "origin", f"pr:refs/pull/{number}/head")
    return repo


@pytest.fixture
def fake_gh(pr_repo: PrRepo) -> FakeGh:
    fake = FakeGh()
    for number, repo in REPOS.items():
        fake.on(
            "pr", "view", str(number), stdout=pr_view_json(pr_repo, number=number, repo_name=repo)
        )
    inbox_rule(fake, "direct", gql_page([]))
    return fake


@pytest.fixture
def agent_settings(settings: Settings, pr_repo: PrRepo) -> Settings:
    write_config(settings.home, _config(pr_repo.clone))
    return replace(settings, agent_enabled=True)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake() -> FakeAgents:
    return FakeAgents()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def prs(store: Store, hub: ReviewHub, agent_settings: Settings, fake_gh: FakeGh) -> PrService:
    worktrees = WorktreeManager(agent_settings.home, credential_helper=None)
    return PrService(store, hub, agent_settings, GitHubClient(fake_gh), worktrees)


@pytest.fixture
def threads(store: Store, hub: ReviewHub, prs: PrService, fake_gh: FakeGh) -> ThreadService:
    return ThreadService(store, hub, prs, GitHubClient(fake_gh), GitHubReviewWriter(fake_gh))


def _runner(
    store: Store,
    hub: ReviewHub,
    settings: Settings,
    prs: PrService,
    threads: ThreadService,
    fake_gh: FakeGh,
    fake: FakeAgents,
    clock: Clock,
) -> AgentRunner:
    return AgentRunner(
        store,
        hub,
        settings,
        prs,
        threads,
        GitHubClient(fake_gh),
        lambda: load_repo_config(settings.home),
        fake.factory,
        clock=clock,
    )


@pytest.fixture
async def runner(
    store: Store,
    hub: ReviewHub,
    agent_settings: Settings,
    prs: PrService,
    threads: ThreadService,
    fake_gh: FakeGh,
    fake: FakeAgents,
    clock: Clock,
) -> AsyncIterator[AgentRunner]:
    created = _runner(store, hub, agent_settings, prs, threads, fake_gh, fake, clock)
    yield created
    await created.shutdown()


async def _open(prs: PrService, number: int = PR_NUMBER) -> str:
    return (await prs.open_pr(_url(number))).review.id


def _ask(
    runner: AgentRunner,
    threads: ThreadService,
    review_id: str,
    body: str,
    *,
    path: str = "app.py",
    line: int = 2,
) -> str:
    thread = threads.create_question(
        review_id, path, AnchorRequest("additions", line, None, None), body
    )
    thread_id = str(thread["id"])
    runner.ask(thread_id)
    return thread_id


def _drain_events(queue: asyncio.Queue[str]) -> list[dict[str, Any]]:
    return [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]


def _bodies(store: Store, thread_id: str) -> list[tuple[str, str]]:
    return [(m.author, m.body) for m in store.list_messages(thread_id)]


async def test_question_streams_and_stores_the_answer(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    hub: ReviewHub,
    fake: FakeAgents,
    pr_repo: PrRepo,
) -> None:
    review_id = await _open(prs)
    events = hub.subscribe(review_id)

    thread_id = _ask(runner, threads, review_id, "why is x two?")
    await runner.wait_idle(review_id)

    assert _bodies(store, thread_id) == [
        ("user", "why is x two?"),
        ("agent", "answer to why is x two?"),
    ]
    published = _drain_events(events)
    deltas = [e["text"] for e in published if e["type"] == "agent_delta"]
    assert "".join(deltas) == "answer to why is x two?"
    [message] = [e for e in published if e["type"] == "agent_message"]
    assert message["thread_id"] == thread_id
    assert set(message["message"]) == {"id", "author", "body", "created_at"}
    assert message["message"]["author"] == "agent"
    statuses = [e["state"] for e in published if e["type"] == "agent_status"]
    assert statuses[0] == "queued" and "running" in statuses and statuses[-1] == "idle"
    [session] = fake.sessions
    review = store.get_review(review_id)
    assert review is not None
    assert (session.spec.resume, session.spec.session_id) == (False, review.agent_session_id)
    assert session.spec.model == "haiku"
    assert session.spec.cwd == Path(review.worktree_path or "")
    assert review.agent_cost_usd == pytest.approx(0.01)
    assert review.agent_last_turn_at is not None
    assert review.agent_head_sha == pr_repo.head_sha
    prompt = session.prompts[0]
    assert f"Pull request {REPO}#1: Make x two" in prompt
    assert "app.py:2 (new file, additions side)" in prompt
    assert prompt.endswith("Question:\nwhy is x two?")


async def test_questions_queue_per_review_and_reviews_run_in_parallel(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    gate = asyncio.Event()
    started: list[str] = []

    async def gated(session: FakeSession, prompt: str) -> None:
        started.append(question_of(prompt))
        if question_of(prompt) == "first":
            await gate.wait()
        await echo_answer(session, prompt)

    fake.behavior = gated
    review_a = await _open(prs, 1)
    review_b = await _open(prs, 2)
    first = _ask(runner, threads, review_a, "first")
    second = _ask(runner, threads, review_a, "second")
    other = _ask(runner, threads, review_b, "other review")
    await runner.wait_idle(review_b)

    status_a = runner.status(review_a)
    assert (status_a["state"], status_a["running_thread_id"], status_a["queue"]) == (
        "running",
        first,
        [second],
    )
    assert started == ["first", "other review"]
    assert _bodies(store, other)[-1] == ("agent", "answer to other review")
    gate.set()
    await runner.wait_idle(review_a)

    assert started == ["first", "other review", "second"]
    assert _bodies(store, first)[-1] == ("agent", "answer to first")
    assert _bodies(store, second)[-1] == ("agent", "answer to second")
    assert len(fake.for_review(review_a)) == 1


async def test_follow_up_reply_uses_the_same_session(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    review_id = await _open(prs)
    thread_id = _ask(runner, threads, review_id, "what is x?")
    await runner.wait_idle(review_id)
    store.add_message(thread_id, author="user", body="and why?")
    runner.ask(thread_id)
    await runner.wait_idle(review_id)

    [session] = fake.sessions
    assert "Follow-up in question thread" in session.prompts[1]
    assert session.prompts[1].endswith("Question:\nand why?")
    assert "Pull request" not in session.prompts[1]
    assert [b for _, b in _bodies(store, thread_id)] == [
        "what is x?",
        "answer to what is x?",
        "and why?",
        "answer to and why?",
    ]


async def test_stop_interrupts_drains_and_the_next_answer_matches(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    fake: FakeAgents,
) -> None:
    async def long_then_echo(session: FakeSession, prompt: str) -> None:
        if question_of(prompt) == "write an essay":
            await long_answer(session, prompt)
        else:
            await echo_answer(session, prompt)

    fake.behavior = long_then_echo
    review_id = await _open(prs)
    essay = _ask(runner, threads, review_id, "write an essay")
    for _ in range(100):
        if (
            runner.status(review_id)["running_thread_id"] == essay
            and fake.sessions
            and fake.sessions[0].prompts
        ):
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    await runner.stop(essay)
    after = _ask(runner, threads, review_id, "what is 2+2?")
    await runner.wait_idle(review_id)

    [session] = fake.sessions
    assert session.interrupts == 1
    stopped = _bodies(store, essay)[-1]
    assert stopped[0] == "agent" and stopped[1].endswith("_(stopped)_")
    assert "tail-after-interrupt" in stopped[1]
    assert _bodies(store, after)[-1] == ("agent", "answer to what is 2+2?")
    assert runner.status(review_id)["state"] == "idle"
    with pytest.raises(Exception, match="No agent turn is running"):
        await runner.stop(essay)


async def test_question_timeout_interrupts(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    fake: FakeAgents,
    agent_settings: Settings,
    pr_repo: PrRepo,
) -> None:
    write_config(agent_settings.home, _config(pr_repo.clone, question_timeout_minutes=0.002))
    fake.behavior = long_answer
    review_id = await _open(prs)

    thread_id = _ask(runner, threads, review_id, "never ends")
    await runner.wait_idle(review_id)

    author, body = _bodies(store, thread_id)[-1]
    assert author == "agent"
    assert body.endswith("_(stopped after 0.002 minutes)_")
    assert fake.sessions[0].interrupts == 1


async def test_cost_delta_and_recycle(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    costs = iter([3.0, 1.5, 1.0])

    async def priced(session: FakeSession, prompt: str) -> None:
        await session.say("ok")
        await session.end("ok", cost=next(costs))

    fake.behavior = priced
    review_id = await _open(prs)
    for question in ("one", "two", "three"):
        _ask(runner, threads, review_id, question)
        await runner.wait_idle(review_id)

    first, second = fake.sessions
    review = store.get_review(review_id)
    assert review is not None
    assert review.agent_cost_usd == pytest.approx(5.5)
    assert first.closed and len(first.prompts) == 2
    assert (second.spec.resume, second.spec.session_id) == (True, first.spec.session_id)
    assert runner.status(review_id)["context_tokens"] == 1010


async def test_idle_close_and_resume(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    fake: FakeAgents,
    clock: Clock,
) -> None:
    review_id = await _open(prs)
    _ask(runner, threads, review_id, "one")
    await runner.wait_idle(review_id)
    clock.now += 59
    assert await runner.reap_idle() == []
    clock.now += 2
    assert await runner.reap_idle() == [review_id]
    _ask(runner, threads, review_id, "two")
    await runner.wait_idle(review_id)

    first, second = fake.sessions
    assert first.closed
    assert (second.spec.resume, second.spec.session_id) == (True, first.spec.session_id)


async def test_lru_cap_closes_the_least_recently_used_idle_client(
    runner: AgentRunner, prs: PrService, threads: ThreadService, fake: FakeAgents, clock: Clock
) -> None:
    reviews = [await _open(prs, n) for n in (1, 2, 3)]
    for review_id in reviews[:2]:
        clock.now += 10
        _ask(runner, threads, review_id, "hi")
        await runner.wait_idle(review_id)
    clock.now += 10
    _ask(runner, threads, reviews[0], "again")
    await runner.wait_idle(reviews[0])
    clock.now += 10
    _ask(runner, threads, reviews[2], "third review")
    await runner.wait_idle(reviews[2])

    by_review = {s.spec.review_id: s for s in fake.sessions}
    assert by_review[reviews[1]].closed
    assert not by_review[reviews[0]].closed
    assert not by_review[reviews[2]].closed
    assert runner.live_clients() == 2


async def test_resume_after_restart_uses_the_stored_session(
    store: Store,
    hub: ReviewHub,
    agent_settings: Settings,
    prs: PrService,
    threads: ThreadService,
    fake_gh: FakeGh,
    clock: Clock,
) -> None:
    review_id = await _open(prs)
    before = FakeAgents()
    first = _runner(store, hub, agent_settings, prs, threads, fake_gh, before, clock)
    _ask(first, threads, review_id, "remember 7341")
    await first.wait_idle(review_id)
    await first.shutdown()
    after = FakeAgents()
    second = _runner(store, hub, agent_settings, prs, threads, fake_gh, after, clock)
    _ask(second, threads, review_id, "what number?")
    await second.wait_idle(review_id)
    await second.shutdown()

    [old], [new] = before.sessions, after.sessions
    assert old.closed
    assert (old.spec.resume, new.spec.resume) == (False, True)
    assert new.spec.session_id == old.spec.session_id
    assert "Pull request" in old.prompts[0]
    assert "Pull request" not in new.prompts[0]


async def test_worktree_release_and_close_end_the_client(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    fake: FakeAgents,
) -> None:
    review_id = await _open(prs)
    _ask(runner, threads, review_id, "one")
    await runner.wait_idle(review_id)
    store._conn.execute(
        "UPDATE reviews SET last_activity_at = ? WHERE id = ?",
        (timestamp(datetime.now(UTC) - timedelta(days=30)), review_id),
    )
    store._conn.execute(
        "UPDATE threads SET updated_at = '2000-01-01' WHERE review_id = ?", (review_id,)
    )
    assert await prs.release_if_inactive(review_id, datetime.now(UTC) - timedelta(days=7))
    for _ in range(50):
        if fake.sessions[0].closed:
            break
        await asyncio.sleep(0.01)
    assert fake.sessions[0].closed
    assert runner.live_clients() == 0

    _ask(runner, threads, review_id, "two")
    await runner.wait_idle(review_id)
    await prs.close(review_id)
    for _ in range(50):
        if fake.sessions[1].closed:
            break
        await asyncio.sleep(0.01)
    assert fake.sessions[1].closed


def _thread(**fields: Any) -> ThreadRow:
    values: dict[str, Any] = {
        "id": "t",
        "review_id": "r",
        "kind": "review_comment",
        "path": "app.py",
        "side": "additions",
        "line": 2,
        "start_line": None,
        "start_side": None,
        "line_content": "",
        "anchor_sha": "h",
        "status": "draft",
        "created_by": "user",
        "created_at": "2026-10-05T10:00:00+00:00",
        "updated_at": "2026-10-05T10:00:00+00:00",
        "github_comment_id": None,
        "github_url": None,
    }
    values.update(fields)
    return ThreadRow(**values)


def _review(**fields: Any) -> ReviewRow:
    values: dict[str, Any] = {name: None for name in ReviewRow.__dataclass_fields__}
    values.update(
        id="r",
        kind="pr",
        title="T",
        repo=REPO,
        pr_number=1,
        agent_cost_usd=0.0,
        status="open",
        created_at="x",
        updated_at="x",
        is_draft=False,
        head_sha="b" * 40,
        merge_base_sha="m" * 40,
        agent_head_sha="b" * 40,
        agent_last_turn_at="2026-10-05T09:00:00+00:00",
    )
    values.update(fields)
    return ReviewRow(**values)


def test_review_delta_content() -> None:
    threads = [
        _thread(
            id="new", created_at="2026-10-05T10:00:00+00:00", updated_at="2026-10-05T10:00:00+00:00"
        ),
        _thread(
            id="edited",
            created_at="2026-10-05T08:00:00+00:00",
            updated_at="2026-10-05T10:00:00+00:00",
        ),
        _thread(
            id="posted",
            status="posted",
            created_at="2026-10-05T08:00:00+00:00",
            updated_at="2026-10-05T10:00:00+00:00",
        ),
        _thread(
            id="stale",
            status="stale",
            line=7,
            created_at="2026-10-05T08:00:00+00:00",
            updated_at="2026-10-05T10:00:00+00:00",
        ),
        _thread(
            id="old", created_at="2026-10-05T08:00:00+00:00", updated_at="2026-10-05T08:30:00+00:00"
        ),
        _thread(
            id="resolved",
            kind="question",
            status="resolved",
            path="",
            line=0,
            created_at="2026-10-05T08:00:00+00:00",
            updated_at="2026-10-05T10:00:00+00:00",
        ),
        _thread(
            id="question",
            kind="question",
            status="submitted",
            path="lib.py",
            line=0,
            created_at="2026-10-05T10:00:00+00:00",
            updated_at="2026-10-05T10:00:00+00:00",
        ),
        _thread(
            id="current",
            kind="question",
            status="submitted",
            created_at="2026-10-05T10:00:00+00:00",
            updated_at="2026-10-05T10:00:00+00:00",
        ),
    ]
    messages = {
        t.id: [MessageRow(f"m-{t.id}", t.id, "user", f"body of {t.id}", "x")] for t in threads
    }
    moved = _review(head_sha="c" * 40)

    delta = review_delta(
        moved,
        threads,
        messages,
        ["app.py", "lib.py"],
        exclude_thread_id="current",
        head_changes=[ChangedFile("app.py", "modified", None, 1, 1)],
    )
    quiet = review_delta(
        _review(),
        [threads[4]],
        messages,
        [],
        exclude_thread_id=None,
        head_changes=None,
    )

    assert delta.splitlines() == [
        "Changes in the review since your last turn:",
        f"- The PR head moved from {'b' * 12} to {'c' * 12}. Files changed between them: "
        "modified app.py",
        f'- New draft review comment by the user on {LOC}: "body of new"',
        f'- Edited draft review comment on {LOC}: "body of edited"',
        f'- Review comment posted to GitHub on {LOC}: "body of posted"',
        "- Review comment now stale (its line changed) on app.py:7 (new file, additions side): "
        '"body of stale"',
        '- Resolved thread on the whole PR: "body of resolved"',
        '- New question thread on the file lib.py: "body of question"',
        "- Files marked viewed: app.py, lib.py",
    ]
    assert quiet == ""


async def test_runner_prompt_carries_the_delta(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    fake: FakeAgents,
) -> None:
    review_id = await _open(prs)
    _ask(runner, threads, review_id, "one")
    await runner.wait_idle(review_id)
    await threads.create_thread(
        review_id, "app.py", AnchorRequest("additions", 2, None, None), "rename x"
    )
    await prs.set_viewed(review_id, "gone.txt", True)
    _ask(runner, threads, review_id, "two")
    await runner.wait_idle(review_id)

    second_prompt = fake.sessions[0].prompts[1]
    assert (
        'New draft review comment by the user on app.py:2 (new file, additions side): "rename x"'
        in second_prompt
    )
    assert "Files marked viewed: gone.txt" in second_prompt
    assert "New question thread on" not in second_prompt


async def test_draft_tool_creates_an_agent_draft(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    hub: ReviewHub,
    fake: FakeAgents,
) -> None:
    outcomes = []

    async def drafting(session: FakeSession, prompt: str) -> None:
        outcomes.append(
            await session.spec.draft(
                {"path": "app.py", "line": 2, "side": "additions", "body": "Name it y."}
            )
        )
        outcomes.append(
            await session.spec.draft(
                {"path": "new_name.py", "line": 5, "side": "additions", "body": "out of diff"}
            )
        )
        outcomes.append(await session.spec.draft({"path": "app.py", "body": "no line"}))
        await session.say("drafted")
        await session.end("drafted")

    fake.behavior = drafting
    review_id = await _open(prs)
    events = hub.subscribe(review_id)
    _ask(runner, threads, review_id, "draft a comment")
    await runner.wait_idle(review_id)

    good, outside, missing = outcomes
    assert good.ok and "saved on app.py line 2" in good.text
    assert not outside.ok and "outside the diff" in outside.text
    assert not missing.ok and "required" in missing.text
    [draft] = store.list_threads(review_id, kind="review_comment")
    assert (draft.created_by, draft.status, draft.line) == ("agent", "draft", 2)
    added = [e for e in _drain_events(events) if e["type"] == "thread_added"]
    assert [e["thread"]["kind"] for e in added] == ["question", "review_comment"]


async def test_error_turn_publishes_agent_error(
    runner: AgentRunner, prs: PrService, threads: ThreadService, hub: ReviewHub, fake: FakeAgents
) -> None:
    async def failing(session: FakeSession, prompt: str) -> None:
        await session.end(None, is_error=True, subtype="error_max_turns")

    fake.behavior = failing
    review_id = await _open(prs)
    events = hub.subscribe(review_id)
    thread_id = _ask(runner, threads, review_id, "too hard")
    await runner.wait_idle(review_id)

    errors = [e for e in _drain_events(events) if e["type"] == "agent_error"]
    assert errors == [
        {
            "type": "agent_error",
            "review_id": review_id,
            "thread_id": thread_id,
            "error": "The agent turn ended with error_max_turns",
        }
    ]
    assert runner.status(review_id)["state"] == "error"


async def _await_warmup(runner: AgentRunner, review_id: str) -> None:
    for _ in range(100):
        await runner.wait_idle(review_id)
        if runner.status(review_id)["warmup"]["status"] != "none":  # type: ignore[index]
            await runner.wait_idle(review_id)
            return
        await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    ("mode", "in_inbox", "expected"),
    [
        ("inbox", True, "done"),
        ("inbox", False, "none"),
        ("always", False, "done"),
        ("never", True, "none"),
    ],
)
async def test_warmup_rules(
    runner: AgentRunner,
    prs: PrService,
    store: Store,
    fake: FakeAgents,
    fake_gh: FakeGh,
    agent_settings: Settings,
    pr_repo: PrRepo,
    mode: str,
    in_inbox: bool,
    expected: str,
) -> None:
    write_config(agent_settings.home, _config(pr_repo.clone, warmup=mode))
    if in_inbox:
        inbox_rule(fake_gh, "direct", gql_page([gql_pr(PR_NUMBER, repo=REPO)]))

    review_id = await _open(prs)
    await asyncio.sleep(0.05)
    await _await_warmup(runner, review_id)
    await _open(prs)
    await asyncio.sleep(0.05)
    await runner.wait_idle(review_id)

    status = runner.status(review_id)
    assert status["warmup"]["status"] == expected  # type: ignore[index]
    overviews = [t for t in store.list_threads(review_id) if t.created_by == "agent"]
    if expected == "done":
        [overview] = overviews
        assert (overview.kind, overview.path, overview.line, overview.status) == (
            "question",
            "",
            0,
            "submitted",
        )
        assert status["warmup"]["thread_id"] == overview.id  # type: ignore[index]
        assert [m.body for m in store.list_messages(overview.id)] == ["answer to warm-up"]
        assert "Summarize the change, the risks" in fake.sessions[0].prompts[0]
        assert len(fake.sessions[0].prompts) == 1
    else:
        assert overviews == []
        assert fake.sessions == []


class Served(NamedTuple):
    app: FastAPI
    api: httpx.AsyncClient


@contextlib.asynccontextmanager
async def _serve(
    settings: Settings, store: Store, fake_gh: FakeGh, fake: FakeAgents
) -> AsyncIterator[Served]:
    built = create_app(
        settings,
        store,
        github=GitHubClient(fake_gh),
        worktrees=WorktreeManager(settings.home, credential_helper=None),
        agent_sessions=fake.factory,
    )
    async with (
        built.router.lifespan_context(built),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=built), base_url=BASE_URL) as api,
    ):
        yield Served(built, api)


async def test_routes_and_payloads(
    agent_settings: Settings, store: Store, fake_gh: FakeGh, fake: FakeAgents
) -> None:
    async with _serve(agent_settings, store, fake_gh, fake) as (app, api):
        review_id = (await api.post("/api/prs/open", json={"ref": _url(1)})).json()["review_id"]
        queue = app.state.prs._hub.subscribe(review_id)
        idle = (await api.get(f"/api/reviews/{review_id}/agent")).json()

        created = await api.post(
            f"/api/reviews/{review_id}/threads",
            json={"kind": "question", "path": "", "line": 0, "body": "what does this PR do?"},
        )
        await app.state.agents.wait_idle(review_id)
        thread = created.json()
        reply = await api.post(f"/api/threads/{thread['id']}/reply", json={"message": "and risks?"})
        await app.state.agents.wait_idle(review_id)
        listed = (await api.get(f"/api/reviews/{review_id}/threads")).json()
        status = (await api.get(f"/api/reviews/{review_id}/agent")).json()
        stop = await api.post(f"/api/threads/{thread['id']}/stop")
        warmup = await api.post(f"/api/reviews/{review_id}/agent/warmup")
        await app.state.agents.wait_idle(review_id)
        again = await api.post(f"/api/reviews/{review_id}/agent/warmup")
        line_question = await api.post(
            f"/api/reviews/{review_id}/threads",
            json={
                "kind": "question",
                "path": "new_name.py",
                "side": "additions",
                "line": 5,
                "body": "why?",
            },
        )
        bad_pr_level = await api.post(
            f"/api/reviews/{review_id}/threads",
            json={"kind": "question", "path": "", "line": 3, "side": "additions", "body": "x"},
        )
        await app.state.agents.wait_idle(review_id)

        assert idle == {
            "state": "idle",
            "session_id": None,
            "model": "haiku",
            "cost_usd": 0.0,
            "context_tokens": None,
            "queue": [],
            "running_thread_id": None,
            "warmup": {"status": "none", "thread_id": None},
        }
        assert created.status_code == 200
        assert (
            thread["kind"],
            thread["status"],
            thread["path"],
            thread["line"],
            thread["created_by"],
        ) == (
            "question",
            "submitted",
            "",
            0,
            "user",
        )
        assert reply.status_code == 200
        [listed_thread] = [t for t in listed if t["id"] == thread["id"]]
        assert [m["body"] for m in listed_thread["messages"]] == [
            "what does this PR do?",
            "answer to what does this PR do?",
            "and risks?",
            "answer to and risks?",
        ]
        assert (
            status["state"] == "idle"
            and status["session_id"]
            and status["cost_usd"] == pytest.approx(0.02)
        )
        assert stop.status_code == 409
        assert warmup.status_code == 200 and warmup.json()["warmup"]["status"] in (
            "running",
            "done",
        )
        assert again.status_code == 409
        assert line_question.status_code == 200
        assert bad_pr_level.status_code == 400
        events = _drain_events(queue)
        statuses = [e for e in events if e["type"] == "agent_status"]
        final = {k: v for k, v in statuses[-1].items() if k not in ("type", "review_id")}
        assert final == (await api.get(f"/api/reviews/{review_id}/agent")).json()
        updated = [e for e in events if e["type"] == "thread_updated"]
        assert updated and updated[0]["thread"]["id"] == thread["id"]
        assert [m["body"] for m in updated[0]["thread"]["messages"]][-1] == "and risks?"


async def test_agent_off(
    settings: Settings, store: Store, fake_gh: FakeGh, fake: FakeAgents, pr_repo: PrRepo
) -> None:
    write_config(settings.home, _config(pr_repo.clone, warmup="always", enabled=False))
    async with _serve(replace(settings, agent_enabled=True), store, fake_gh, fake) as (_, api):
        review_id = (await api.post("/api/prs/open", json={"ref": _url(1)})).json()["review_id"]
        await asyncio.sleep(0.05)
        status = (await api.get(f"/api/reviews/{review_id}/agent")).json()
        question = await api.post(
            f"/api/reviews/{review_id}/threads",
            json={"kind": "question", "path": "", "line": 0, "body": "hello?"},
        )
        warmup = await api.post(f"/api/reviews/{review_id}/agent/warmup")

        assert status["state"] == "off"
        assert (question.status_code, warmup.status_code) == (409, 409)
        assert store.list_threads(review_id) == []
        assert fake.sessions == []


def test_turn_end_is_frozen() -> None:
    end = TurnEnd(None, None, None, False, "success", False)
    with pytest.raises(AttributeError):
        end.is_error = True  # type: ignore[misc]


def test_agent_config(tmp_path: Path) -> None:
    assert load_repo_config(tmp_path).agent == AgentConfig(
        enabled=True,
        model="claude-opus-5-5",
        idle_minutes=30.0,
        max_live_clients=3,
        max_turns=30,
        max_client_budget_usd=10.0,
        question_timeout_minutes=5.0,
        warmup="inbox",
    )
    write_config(
        tmp_path,
        '[agent]\nenabled = false\nmodel = "haiku"\nidle_minutes = 2\nmax_live_clients = 1\n'
        "max_turns = 4\nmax_client_budget_usd = 0.5\nquestion_timeout_minutes = 1\n"
        'warmup = "never"\n',
    )
    assert load_repo_config(tmp_path).agent == AgentConfig(
        enabled=False,
        model="haiku",
        idle_minutes=2.0,
        max_live_clients=1,
        max_turns=4,
        max_client_budget_usd=0.5,
        question_timeout_minutes=1.0,
        warmup="never",
    )


@pytest.mark.parametrize(
    "body",
    [
        "agent = 1",
        "[agent]\nenabled = 'yes'",
        "[agent]\nmodel = ''",
        "[agent]\nwarmup = 'sometimes'",
        "[agent]\nmax_live_clients = 0",
        "[agent]\nmax_turns = 2.5",
        "[agent]\nmax_client_budget_usd = -1",
        "[agent]\nidle_minutes = true",
    ],
)
def test_agent_config_errors(tmp_path: Path, body: str) -> None:
    write_config(tmp_path, body)
    with pytest.raises(ConfigError):
        load_repo_config(tmp_path)
