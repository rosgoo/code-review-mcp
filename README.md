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
| `GET /api/inbox/{direct\|mine\|team}?refresh=false` | One list: `{name, total, fetched_at, refreshing, items}`; each item has its `review_id` if opened, and `stack: {number, size, position}` when it is in a GitHub stack. `team` leaves out PRs in `direct` |
| `GET /api/inbox?refresh=false` | All three lists: `{fetched_at, direct, mine, team}` |
| `POST /api/prs/open {ref}` | Open or reopen a PR → `{review_id, url, note?}` |
| `GET /api/reviews/{id}/pr` | PR metadata and changed files (status, old path, line counts, viewed) |
| `GET /api/reviews/{id}/stack` | The PR's stack, or `null`: GitHub's native stack, or else the chain of open PRs whose base is the head of the PR below (`source: "branches"`). `extensions` are open PRs based on a stack PR's head but not in the stack. Cached 60 s |
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
- Submit posts one review on the current head: it creates a pending review with the line comments, adds each file comment, and submits the review as `COMMENT`, `APPROVE`, or `REQUEST_CHANGES`. If a step fails, the pending review is deleted and nothing is marked posted. On your own PR, GitHub allows only `COMMENT`. `REQUEST_CHANGES` needs a body. `COMMENT` needs a body or at least one comment.

| Route | Purpose |
|---|---|
| `GET /api/reviews/{id}/threads` | Every thread with its messages and GitHub URL |
| `POST /api/reviews/{id}/threads` | Add a draft review comment `{kind: "review_comment", path, side, line, start_line?, start_side?, body}` |
| `PATCH /api/threads/{id}` | Edit a draft's `body`, or move a draft or stale comment (`side`, `line`, `start_line?`, `start_side?`) |
| `DELETE /api/threads/{id}` | Delete a draft or stale thread |
| `POST /api/reviews/{id}/submit-review` | `{event, body}` → `{github_review_id, html_url, posted}`; 403 for an event GitHub does not allow you, 409 with `stale_thread_ids` while a comment is stale |

## Ask the review agent

A PR review can ask a built-in Claude agent questions. The daemon runs one Claude Agent SDK session per PR review, so the agent keeps every earlier question, answer, and file it read.

Questions work like a GitHub review:

1. A question on a line, a range, a file (`line: 0`), or the whole PR (`path: ""`, `line: 0`) is staged as a draft. A reply on a question thread is a staged follow-up. Staged items can be edited or deleted, and nothing goes to the agent yet.
2. Send sends every staged item, or the threads you name, as one batch turn. A thread that still waits for an earlier answer stays staged until that answer arrives.
3. In the batch turn the agent answers each item with the tool `answer_question(thread_id, answer)`. Each answer is stored in its thread when the agent gives it, and its text streams while the agent writes it. Text the agent writes outside the tool is logged and dropped, except in a one-item batch with no tool call, where that text is the answer.
4. An item the agent skips or fails goes back to staged, and the thread's `agent_error` says why. A new send clears it.
5. Stop interrupts the running batch and cancels the queued ones. Answered items keep their answers; the other items go back to staged. A daemon stop or restart also puts unanswered items back to staged, with an `agent_error`.

- The agent runs in the review's worktree and is read-only. It can use Read, Grep, and Glob inside the worktree, and only `git log`, `git show`, `git diff`, and `git blame` in Bash. A gate denies every other tool, every path outside the worktree, and any shell operator. It loads the repo's `CLAUDE.md` but no repo hooks and no MCP servers.
- Its only write is `draft_review_comment`, which saves a draft (`created_by: "agent"`) that you edit or delete. Nothing goes to GitHub until you submit.
- Each turn tells the agent what changed since its last turn: review comments, threads, files marked viewed, and a moved head.
- One live client per review, at most `max_live_clients` (the least recently used idle one closes first). A client closes after `idle_minutes`, when the worktree is released or closed, and on shutdown; the next batch resumes the same session.
- With `warmup = "inbox"`, the first open of a PR from your direct review requests writes an overview thread: the change, its risks, and questions for the author.
- `CODE_REVIEW_MCP_AGENT=0` turns the agent off. Staging still works; send and warm-up return 409. Each batch uses your Claude account and costs money; `GET /api/reviews/{id}/agent` shows the total.

```toml
[agent]
enabled = true
model = "claude-opus-5-5"
idle_minutes = 120
max_live_clients = 3
max_turns = 30                 # passed to the CLI as --max-turns
question_timeout_minutes = 5   # per batch turn
max_client_budget_usd = 10     # per client process; the client restarts at 80%
warmup = "inbox"               # inbox | always | never
```

| Route | Purpose |
|---|---|
| `POST /api/reviews/{id}/threads` `{kind: "question", path, side, line, start_line?, start_side?, body}` | Stage a question (thread `draft`, message `staged`); any line, `line: 0` is the file, `path: ""` + `line: 0` the whole PR |
| `PATCH /api/threads/{id} {body}` | Edit a staged question (409 once sent) |
| `DELETE /api/threads/{id}` | Delete a question thread in any status; 409 while its batch is queued or running |
| `POST /api/threads/{id}/reply {message}` | Stage a follow-up → `{id, reopened: false, status: "staged"}` |
| `PATCH /api/messages/{id} {body}` / `DELETE /api/messages/{id}` | Edit or delete a staged message → the thread; 409 once sent, and for the question itself on delete |
| `POST /api/reviews/{id}/agent/send {thread_ids?}` | Send staged items as one batch → `{batch_id, thread_ids}`; 409 when nothing is staged |
| `POST /api/reviews/{id}/agent/stop` | Stop the running turn and cancel queued batches → `{interrupted, cancelled_batch_ids}`; 409 if nothing runs or waits |
| `GET /api/reviews/{id}/agent` | `{state, session_id, model, cost_usd, context_tokens, staged_count, queue, running_thread_id, batch, partial, warmup}` |
| `POST /api/reviews/{id}/agent/warmup` | Write the overview now (409 if one exists or runs) |

`batch` is the running or latest batch: `{id, thread_ids, answered_ids, state}` with `state` one of `queued`, `running`, `done`, `stopped`, `error`. `partial` is `{thread_id, text}`, the text streamed so far for the item being answered, or `null`. A thread carries `agent_error` and each message carries `status` (`staged` or `sent`).

SSE events: `agent_status` (the GET payload), `agent_delta {thread_id, text}`, `agent_message {thread_id, message}`, `agent_error {thread_id, error}`, `thread_added`, `thread_updated {thread}`, `thread_deleted {thread_id, comment_id}`.

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
