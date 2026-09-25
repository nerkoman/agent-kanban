# Connecting AI agents to agent-kanban

Three integration paths exist; pick the one your agent supports:

1. **MCP server (stdio)** — easiest for Claude Code, Cline. Works out of the box.
2. **Built-in REST + auto-generated OpenAPI** — for opencode, Open WebUI, any HTTP-aware agent.
3. **Function-calling LLM** — define OpenAI-compatible tools yourself; the kanban server stays REST-only.

The kanban server is the same in all three cases — the difference is only how the agent discovers and invokes its endpoints.

---

## 1. Claude Code (MCP)

**Setup — one command.** Register the project on the board (UI → `+ New project`, pick its directory), then:

```bash
.venv/bin/python -m kanban_mcp.connect ~/code/myproj --project myproj
```

It writes (or merges into) `~/code/myproj/.mcp.json` and refreshes the "Kanban board" block in that project's `CLAUDE.md` (and `AGENTS.md` if there is one). Other MCP servers in the file are left alone; an existing kanban entry keeps its name. The project wizard in the UI does the same when "Connect Claude Code" is ticked, and `python -m kanban_store.maintenance doctor` lists registered projects that still lack a `.mcp.json`.

**Setup — by hand.** The resulting project-scope `.mcp.json` (or put the same block into `~/.claude.json` for global access — `~/.claude/settings.json` is *not* read for MCP servers):

```jsonc
{
  "mcpServers": {
    "agent-kanban": {
      "type": "stdio",
      "command": "/abs/path/to/agent-kanban/.venv/bin/python",
      "args":    ["-m", "kanban_mcp"],
      "cwd":     "/abs/path/to/agent-kanban",
      "env": {
        "PYTHONPATH": "/abs/path/to/agent-kanban",
        "KANBAN_DB":  "/abs/path/to/agent-kanban/tasks.db",
        "KANBAN_PROJECT_ID": "myproj",
        "KANBAN_ACTOR": "claude"
      }
    }
  }
}
```

**`PYTHONPATH` is required**: Claude Code launches the stdio MCP server while ignoring the `cwd` field — without `PYTHONPATH`, python won't find the `kanban_mcp` module and MCP fails with `Failed to connect`.

`KANBAN_DB` — absolute path to the SQLite file (especially important if Claude Code and the kanban live in different directories).
`KANBAN_PROJECT_ID` — this agent's project: the default for `kanban_create` and the scope of `kanban_list` / `kanban_search` / `kanban_board` / `kanban_my_active` (pass `project_id="*"` to look across projects).
`KANBAN_ACTOR` — author name written into `task_history` for every move/comment the agent makes (default `claude`), so the history distinguishes agent actions from human drag-drops.
`KANBAN_MCP_HUMAN_ONLY` — statuses agents may not move or create cards into (default `done`; `uat,done,cancelled` for a stricter board, `""` to allow everything). The agent gets an error telling it to move the card to `testing` instead.

Restart Claude Code (or run `claude mcp list` to confirm `✓ Connected`). 16 tools become available:

| Tool | Purpose |
|---|---|
| `kanban_columns` | list of columns + ownership semantics |
| `kanban_projects` | projects with task counts per column |
| `kanban_board` | compact overview of one project (counts + first cards per column) |
| `kanban_list` | tasks with optional status / assignee / project filters |
| `kanban_search` | substring search in title/description |
| `kanban_my_active` | this agent's cards in analyst / in_progress / testing |
| `kanban_get` | card with links, blockers and the newest 30 history rows (`history_total` = all) |
| `kanban_history` | older history, page by page |
| `kanban_pull` | atomically claim an `approved` task → `analyst`, assignee = current agent |
| `kanban_move` | move card to a new column; `expected_from` refuses if someone moved it meanwhile |
| `kanban_assign` | set or clear the assignee (hand the card to another agent or lane) |
| `kanban_comment` | append comment to history |
| `kanban_create` | new card (unknown `project_id` is an error, not a silent default) |
| `kanban_update` | edit title/priority/size/description/acceptance/external blocker (`""` clears it) |
| `kanban_link` | attach memory/file/pr/url/plan link |
| `kanban_blockers` | set/replace inter-task blockers (unknown ids and cycles are rejected) |

