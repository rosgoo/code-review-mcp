import logging
import re
import shlex
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from claude_agent_sdk import HookCallback, HookContext, HookInput, HookJSONOutput

logger = logging.getLogger(__name__)

DRAFT_TOOL = "mcp__review__draft_review_comment"
GIT_SUBCOMMANDS = frozenset({"log", "show", "diff", "blame"})
PATH_TOOLS = frozenset({"Read", "Grep", "Glob"})

_SHELL_METACHARACTERS = re.compile(r"[|;&<>$`\n\r]")
_DENIED_LONG_OPTIONS = (
    "--output",
    "--ext-diff",
    "--textconv",
    "--exec",
    "--no-index",
    "--contents",
    "--ignore-revs-file",
    "--git-dir",
    "--work-tree",
    "--open-files-in-pager",
)
_DENIED_SHORT_OPTIONS = ("-c", "-O")
_DENIED_BLAME_SHORT_OPTIONS = ("-S",)
_GLOB_CHARACTERS = frozenset("*?[{")


def bash_denial(command: str) -> str | None:
    """Return why a Bash command is denied, or None if it is an allowed read-only git command.

    Allowed: `git log|show|diff|blame` with arguments that cannot write files, run
    programs, or read outside the repository. Shell metacharacters and newlines are denied.
    """
    if _SHELL_METACHARACTERS.search(command):
        return "shell operators, redirection, substitution, and newlines are not allowed"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "the command could not be parsed"
    if len(tokens) < 2 or tokens[0] != "git" or tokens[1] not in GIT_SUBCOMMANDS:
        return "only git log, git show, git diff, and git blame are allowed"
    for token in tokens[2:]:
        if token.startswith("~"):
            return f"{token!r} refers to a home directory"
        if any(token == opt or token.startswith(f"{opt}=") for opt in _DENIED_LONG_OPTIONS):
            return f"the option {token.split('=')[0]!r} is not allowed"
        short = _DENIED_SHORT_OPTIONS + (
            _DENIED_BLAME_SHORT_OPTIONS if tokens[1] == "blame" else ()
        )
        if any(token.startswith(opt) and not token.startswith("--") for opt in short):
            return f"the option {token[:2]!r} is not allowed"
    return None


def _inside(path: Path, root: Path) -> bool:
    return path.resolve().is_relative_to(root.resolve())


def _resolve(value: str, root: Path) -> Path:
    candidate = Path(value).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def _glob_prefix(pattern: str) -> str:
    parts: list[str] = []
    for part in PurePosixPath(pattern).parts:
        if _GLOB_CHARACTERS & set(part):
            break
        parts.append(part)
    return str(PurePosixPath(*parts)) if parts else ""


def _pattern_denial(pattern: str, base: Path, root: Path, label: str) -> str | None:
    if ".." in PurePosixPath(pattern).parts or pattern.startswith("~"):
        return f"the {label} {pattern!r} leaves the worktree"
    if pattern.startswith("/"):
        prefix = _glob_prefix(pattern)
        if not prefix or not _inside(Path(prefix), root):
            return f"the {label} {pattern!r} is outside the worktree"
    elif not _inside(base, root):
        return f"the {label} {pattern!r} is outside the worktree"
    return None


def path_denial(tool_name: str, tool_input: Mapping[str, Any], root: Path) -> str | None:
    """Return why a Read, Grep, or Glob call is denied, or None if it stays in `root`.

    Paths resolve against `root` with symlinks followed, so a link that points out of the
    worktree is denied.
    """
    if tool_name == "Read":
        file_path = tool_input.get("file_path")
        if not isinstance(file_path, str) or not file_path:
            return "Read needs a file_path"
        if not _inside(_resolve(file_path, root), root):
            return f"{file_path!r} is outside the worktree"
        return None
    base_value = tool_input.get("path") or str(root)
    if not isinstance(base_value, str):
        return f"{tool_name} needs a string path"
    base = _resolve(base_value, root)
    if not _inside(base, root):
        return f"{base_value!r} is outside the worktree"
    if tool_name == "Glob":
        pattern = tool_input.get("pattern")
        if not isinstance(pattern, str):
            return "Glob needs a pattern"
        return _pattern_denial(pattern, base, root, "pattern")
    file_glob = tool_input.get("glob")
    if isinstance(file_glob, str) and file_glob:
        return _pattern_denial(file_glob, base, root, "glob")
    return None


def tool_denial(tool_name: str, tool_input: Mapping[str, Any], root: Path) -> str | None:
    """The review agent's tool policy: None to allow, else the reason to deny.

    Every tool not named here is denied.
    """
    if tool_name == "Bash":
        command = tool_input.get("command")
        return bash_denial(command) if isinstance(command, str) else "Bash needs a command"
    if tool_name in PATH_TOOLS:
        return path_denial(tool_name, tool_input, root)
    if tool_name == DRAFT_TOOL:
        return None
    return f"the review agent cannot use {tool_name}"


def make_pre_tool_use_gate(root: Path) -> HookCallback:
    """A PreToolUse hook that applies tool_denial to every tool call in `root`."""

    async def gate(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        tool_name = str(input_data.get("tool_name", ""))
        raw_input = input_data.get("tool_input")
        tool_input: Mapping[str, Any] = raw_input if isinstance(raw_input, dict) else {}
        reason = tool_denial(tool_name, tool_input, root)
        if reason is None:
            return {}
        logger.info("denied %s %s: %s", tool_name, dict(tool_input), reason)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Read-only review agent: {reason}.",
            }
        }

    return gate
