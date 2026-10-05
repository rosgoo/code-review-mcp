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
from code_review_mcp.agents import (
    ANSWER_TOOL,
    DAEMON_STOPPED,
    NOT_ANSWERED,
    AgentRunner,
    ToolInputDelta,
)
from code_review_mcp.answer_stream import AnswerStream
from code_review_mcp.config import Settings
from code_review_mcp.errors import ConflictError, NotFoundError, ReviewError
from code_review_mcp.github import GitHubClient
from code_review_mcp.github_reviews import GitHubReviewWriter
from code_review_mcp.hub import ReviewHub
from code_review_mcp.pr_service import PrService
from code_review_mcp.repo_config import AgentConfig, ConfigError, load_repo_config
from code_review_mcp.review_threads import AnchorRequest, ThreadService
from code_review_mcp.store import MessageRow, ReviewRow, Side, Store, ThreadRow, timestamp
from code_review_mcp.web import create_app
from code_review_mcp.worktrees import ChangedFile, WorktreeManager

from .agent_fakes import FakeAgents, FakeSession, answer_all, items_of, long_answer
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


def _stage(
    threads: ThreadService, review_id: str, body: str, *, path: str = "app.py", line: int = 2
) -> str:
    side: Side | None = "additions" if line else None
    thread = threads.create_question(review_id, path, AnchorRequest(side, line, None, None), body)
    return str(thread["id"])


async def _ask(runner: AgentRunner, threads: ThreadService, review_id: str, body: str) -> str:
    thread_id = _stage(threads, review_id, body)
    runner.send(review_id, [thread_id])
    await runner.wait_idle(review_id)
    return thread_id


async def _until(condition: Any) -> None:
    for _ in range(300):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition never held")


def _drain_events(queue: asyncio.Queue[str]) -> list[dict[str, Any]]:
    return [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]


def _bodies(store: Store, thread_id: str) -> list[tuple[str, str, str]]:
    return [(m.author, m.status, m.body) for m in store.list_messages(thread_id)]


def _status(store: Store, thread_id: str) -> str:
    thread = store.get_thread(thread_id)
    assert thread is not None
    return thread.status


def _batch(runner: AgentRunner, review_id: str) -> dict[str, Any]:
    batch = runner.status(review_id)["batch"]
    assert isinstance(batch, dict)
    return batch


