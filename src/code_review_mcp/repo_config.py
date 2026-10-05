import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from code_review_mcp.errors import ReviewError

CONFIG_FILENAME = "config.toml"

REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ReviewError):
    pass


@dataclass(frozen=True)
class RepoConfig:
    default_repo: str | None = None
    repos: Mapping[str, Path] = field(default_factory=dict)

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


def load_repo_config(home: Path) -> RepoConfig:
    """Read `<home>/config.toml`. A missing file gives an empty config.

    Keys: `default_repo = "owner/name"` and a `[repos]` table of `"owner/name" = "<clone path>"`
    (`~` is expanded; paths must be absolute). Other keys are ignored.
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
    return RepoConfig(default_repo=default_repo, repos=repos)
