# Changelog

## 0.2.0 — 2026-09-25

This release comes from running the board daily with Claude Code agents for
four and a half months across several projects — hundreds of cards and
agent sessions, and one autonomous Claude-implements / Codex-reviews
pipeline. Almost every item below maps to something that went
wrong there.

### Fixed

- **Polling rules no longer fire every minute on the same card.** A
  `task_idle` rule (e.g. "blocked for 7 days → priority high + comment")
  re-applied itself on every tick for as long as the card stayed put; one
  database collected ~750k identical history rows in four months, card
  modals froze and `kanban_get` overflowed agent context. Polling rules now
  act once per stay in a column (`rule_firings`), `set_priority` to the
  current value is a no-op, and updates that change nothing write no history.
  Clean up an affected database with `maintenance dedupe-history`.
- **Moves made over MCP trigger rules and webhooks.** `kanban_move` wrote
  straight to SQLite, so the rule engine only ever saw drag-drops in the UI —
  an agent approving a card never launched anything. The server now tails
  `task_history` (event feed) and dispatches every change, whoever made it:
  UI, REST, MCP agents, scripts importing the store, the rule engine itself.
- **A card created directly in a column counts as arriving there**
  (`task_moved` with `from_status: null`). Cards an agent filed straight into
  Approved used to wait forever.
- **`KANBAN_DB` is honoured** by the store (documented before, ignored in
  code), and **`KANBAN_ACTOR` by the MCP server**.
- `POST /api/tasks` with malformed `links` returned 500 (`KeyError: 'type'`);
  it is a 400 with a readable message now. Links/blockers on unknown tasks are
  404s, unknown blocker ids and dependency cycles are rejected.
- Concurrent requests could hit `sqlite3.InterfaceError` on the shared
  connection (`GET /api/board`); all store access is serialised now.
- Writes from two processes (the web server and an MCP server, or two MCP
  servers) could fail instantly with "database is locked": transactions
  started with a deferred `BEGIN`, read, then tried to write, and SQLite does
  not wait in that case. Write transactions now start with `BEGIN IMMEDIATE`
  and wait up to 15 s for the other writer.
- The external blocker could not be cleared from the UI (empty field was sent
  as "leave unchanged"). `""` clears it in the UI, REST and MCP.
- Reordering a card inside its column reset its "in column since" time and
  wrote a history row; drag-drop indexes now renumber the column instead of
  producing duplicate positions.
- `run_command` processes were in the server's process group — restarting
  the kanban (or launchd) killed running agents — and without `log_file`
  their output went to `/dev/null`. They get their own session, a UTF-8
  locale when launchd provides none, and a default log file.
- The HTTP MCP endpoint used fastapi-mcp's deprecated `mount()`, which served
  SSE at `/mcp` although the docs said streamable HTTP.
- The Cursor setup in the docs pointed at the wrong config file.

### Added

- **Archive**: `archived_at` on tasks, `POST /api/tasks/{id}/archive`, an
  `archive` rule action, an Archive button in the card and "· N archived" in
  the top bar. Archived cards leave the board and keep their status.
  (Archiving by `move_to cancelled` made done work look abandoned; run
  `maintenance restore-archived` to fix old data.)
- **Assignee** over REST (`POST /api/tasks/{id}/assign`), MCP
  (`kanban_assign`), rules (`assign` action) and editable in the card dialog.
- **Rule engine**: `blockers_done` trigger (with optional `resolved_statuses`),
  `task_idle` in `hours`/`minutes`, rule-level `repeat_every` and
  `wip_limit` (polling rules), `project_id` lists, a top-level `"paused": true` kill switch
  (also `POST /api/automation/pause` and ⏸ in the top bar), a loop guard for
  reactive rules, and invalid rules are skipped instead of disabling the file.
