import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from code_review_mcp.errors import ReviewError

CONFIG_FILENAME = "config.toml"

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ReviewError):
    pass


@dataclass(frozen=True)
class CleanupConfig:
    enabled: bool = True
    interval_minutes: float = 15.0
    idle_days: float = 7.0


AgentWarmup = Literal["inbox", "always", "never"]
AGENT_WARMUPS: tuple[AgentWarmup, ...] = ("inbox", "always", "never")


@dataclass(frozen=True)
class AgentConfig:
    enabled: bool = True
    model: str = "claude-opus-5-5"
    idle_minutes: float = 120.0
    max_live_clients: int = 3
    max_turns: int = 30
    max_client_budget_usd: float = 10.0
    question_timeout_minutes: float = 5.0
    warmup: AgentWarmup = "inbox"


@dataclass(frozen=True)
class RepoConfig:
    default_repo: str | None = None
    repos: Mapping[str, Path] = field(default_factory=dict)
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)

    def clone_path(self, repo: str) -> Path | None:
        """Return the mapped local clone for `repo` (compared case-insensitively), or None."""
        wanted = repo.lower()
        for name, path in self.repos.items():
            if name.lower() == wanted:
                return path
        return None

    @property
    def known_repos(self) -> list[str]:
        """The default repo and every mapped repo, without case-insensitive duplicates."""
        seen: dict[str, str] = {}
        for name in [*([self.default_repo] if self.default_repo else []), *self.repos]:
            seen.setdefault(name.lower(), name)
        return list(seen.values())


def _require_repo_name(value: object, where: str) -> str:
    if not isinstance(value, str) or not REPO_PATTERN.match(value):
        raise ConfigError(f"{where} must be an 'owner/name' string, got {value!r}")
    return value


def _positive_number(table: Mapping[str, object], key: str, default: float, where: str) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ConfigError(f"{key} in {where} must be a positive number, got {value!r}")
    return float(value)


def _load_cleanup(data: Mapping[str, object], path: Path) -> CleanupConfig:
    table = data.get("cleanup", {})
    if not isinstance(table, dict):
        raise ConfigError(f"[cleanup] in {path} must be a table")
    where = f"[cleanup] in {path}"
    enabled = table.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f"enabled in {where} must be true or false, got {enabled!r}")
    return CleanupConfig(
        enabled=enabled,
        interval_minutes=_positive_number(table, "interval_minutes", 15.0, where),
        idle_days=_positive_number(table, "idle_days", 7.0, where),
    )


def _positive_int(table: Mapping[str, object], key: str, default: int, where: str) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{key} in {where} must be a positive integer, got {value!r}")
    return value


def _load_agent(data: Mapping[str, object], path: Path) -> AgentConfig:
    table = data.get("agent", {})
    if not isinstance(table, dict):
        raise ConfigError(f"[agent] in {path} must be a table")
    where = f"[agent] in {path}"
    defaults = AgentConfig()
    enabled = table.get("enabled", defaults.enabled)
    if not isinstance(enabled, bool):
        raise ConfigError(f"enabled in {where} must be true or false, got {enabled!r}")
    model = table.get("model", defaults.model)
    if not isinstance(model, str) or not model.strip():
        raise ConfigError(f"model in {where} must be a model name, got {model!r}")
    warmup = table.get("warmup", defaults.warmup)
    if warmup not in AGENT_WARMUPS:
        raise ConfigError(f"warmup in {where} must be one of {', '.join(AGENT_WARMUPS)}")
    return AgentConfig(
        enabled=enabled,
        model=model.strip(),
        idle_minutes=_positive_number(table, "idle_minutes", defaults.idle_minutes, where),
        max_live_clients=_positive_int(table, "max_live_clients", defaults.max_live_clients, where),
        max_turns=_positive_int(table, "max_turns", defaults.max_turns, where),
        max_client_budget_usd=_positive_number(
            table, "max_client_budget_usd", defaults.max_client_budget_usd, where
        ),
        question_timeout_minutes=_positive_number(
            table, "question_timeout_minutes", defaults.question_timeout_minutes, where
        ),
        warmup=warmup,
    )


def load_repo_config(home: Path) -> RepoConfig:
    """Read `<home>/config.toml`. A missing file gives an empty config.

    Keys: `default_repo = "owner/name"`, a `[repos]` table of `"owner/name" = "<clone path>"`
    (`~` is expanded; paths must be absolute), a `[cleanup]` table with `enabled`,
    `interval_minutes`, and `idle_days`, and an `[agent]` table (see AgentConfig). Other
    keys are ignored.
    Raises ConfigError if the file cannot be read or parsed, or a value is invalid.
    """
    path = home / CONFIG_FILENAME
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return RepoConfig()
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"Cannot read {path}: {e}") from e

    default_repo = data.get("default_repo")
    if default_repo is not None:
        default_repo = _require_repo_name(default_repo, f"default_repo in {path}")

    raw_repos = data.get("repos", {})
    if not isinstance(raw_repos, dict):
        raise ConfigError(f"[repos] in {path} must be a table")
    repos: dict[str, Path] = {}
    for name, clone in raw_repos.items():
        repo = _require_repo_name(name, f"a [repos] key in {path}")
        if not isinstance(clone, str):
            raise ConfigError(f"[repos] {repo!r} in {path} must be a path string")
        clone_path = Path(clone).expanduser()
        if not clone_path.is_absolute():
            raise ConfigError(f"[repos] {repo!r} in {path} must be absolute or start with ~")
        repos[repo] = clone_path
    return RepoConfig(
        default_repo=default_repo,
        repos=repos,
        cleanup=_load_cleanup(data, path),
        agent=_load_agent(data, path),
    )
