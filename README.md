# code-review-mcp

An MCP server that gives Claude (and other MCP-capable agents) an **interactive, GitHub-style code review UI** in the browser. Your agent writes code, opens a diff in your browser, and blocks until you click **Submit Comments** — your inline comments flow back as structured tool output, and the agent keeps working. No "go check the browser" nudges, no terminal code dumps.

<p align="center">
  <em>Unified diff and annotated-file views · inline commenting · multi-round review · blocks until you submit.</em>
</p>

---

## Why

LLM agents currently write code in a feedback-sparse loop: they dump changes into chat, you read them there, and you paste comments back as text. That works poorly for anything non-trivial. This MCP server gives you a proper review surface:

- Full diff / annotated-file views with syntax highlighting
- Inline comments on any line (or multi-line selection), just like GitHub
- The agent's replies appear inline as threaded responses
- The agent **blocks on `wait_for_comments`** until you click Submit — no polling, no nudging
- Multi-round review: agent fixes, pushes an updated diff, `wait_for_comments` re-arms

## Requirements

- Python **≥ 3.13**
- [`uv`](https://docs.astral.sh/uv/) (recommended) or any Python package manager
- A modern browser
- An MCP client — Claude Code, Claude Desktop, Cursor, Zed, etc.

## Install

### Option A: Run directly from the repo (recommended for now)

Clone and let `uv` manage the environment:

```bash
git clone https://github.com/rosgoo/code-review-mcp.git
cd code-review-mcp
uv sync
```

Then point your MCP client at it (see **Configure your MCP client** below).

### Option B: Install with `uv tool`

```bash
uv tool install git+https://github.com/rosgoo/code-review-mcp.git
```

This installs the `code-review-mcp` command on your PATH. If you change the source later and want the edits to take effect, re-run `uv tool install --reinstall git+https://github.com/rosgoo/code-review-mcp.git`.

## Configure your MCP client

### Claude Code

Add an entry to `.mcp.json` (project-local) or `~/.claude/.mcp.json` (global):

```json
{
  "mcpServers": {
    "code": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/code-review-mcp",
        "code-review-mcp"
      ]
    }
  }
}
```

If you installed with `uv tool`, the entry is simpler:

```json
{
  "mcpServers": {
    "code": {
      "command": "code-review-mcp"
    }
  }
}
```

Restart Claude Code (or reload MCP servers) to pick up the config.

### Claude Desktop / Cursor / Zed

Same shape — drop the `mcpServers.code` block into your client's MCP config file.

## Use it

Once the server is wired up, your agent has these tools. You don't call them — the agent does. You just interact in the browser.

### `open_diff` — review a unified diff

Agent usage:

```
open_diff(diff_file="/tmp/pr.diff", title="Refactor auth middleware", working_dir="/Users/you/proj")
```

A browser tab opens at `http://127.0.0.1:<auto-port>` showing the diff. If `working_dir` is passed, you get the **annotated file view**: full files with changed lines highlighted and unchanged regions collapsed — nicer than a bare hunk-only diff.

### `show_files` — just show files (no diff)

```
show_files(paths=["src/auth.py", "src/session.py"], title="Auth module")
```

Useful when the agent wants you to review code without a change context.

### `wait_for_comments` — block until Submit is clicked

This is the magic. After `open_diff` / `show_files`, the agent calls:

```
wait_for_comments(timeout_seconds=540)
```

That call **hangs** inside the MCP server until you click **Submit Comments** in the browser. As soon as you do, the tool returns:

```json
{"status": "submitted", "comments": [{...}, {...}]}
```

The agent can now address each comment. No polling. No "hey can you look at the browser now" prompts.

If you take longer than the timeout (default 9 min, under typical MCP tool-call limits), it returns `{"status": "timeout"}` and the agent decides whether to call again.

### `reply_to_comment` — agent responds inline

```
reply_to_comment(comment_id="...", message="Good catch — I'll hoist that into a helper.")
```

Shows up as a threaded reply under your original comment in the browser (markdown-rendered).

### `mark_comment_resolved`

When the agent has addressed a comment, it calls this. Resolved threads fade out and won't come back on subsequent `wait_for_comments` rounds.

### `update_diff` — push new changes to the open tab

After fixing things, the agent replaces the diff:

```
update_diff(diff_file="/tmp/pr.diff")
```

The browser auto-refreshes via SSE. Your previous comments stay in place (unlike `open_diff`, which resets the thread).

## The multi-round loop

```
agent -> open_diff(...)               ──► browser opens
agent -> wait_for_comments()          ══► [blocks here]
you    -> leave inline comments, click Submit
agent <- {"status":"submitted", "comments":[...]}
agent -> reply_to_comment(...) / mark_comment_resolved(...)
agent -> (edit code)
agent -> update_diff(...)             ──► browser refreshes in place
agent -> wait_for_comments()          ══► [blocks again for next round]
you    -> more comments, Submit
... repeat ...
```

## Browser UI

- **File tree** on the left (toggle via the hamburger icon)
- **Diff** in the middle — unified or split view
- **Comments drawer** on the right, opened on-demand via the speech-bubble icon in the header (so the diff gets the full horizontal space)
- Click the `+` next to any line number (or click-and-drag across line numbers) to comment
- **Cmd/Ctrl+Enter** in any textarea = submit
- **Esc** closes the comments drawer
- Dark/light theme toggle

## Tips

- **Unblocking the agent manually**: if you close the browser or change your mind, press Esc in Claude Code to interrupt the current `wait_for_comments` tool call. The agent returns control and you can chat.
- **Prefer `diff_file`/`content_file` over inlined content** — the agent should write diffs to `/tmp/*.diff` and pass the path, to avoid bloating tool-call payloads.
- **Server port**: picks a free port automatically on first tool call, prints nothing to stderr by default (to avoid MCP stdio noise). Check `http://127.0.0.1:<port>/` — the agent's tool response includes the URL.

## Development

```bash
git clone https://github.com/rosgoo/code-review-mcp.git
cd code-review-mcp
uv sync --extra dev

uv run ruff check .
uv run mypy src
```

The Python server is in `src/code_review_mcp/`. The browser UI is static HTML/CSS/JS in `src/code_review_mcp/static/` — edit live, refresh the browser to pick up changes (no build step).

Key files:
- `tools.py` — MCP tool definitions (`open_diff`, `wait_for_comments`, etc.)
- `web.py` — FastAPI server serving the UI and REST endpoints
- `state.py` / `models.py` — in-memory review state
- `static/js/main.js` — UI entry point and layout orchestration
- `static/js/diff.js` / `files.js` — diff and file-view renderers
- `static/js/sse.js` — SSE subscriber for live-refresh from `update_diff`

## License

See [`LICENSE`](LICENSE) if present, otherwise all rights reserved by the repo owner.
