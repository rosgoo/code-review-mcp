import argparse
import logging
import sys
from collections.abc import Sequence

import uvicorn

from code_review_mcp.config import load_settings
from code_review_mcp.store import Store
from code_review_mcp.web import create_app


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-review-mcp",
        description="Local code review daemon: browser UI plus an MCP server at /mcp.",
    )
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="run the review daemon in the foreground")
    serve.add_argument(
        "--port", type=int, default=None, help="default: $CODE_REVIEW_MCP_PORT or 7790"
    )
    serve.add_argument("--host", default=None, help="default: 127.0.0.1")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "serve":
        parser.print_help(sys.stderr)
        return 2

    try:
        settings = load_settings(port=args.port, host=args.host)
    except ValueError as e:
        parser.error(f"invalid CODE_REVIEW_MCP_PORT: {e}")

    log_handler = logging.StreamHandler()
    log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    app_logger = logging.getLogger("code_review_mcp")
    app_logger.addHandler(log_handler)
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False

    store = Store.open(settings.db_path)
    print(f"code-review-mcp: state in {settings.db_path}", file=sys.stderr)
    print(f"code-review-mcp: MCP endpoint {settings.base_url}/mcp", file=sys.stderr)
    uvicorn.run(
        create_app(settings, store),
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=False,
        timeout_graceful_shutdown=3,
    )
    return 0