Mutating tools answer with a short card (`id`, `status`, `url`, …), not the whole history. Arguments are strict: `project=` instead of `project_id=` is an error that names the right parameter, not a silently dropped value. Status names are forgiving: `"In progress"`, `"Testing"` and `"в работе"` all work.

Changes made over MCP reach automation rules and webhooks the same way UI drag-drops do: every change lands in `task_history`, and the web server's event feed dispatches it within about a second (the web server has to be running for that part).

**Example prompt for the agent:**

> "List my pending tasks for project `myproj`, then claim the highest-priority approved one."

The agent will call `kanban_list(status="approved", project_id="myproj")` then `kanban_pull(task_id="T-007")`.

---

## 2. Cline (MCP)

Cline (VSCode extension, formerly Claude Dev) speaks the same MCP protocol.

**Setup.** Open VSCode → Cline panel → Settings (gear icon) → MCP Servers → Add → paste the same JSON block as for Claude Code (with absolute paths). The extension reloads automatically.

Ask Cline: *"What's in my kanban backlog?"* — it should call `kanban_list(status="backlog")`.

---

## HTTP MCP transports (`/mcp` and `/sse`)

In addition to the stdio server in `kanban_mcp/`, the web server exposes the REST routes as MCP tools, mounted by `fastapi_mcp` on the same FastAPI app (same data, no extra process):

- `http://localhost:7777/mcp` — **streamable HTTP** (the current MCP transport);
- `http://localhost:7777/sse` — legacy **SSE**, for clients that only speak that. (v0.1.x served SSE at `/mcp`; point such clients at `/sse` now.)

Use this path when your MCP client doesn't speak stdio (Cursor, recent Cline) or for ad-hoc debugging (MCP Inspector). The tools here are the REST operations (names like `move_task_api_tasks__task_id__move_post`). Callers are treated as agents: `KANBAN_MCP_HUMAN_ONLY` applies (no closing cards by default), changes are recorded as `agent:mcp-http` unless the client sends `X-Kanban-Actor`, and endpoints that act on the host machine — native file pickers, the Claude CLI login, writing `.mcp.json`/`PLAN.md` into project folders, creating or repointing projects — and the automation pause switch are not exposed.

Quick smoke check that the endpoint is live:

```bash
curl -s -X POST http://localhost:7777/mcp \
  -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'
# → {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-03-26",...}}
```

### Cursor (HTTP MCP) <a id="cursor-http-mcp"></a>

