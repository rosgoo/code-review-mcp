import asyncio
import contextlib
import logging
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol

from code_review_mcp.agent_prompts import (
    pr_context,
    question_prompt,
    review_delta,
    review_instructions,
    warmup_prompt,
)
from code_review_mcp.config import Settings
from code_review_mcp.errors import ConflictError, NotFoundError, ReviewError
from code_review_mcp.github import GitHubClient
from code_review_mcp.hub import ReviewHub
from code_review_mcp.pr_service import PrService, ReadyPr
from code_review_mcp.repo_config import AgentConfig, ConfigError, RepoConfig
from code_review_mcp.review_threads import AnchorRequest, ThreadService, serialize_thread
from code_review_mcp.store import MessageRow, ReviewRow, Side, Store, ThreadRow, utc_now

logger = logging.getLogger(__name__)

RECYCLE_FRACTION = 0.8
STOP_GRACE_SECONDS = 30.0
REAP_INTERVAL_SECONDS = 60.0
OVERVIEW_PATH = ""

JobKind = Literal["question", "warmup"]
AgentState = Literal["idle", "queued", "running", "error", "off"]
WarmupStatus = Literal["none", "running", "done", "error"]


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class TurnEnd:
    result_text: str | None
    total_cost_usd: float | None
    session_id: str | None
    is_error: bool
    subtype: str
    aborted: bool


AgentEvent = TextDelta | TurnEnd


class AgentSession(Protocol):
    """One live agent client: a conversation that answers one prompt at a time."""

    async def send(self, prompt: str) -> None: ...

    def events(self) -> AsyncIterator[AgentEvent]: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...

    async def context_tokens(self) -> int | None: ...


class DraftOutcome(NamedTuple):
    ok: bool
    text: str


class _Question(NamedTuple):
    body: str
    follow_up: bool


class _Drained(NamedTuple):
    end: TurnEnd
    text: str
    timed_out: bool


DraftHandler = Callable[[Mapping[str, Any]], Awaitable[DraftOutcome]]


@dataclass(frozen=True)
class SessionSpec:
    review_id: str
    cwd: Path
    model: str
    session_id: str
    resume: bool
    max_turns: int
    max_budget_usd: float
    instructions: str
    draft: DraftHandler


SessionFactory = Callable[[SessionSpec], Awaitable[AgentSession]]


class AgentFailure(ReviewError):
    pass


def scrub_agent_env(environ: MutableMapping[str, str]) -> list[str]:
    """Remove the variables that would make an agent client act as a nested Claude Code run.

    Returns the removed names.
    """
    removed = [
        name
        for name in list(environ)
        if name == "CLAUDECODE" or name.startswith("CLAUDE_CODE_") or name == "NODE_EXTRA_CA_CERTS"
    ]
    for name in removed:
        del environ[name]
    return removed


@dataclass
class _Job:
    thread_id: str
    kind: JobKind


@dataclass
class _ReviewAgent:
    review_id: str
    jobs: deque[_Job] = field(default_factory=deque)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    worker: asyncio.Task[None] | None = None
    session: AgentSession | None = None
    session_fresh: bool = False
    session_cost: float = 0.0
    last_used: float = 0.0
    running: _Job | None = None
    stop_requested: bool = False
    error: str | None = None
    context_tokens: int | None = None
    warmup_status: WarmupStatus | None = None
    warmup_thread_id: str | None = None


def _anchor_side(value: object) -> Side | None:
    if value in ("additions", "RIGHT", "right"):
        return "additions"
    if value in ("deletions", "LEFT", "left"):
        return "deletions"
    return None