- **`run_command`**: one process per card and rule at a time,
  `max_concurrent` with a queue, `max_runs` (block the card instead of the
  N+1-th launch; a person moving it out of Blocked resets the count — rules
  and agents don't), exit code 75 = "try again later", not counted. Queued
  and running jobs are stored in the database: after a server restart queued
  launches resume and agents that are still working keep their slot. Running
  and queued commands are listed in `/api/automation/status`, and cards show
  `▶ agent` while one runs.
- **Agent launcher v2** (`examples/agent-launcher/launch-claude.sh`):
  preflight (card still approved, `claude` found and logged in), per-card
  lock, explicit `--mcp-config` + `--strict-mcp-config`, `--permission-mode
  dontAsk`, hard timeout that kills the whole process tree (and cleans up
  whatever the agent left running), optional model per card size, optional
  gate command, and a card blocked with the reason when the agent leaves it
  behind — only if nobody has moved it since. Usage limits are recognised
  from the CLI's error output, not from any "429" in token counts. Covered
  by tests with a fake `claude`.
- **MCP**: `kanban_assign` and `kanban_history` (16 tools); `kanban_get` returns
  the newest 30 history rows plus `history_total`; mutating tools answer with a
  short card and a `url`; `kanban_move(expected_from=…)` refuses to act on a
  card someone else just moved; `KANBAN_PROJECT_ID` scopes list/search/board;
  `KANBAN_MCP_HUMAN_ONLY` (default `done`) — agents stop at `testing`.
  Arguments are strict: `project=`/`body=`/`column=` are errors that name the
  right parameter (they used to be dropped silently, creating empty cards);
  status names accept `"In progress"`, `"Testing"`, Russian column names.
- **`python -m kanban_mcp.connect <dir> --project <id>`** and
  `POST /api/projects/{id}/connect`: write/merge `.mcp.json` and refresh the
  CLAUDE.md block (and AGENTS.md). An existing entry keeps its name and your
  settings; only the code path, database and project are updated. The
  new-project wizard offers it.
- **`python -m kanban_store.maintenance`**: `doctor`, `dedupe-history`,
  `restore-archived`, `vacuum` — dry-run by default, backup before `--apply`.
  `dedupe-history` only removes identical automation rows less than
  `--window-minutes` (5) apart with nothing else in between; `doctor` also
  flags `.mcp.json` files whose `KANBAN_DB` points at another database.
- REST: `X-Kanban-Actor` header, `GET /api/tasks/{id}/history` (paged),
  `history_limit` on `GET /api/tasks/{id}`, `expected_from` on moves (409),
  `status`/`created_at`/`running` on every board card, `project_id` alias on
  `/api/board`, `comment` alias for `text`, SSE at `/sse`.
- UI: card links `/t/T-042` and `/p/<project>/t/T-042` (click the id in the
  dialog to copy), "time in column" chips, history in local time with a note
  about older rows, critical priority and XL size.
- PLAN.md import: skips template placeholder lines, strips markdown from
  titles, shortens titles over 120 characters (full text in the description),
  refuses CLAUDE.md/AGENTS.md as plan sources; re-importing a file imported
  by v0.1 recognises the old spellings instead of duplicating cards.
- CI: pytest on Python 3.12 and 3.13.

### Changed

- **Webhook and rule events come from the event feed**, ~1 s after the change
  (immediately for changes made through this server). Moves by MCP agents and
  scripts are now delivered too. Payloads include `actor`, and the task
  carries its newest 20 history rows plus `history_total` instead of the full
  history. Bulk writers (`KANBAN_EVENTS_QUIET_ACTORS`, default
  `plan-import,maintenance`) trigger neither webhooks nor rules.
- The generated CLAUDE.md block is MCP-first: agents file new work with
  `kanban_create`, and the block says plainly that PLAN.md is a one-shot
  import (it used to promise a sync that did not exist).
- MCP `kanban_move`/`kanban_create`/`kanban_update`/`kanban_pull` answer
  with a short card (pull: the full card with 10 history rows) instead of the
  full card with its entire history.
- HTTP MCP: streamable HTTP at `/mcp`, SSE moved to `/sse`. HTTP MCP
  clients are treated as agents: `KANBAN_MCP_HUMAN_ONLY` applies, changes are
  recorded as `agent:mcp-http` (or the client's `X-Kanban-Actor`), and
  host-side endpoints (file pickers, CLI login, writing into project folders),
  creating/editing projects and the pause switch are not exposed as tools.
- REST request bodies reject unknown fields (422) instead of ignoring them;
  `priority` accepts `critical/high/normal/low` (`medium` → `normal`),
  `size` `S/M/L/XL`.
- Agents calling `kanban_move(…, "done")` get an error by default; set
  `KANBAN_MCP_HUMAN_ONLY=""` to allow it.
- The MCP server no longer creates a database: if `KANBAN_DB` points at a
  file that doesn't exist, every tool returns an error saying so (instead of
  the agent working on a new, empty board).
- The launcher now stays in the foreground while the agent works (the kanban
  tracks the process); scripts written for v0.1 that exit immediately still
  work but lose de-duplication and `max_concurrent`.
- `launchd` template: fuller `PATH` and `LANG=en_US.UTF-8` for `run_command`.

### Upgrading from 0.1.x

1. **Before restarting a server that has `run_command` rules**, look at
   `rules.json`: moves made over MCP (and cards created straight into a
   column) now trigger rules too. If unsure, add `"paused": true` first and
   un-pause when you've checked what will fire.
2. Pull, then `uv sync` (or `pip install -r requirements.txt`) and restart the
   server. The schema migrates itself (v5: `tasks.archived_at`,
   `rule_firings`, `command_runs`, `command_jobs`); v0.1 servers keep working
   on a migrated database.
3. `KANBAN_DB` is honoured now (v0.1 silently used `<repo>/tasks.db`). Check
   the value in every `.mcp.json` and in the launchd plist — `doctor` flags
   `.mcp.json` entries that point at a different database.
4. `python -m kanban_store.maintenance doctor` — it reports history spam,
   done cards relabelled as cancelled, archive rules that do that, projects
   without `.mcp.json` and `.mcp.json` files pointing at another database.
   Each finding names the fix command; the fixing commands are dry runs until
   you add `--apply` and make a backup first. Run `restore-archived` only once
   the server runs v0.2 (a v0.1 server doesn't know about archiving and its
   old rule would relabel the cards again), and `vacuum` with the server stopped.
5. In `rules.json`, replace `"action": {"type": "move_to", "status":
   "cancelled"}` archive rules with `{"type": "archive"}`, and consider
   `max_concurrent`/`max_runs` on `run_command` rules.
6. HTTP MCP clients configured for SSE at `/mcp` → use `/sse`, or switch them
   to streamable HTTP at `/mcp`.
7. Re-run `python -m kanban_mcp.connect <dir> --project <id>` in each project
   to point it at this checkout and refresh the CLAUDE.md block.

## 0.1.2 — 2026-05-11

- uv quickstart: `pyproject.toml` + `uv.lock`, one command from clone to UI.
- HTTP MCP endpoint via fastapi-mcp for Cursor, new Cline and MCP Inspector.
- Integration docs for Cursor, Cline (HTTP) and MCP Inspector.

## 0.1.1 — 2026-05-09

- QUICKSTART, auto-launch agent pipeline example, automation status endpoint.
- Agent workflow rules in the generated CLAUDE.md block; `KANBAN_ACTOR` in
  MCP examples; theme/profile screenshots.

## 0.1.0 — 2026-05-09

- Initial open-source release: multi-project board, REST + OpenAPI, stdio MCP
  server, inbox watcher, PLAN.md import, automation rules, webhooks.
