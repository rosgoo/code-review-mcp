# code-review-mcp

A local daemon that gives Claude (and other MCP-capable agents) an **interactive, GitHub-style code review UI** in the browser. Your agent opens a diff in your browser and blocks until you click **Submit Comments**. Your inline comments come back as structured tool output, and the agent keeps working.

One long-lived process serves the browser UI, a REST + SSE API, and an MCP server over streamable HTTP. Reviews, comments, and replies live in SQLite, so they survive a restart. Several Claude sessions can hold separate reviews at the same time.

## Requirements

- Python **≥ 3.13**
- [`uv`](https://docs.astral.sh/uv/)
- A modern browser
- An MCP client that supports HTTP servers (Claude Code, Cursor, Zed, ...)

## Run the daemon

```bash
git clone https://github.com/rosgoo/code-review-mcp.git
cd code-review-mcp
uv sync
uv run code-review-mcp serve            # http://127.0.0.1:7790, MCP at /mcp
uv run code-review-mcp serve --port 7800
```

The daemon binds to `127.0.0.1` only. It rejects requests whose `Host` header is not `127.0.0.1` or `localhost`. It also rejects a state-changing `/api/` request (any method except GET, HEAD, OPTIONS) whose `Origin` header is not the daemon's own origin, so another web page cannot submit or comment. A request with no `Origin` header (curl, scripts) is allowed.

| Setting | Default | Override |
|---|---|---|
| Port | `7790` | `--port N` or `CODE_REVIEW_MCP_PORT` |
| Host | `127.0.0.1` | `--host` |
| Data dir | `~/.code-review-mcp/` | `CODE_REVIEW_MCP_HOME` |
| Open a browser tab on `open_diff` / `show_files` | on | `CODE_REVIEW_MCP_BROWSER=0` |

State lives in `<data dir>/state.db` (SQLite, WAL mode). The schema version is tracked with `PRAGMA user_version`; the daemon applies pending migrations on startup.

## Configure your MCP client

The daemon must be running before the client starts. An HTTP MCP entry fails to connect if the daemon is down.

Claude Code, in `.mcp.json`:

```json
{
  "mcpServers": {
    "code": { "type": "http", "url": "http://127.0.0.1:7790/mcp" }
  }
}
```

Or: `claude mcp add --transport http --scope user code http://127.0.0.1:7790/mcp`.

The MCP endpoint is stateless (it issues no session id), so a connected client keeps working across a daemon restart. A tool call that is in flight during a restart does not complete.

## Tools

`open_diff` and `show_files` create a new review and return its `review_id`. The other tools take that id. A comment's `id` is its `thread_id`.

| Tool | What it does |
|---|---|
| `open_diff(diff="", diff_file="", title, working_dir="")` → `{review_id, url}` | Review a unified diff. With `working_dir` (absolute repo root), shows full files with changes highlighted. |
| `show_files(paths=[...] \| content=..., title, ...)` → `{review_id, url, file_count}` | Show files with no diff context. |
| `update_diff(review_id, diff="", diff_file="")` | Replace the diff. Comments stay. The open tab refreshes. |
| `wait_for_comments(review_id, timeout_seconds=540)` | Block until the user clicks Submit on that review. Returns `{"status": "submitted", "comments": [...]}` or `{"status": "timeout"}`. |
| `get_comments(review_id)` | Submitted, unresolved comments. |
| `reply_to_thread(thread_id, message)` | Agent reply, shown inline (markdown). |
| `resolve_thread(thread_id)` | Mark a thread handled. A user reply reopens it. |

`reply_to_thread` and `resolve_thread` replace `reply_to_comment` and `mark_comment_resolved`.

The daemon does not run in the agent's working directory. `diff_file`, `content_file`, and `working_dir` must be absolute (`~` is expanded); a relative value returns an error. A relative entry in `paths` shows a "File not found" placeholder.

## The multi-round loop

```
agent -> open_diff(...)                   ──► browser opens /?review=<id>
agent -> wait_for_comments(review_id)     ══► [blocks here]
you    -> leave inline comments, click Submit
agent <- {"status":"submitted", "comments":[...]}
agent -> reply_to_thread(...) / resolve_thread(...)
agent -> (edit code)
agent -> update_diff(review_id, ...)      ──► browser refreshes in place
agent -> wait_for_comments(review_id)     ══► [blocks again for next round]
```

## HTTP API

| Route | Purpose |
|---|---|
| `GET /api/health` | Liveness and schema version |
| `GET /api/reviews` | Reviews, newest first |
| `GET /api/reviews/{id}/view` | Mode, title, diff or files |
| `GET /api/reviews/{id}/comments` | All comments (any status) |
| `POST /api/reviews/{id}/comments` | Add a draft comment |
| `POST /api/reviews/{id}/submit` | Submit drafts; wakes `wait_for_comments` for that review |
| `POST /api/threads/{id}/reply` | User reply; reopens a resolved thread |
| `GET /api/events?review={id}` | SSE stream for one review |

The UI at `/` reads `?review=<id>`. Without it, the UI loads the newest review.

## Browser UI

- **File tree** on the left (toggle via the hamburger icon)
- **Diff** in the middle, unified or split view
- **Comments drawer** on the right, opened via the speech-bubble icon in the header
- Click the `+` next to any line number (or click-and-drag across line numbers) to comment
- **Cmd/Ctrl+Enter** in any textarea submits
- **Esc** closes the comments drawer

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src
uv run pytest -q
```

Key files in `src/code_review_mcp/`:

- `cli.py`: `code-review-mcp serve`
- `web.py`: FastAPI app, REST + SSE routes, MCP mounted at `/mcp`
- `tools.py`: MCP tool definitions and agent instructions
- `service.py`: review rules shared by tools and routes
- `store.py`: SQLite schema, migrations, typed row access
- `hub.py`: in-memory submit wake-ups and SSE fan-out, per review
- `static/`: the browser UI (vanilla JS, no build step)
