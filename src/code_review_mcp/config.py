import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7790
DEFAULT_HOME = "~/.code-review-mcp"

_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::", ""})


@dataclass(frozen=True)
class Settings:
    home: Path
    host: str
    port: int
    open_browser: bool

    @property
    def db_path(self) -> Path:
        return self.home / "state.db"

    @property
    def base_url(self) -> str:
        url_host = DEFAULT_HOST if self.host in _WILDCARD_HOSTS else self.host
        return f"http://{url_host}:{self.port}"

    def review_url(self, review_id: str) -> str:
        return f"{self.base_url}/r/{review_id}"


def load_settings(port: int | None = None, host: str | None = None) -> Settings:
    """Build settings from explicit arguments, then environment variables, then defaults.

    Environment: CODE_REVIEW_MCP_HOME (data dir), CODE_REVIEW_MCP_PORT,
    CODE_REVIEW_MCP_BROWSER ("0", "false", "no" or "off" disables opening a browser).
    Raises ValueError if CODE_REVIEW_MCP_PORT is not an integer.
    """
    home = Path(os.environ.get("CODE_REVIEW_MCP_HOME") or DEFAULT_HOME).expanduser()
    if port is None:
        port = int(os.environ.get("CODE_REVIEW_MCP_PORT") or DEFAULT_PORT)
    browser_flag = os.environ.get("CODE_REVIEW_MCP_BROWSER", "1").strip().lower()
    return Settings(
        home=home,
        host=host or DEFAULT_HOST,
        port=port,
        open_browser=browser_flag not in _FALSE_VALUES,
    )
