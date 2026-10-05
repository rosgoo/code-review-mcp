import asyncio
import os
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from code_review_mcp.errors import ReviewError

_STDERR_LIMIT = 2000


class CommandError(ReviewError):
    pass


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace").strip()[:_STDERR_LIMIT]

    def describe_failure(self) -> str:
        detail = self.stderr_text or f"exit status {self.returncode}"
        return f"`{shlex.join(self.args)}` failed: {detail}"


class CommandRunner(Protocol):
    async def __call__(
        self,
        args: Sequence[str],
        *,
        timeout: float,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult: ...


async def run_command(
    args: Sequence[str],
    *,
    timeout: float,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> CommandResult:
    """Run `args` with no stdin and capture stdout and stderr.

    `env` entries are added to the daemon's environment. The process is killed on timeout
    or cancellation. Raises CommandError if the executable is missing or the timeout expires;
    a non-zero exit status is returned, not raised.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env={**os.environ, **env} if env else None,
        )
    except FileNotFoundError as e:
        raise CommandError(f"{args[0]!r} is not installed or not on PATH") from e
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        raise CommandError(f"`{shlex.join(args)}` timed out after {timeout:g}s") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    returncode = await process.wait()
    return CommandResult(args=tuple(args), returncode=returncode, stdout=stdout, stderr=stderr)
