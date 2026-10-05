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
agent -> open_diff(...)                   ──► browser opens /r/<id>
agent -> wait_for_comments(review_id)     ══► [blocks here]
you    -> leave inline comments, click Submit
agent <- {"status":"submitted", "comments":[...]}
agent -> reply_to_thread(...) / resolve_thread(...)
agent -> (edit code)
agent -> update_diff(review_id, ...)      ──► browser refreshes in place
agent -> wait_for_comments(review_id)     ══► [blocks again for next round]
```

## PR mode

The daemon can open any GitHub PR for review. It talks to GitHub through the `gh` CLI, which must be installed and logged in. The daemon never handles a token. The only write to GitHub is the submit of a review (see below), and only the `submit-review` route does it. No MCP tool writes to GitHub.

Optional config in `<data dir>/config.toml` (read on each request, so no restart is needed):

```toml
default_repo = "Maybern/maybern"           # used for #123, 123, and branch names

[repos]
"Maybern/maybern" = "~/Dev/maybern"        # use this existing clone
```

A repo without a `[repos]` entry gets a blobless clone in `<data dir>/clones/<owner>/<name>`.

Each PR review gets one detached worktree at `<data dir>/worktrees/<owner>-<name>-<number>`, checked out at the PR head. Fetches write only `refs/code-review-mcp/pull/<n>/head` and `.../base`. They do not move branches, tags, `origin/*`, or `FETCH_HEAD`. Every git command runs with `core.hooksPath=/dev/null`, so the clone's hooks do not run. A Maybern worktree takes about 650 MB and 10 s to create.

The daemon frees worktrees on its own. A sweep runs 30 s after startup, then every `interval_minutes`:

- A review of a merged or closed PR is closed: its worktree and refs go, and its threads and viewed state stay. A review with activity in the last 60 minutes is kept.
- A review of an open PR with no activity for `idle_days` releases its worktree and stays open. The next read of the review restores the worktree at the same head, which takes a few seconds.

Activity is an open, a refresh, a PR or file read (REST or `get_review`), a viewed change, or a thread change. Each removal, release, and restore goes to the daemon log. `CODE_REVIEW_MCP_CLEANUP=0` turns the sweep off.

```toml
[cleanup]
enabled = true          # default
interval_minutes = 15   # default
idle_days = 7           # default
```

| Tool | What it does |
|---|---|
| `list_review_requests(refresh=False)` | Your open PRs as `direct` (review requested from you by name), `mine` (you wrote them), and `team` (requested only from a team you are in). Each is `{name, total, fetched_at, refreshing, items}` with up to 100 items, newest first, with CI state, review decision, your own review, and size. Stale-while-revalidate after 60 s |
| `open_pr(ref)` → `{review_id, url}` | `ref`: PR URL, `owner/name#123`, `#123`, commit SHA, or branch. Same PR, same `review_id`. |
| `get_review(review_id)` | Metadata, changed files, worktree path, thread summary |

| Route | Purpose |
|---|---|
| `GET /api/inbox/{direct\|mine\|team}?refresh=false` | One list: `{name, total, fetched_at, refreshing, items}`; each item has its `review_id` if opened. `team` leaves out PRs in `direct` |
| `GET /api/inbox?refresh=false` | All three lists: `{fetched_at, direct, mine, team}` |
| `POST /api/prs/open {ref}` | Open or reopen a PR → `{review_id, url, note?}` |
| `GET /api/reviews/{id}/pr` | PR metadata and changed files (status, old path, line counts, viewed) |
| `GET /api/reviews/{id}/file?path=` | Old content (merge base) and new content (head) of one changed file |
| `POST /api/reviews/{id}/refresh` | Re-read the PR; on a new head, check it out and send SSE `head_moved` |
| `PUT` / `DELETE /api/reviews/{id}/viewed {path}` | Mark or unmark a file viewed at the current head |
| `POST /api/reviews/{id}/close` | Remove the worktree and refs; the review keeps its id |

The inbox comes from GitHub's GraphQL search, one `gh api graphql` request per list and page of 50, with the lists fetched separately. A larger search exceeds GitHub's request time limit. Each list is cached: a cached list returns at once, and when it is older than 60 s one background refresh starts and the response says `refreshing: true`. Only the first read after a daemon start, and `refresh=true`, wait for GitHub. The page loads `direct` and `mine` first and `team` after them.

In the browser, `/` has an Open box (any ref form above), then three lists: requested from you, your PRs, and requested from your teams (collapsed). One set of controls sorts them (updated, created, author, CI, size) and filters them (text, author, CI state, drafts, bots); the browser keeps your choice. A PR whose base branch is another listed PR's head branch nests under it with its place in the stack, such as 3/11. Recent reviews follow. A PR review page shows the PR's refs, CI checks, and review decision; the worktree path, with buttons that copy the path or `cd <path> && claude` (the daemon force-checks-out this worktree when new commits arrive, so edits there are lost); a file tree with a Viewed checkbox per file (a viewed file collapses); and one diff per file, loaded when you scroll near it. Refresh re-reads the PR. When the head moves, a banner offers a reload. Close review removes the worktree. PR pages have no commenting yet.

Code: `github.py` (`gh` calls, ref parsing), `worktrees.py` (git), `pr_service.py`, `pr_web.py` (routes), `repo_config.py` (`config.toml`).

## Review comments and submit

A PR review holds draft review comments until you submit them as one GitHub review.

- A comment is on one line, a range of lines, or the whole file (`line: 0`). A line comment must be inside a diff hunk: GitHub rejects other lines. `GET /api/reviews/{id}/file` lists the commentable ranges per side.
- `additions` is GitHub's `RIGHT` side and `deletions` is `LEFT`.
- When the PR head moves, a draft on a file whose diff did not change follows the new head. Every other draft becomes `stale`. A stale comment must be moved (`PATCH` with a new position) or deleted before submit.
- Submit posts one review on the current head: it creates a pending review with the line comments, adds each file comment, and submits the review as `COMMENT`, `APPROVE`, or `REQUEST_CHANGES`. If a step fails, the pending review is deleted and nothing is marked posted. On your own PR, GitHub allows only `COMMENT`. `COMMENT` and `REQUEST_CHANGES` need a body.

| Route | Purpose |
|---|---|
| `GET /api/reviews/{id}/threads` | Every thread with its messages and GitHub URL |
| `POST /api/reviews/{id}/threads` | Add a draft review comment `{kind: "review_comment", path, side, line, start_line?, start_side?, body}` |
| `PATCH /api/threads/{id}` | Edit a draft's `body`, or move a draft or stale comment (`side`, `line`, `start_line?`, `start_side?`) |
| `DELETE /api/threads/{id}` | Delete a draft or stale thread |
| `POST /api/reviews/{id}/submit-review` | `{event, body}` → `{github_review_id, html_url, posted}`; 403 for an event GitHub does not allow you, 409 with `stale_thread_ids` while a comment is stale |

## HTTP API

| Route | Purpose |
|---|---|
| `GET /api/health` | Liveness, schema version, and `worktrees: {count, released}` |
| `GET /api/reviews` | Reviews, newest first |
| `GET /api/reviews/{id}/view` | Mode, title, diff or files. An `open_diff` review with `working_dir` also returns the diff and, per file, `old_content` rebuilt from it (`null` when the file on disk no longer matches the diff) |
| `GET /api/reviews/{id}/comments` | All comments (any status) |
| `POST /api/reviews/{id}/comments` | Add a draft comment: `path`, `side` (`additions` \| `deletions`), `line`, optional `start_line` / `start_side`, `line_content`, `body` |
| `POST /api/reviews/{id}/submit` | Submit drafts; wakes `wait_for_comments` for that review |
| `POST /api/threads/{id}/reply` | User reply; reopens a resolved thread |
| `DELETE /api/threads/{id}` | Delete a draft thread; `409` once it is submitted |
| `GET /api/events?review={id}` | SSE stream for one review |

SSE event types: `view_updated`, `comment_added`, `thread_deleted`, `comments_submitted`, `reply_added`, `comment_resolved`.

`/` lists the reviews. `/r/<id>` is the review page. An old `/?review=<id>` link redirects to `/r/<id>`.

## Browser UI

The UI is a React app built on [`@pierre/diffs`](https://www.npmjs.com/package/@pierre/diffs).

- **File tree** on the left (toggle with ☰), with a filter box
- **Diff** in the middle, unified or split (remembered per browser). An `open_diff` with `working_dir` shows full files, so you can expand unchanged context
- **Markdown** files from `show_files` render by default; switch to Source to comment on lines
- Click the `+` next to a line number, or drag across line numbers, to comment on one line or a range
- **Comments** button opens a list of every thread; click one to jump to it
- **Overall feedback** box and **Submit** in the bar at the bottom
- **Cmd/Ctrl+Enter** submits a textarea; **Esc** closes a composer or the comments list
- Light and dark themes follow the system setting

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest -q
```

The UI source is in `web/` (Vite + React + TypeScript, pnpm). `pnpm build` writes the bundle to `src/code_review_mcp/static/`. The built files are committed, so the daemon needs no Node at runtime. Rebuild and commit them after a UI change.

```bash
cd web
pnpm install
pnpm exec tsc --noEmit
pnpm exec vitest run
pnpm build      # writes ../src/code_review_mcp/static/
pnpm dev        # dev server; proxies /api and /mcp to $CODE_REVIEW_MCP_DEV_TARGET (default http://127.0.0.1:7790)
```

Key files in `src/code_review_mcp/`:

- `cli.py`: `code-review-mcp serve`
- `web.py`: FastAPI app, REST + SSE routes, MCP mounted at `/mcp`
- `tools.py`: MCP tool definitions and agent instructions
- `service.py`: review rules shared by tools and routes
- `store.py`: SQLite schema, migrations, typed row access
- `hub.py`: in-memory submit wake-ups and SSE fan-out, per review
- `reverse_patch.py`: rebuilds a file's old content from its new content and its diff
- `static/`: the built UI from `web/` (do not edit by hand)