class AgentRunner:
    """Answers question threads with one agent session per PR review.

    Questions on one review run one at a time, in order; different reviews run in
    parallel. At most `max_live_clients` sessions stay open; the least recently used idle
    one closes first. A session closes after `idle_minutes`, when the review's worktree is
    released or removed, and on shutdown. The next question resumes the stored session.
    """

    def __init__(
        self,
        store: Store,
        hub: ReviewHub,
        settings: Settings,
        prs: PrService,
        threads: ThreadService,
        github: GitHubClient,
        config_loader: Callable[[], RepoConfig],
        session_factory: SessionFactory,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._hub = hub
        self._settings = settings
        self._prs = prs
        self._threads = threads
        self._github = github
        self._config_loader = config_loader
        self._factory = session_factory
        self._clock = clock
        self._agents: dict[str, _ReviewAgent] = {}
        self._capacity = asyncio.Condition()
        self._background: set[asyncio.Task[None]] = set()
        prs.on_pr_opened(self._pr_opened)
        prs.on_worktree_removed(self._worktree_removed)

    def _config(self) -> AgentConfig:
        try:
            return self._config_loader().agent
        except ConfigError as e:
            logger.warning("agent config is invalid, using defaults: %s", e)
            return AgentConfig()

    def enabled(self) -> bool:
        return self._settings.agent_enabled and self._config().enabled

    def _agent(self, review_id: str) -> _ReviewAgent:
        agent = self._agents.get(review_id)
        if agent is None:
            agent = _ReviewAgent(review_id=review_id, last_used=self._clock())
            self._agents[review_id] = agent
        return agent

    def _spawn(self, coro: Awaitable[None]) -> None:
        async def run() -> None:
            await coro

        task = asyncio.create_task(run())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _overview_thread(self, review_id: str) -> ThreadRow | None:
        return next(
            (
                t
                for t in self._store.list_threads(review_id, kind="question")
                if t.path == OVERVIEW_PATH and t.line == 0 and t.created_by == "agent"
            ),
            None,
        )

    def status(self, review_id: str) -> dict[str, object]:
        """The review's agent state, as GET /agent and SSE agent_status carry it."""
        review = self._prs.require_pr_review(review_id)
        agent = self._agents.get(review_id) or _ReviewAgent(review_id=review_id)
        state: AgentState
        if not self.enabled():
            state = "off"
        elif agent.running is not None:
            state = "running"
        elif agent.jobs:
            state = "queued"
        elif agent.error is not None:
            state = "error"
        else:
            state = "idle"
        overview = self._overview_thread(review_id)
        warmup_status: WarmupStatus = agent.warmup_status or ("done" if overview else "none")
        return {
            "state": state,
            "session_id": review.agent_session_id,
            "model": self._config().model,
            "cost_usd": review.agent_cost_usd,
            "context_tokens": agent.context_tokens,
            "queue": [job.thread_id for job in agent.jobs],
            "running_thread_id": agent.running.thread_id if agent.running else None,
            "warmup": {
                "status": warmup_status,
                "thread_id": agent.warmup_thread_id or (overview.id if overview else None),
            },
        }

    def _publish_status(self, review_id: str) -> None:
        self._hub.publish(review_id, "agent_status", self.status(review_id))

    def require_on(self) -> None:
        """Raise ConflictError when the agent is off (config or CODE_REVIEW_MCP_AGENT=0)."""
        if not self.enabled():
            raise ConflictError("The review agent is off")

    def ask(self, thread_id: str) -> None:
        """Queue a turn that answers the question thread's latest user message."""
        self.require_on()
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise NotFoundError(f"Thread {thread_id!r} not found")
        if thread.kind != "question":
            raise ReviewError(f"Thread {thread_id!r} is not a question thread")
        self._prs.require_pr_review(thread.review_id)
        self._enqueue(thread.review_id, _Job(thread_id=thread_id, kind="question"))

    def _enqueue(self, review_id: str, job: _Job) -> None:
        agent = self._agent(review_id)
        agent.jobs.append(job)
        if agent.worker is None or agent.worker.done():
            agent.worker = asyncio.create_task(self._work(agent))
        self._publish_status(review_id)

    async def stop(self, thread_id: str) -> None:
        """Interrupt the running turn of the thread. Raises ConflictError if none runs."""
        thread = self._store.get_thread(thread_id)
        if thread is None:
            raise NotFoundError(f"Thread {thread_id!r} not found")
        agent = self._agents.get(thread.review_id)
        if agent is None or agent.running is None or agent.running.thread_id != thread_id:
            raise ConflictError(f"No agent turn is running for thread {thread_id!r}")
        agent.stop_requested = True
        if agent.session is not None:
            await agent.session.interrupt()

    async def start_warmup(self, review_id: str) -> dict[str, object]:
        """Create the overview thread and queue the warm-up turn.

        Raises ConflictError when the agent is off, or a warm-up exists or is running. A
        failed warm-up can start again.
        """
        self.require_on()
        review = self._prs.require_pr_review(review_id)
        if review.status == "closed":
            raise ReviewError(f"Review {review_id!r} is closed. Open the PR again.")
        agent = self._agent(review_id)
        if agent.warmup_status in ("running",) or any(j.kind == "warmup" for j in agent.jobs):
            raise ConflictError("A warm-up is already running for this review")
        existing = self._overview_thread(review_id)
        if existing is not None:
            if agent.warmup_status != "error":
                raise ConflictError("This review already has an overview")
            self._store.delete_unposted_thread(existing.id)
            self._hub.publish(review_id, "thread_deleted", {"thread_id": existing.id})
        thread = self._store.create_thread(
            review_id=review_id,
            kind="question",
            path=OVERVIEW_PATH,
            side="additions",
            line=0,
            status="submitted",
            author="agent",
            body="",
            anchor_sha=review.head_sha,
        )
        agent.warmup_status = "running"
        agent.warmup_thread_id = thread.id
        self._hub.publish(
            review_id,
            "thread_added",
            {"thread": serialize_thread(thread, self._store.list_messages(thread.id))},
        )
        self._enqueue(review_id, _Job(thread_id=thread.id, kind="warmup"))
        return self.status(review_id)

    def _pr_opened(self, review: ReviewRow, created: bool) -> None:
        if created and self.enabled():
            self._spawn(self._maybe_warm_up(review))

    async def _maybe_warm_up(self, review: ReviewRow) -> None:
        mode = self._config().warmup
        if mode == "never":
            return
        if mode == "inbox":
            try:
                direct = (await self._github.inbox_list("direct")).inbox_list
            except ReviewError as e:
                logger.warning("warm-up skipped for %s#%s: %s", review.repo, review.pr_number, e)
                return
            if not any(
                pr.repo == review.repo and pr.number == review.pr_number for pr in direct.items
            ):
                return
        try:
            await self.start_warmup(review.id)
        except ReviewError as e:
            logger.info("warm-up skipped for %s#%s: %s", review.repo, review.pr_number, e)

    def _worktree_removed(self, review_id: str) -> None:
        if review_id in self._agents:
            self._spawn(self.close_session(review_id, stop_running=True))

    async def close_session(self, review_id: str, *, stop_running: bool = False) -> None:
        """Close the review's live client, if any. With `stop_running`, a running turn is
        interrupted first; otherwise it finishes before the close."""
        agent = self._agents.get(review_id)
        if agent is None:
            return
        if stop_running and agent.running is not None and agent.session is not None:
            agent.stop_requested = True
            with contextlib.suppress(Exception):
                await agent.session.interrupt()
        async with agent.lock:
            await self._close(agent)

    async def _close(self, agent: _ReviewAgent) -> None:
        session, agent.session = agent.session, None
        agent.session_cost = 0.0
        if session is None:
            return
        try:
            await session.close()
        except Exception:  # a client that fails to close must not block the next one
            logger.exception("closing the agent client of review %s failed", agent.review_id)
        async with self._capacity:
            self._capacity.notify_all()

    async def reap_idle(self) -> list[str]:
        """Close every client idle for longer than `idle_minutes`. Returns their review ids."""
        limit = self._config().idle_minutes * 60
        closed: list[str] = []
        for agent in list(self._agents.values()):
            if agent.session is None or agent.lock.locked() or agent.jobs:
                continue
            if self._clock() - agent.last_used >= limit:
                async with agent.lock:
                    await self._close(agent)
                closed.append(agent.review_id)
                logger.info("closed the idle agent client of review %s", agent.review_id)
        return closed

    async def run_reaper(self, interval: float = REAP_INTERVAL_SECONDS) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.reap_idle()

    async def shutdown(self) -> None:
        """Stop every worker and close every client."""
        tasks = [a.worker for a in self._agents.values() if a.worker and not a.worker.done()]
        tasks += list(self._background)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for agent in self._agents.values():
            await self._close(agent)

    async def wait_idle(self, review_id: str) -> None:
        """Wait until the review has no queued or running agent turn."""
        agent = self._agents.get(review_id)
        while agent is not None and agent.worker is not None and not agent.worker.done():
            await asyncio.shield(agent.worker)

    def live_clients(self) -> int:
        return sum(1 for a in self._agents.values() if a.session is not None)

    async def _make_room(self, agent: _ReviewAgent) -> None:
        limit = self._config().max_live_clients
        while True:
            async with self._capacity:
                if self.live_clients() < limit:
                    return
                idle = [
                    a
                    for a in self._agents.values()
                    if a.session is not None and a is not agent and a.running is None
                ]
                if not idle:
                    await self._capacity.wait()
                    continue
                oldest = min(idle, key=lambda a: a.last_used)
            logger.info("closing the agent client of review %s for room", oldest.review_id)
            async with oldest.lock:
                await self._close(oldest)

    async def _session(self, agent: _ReviewAgent, pr: ReadyPr) -> AgentSession:
        if agent.session is not None:
            return agent.session
        await self._make_room(agent)
        config = self._config()
        review = pr.review
        session_id = review.agent_session_id
        resume = session_id is not None and review.agent_last_turn_at is not None
        if session_id is None:
            session_id = str(uuid.uuid4())
            self._store.set_agent_session_id(review.id, session_id)
        assert review.repo is not None and review.pr_number is not None
        spec = SessionSpec(
            review_id=review.id,
            cwd=pr.repo_dir,
            model=config.model,
            session_id=session_id,
            resume=resume,
            max_turns=config.max_turns,
            max_budget_usd=config.max_client_budget_usd,
            instructions=review_instructions(review.repo, review.pr_number),
            draft=lambda args: self._draft(review.id, args),
        )
        agent.session = await self._factory(spec)
        agent.session_fresh = not resume
        agent.session_cost = 0.0
        return agent.session

    async def _draft(self, review_id: str, args: Mapping[str, Any]) -> DraftOutcome:
        path = args.get("path")
        line = args.get("line")
        body = args.get("body")
        start_line = args.get("start_line")
        if not isinstance(path, str) or not isinstance(line, int) or not isinstance(body, str):
            return DraftOutcome(False, "path (string), line (integer), and body are required")
        try:
            thread = await self._threads.create_thread(
                review_id,
                path,
                AnchorRequest(
                    _anchor_side(args.get("side", "additions")),
                    line,
                    start_line if isinstance(start_line, int) and start_line > 0 else None,
                    _anchor_side(args.get("start_side")),
                ),
                body,
                created_by="agent",
            )
        except ReviewError as e:
            return DraftOutcome(False, f"The draft was not saved: {e}")
        return DraftOutcome(
            True,
            f"Draft review comment {thread['id']} saved on {path} line {thread['line']}. The "
            "user reviews it before anything is posted.",
        )

    async def _work(self, agent: _ReviewAgent) -> None:
        while agent.jobs:
            job = agent.jobs[0]
            async with agent.lock:
                agent.jobs.popleft()
                agent.running = job
                agent.stop_requested = False
                self._publish_status(agent.review_id)
                try:
                    await self._run(agent, job)
                    agent.error = None
                except (ReviewError, OSError) as e:
                    agent.error = str(e)
                    logger.warning("agent turn for thread %s failed: %s", job.thread_id, e)
                    self._hub.publish(
                        agent.review_id,
                        "agent_error",
                        {"thread_id": job.thread_id, "error": str(e)},
                    )
                    if job.kind == "warmup":
                        agent.warmup_status = "error"
                finally:
                    agent.running = None
                    agent.last_used = self._clock()
            async with self._capacity:
                self._capacity.notify_all()
            self._publish_status(agent.review_id)
        agent.worker = None

    def _thread_question(self, thread: ThreadRow) -> _Question:
        messages = self._store.list_messages(thread.id)
        user = [m for m in messages if m.author == "user"]
        if not user:
            raise ReviewError(f"Thread {thread.id!r} has no question")
        return _Question(body=user[-1].body, follow_up=len(user) > 1)

    async def _prompt(self, agent: _ReviewAgent, job: _Job, pr: ReadyPr, review: ReviewRow) -> str:
        thread = self._store.get_thread(job.thread_id)
        if thread is None:
            raise ReviewError(f"Thread {job.thread_id!r} was deleted")
        files = await self._prs.changed_files(pr)
        head_changes = None
        if review.agent_head_sha and review.head_sha and review.agent_head_sha != review.head_sha:
            head_changes = await self._prs.files_between(pr, review.agent_head_sha, review.head_sha)
        assert review.head_sha is not None
        delta = review_delta(
            review,
            self._store.list_threads(review.id),
            self._store.messages_for_review(review.id),
            self._store.viewed_since(review.id, review.head_sha, review.agent_last_turn_at),
            exclude_thread_id=thread.id,
            head_changes=head_changes,
        )
        context = pr_context(review, files)
        if job.kind == "warmup":
            return warmup_prompt(review, context, delta)
        question = self._thread_question(thread)
        return question_prompt(
            review,
            thread,
            question.body,
            follow_up=question.follow_up,
            context=context if agent.session_fresh else None,
            delta=delta,
        )

    async def _run(self, agent: _ReviewAgent, job: _Job) -> None:
        config = self._config()
        pr = await self._prs.ready_pr(agent.review_id)
        session = await self._session(agent, pr)
        review = self._prs.require_pr_review(agent.review_id)
        prompt = await self._prompt(agent, job, pr, review)
        await session.send(prompt)
        agent.session_fresh = False
        drained = await self._drain(agent, session, job, config.question_timeout_minutes * 60)
        self._store.record_agent_turn(agent.review_id, at=utc_now(), head_sha=pr.head_sha)
        await self._finish(agent, job, drained, config)

    async def _drain(
        self, agent: _ReviewAgent, session: AgentSession, job: _Job, timeout: float
    ) -> _Drained:
        deltas: list[str] = []
        end: TurnEnd | None = None
        timed_out = asyncio.Event()

        async def watchdog() -> None:
            await asyncio.sleep(timeout)
            timed_out.set()
            await session.interrupt()
            await asyncio.sleep(STOP_GRACE_SECONDS)
            logger.warning("agent turn for %s did not stop; closing its client", job.thread_id)
            await session.close()

        guard = asyncio.create_task(watchdog())
        try:
            async for event in session.events():
                if isinstance(event, TextDelta):
                    deltas.append(event.text)
                    self._hub.publish(
                        agent.review_id,
                        "agent_delta",
                        {"thread_id": job.thread_id, "text": event.text},
                    )
                else:
                    end = event
                    break
        finally:
            guard.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await guard
        partial = "".join(deltas)
        if end is None:
            await self._close(agent)
            raise AgentFailure(
                "The agent client stopped without finishing the answer"
                + (f". Partial answer: {partial[:200]}" if partial else "")
            )
        return _Drained(end=end, text=partial, timed_out=timed_out.is_set())

    async def _finish(
        self, agent: _ReviewAgent, job: _Job, drained: _Drained, config: AgentConfig
    ) -> None:
        end = drained.end
        if end.total_cost_usd is not None:
            spent = max(0.0, end.total_cost_usd - agent.session_cost)
            agent.session_cost = end.total_cost_usd
            self._store.add_agent_cost(agent.review_id, spent)
        stopped = drained.timed_out or agent.stop_requested or end.aborted
        if drained.timed_out:
            note = f"_(stopped after {config.question_timeout_minutes:g} minutes)_"
            body = f"{drained.text}\n\n{note}".strip()
        elif stopped:
            body = f"{drained.text}\n\n_(stopped)_".strip()
        elif end.is_error:
            body = drained.text.strip()
        else:
            body = (end.result_text or drained.text).strip()
        if agent.session is not None:
            with contextlib.suppress(Exception):
                agent.context_tokens = await agent.session.context_tokens()
        if agent.session is not None and (
            end.subtype == "error_max_budget_usd"
            or agent.session_cost >= RECYCLE_FRACTION * config.max_client_budget_usd
        ):
            logger.info("recycling the agent client of review %s", agent.review_id)
            await self._close(agent)
        if body:
            message = self._store_answer(job, body)
            self._hub.publish(
                agent.review_id,
                "agent_message",
                {"thread_id": job.thread_id, "message": _message_json(message)},
            )
        if job.kind == "warmup":
            agent.warmup_status = "error" if end.is_error and not stopped else "done"
        if end.is_error and not stopped:
            raise AgentFailure(f"The agent turn ended with {end.subtype}")

    def _store_answer(self, job: _Job, body: str) -> MessageRow:
        if job.kind == "warmup":
            self._store.update_first_message(job.thread_id, body)
            first = self._store.list_messages(job.thread_id)[0]
            return first
        return self._store.add_message(job.thread_id, author="agent", body=body)


def _message_json(message: MessageRow) -> dict[str, object]:
    return {
        "id": message.id,
        "author": message.author,
        "body": message.body,
        "created_at": message.created_at,
    }