async def test_a_staged_question_waits_until_send(
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

    thread_id = _stage(threads, review_id, "why is x two?")
    await asyncio.sleep(0.05)
    sessions_before_send = len(fake.sessions)
    staged = runner.status(review_id)
    sent = runner.send(review_id)
    await runner.wait_idle(review_id)

    assert (sessions_before_send, staged["staged_count"], staged["batch"]) == (0, 1, None)
    assert sent["thread_ids"] == [thread_id]
    assert _bodies(store, thread_id) == [
        ("user", "sent", "why is x two?"),
        ("agent", "sent", "answer to why is x two?"),
    ]
    assert _status(store, thread_id) == "submitted"
    published = _drain_events(events)
    types = [e["type"] for e in published]
    assert types[0] == "thread_added"
    assert "thread_updated" in types
    deltas = "".join(e["text"] for e in published if e["type"] == "agent_delta")
    assert deltas == "answer to why is x two?"
    assert {e["thread_id"] for e in published if e["type"] == "agent_delta"} == {thread_id}
    [message] = [e for e in published if e["type"] == "agent_message"]
    assert message["thread_id"] == thread_id
    assert set(message["message"]) == {"id", "author", "body", "created_at", "status"}
    running = [e for e in published if e["type"] == "agent_status" and e["state"] == "running"]
    assert thread_id in [e["running_thread_id"] for e in running]
    final = runner.status(review_id)
    assert final["batch"] == {
        "id": sent["batch_id"],
        "thread_ids": [thread_id],
        "answered_ids": [thread_id],
        "state": "done",
    }
    assert (final["state"], final["staged_count"], final["running_thread_id"]) == ("idle", 0, None)
    [session] = fake.sessions
    review = store.get_review(review_id)
    assert review is not None
    assert (session.spec.resume, session.spec.session_id) == (False, review.agent_session_id)
    assert session.spec.cwd == Path(review.worktree_path or "")
    assert review.agent_cost_usd == pytest.approx(0.01)
    assert review.agent_head_sha == pr_repo.head_sha
    prompt = session.prompts[0]
    assert f"Pull request {REPO}#1: Make x two" in prompt
    assert f"Item 1: thread {thread_id}, about app.py:2 (additions side)" in prompt
    assert "answer_question(thread_id, answer) once per item" in prompt
    assert prompt.endswith("Question:\nwhy is x two?")


async def test_batch_of_three_in_order_then_follow_ups_out_of_order(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    async def reversed_answers(session: FakeSession, prompt: str) -> None:
        for item in reversed(items_of(prompt)):
            await session.answer(item.thread_id, f"late answer to {item.text}")
        await session.end("done")

    review_id = await _open(prs)
    ids = [
        _stage(threads, review_id, "line question"),
        _stage(threads, review_id, "file question", line=0),
        _stage(threads, review_id, "pr question", path="", line=0),
    ]
    first_send = runner.send(review_id)
    await runner.wait_idle(review_id)
    first_batch = _batch(runner, review_id)
    fake.behavior = reversed_answers
    for n, thread_id in enumerate(ids):
        threads.stage_reply(thread_id, f"more on {n}")
    runner.send(review_id)
    await runner.wait_idle(review_id)

    assert first_send["thread_ids"] == ids
    assert first_batch["answered_ids"] == ids and first_batch["state"] == "done"
    [session] = fake.sessions
    first, second = session.prompts
    assert [i.thread_id for i in items_of(first)] == ids
    assert [i.follow_up for i in items_of(first)] == [False, False, False]
    assert "about file app.py\n" in first and "about the PR\n" in first
    assert [i.follow_up for i in items_of(second)] == [True, True, True]
    assert "Earlier messages in this thread:\n[user] line question\n[agent] answer to" in second
    for n, thread_id in enumerate(ids):
        assert [b for _, _, b in _bodies(store, thread_id)][1:] == [
            f"answer to {['line', 'file', 'pr'][n]} question",
            f"more on {n}",
            f"late answer to more on {n}",
        ]
    assert _batch(runner, review_id)["answered_ids"] == ids


async def test_unanswered_item_goes_back_to_staged(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    hub: ReviewHub,
    fake: FakeAgents,
) -> None:
    async def skip_second(session: FakeSession, prompt: str) -> None:
        first, _second = items_of(prompt)
        await session.answer(first.thread_id, "only the first")
        await session.say("I forgot the second one.")
        await session.end("I forgot the second one.")

    fake.behavior = skip_second
    review_id = await _open(prs)
    answered = _stage(threads, review_id, "one")
    missed = _stage(threads, review_id, "two")
    events = hub.subscribe(review_id)
    runner.send(review_id)
    await runner.wait_idle(review_id)

    assert _bodies(store, answered)[-1] == ("agent", "sent", "only the first")
    assert _bodies(store, missed) == [("user", "staged", "two")]
    assert _status(store, missed) == "draft"
    published = _drain_events(events)
    errors = [e for e in published if e["type"] == "agent_error"]
    assert errors == [
        {"type": "agent_error", "review_id": review_id, "thread_id": missed, "error": NOT_ANSWERED}
    ]
    bodies = [e["message"]["body"] for e in published if e["type"] == "agent_message"]
    assert bodies == ["only the first"]
    status = runner.status(review_id)
    assert (status["staged_count"], _batch(runner, review_id)["state"]) == (1, "done")
    assert _batch(runner, review_id)["answered_ids"] == [answered]
    assert threads.thread_json(missed)["agent_error"] == NOT_ANSWERED
    assert threads.thread_json(answered)["agent_error"] is None

    await threads.update_thread(missed, body="two, edited", anchor=None)
    fake.behavior = answer_all
    resent = runner.send(review_id)
    cleared_on_send = threads.thread_json(missed)["agent_error"]
    await runner.wait_idle(review_id)

    assert resent["thread_ids"] == [missed]
    assert cleared_on_send is None
    assert threads.thread_json(missed)["agent_error"] is None
    assert _bodies(store, missed) == [
        ("user", "sent", "two, edited"),
        ("agent", "sent", "answer to two, edited"),
    ]


async def _wait_for_stop(session: FakeSession, prompt: str) -> None:
    await session.interrupted.wait()
    await session.end(None, is_error=True, subtype="error_during_execution", aborted=True)


async def test_stop_cancels_a_queued_batch_and_guards_deletes(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    hub: ReviewHub,
    fake: FakeAgents,
) -> None:
    fake.behavior = _wait_for_stop
    review_id = await _open(prs)
    running = _stage(threads, review_id, "running")
    runner.send(review_id)
    await _until(lambda: bool(fake.sessions and fake.sessions[0].prompts))
    queued = _stage(threads, review_id, "queued")
    second = runner.send(review_id)
    queued_status = runner.status(review_id)
    with pytest.raises(ConflictError, match="queued or running"):
        runner.delete_question(running)
    with pytest.raises(ConflictError, match="queued or running"):
        runner.delete_question(queued)
    events = hub.subscribe(review_id)
    stopped = await runner.stop(review_id)
    await runner.wait_idle(review_id)
    after_stop = runner.status(review_id)
    runner.delete_question(queued)

    assert (queued_status["state"], queued_status["queue"]) == ("running", [queued])
    assert stopped == {"interrupted": True, "cancelled_batch_ids": [second["batch_id"]]}
    assert fake.sessions[0].interrupts == 1 and len(fake.sessions[0].prompts) == 1
    assert _bodies(store, running) == [("user", "staged", "running")]
    assert _status(store, running) == "draft"
    assert threads.thread_json(running)["agent_error"] is None
    assert after_stop["batch"] == {
        "id": second["batch_id"],
        "thread_ids": [queued],
        "answered_ids": [],
        "state": "stopped",
    }
    assert (after_stop["state"], after_stop["staged_count"], after_stop["queue"]) == (
        "idle",
        2,
        [],
    )
    assert store.get_thread(queued) is None
    published = _drain_events(events)
    assert not any(e["type"] == "agent_error" for e in published)
    [deleted] = [e for e in published if e["type"] == "thread_deleted"]
    assert (deleted["thread_id"], deleted["comment_id"]) == (queued, queued)
    with pytest.raises(ConflictError, match="No agent turn is running"):
        await runner.stop(review_id)


async def test_question_delete_rules(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store
) -> None:
    review_id = await _open(prs)
    answered = await _ask(runner, threads, review_id, "answered")
    staged = _stage(threads, review_id, "staged")
    comment = await threads.create_thread(
        review_id, "app.py", AnchorRequest("additions", 2, None, None), "a review comment"
    )

    runner.delete_question(answered)
    runner.delete_question(staged)
    with pytest.raises(NotFoundError):
        runner.delete_question(staged)
    with pytest.raises(ReviewError, match="not a question"):
        runner.delete_question(str(comment["id"]))

    assert store.list_threads(review_id, kind="question") == []
    assert runner.status(review_id)["staged_count"] == 0


async def test_send_skips_threads_that_wait_for_an_answer(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    gate = asyncio.Event()

    async def gated(session: FakeSession, prompt: str) -> None:
        if len(session.prompts) == 1:
            await gate.wait()
        await answer_all(session, prompt)

    fake.behavior = gated
    review_id = await _open(prs)
    waiting = _stage(threads, review_id, "first")
    runner.send(review_id)
    await _until(lambda: bool(fake.sessions and fake.sessions[0].prompts))
    threads.stage_reply(waiting, "more")
    other = _stage(threads, review_id, "other")
    with pytest.raises(ConflictError, match="waits for an answer"):
        runner.send(review_id, [waiting])
    skipped = runner.send(review_id)
    gate.set()
    await runner.wait_idle(review_id)
    later = runner.send(review_id)
    await runner.wait_idle(review_id)

    assert skipped["thread_ids"] == [other]
    assert later["thread_ids"] == [waiting]
    assert (
        "Earlier messages in this thread:\n[user] first\n[agent] answer to first\nFollow-up:\nmore"
        in (fake.sessions[0].prompts[2])
    )
    assert [b for _, _, b in _bodies(store, waiting)] == [
        "first",
        "answer to first",
        "more",
        "answer to more",
    ]


async def test_partial_answer_in_status(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    release = asyncio.Event()

    async def slow(session: FakeSession, prompt: str) -> None:
        [item] = items_of(prompt)
        raw = json.dumps({"thread_id": item.thread_id, "answer": "half and the rest"})
        cut = raw.index(" and")
        await session.stream.put(ToolInputDelta(ANSWER_TOOL, "0:1", raw[:cut]))
        await release.wait()
        await session.stream.put(ToolInputDelta(ANSWER_TOOL, "0:1", raw[cut:]))
        await asyncio.sleep(0.01)
        await session.spec.answer(json.loads(raw))
        await session.end("done")

    fake.behavior = slow
    review_id = await _open(prs)
    thread_id = _stage(threads, review_id, "q")
    runner.send(review_id)
    await _until(lambda: runner.status(review_id)["partial"] is not None)
    mid = runner.status(review_id)
    release.set()
    await runner.wait_idle(review_id)

    assert mid["partial"] == {"thread_id": thread_id, "text": "half"}
    assert mid["running_thread_id"] == thread_id
    assert runner.status(review_id)["partial"] is None
    assert _bodies(store, thread_id)[-1] == ("agent", "sent", "half and the rest")


async def test_stop_and_delete_the_overview(
    runner: AgentRunner, prs: PrService, store: Store, fake: FakeAgents
) -> None:
    async def endless(session: FakeSession, prompt: str) -> None:
        while not session.interrupted.is_set():
            await session.say("overview ")
            await asyncio.sleep(0.01)
        await session.end(None, is_error=True, subtype="error_during_execution", aborted=True)

    fake.behavior = endless
    review_id = await _open(prs)
    started: Any = await runner.start_warmup(review_id)
    overview = started["warmup"]["thread_id"]
    await _until(lambda: runner.status(review_id)["partial"] is not None)
    with pytest.raises(ConflictError, match="queued or running"):
        runner.delete_question(overview)
    mid: Any = runner.status(review_id)["partial"]
    await runner.stop(review_id)
    await runner.wait_idle(review_id)
    body = store.list_messages(overview)[0].body
    runner.delete_question(overview)
    after = runner.status(review_id)["warmup"]
    fake.behavior = answer_all
    again: Any = await runner.start_warmup(review_id)
    await runner.wait_idle(review_id)

    assert mid["thread_id"] == overview and mid["text"].startswith("overview ")
    assert body.startswith("overview ") and body.endswith("_(stopped)_")
    assert after == {"status": "none", "thread_id": None}
    assert again["warmup"]["status"] == "running"
    assert runner.status(review_id)["warmup"] == {
        "status": "done",
        "thread_id": again["warmup"]["thread_id"],
    }


async def test_shutdown_and_restart_stage_unanswered_items_again(
    store: Store,
    hub: ReviewHub,
    agent_settings: Settings,
    prs: PrService,
    threads: ThreadService,
    fake_gh: FakeGh,
    clock: Clock,
) -> None:
    review_id = await _open(prs)
    blocked = FakeAgents(behavior=_wait_for_stop)
    first = _runner(store, hub, agent_settings, prs, threads, fake_gh, blocked, clock)
    running = _stage(threads, review_id, "running")
    first.send(review_id)
    await _until(lambda: bool(blocked.sessions and blocked.sessions[0].prompts))
    queued = _stage(threads, review_id, "queued")
    first.send(review_id)
    await first.shutdown()
    crashed = _stage(threads, review_id, "crashed")
    store.mark_question_sent(crashed, [store.list_messages(crashed)[0].id])
    follow_up = _stage(threads, review_id, "answered")
    store.mark_question_sent(follow_up, [store.list_messages(follow_up)[0].id])
    store.add_message(follow_up, author="agent", body="an answer")
    lost = store.add_message(follow_up, author="user", body="lost follow-up")
    second = _runner(store, hub, agent_settings, prs, threads, fake_gh, FakeAgents(), clock)
    await second.shutdown()

    assert blocked.sessions[0].closed
    for thread_id, body in ((running, "running"), (queued, "queued"), (crashed, "crashed")):
        assert _bodies(store, thread_id) == [("user", "staged", body)]
        assert _status(store, thread_id) == "draft"
        assert threads.thread_json(thread_id)["agent_error"] == DAEMON_STOPPED
    assert _bodies(store, follow_up) == [
        ("user", "sent", "answered"),
        ("agent", "sent", "an answer"),
        ("user", "staged", "lost follow-up"),
    ]
    assert _status(store, follow_up) == "submitted"
    assert store.get_message(lost.id) is not None
    assert store.unanswered_questions() == {}


async def test_single_item_text_answer_is_kept(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    async def text_only(session: FakeSession, prompt: str) -> None:
        await session.say("plain ", "text")
        await session.end("plain text")

    fake.behavior = text_only
    review_id = await _open(prs)
    thread_id = await _ask(runner, threads, review_id, "what?")

    assert _bodies(store, thread_id)[-1] == ("agent", "sent", "plain text")
    assert runner.status(review_id)["staged_count"] == 0


async def test_answer_question_rejects_duplicates_and_strangers(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    async def misbehave(session: FakeSession, prompt: str) -> None:
        [item] = items_of(prompt)
        await session.answer(item.thread_id, "first")
        await session.answer(item.thread_id, "again")
        await session.answer("not-a-thread", "who?")
        session.tool_results.append(await session.spec.answer({"thread_id": item.thread_id}))
        await session.end("done")

    fake.behavior = misbehave
    review_id = await _open(prs)
    thread_id = await _ask(runner, threads, review_id, "q")
    outside = await fake.sessions[0].spec.answer({"thread_id": thread_id, "answer": "late"})

    good, duplicate, stranger, missing = fake.sessions[0].tool_results
    assert good.ok and "Every item is answered" in good.text
    assert not duplicate.ok and "already has an answer" in duplicate.text
    assert not stranger.ok and "not in this batch" in stranger.text
    assert not missing.ok and "required" in missing.text
    assert not outside.ok and "No questions are waiting" in outside.text
    assert [b for a, _, b in _bodies(store, thread_id) if a == "agent"] == ["first"]


async def test_stop_mid_batch_keeps_answers_and_restages_the_rest(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    hub: ReviewHub,
    fake: FakeAgents,
) -> None:
    fake.behavior = long_answer
    review_id = await _open(prs)
    first = _stage(threads, review_id, "first")
    second = _stage(threads, review_id, "second")
    runner.send(review_id)
    await _until(lambda: store.list_messages(first)[-1].author == "agent")
    events = hub.subscribe(review_id)
    await runner.stop(review_id)
    await runner.wait_idle(review_id)
    stopped = _batch(runner, review_id)
    second_after_stop = _bodies(store, second)
    fake.behavior = answer_all
    runner.send(review_id)
    await runner.wait_idle(review_id)

    assert fake.sessions[0].interrupts == 1
    assert (stopped["state"], stopped["answered_ids"]) == ("stopped", [first])
    assert second_after_stop == [("user", "staged", "second")]
    assert _bodies(store, first)[-1] == ("agent", "sent", "answer to first")
    assert _bodies(store, second) == [
        ("user", "sent", "second"),
        ("agent", "sent", "answer to second"),
    ]
    published = _drain_events(events)
    assert not any(e["type"] == "agent_error" for e in published)
    assert not any("tail-after-interrupt" in json.dumps(e) for e in published)
    assert _batch(runner, review_id)["state"] == "done"
    with pytest.raises(ConflictError, match="No agent turn is running"):
        await runner.stop(review_id)


async def test_timeout_restages_with_an_error(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    hub: ReviewHub,
    fake: FakeAgents,
    agent_settings: Settings,
    pr_repo: PrRepo,
) -> None:
    write_config(agent_settings.home, _config(pr_repo.clone, question_timeout_minutes=0.002))
    fake.behavior = long_answer
    review_id = await _open(prs)
    first = _stage(threads, review_id, "first")
    second = _stage(threads, review_id, "second")
    events = hub.subscribe(review_id)
    runner.send(review_id)
    await runner.wait_idle(review_id)

    assert _bodies(store, first)[-1][0] == "agent"
    assert _status(store, second) == "draft"
    [error] = [e for e in _drain_events(events) if e["type"] == "agent_error"]
    assert error["thread_id"] == second
    assert "stopped after 0.002 minutes" in error["error"]
    assert _batch(runner, review_id)["state"] == "error"


async def test_send_rules(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store
) -> None:
    review_id = await _open(prs)
    with pytest.raises(ConflictError, match="Nothing is staged"):
        runner.send(review_id)
    a = _stage(threads, review_id, "a")
    b = _stage(threads, review_id, "b")
    with pytest.raises(NotFoundError):
        runner.send(review_id, ["nope"])

    sent = runner.send(review_id, [b])
    await runner.wait_idle(review_id)

    assert sent["thread_ids"] == [b]
    assert (_status(store, a), _status(store, b)) == ("draft", "submitted")
    with pytest.raises(ConflictError, match="nothing staged"):
        runner.send(review_id, [b])
    assert runner.send(review_id)["thread_ids"] == [a]
    await runner.wait_idle(review_id)
    with pytest.raises(ConflictError, match="Nothing is staged"):
        runner.send(review_id)


async def test_staged_message_rules(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store
) -> None:
    review_id = await _open(prs)
    thread_id = await _ask(runner, threads, review_id, "q")
    staged = threads.stage_reply(thread_id, "follow")
    sent_question = store.list_messages(thread_id)[0]

    edited = threads.update_message(staged.id, " better ")
    with pytest.raises(ConflictError, match="was sent"):
        threads.update_message(sent_question.id, "x")
    with pytest.raises(ConflictError, match="was sent"):
        threads.delete_message(sent_question.id)
    with pytest.raises(NotFoundError):
        threads.delete_message("nope")
    after_delete = threads.delete_message(staged.id)
    draft_id = _stage(threads, review_id, "draft q")
    draft_first = store.list_messages(draft_id)[0]
    with pytest.raises(ConflictError, match="delete the thread"):
        threads.delete_message(draft_first.id)
    renamed = await threads.update_thread(draft_id, body="draft q2", anchor=None)
    store.set_thread_status(draft_id, "submitted")
    still_staged = await threads.update_thread(draft_id, body="draft q3", anchor=None)
    with pytest.raises(ConflictError, match="only a staged question"):
        await threads.update_thread(thread_id, body="x", anchor=None)

    edited_messages: Any = edited["messages"]
    deleted_messages: Any = after_delete["messages"]
    renamed_messages: Any = renamed["messages"]
    assert (edited_messages[-1]["body"], edited_messages[-1]["status"]) == ("better", "staged")
    assert [m["status"] for m in deleted_messages] == ["sent", "sent"]
    assert (renamed_messages[0]["body"], renamed_messages[0]["status"]) == ("draft q2", "staged")
    assert still_staged["messages"][0]["body"] == "draft q3"  # type: ignore[index]
    store.set_thread_status(draft_id, "draft")
    assert store.delete_unposted_thread(draft_id)


async def test_cost_delta_and_recycle(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    costs = iter([3.0, 1.5, 1.0])

    async def priced(session: FakeSession, prompt: str) -> None:
        for item in items_of(prompt):
            await session.answer(item.thread_id, "ok")
        await session.end("done", cost=next(costs))

    fake.behavior = priced
    review_id = await _open(prs)
    for question in ("one", "two", "three"):
        await _ask(runner, threads, review_id, question)

    first, second = fake.sessions
    review = store.get_review(review_id)
    assert review is not None
    assert review.agent_cost_usd == pytest.approx(5.5)
    assert first.closed and len(first.prompts) == 2
    assert (second.spec.resume, second.spec.session_id) == (True, first.spec.session_id)
    assert runner.status(review_id)["context_tokens"] == 1010


async def test_queue_per_review_and_parallel_reviews(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    gate = asyncio.Event()
    started: list[str] = []

    async def gated(session: FakeSession, prompt: str) -> None:
        texts = [i.text for i in items_of(prompt)]
        started.extend(texts)
        if "first" in texts:
            await gate.wait()
        await answer_all(session, prompt)

    fake.behavior = gated
    review_a = await _open(prs, 1)
    review_b = await _open(prs, 2)
    first = _stage(threads, review_a, "first")
    runner.send(review_a)
    await _until(lambda: "first" in started)
    second = _stage(threads, review_a, "second")
    runner.send(review_a)
    _stage(threads, review_b, "other review")
    runner.send(review_b)
    await runner.wait_idle(review_b)

    status_a = runner.status(review_a)
    assert (status_a["state"], status_a["queue"]) == ("running", [second])
    assert _batch(runner, review_a)["thread_ids"] == [first]
    assert started == ["first", "other review"]
    gate.set()
    await runner.wait_idle(review_a)

    assert started == ["first", "other review", "second"]
    assert _bodies(store, second)[-1] == ("agent", "sent", "answer to second")
    assert len(fake.for_review(review_a)) == 1


async def test_idle_close_lru_cap_and_resume(
    runner: AgentRunner, prs: PrService, threads: ThreadService, fake: FakeAgents, clock: Clock
) -> None:
    reviews = [await _open(prs, n) for n in (1, 2, 3)]
    for review_id in reviews[:2]:
        clock.now += 10
        await _ask(runner, threads, review_id, "hi")
    clock.now += 10
    await _ask(runner, threads, reviews[0], "again")
    clock.now += 10
    await _ask(runner, threads, reviews[2], "third review")

    by_review = {s.spec.review_id: s for s in fake.sessions}
    assert by_review[reviews[1]].closed and runner.live_clients() == 2
    clock.now += 61
    assert sorted(await runner.reap_idle()) == sorted([reviews[0], reviews[2]])
    assert runner.live_clients() == 0
    await _ask(runner, threads, reviews[0], "after idle")
    first, resumed = fake.for_review(reviews[0])
    assert (resumed.spec.resume, resumed.spec.session_id) == (True, first.spec.session_id)


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
    await _ask(first, threads, review_id, "remember 7341")
    await first.shutdown()
    after = FakeAgents()
    second = _runner(store, hub, agent_settings, prs, threads, fake_gh, after, clock)
    await _ask(second, threads, review_id, "what number?")
    await second.shutdown()

    [old], [new] = before.sessions, after.sessions
    assert old.closed
    assert (old.spec.resume, new.spec.resume) == (False, True)
    assert new.spec.session_id == old.spec.session_id
    assert "Pull request" in old.prompts[0]
    assert "Pull request" not in new.prompts[0]


async def test_worktree_release_and_close_end_the_client(
    runner: AgentRunner, prs: PrService, threads: ThreadService, store: Store, fake: FakeAgents
) -> None:
    review_id = await _open(prs)
    await _ask(runner, threads, review_id, "one")
    store._conn.execute(
        "UPDATE reviews SET last_activity_at = ? WHERE id = ?",
        (timestamp(datetime.now(UTC) - timedelta(days=30)), review_id),
    )
    store._conn.execute(
        "UPDATE threads SET updated_at = '2000-01-01' WHERE review_id = ?", (review_id,)
    )
    assert await prs.release_if_inactive(review_id, datetime.now(UTC) - timedelta(days=7))
    await _until(lambda: fake.sessions[0].closed)
    assert runner.live_clients() == 0

    await _ask(runner, threads, review_id, "two")
    await prs.close(review_id)
    await _until(lambda: fake.sessions[1].closed)


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
    values: dict[str, Any] = dict.fromkeys(ReviewRow.__dataclass_fields__)
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
    old = "2026-10-05T08:00:00+00:00"
    threads = [
        _thread(id="new"),
        _thread(id="edited", created_at=old),
        _thread(id="posted", status="posted", created_at=old),
        _thread(id="stale", status="stale", line=7, created_at=old),
        _thread(id="old", created_at=old, updated_at="2026-10-05T08:30:00+00:00"),
        _thread(id="resolved", kind="question", status="resolved", path="", line=0, created_at=old),
        _thread(id="question", kind="question", status="submitted", path="lib.py", line=0),
        _thread(id="staged", kind="question", status="draft", path="lib.py", line=0),
        _thread(id="current", kind="question", status="submitted"),
        _thread(id="also-current", kind="question", status="submitted"),
    ]
    messages = {
        t.id: [MessageRow(f"m-{t.id}", t.id, "user", f"body of {t.id}", "x")] for t in threads
    }

    delta = review_delta(
        _review(head_sha="c" * 40),
        threads,
        messages,
        ["app.py", "lib.py"],
        exclude_thread_ids={"current", "also-current"},
        head_changes=[ChangedFile("app.py", "modified", None, 1, 1)],
    )
    quiet = review_delta(
        _review(), [threads[4]], messages, [], exclude_thread_ids=(), head_changes=None
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
    runner: AgentRunner, prs: PrService, threads: ThreadService, fake: FakeAgents
) -> None:
    review_id = await _open(prs)
    await _ask(runner, threads, review_id, "one")
    await threads.create_thread(
        review_id, "app.py", AnchorRequest("additions", 2, None, None), "rename x"
    )
    await prs.set_viewed(review_id, "gone.txt", True)
    _stage(threads, review_id, "staged and not sent")
    await _ask(runner, threads, review_id, "two")

    second_prompt = fake.sessions[0].prompts[1]
    delta, items = second_prompt.split("\n\nAnswer 1 item(s)", 1)
    assert f'New draft review comment by the user on {LOC}: "rename x"' in delta
    assert "Files marked viewed: gone.txt" in delta
    assert "staged and not sent" not in second_prompt
    assert '"two"' not in delta and items.endswith("Question:\ntwo")


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
        draft = session.spec.draft
        outcomes.append(
            await draft({"path": "app.py", "line": 2, "side": "additions", "body": "Name it y."})
        )
        outcomes.append(
            await draft({"path": "new_name.py", "line": 5, "side": "additions", "body": "x"})
        )
        outcomes.append(await draft({"path": "app.py", "body": "no line"}))
        await answer_all(session, prompt)

    fake.behavior = drafting
    review_id = await _open(prs)
    events = hub.subscribe(review_id)
    await _ask(runner, threads, review_id, "draft a comment")

    good, outside, missing = outcomes
    assert good.ok and "saved on app.py line 2" in good.text
    assert not outside.ok and "outside the diff" in outside.text
    assert not missing.ok and "required" in missing.text
    [draft] = store.list_threads(review_id, kind="review_comment")
    assert (draft.created_by, draft.status, draft.line) == ("agent", "draft", 2)
    added = [e for e in _drain_events(events) if e["type"] == "thread_added"]
    assert [e["thread"]["kind"] for e in added] == ["question", "review_comment"]


async def test_error_turn_restages_and_reports(
    runner: AgentRunner,
    prs: PrService,
    threads: ThreadService,
    store: Store,
    hub: ReviewHub,
    fake: FakeAgents,
) -> None:
    async def failing(session: FakeSession, prompt: str) -> None:
        await session.end(None, is_error=True, subtype="error_max_turns")

    fake.behavior = failing
    review_id = await _open(prs)
    events = hub.subscribe(review_id)
    thread_id = await _ask(runner, threads, review_id, "too hard")

    errors = [e for e in _drain_events(events) if e["type"] == "agent_error"]
    assert errors == [
        {
            "type": "agent_error",
            "review_id": review_id,
            "thread_id": thread_id,
            "error": "The agent turn ended with error_max_turns",
        }
    ]
    assert _bodies(store, thread_id) == [("user", "staged", "too hard")]
    assert _status(store, thread_id) == "draft"
    assert (runner.status(review_id)["state"], _batch(runner, review_id)["state"]) == (
        "error",
        "error",
    )


@pytest.mark.parametrize(
    ("chunks", "thread_id", "answer"),
    [
        (['{"thread_id": "t1", "answer": "Hello', ' world"}'], "t1", "Hello world"),
        (
            ['{"thread_id": "t', '1", "ans', 'wer": "a\\', "nb \\u00", 'e9 \\"q\\"', '"}'],
            "t1",
            'a\nb é "q"',
        ),
        (['{"answer": "early', ' text", "thread_id": "t9"}'], "t9", "early text"),
        (['{"thread_id": "t1", "answer": "😀 \\ud83d', '\\ude00 done"}'], "t1", "😀 😀 done"),
        (['{"thread_id": "t1", "answer": "ends"', ', "extra": "x"}'], "t1", "ends"),
    ],
)
def test_answer_stream(chunks: list[str], thread_id: str, answer: str) -> None:
    stream = AnswerStream()
    text = "".join(stream.feed(chunk) for chunk in chunks)

    assert (stream.thread_id, text) == (thread_id, answer)


def test_answer_stream_waits_for_the_thread_id() -> None:
    stream = AnswerStream()

    assert stream.feed('{"answer": "a') == ""
    assert stream.thread_id is None
    assert stream.feed('b", "thread_id": "t2"') == "ab"
    assert stream.feed("}") == ""


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
    for _ in range(50):
        await asyncio.sleep(0.01)
        await runner.wait_idle(review_id)
    await _open(prs)
    await asyncio.sleep(0.05)
    await runner.wait_idle(review_id)

    status = runner.status(review_id)
    warmup: Any = status["warmup"]
    assert warmup["status"] == expected
    assert status["batch"] is None
    overviews = [t for t in store.list_threads(review_id) if t.created_by == "agent"]
    if expected == "done":
        [overview] = overviews
        assert (overview.kind, overview.path, overview.line, overview.status) == (
            "question",
            "",
            0,
            "submitted",
        )
        assert warmup["thread_id"] == overview.id
        assert _bodies(store, overview.id) == [("agent", "sent", "answer to warm-up")]
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
        base = f"/api/reviews/{review_id}"
        empty = await api.get(f"{base}/agent")
        nothing = await api.post(f"{base}/agent/send", json={})

        staged = (
            await api.post(
                f"{base}/threads",
                json={"kind": "question", "path": "", "line": 0, "body": "what does it do?"},
            )
        ).json()
        edited = await api.patch(
            f"/api/threads/{staged['id']}", json={"body": "what does this PR do?"}
        )
        line_q = (
            await api.post(
                f"{base}/threads",
                json={
                    "kind": "question",
                    "path": "new_name.py",
                    "side": "additions",
                    "line": 5,
                    "body": "why?",
                },
            )
        ).json()
        removed = await api.delete(f"/api/threads/{line_q['id']}")
        before_send = (await api.get(f"{base}/agent")).json()
        unknown = await api.post(f"{base}/agent/send", json={"thread_ids": ["nope"]})
        sent = await api.post(f"{base}/agent/send", json={"thread_ids": [staged["id"]]})
        await app.state.agents.wait_idle(review_id)
        reply = await api.post(f"/api/threads/{staged['id']}/reply", json={"message": "and risks?"})
        reply_id = reply.json()["id"]
        patched = await api.patch(f"/api/messages/{reply_id}", json={"body": "and the risks?"})
        sent_reply = await api.post(f"{base}/agent/send")
        await app.state.agents.wait_idle(review_id)
        late_patch = await api.patch(f"/api/messages/{reply_id}", json={"body": "x"})
        late_delete = await api.delete(f"/api/messages/{reply_id}")
        missing_message = await api.delete("/api/messages/nope")
        stop = await api.post(f"{base}/agent/stop")
        listed = (await api.get(f"{base}/threads")).json()
        status = (await api.get(f"{base}/agent")).json()
        bad_pr_level = await api.post(
            f"{base}/threads",
            json={"kind": "question", "path": "", "line": 3, "side": "additions", "body": "x"},
        )
        events = _drain_events(queue)

    assert empty.json() == {
        "state": "idle",
        "session_id": None,
        "model": "haiku",
        "cost_usd": 0.0,
        "context_tokens": None,
        "staged_count": 0,
        "queue": [],
        "running_thread_id": None,
        "batch": None,
        "partial": None,
        "warmup": {"status": "none", "thread_id": None},
    }
    assert nothing.status_code == 409
    assert (staged["kind"], staged["status"], staged["created_by"]) == ("question", "draft", "user")
    assert staged["messages"][0]["status"] == "staged"
    assert edited.json()["messages"][0]["body"] == "what does this PR do?"
    assert removed.status_code == 200
    assert before_send["staged_count"] == 1
    assert unknown.status_code == 404
    assert sent.status_code == 200
    assert sent.json()["thread_ids"] == [staged["id"]] and sent.json()["batch_id"]
    assert reply.status_code == 200 and reply.json()["reopened"] is False
    last = patched.json()["messages"][-1]
    assert (last["id"], last["body"], last["status"]) == (reply_id, "and the risks?", "staged")
    assert sent_reply.json()["thread_ids"] == [staged["id"]]
    assert (late_patch.status_code, late_delete.status_code) == (409, 409)
    assert (missing_message.status_code, stop.status_code) == (404, 409)
    [thread] = [t for t in listed if t["id"] == staged["id"]]
    assert [(m["author"], m["status"], m["body"]) for m in thread["messages"]] == [
        ("user", "sent", "what does this PR do?"),
        ("agent", "sent", "answer to what does this PR do?"),
        ("user", "sent", "and the risks?"),
        ("agent", "sent", "answer to and the risks?"),
    ]
    assert status["batch"]["state"] == "done" and status["staged_count"] == 0
    assert status["batch"]["id"] == sent_reply.json()["batch_id"]
    assert status["cost_usd"] == pytest.approx(0.02)
    assert bad_pr_level.status_code == 400
    statuses = [e for e in events if e["type"] == "agent_status"]
    final = {k: v for k, v in statuses[-1].items() if k not in ("type", "review_id")}
    assert final == status
    updated = [e for e in events if e["type"] == "thread_updated"]
    assert {e["thread"]["id"] for e in updated} == {staged["id"]}
    assert [e["thread_id"] for e in events if e["type"] == "thread_deleted"] == [line_q["id"]]


async def test_agent_off(
    settings: Settings, store: Store, fake_gh: FakeGh, fake: FakeAgents, pr_repo: PrRepo
) -> None:
    write_config(settings.home, _config(pr_repo.clone, warmup="always", enabled=False))
    async with _serve(replace(settings, agent_enabled=True), store, fake_gh, fake) as (_, api):
        review_id = (await api.post("/api/prs/open", json={"ref": _url(1)})).json()["review_id"]
        await asyncio.sleep(0.05)
        base = f"/api/reviews/{review_id}"
        status = (await api.get(f"{base}/agent")).json()
        staged = await api.post(
            f"{base}/threads", json={"kind": "question", "path": "", "line": 0, "body": "hello?"}
        )
        send = await api.post(f"{base}/agent/send")
        warmup = await api.post(f"{base}/agent/warmup")

    assert status["state"] == "off"
    assert staged.status_code == 200
    assert (send.status_code, warmup.status_code) == (409, 409)
    assert fake.sessions == []


def test_agent_config(tmp_path: Path) -> None:
    assert load_repo_config(tmp_path).agent == AgentConfig(
        enabled=True,
        model="claude-opus-5-5",
        idle_minutes=120.0,
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
