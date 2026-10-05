import os
from pathlib import Path
from typing import Any

import pytest

from code_review_mcp.agent_gates import (
    ANSWER_TOOL,
    DRAFT_TOOL,
    bash_denial,
    make_pre_tool_use_gate,
    path_denial,
    tool_denial,
)
from code_review_mcp.agents import scrub_agent_env


@pytest.mark.parametrize(
    "command",
    [
        "git log --oneline -5",
        "git log -p -S'needle' -- src/app.py",
        "git show HEAD:src/app.py",
        "git show abc123 --stat",
        "git diff abc123..def456 -- src/app.py",
        "git diff --stat --no-ext-diff --no-textconv main...HEAD",
        "git blame -L 10,20 src/app.py",
        'git log --format="%h %s" -n 3',
    ],
)
def test_bash_gate_allows_read_only_git(command: str) -> None:
    assert bash_denial(command) is None


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        ('python3 -c \'open("/tmp/x","w")\'', "only git log"),
        ("cat ~/.ssh/config", "only git log"),
        ("ls", "only git log"),
        ("git status", "only git log"),
        ("git -C /etc log", "only git log"),
        ("git checkout main", "only git log"),
        ("git log | head", "shell operators"),
        ("git log; rm -rf /", "shell operators"),
        ("git log && touch x", "shell operators"),
        ("git log > out.txt", "shell operators"),
        ("git show $(whoami)", "shell operators"),
        ("git show `whoami`", "shell operators"),
        ("git log\nrm x", "shell operators"),
        ("git diff --output=/tmp/x", "--output"),
        ('git diff "--output=/tmp/x"', "--output"),
        ("git log --ext-diff -p", "--ext-diff"),
        ("git log -p --textconv", "--textconv"),
        ("git log -c", "'-c'"),
        ("git diff --no-index /dev/null /etc/passwd", "--no-index"),
        ("git blame --contents /etc/passwd app.py", "--contents"),
        ("git blame -S /tmp/revs app.py", "'-S'"),
        ("git log -O/tmp/order -p", "'-O'"),
        ("git show HEAD:~/x", None),
        ("git blame ~/.ssh/config", "home directory"),
        ("git log 'unterminated", "could not be parsed"),
        ("git", "only git log"),
    ],
)
def test_bash_gate_denies(command: str, reason: str | None) -> None:
    denial = bash_denial(command)
    if reason is None:
        assert denial is None
    else:
        assert denial is not None and reason in denial


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("x = 1\n")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret\n")
    os.symlink(outside, root / "src" / "link.txt")
    os.symlink(tmp_path, root / "escape")
    return root


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "src/app.py"}),
        ("Read", {"file_path": "{root}/src/app.py"}),
        ("Grep", {"pattern": "x", "path": "src"}),
        ("Grep", {"pattern": "x"}),
        ("Grep", {"pattern": "x", "glob": "**/*.py"}),
        ("Glob", {"pattern": "**/*.py"}),
        ("Glob", {"pattern": "{root}/src/*.py"}),
        ("Glob", {"pattern": "*.py", "path": "{root}/src"}),
    ],
)
def test_path_gate_allows_the_worktree(
    worktree: Path, tool: str, tool_input: dict[str, str]
) -> None:
    filled = {k: v.format(root=worktree) for k, v in tool_input.items()}
    assert path_denial(tool, filled, worktree) is None


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "~/.ssh/config"}),
        ("Read", {"file_path": "/etc/passwd"}),
        ("Read", {"file_path": "../secret.txt"}),
        ("Read", {"file_path": "src/link.txt"}),
        ("Read", {"file_path": "escape/secret.txt"}),
        ("Read", {"file_path": "{home}/.code-review-mcp/state.db"}),
        ("Read", {}),
        ("Grep", {"pattern": "x", "path": "/etc"}),
        ("Grep", {"pattern": "x", "path": "escape"}),
        ("Grep", {"pattern": "x", "glob": "../**"}),
        ("Grep", {"pattern": "x", "glob": "/etc/*"}),
        ("Glob", {"pattern": "/etc/*"}),
        ("Glob", {"pattern": "../*"}),
        ("Glob", {"pattern": "src/**/../../../*"}),
        ("Glob", {"pattern": "~/.ssh/*"}),
        ("Glob", {"pattern": "*", "path": "/Users"}),
        ("Glob", {}),
    ],
)
def test_path_gate_denies_outside(worktree: Path, tool: str, tool_input: dict[str, str]) -> None:
    filled = {k: v.format(home=Path.home()) for k, v in tool_input.items()}
    assert path_denial(tool, filled, worktree) is not None


def test_tool_policy_is_deny_by_default(worktree: Path) -> None:
    assert tool_denial(DRAFT_TOOL, {"path": "a", "line": 1}, worktree) is None
    assert tool_denial(ANSWER_TOOL, {"thread_id": "t", "answer": "a"}, worktree) is None
    assert tool_denial("Bash", {"command": "git log"}, worktree) is None
    for tool in ("Write", "Edit", "NotebookEdit", "WebFetch", "Task", "mcp__other__x"):
        reason = tool_denial(tool, {}, worktree)
        assert reason is not None and tool in reason
    assert tool_denial("Bash", {}, worktree) == "Bash needs a command"


async def test_hook_output(worktree: Path) -> None:
    gate = make_pre_tool_use_gate(worktree)

    def hook_input(tool: str, tool_input: dict[str, Any]) -> Any:
        return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input}

    allowed = await gate(hook_input("Read", {"file_path": "src/app.py"}), "t1", {"signal": None})
    denied = await gate(hook_input("Bash", {"command": "python3 -c 1"}), "t2", {"signal": None})

    assert allowed == {}
    assert denied == {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "Read-only review agent: only git log, git show, "
            "git diff, and git blame are allowed.",
        }
    }


def test_scrub_agent_env() -> None:
    env = {
        "CLAUDECODE": "1",
        "CLAUDE_CODE_ENTRYPOINT": "cli",
        "CLAUDE_CODE_SSE_PORT": "1",
        "NODE_EXTRA_CA_CERTS": ".certs/ca-bundle.pem",
        "CLAUDE_CONFIG_DIR": "/keep",
        "PATH": "/bin",
    }

    removed = scrub_agent_env(env)

    assert sorted(removed) == [
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "NODE_EXTRA_CA_CERTS",
    ]
    assert env == {"CLAUDE_CONFIG_DIR": "/keep", "PATH": "/bin"}
