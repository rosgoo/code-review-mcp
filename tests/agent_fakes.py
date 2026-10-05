import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from code_review_mcp.agents import AgentEvent, SessionSpec, TextDelta, TurnEnd

Behavior = Callable[["FakeSession", str], Coroutine[Any, Any, None]]


def question_of(prompt: str) -> str:
    marker = "Question:\n"
    return prompt.split(marker, 1)[1] if marker in prompt else "warm-up"


class FakeSession:
    """An AgentSession that models one CLI process: a single buffered event stream that
    every turn writes to, so a turn left undrained leaks into the next one."""

    def __init__(self, spec: SessionSpec, owner: "FakeAgents") -> None:
        self.spec = spec
        self.owner = owner
        self.stream: asyncio.Queue[AgentEvent | None] = asyncio.Queue()
        self.prompts: list[str] = []
        self.cost = 0.0
        self.closed = False
        self.interrupts = 0
        self.interrupted = asyncio.Event()
        self.turn: asyncio.Task[None] | None = None

    async def send(self, prompt: str) -> None:
        self.prompts.append(prompt)
        self.interrupted = asyncio.Event()
        self.turn = asyncio.create_task(self.owner.behavior(self, prompt))

    async def events(self) -> AsyncIterator[AgentEvent]:
        while True:
            event = await self.stream.get()
            if event is None:
                return
            yield event
            if isinstance(event, TurnEnd):
                return

    async def interrupt(self) -> None:
        self.interrupts += 1
        self.interrupted.set()

    async def close(self) -> None:
        self.closed = True
        if self.turn is not None and not self.turn.done():
            self.turn.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.turn
        await self.stream.put(None)

    async def context_tokens(self) -> int | None:
        return 1000 + 10 * len(self.prompts)

    async def say(self, *chunks: str) -> None:
        for chunk in chunks:
            await self.stream.put(TextDelta(chunk))

    async def end(
        self,
        text: str | None,
        *,
        cost: float = 0.01,
        is_error: bool = False,
        subtype: str = "success",
        aborted: bool = False,
    ) -> None:
        self.cost += cost
        await self.stream.put(
            TurnEnd(
                result_text=text,
                total_cost_usd=self.cost,
                session_id=self.spec.session_id,
                is_error=is_error,
                subtype=subtype,
                aborted=aborted,
            )
        )


async def echo_answer(session: FakeSession, prompt: str) -> None:
    question = question_of(prompt).strip()
    await session.say("answer to ", question)
    await session.end(f"answer to {question}")


async def long_answer(session: FakeSession, prompt: str) -> None:
    """Stream until interrupted, then stream a tail and end aborted, like the real CLI."""
    while not session.interrupted.is_set():
        await session.say("word ")
        await asyncio.sleep(0.01)
    await session.say("tail-after-interrupt ")
    await session.end(None, is_error=True, subtype="error_during_execution", aborted=True)


@dataclass
class FakeAgents:
    behavior: Behavior = echo_answer
    sessions: list[FakeSession] = field(default_factory=list)

    async def factory(self, spec: SessionSpec) -> FakeSession:
        session = FakeSession(spec, self)
        self.sessions.append(session)
        return session

    def for_review(self, review_id: str) -> list[FakeSession]:
        return [s for s in self.sessions if s.spec.review_id == review_id]