[Cursor](https://cursor.com) speaks HTTP MCP natively.

**Setup.** Cursor → Settings → MCP → Add new server, or edit `~/.cursor/mcp.json` (global) / `.cursor/mcp.json` (per project):

```jsonc
{
  "mcpServers": {
    "agent-kanban": {
      "url": "http://localhost:7777/mcp"
    }
  }
}
```

Restart Cursor; in chat ask: *"What's in my kanban backlog for project myproj?"*. Cursor will call the kanban tools via the HTTP transport and respond with live state.

### Cline (HTTP) <a id="cline-http-mcp"></a>

Recent Cline versions (≥ 3.x) support HTTP MCP servers alongside stdio.

**Setup.** VSCode → Cline panel → ⚙ Settings → MCP Servers → "Add HTTP server" → URL `http://localhost:7777/mcp`. Reload Cline.

If you've been using the old stdio Cline config (section 2 above), you can keep both — Cline merges tool listings from all servers, but the kanban will appear twice. Pick one transport per Cline instance to avoid duplicate tools in the picker.

### MCP Inspector <a id="mcp-inspector"></a>

[modelcontextprotocol/inspector](https://github.com/modelcontextprotocol/inspector) is the canonical debugging tool for MCP servers — a browser-based UI that lets you call tools by hand, inspect schemas, and watch the SSE stream live.

```bash
npx @modelcontextprotocol/inspector http://localhost:7777/mcp
# opens http://localhost:5173 in your browser
```

In the Inspector UI:

1. **Transport:** pick "Streamable HTTP" for `/mcp` (or "SSE" with `http://localhost:7777/sse`).
2. **Tools tab:** lists all kanban operations exposed via the HTTP endpoint — click any to invoke with form inputs.
3. **Network tab:** every request/response in raw JSON-RPC, useful when something feels off.

This is the fastest way to verify a fresh install before wiring up a real client.

---

## 3. opencode (REST + OpenAPI)

[opencode](https://opencode.ai) (sst/opencode) doesn't speak MCP, but supports model-native function calling. Two integration approaches:

### 3a. Per-tool curl shells (simple)

In `opencode.toml`:

```toml
[[tools]]
name = "kanban_list"
description = "List tasks in agent-kanban (filter by status)"
command = "curl -s 'http://localhost:7777/api/board?project=default' | jq '.tasks'"

[[tools]]
name = "kanban_create"
description = "Create a kanban task. Args: title, project_id"
command = "curl -s -X POST 'http://localhost:7777/api/tasks' -H 'Content-Type: application/json' -d '{\"title\":\"$title\",\"project_id\":\"$project_id\"}'"
```

### 3b. OpenAPI import (preferred when supported)

Static OpenAPI 3.1 schema is committed at [`docs/openapi.yaml`](openapi.yaml). Point opencode at it (or its live counterpart `http://localhost:7777/openapi.json`). All endpoints become first-class tools without manual TOML.

---

## 4. Open WebUI (OpenAPI Tool Server)

[Open WebUI](https://github.com/open-webui/open-webui) supports two paths:

- **Pipelines** — Python plugin uploaded to `/pipelines`. Heavy.
- **OpenAPI Tool Server** — point WebUI at the kanban's `/openapi.json`. Lightweight, recommended.

**Setup.** Settings → Tools → Add OpenAPI Tool Server → URL: `http://localhost:7777/openapi.json` (or `http://host.docker.internal:7777/openapi.json` if WebUI is in Docker). Save → tools auto-discovered.

If WebUI runs on a different host, enable CORS:

```bash
KANBAN_CORS_ORIGINS=https://your-webui.example python -m kanban_ui
```

---

## 5. Generic function-calling LLM

For Hermes (NousResearch), Llama 3.1, Mistral, Ollama with `tools=`, vLLM with `--enable-auto-tool-choice`, etc. — anything OpenAI-SDK-compatible.

The kanban server is a plain REST service. Define tools in your client and dispatch tool_calls back via HTTP. Full working examples in [`examples/llm-tool-calling/`](../examples/llm-tool-calling/):

- `openai_sdk_demo.py` — OpenAI Python SDK against Ollama / vLLM endpoint.
- `ollama_demo.py` — direct Ollama `/api/chat` with `tools=`.
- `curl_examples.sh` — copy-pasteable for any agent or shell-driven workflow.

Minimal sketch:

```python
from openai import OpenAI
import httpx

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")  # or any OAI-compatible
KANBAN = "http://localhost:7777"

tools = [{
    "type": "function",
    "function": {
        "name": "kanban_list",
        "description": "List tasks in a project",
        "parameters": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}},
            "required": ["project_id"],
        },
    },
}]

def dispatch(name, args):
    if name == "kanban_list":
        return httpx.get(f"{KANBAN}/api/board", params={"project": args["project_id"]}).json()
    raise ValueError(name)

# (then a normal tool-call loop)
```

---

## REST API reference

- **Interactive Swagger UI**: `http://localhost:7777/docs`
- **Static OpenAPI 3.1 spec**: [`docs/openapi.yaml`](openapi.yaml) (regenerate with `make openapi`)
- **Live JSON**: `http://localhost:7777/openapi.json`

Key endpoints:

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/board?project=<id>` | board view (tasks grouped by column; each card carries `status`, `running`); `include_archived=true` |
| GET | `/api/projects` | list of projects |
| POST | `/api/projects` | create project |
| POST | `/api/projects/{id}/connect` | write `.mcp.json` + CLAUDE.md block into the project directory |
| GET | `/api/tasks/{id}` | task with the newest `history_limit` (default 200) history rows and `history_total` |
| GET | `/api/tasks/{id}/history?limit=&before_id=` | history, page by page |
| POST | `/api/tasks` | create task |
| PATCH | `/api/tasks/{id}` | edit fields (`external_blocker: ""` clears it) |
| POST | `/api/tasks/{id}/move` | move; `expected_from` → 409 if the card is no longer there |
| POST | `/api/tasks/{id}/assign` | set/clear assignee |
| POST | `/api/tasks/{id}/archive` | hide from the board / restore (`{"archived": false}`), status kept |
| POST | `/api/tasks/{id}/comment` | comment in history (`text`, alias `comment`) |
| POST | `/api/tasks/{id}/links` | attach link |
| POST | `/api/tasks/{id}/blockers` | replace internal blockers |
| GET | `/api/automation/status` | inbox, rules (+ running/queued commands), event feed, webhooks |
| POST | `/api/automation/pause` | `{"paused": true}` stops every rule — a kill switch for runaway pipelines |

Request bodies are strict: unknown fields are a 422 instead of being ignored.

**Who did it.** Scripts should name themselves with an `X-Kanban-Actor` header
(`agent:launcher`, `ci`, `watchdog`, …) — it is written to the history instead
of the server-wide `KANBAN_ACTOR` (default `user`), so a human's drag-drop and
a script's move no longer look the same.

---

## Outbound webhooks (Slack / Telegram / generic)

The kanban can fire HTTP notifications on changes. Config:
`kanban_data/webhooks.json`, hot-reloaded by mtime:

```json
{
  "webhooks": [
    {
      "name": "Slack #dev",
      "url": "https://hooks.slack.com/services/XXX/YYY/ZZZ",
      "events": ["task_created", "task_moved"],
      "format": "slack",
      "project_id": null
    },
    {
      "name": "Telegram bot → my chat",
      "url": "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>",
      "events": ["task_moved"],
      "format": "telegram"
    },
    {
      "name": "Custom collector",
      "url": "http://localhost:8765/collect",
      "events": ["task_created", "task_moved", "task_commented", "task_updated"],
      "format": "generic"
    }
  ]
}
```

Fields:
- `events`: `task_created` / `task_moved` / `task_commented` / `task_updated`
- `format`: `slack` (`{text: "..."}`), `telegram` (`{text: "..."}`, `chat_id` in URL), `generic` (raw JSON)
- `project_id`: limit a webhook to one project; `null` = all projects
- `enabled`: defaults to `true`

Events come from the task history, so moves and comments made by MCP agents,
scripts and automation rules are delivered too, not only UI actions. Payloads
carry `actor` and the task with its newest 20 history rows (`history_total`
tells the full count). Bulk imports (actor `plan-import`, see
`KANBAN_EVENTS_QUIET_ACTORS`) are not announced.

Delivery is a **fire-and-forget asyncio task**: the user's main HTTP request
isn't blocked on webhook timeouts. Delivery logs (status_code, ms) live at
`/api/automation/status.webhooks.last_deliveries`.

### How to get a Slack / Telegram URL

- **Slack**: Workspace → Apps → Incoming Webhooks → Add → pick a channel → copy the URL.
- **Telegram**:
  ```bash
  curl https://api.telegram.org/bot<TOKEN>/getMe          # verify the token
  curl https://api.telegram.org/bot<TOKEN>/getUpdates     # find your chat_id
  ```
  Then the URL: `https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<CHAT_ID>`.

## Capturing Claude Code sessions

If you want tasks auto-created from Claude's session notes — wire up a
Stop-hook (or a slash command) to write a summary into
`~/.claude/projects/<encoded-path>/memory/inbox/<timestamp>.md`, and set:

```bash
export KANBAN_INBOX_DIR=~/.claude/projects/-Users-you-myproj/memory/inbox
```

The inbox watcher picks the file up within 5 seconds and turns it into a card.
See `Inbox watcher` in [README.md](../README.md).

## CORS

The kanban server binds to `127.0.0.1:7777` by default — no CORS headers, only same-origin (the bundled UI) and localhost agents (Claude Code via MCP, opencode dispatching curl) can talk to it.

For **remote** agents (Open WebUI on another host, a containerized agent) opt in via env:

```bash
export KANBAN_CORS_ORIGINS="https://webui.example,https://other.example"
python -m kanban_ui
```

Comma-separated origins. `allow_methods=*`, `allow_headers=*`, `allow_credentials=false`.

If you expose the kanban server itself externally (not just CORS — actually `0.0.0.0`), add **nginx** in front with auth — there's no built-in authentication.
