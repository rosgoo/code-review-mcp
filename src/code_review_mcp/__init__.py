"""Code Review MCP — interactive code review with GitHub-style diff UI."""

from code_review_mcp.tools import mcp


def main() -> None:
    """stdio entry point for Claude Code MCP integration."""
    mcp.run()
