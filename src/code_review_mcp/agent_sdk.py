import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    HookMatcher,
    ResultMessage,
    StreamEvent,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from code_review_mcp.agent_gates import DRAFT_TOOL, make_pre_tool_use_gate
from code_review_mcp.agents import (
    AgentEvent,
    AgentFailure,
    DraftHandler,
    SessionSpec,
    TextDelta,
    TurnEnd,
)

logger = logging.getLogger(__name__)

TOOLS = ["Read", "Grep", "Glob", "Bash"]
ALLOWED_TOOLS = [
    "Read",
    "Grep",
    "Glob",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git diff:*)",
    "Bash(git blame:*)",
    DRAFT_TOOL,
]
DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path in the PR, relative to the repo"},
        "line": {
            "type": "integer",
            "description": "Line number on `side`; 0 for a comment on the whole file",
        },
        "side": {
            "type": "string",
            "enum": ["additions", "deletions"],
            "description": "additions = new file at the head; deletions = old file",
        },
        "start_line": {
            "type": "integer",
            "description": "First line of a multi-line comment on the same side",
        },
        "body": {"type": "string", "description": "The comment text, in markdown"},
    },
    "required": ["path", "line", "side", "body"],
}


def draft_server(handler: DraftHandler) -> Any:
    """An in-process MCP server named "review" with one tool, draft_review_comment."""

    @tool(
        "draft_review_comment",
        "Save a draft review comment on a line inside the PR diff (or line 0 for the whole "
        "file). The user edits or deletes drafts before anything is posted to GitHub. Use "
        "it only when the user asks you to draft a comment.",
        DRAFT_SCHEMA,
    )
    async def draft_review_comment(args: dict[str, Any]) -> dict[str, Any]:
        outcome = await handler(args)
        result: dict[str, Any] = {"content": [{"type": "text", "text": outcome.text}]}
        if not outcome.ok:
            result["is_error"] = True
        return result

    return create_sdk_mcp_server(name="review", version="1.0.0", tools=[draft_review_comment])


def session_options(spec: SessionSpec) -> ClaudeAgentOptions:
    """The tested option set: project CLAUDE.md, no repo hooks or MCP servers, read-only
    tools behind the PreToolUse gate, and one in-process draft tool."""
    return ClaudeAgentOptions(
        cwd=str(spec.cwd),
        model=spec.model,
        setting_sources=["project"],
        system_prompt={"type": "preset", "preset": "claude_code", "append": spec.instructions},
        permission_mode="dontAsk",
        tools=TOOLS,
        allowed_tools=ALLOWED_TOOLS,
        settings=json.dumps({"disableAllHooks": True}),
        strict_mcp_config=True,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_gate(spec.cwd)])]},
        mcp_servers={"review": draft_server(spec.draft)},
        include_partial_messages=True,
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        resume=spec.session_id if spec.resume else None,
        session_id=None if spec.resume else spec.session_id,
    )


class SdkAgentSession:
    """An AgentSession backed by one ClaudeSDKClient (one CLI process)."""

    def __init__(self, client: ClaudeSDKClient) -> None:
        self._client = client

    async def send(self, prompt: str) -> None:
        try:
            await self._client.query(prompt)
        except ClaudeSDKError as e:
            raise AgentFailure(f"The agent client rejected the question: {e}") from e

    async def events(self) -> AsyncIterator[AgentEvent]:
        text_blocks: list[str] = []
        try:
            async for message in self._client.receive_response():
                if isinstance(message, StreamEvent):
                    event = message.event
                    delta = event.get("delta") or {}
                    if event.get("type") == "content_block_delta" and delta.get("type") == (
                        "text_delta"
                    ):
                        yield TextDelta(text=str(delta.get("text", "")))
                elif isinstance(message, AssistantMessage):
                    text_blocks += [b.text for b in message.content if isinstance(b, TextBlock)]
                elif isinstance(message, ResultMessage):
                    yield TurnEnd(
                        result_text=message.result,
                        total_cost_usd=message.total_cost_usd,
                        session_id=message.session_id,
                        is_error=message.is_error,
                        subtype=message.subtype,
                        aborted=message.terminal_reason == "aborted_streaming",
                    )
                    return
        except ClaudeSDKError as e:
            raise AgentFailure(f"The agent client failed: {e}") from e

    async def interrupt(self) -> None:
        try:
            await self._client.interrupt()
        except ClaudeSDKError as e:
            logger.warning("interrupting the agent client failed: %s", e)

    async def close(self) -> None:
        await self._client.disconnect()

    async def context_tokens(self) -> int | None:
        usage = await self._client.get_context_usage()
        return int(usage["totalTokens"])


async def open_sdk_session(spec: SessionSpec) -> SdkAgentSession:
    """Start a CLI process for the review and connect to it. Raises AgentFailure."""
    client = ClaudeSDKClient(session_options(spec))
    try:
        await client.connect()
    except ClaudeSDKError as e:
        raise AgentFailure(f"The agent client did not start: {e}") from e
    logger.info(
        "agent client for review %s started (%s session %s)",
        spec.review_id,
        "resumed" if spec.resume else "new",
        spec.session_id,
    )
    return SdkAgentSession(client)
