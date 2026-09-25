# Agent launcher

Scripts the kanban invokes via a `run_command` action when a card lands in
a given column. The goal: **"drag a task into Approved → the agent picks it
up itself and drives it to Testing"** — and when it doesn't, the card says
why instead of sitting in Analyst forever.

## What's in here

| File | What it does |
|---|---|
| [`launch-claude.sh`](launch-claude.sh) | Runs Claude Code (`claude -p`) headless in the project directory, with the kanban MCP server passed explicitly, a hard timeout, and a safety net that blocks the card with a reason if the agent leaves it behind. |

## How to wire it up

1. **Bind the kanban project to the code directory** — UI → `⋯` next to the
   project → `Project directory` (e.g. `~/code/myapp`). Optionally run
   `python -m kanban_mcp.connect ~/code/myapp --project myproj` so
   interactive Claude sessions there get the kanban tools too (the launcher
   itself doesn't need it).

2. **Add a rule in `kanban_data/rules.json`**:
   ```json
   {
     "rules": [
       {
         "name": "Claude on approved",
         "trigger": {"type": "task_moved", "to_status": "approved", "project_id": "myproj"},
         "action": {
           "type": "run_command",
           "cmd": "/abs/path/to/agent-kanban/examples/agent-launcher/launch-claude.sh",
           "args": ["{task_id}", "{project_id}"],
           "log_file": "~/Library/Logs/agent-kanban/launcher.log",
           "max_concurrent": 1,
           "max_runs": 3
         }
       }
     ]
   }
   ```
   Hot-reloaded by mtime — no restart required. `project_id` may also be a
   list (`["web", "api"]`) instead of three copies of the same rule.

3. **Test**: create a task in the backlog → drag it into `Approved` → within
   a few seconds the card shows `▶ agent` and moves to `analyst`.

It fires no matter who moves the card: a drag-drop, an MCP agent, a script
calling the REST API, or a card created directly in Approved.

## What the launcher does

1. **Preflight** — the board answers, the card is still in `approved`
   (someone may have moved it back), the project has a directory, `claude`
   is found (PATH, `CLAUDE_BIN`, or the macOS desktop app bundle) and logged
   in. A failed check moves the card to **Blocked** with the reason.
2. **Per-card lock** (`mkdir`, portable — macOS has no `flock`), on top of
   the kanban's own "one run per card" rule.
3. **Runs the agent** with
   - `--mcp-config <run dir>/mcp.json --strict-mcp-config` — the kanban
     server is handed over explicitly, so there's no dependency on the
     project's `.mcp.json` alias and nothing to approve;
   - `--permission-mode dontAsk --allowedTools …` — a tool that isn't on
     the list is denied immediately; a headless run never waits for an
     answer nobody will give;
   - its own process group, so a timeout kills claude *and* the MCP servers
     and shell commands it started.
4. **Hard timeout** (`AGENT_TIMEOUT_SEC`, default 3600): a live process is
   not proof of progress.
5. **Afterwards** — whatever is still running in the agent's process group
   is stopped. If the agent left the card in approved / analyst /
   in_progress, the launcher blocks it with the exit code, the agent's last
   words and the run folder (only if the card is still where it was — a
   card a human has moved meanwhile just gets a comment). With
   `KANBAN_GATE_CMD` set (say `pytest -q`), a card in testing must also pass
   the gate.

Everything for one card lives in `~/Library/Logs/agent-kanban/runs/T-XXX/`:
`prompt.md`, `mcp.json`, `result.json` (turns, cost, session id), `agent.log`,
`launcher.log`, `gate.log`.

### Exit codes

| Code | Meaning | Kanban |
|---|---|---|
| 0 | finished; the outcome is on the card | counts as a run |
| 75 | the model's usage limit was hit | not counted against `max_runs`; card stays put |
| 1 | the launcher itself couldn't run (board unreachable) | counts as a run |

To retry cards that hit a usage limit, add a polling rule next to the
reactive one:

```json
{
  "name": "Retry approved cards nobody picked up",
  "project_id": "myproj",
  "trigger": {"type": "task_idle", "status": "approved", "minutes": 30},
  "repeat_every": {"minutes": 30},
  "action": {"type": "run_command", "cmd": "/abs/path/…/launch-claude.sh",
             "args": ["{task_id}", "{project_id}"], "max_concurrent": 1}
}
```

## Environment

Set these in the rule's `"env"` object.

| Variable | Default | Purpose |
|---|---|---|
| `KANBAN_URL` | `http://localhost:7777` | board address |
| `KANBAN_LAUNCHER_ACTOR` | `agent:launcher` | name in the card history |
| `AGENT_TIMEOUT_SEC` | `3600` | hard limit per run |
| `CLAUDE_BIN` | auto | path to the `claude` CLI |
| `CLAUDE_MODEL_S` / `_M` / `_L` | CLI default | model by card size, e.g. a small model for S cards |
| `KANBAN_PERMISSION_MODE` | `dontAsk` | `acceptEdits` / `bypassPermissions` if you know why |
| `KANBAN_ALLOWED_TOOLS` | `Bash Read Edit Write Grep Glob TodoWrite` | tools besides the kanban ones |
| `KANBAN_STRICT_MCP` | `1` | `0` also loads the project's own `.mcp.json` servers |
| `KANBAN_GATE_CMD` | — | command that must pass before a card stays in testing |
| `KANBAN_RUNS_DIR` | `~/Library/Logs/agent-kanban/runs` | per-card run folders |

## Supported placeholders

Substituted into `args`:

| Placeholder | Contains |
|---|---|
| `{task_id}` | task ID (T-XXX) |
| `{title}` | task title |
| `{project_id}` | project slug |
| `{status}` | the card's column when the rule fired |
| `{from_status}` | column the card came from (empty for a created card) |
| `{to_status}` | column the card moved to |

## Concurrency and runaway protection

All of this is in the kanban, not in the script:

- **One process per card and rule** — a second trigger while the first run
  is alive is ignored (the classic "two agents on one card" after a human
  drags it back and forth).
- **`max_concurrent`** — extra launches wait in a FIFO queue; `/api/automation/status`
  lists what's running and queued, and the card shows `▶ agent`.
- **`max_runs`** — after N launches for the same card it's blocked instead;
  a person moving it out of Blocked is the "try again" signal and resets the
  count (a retry rule or an agent moving it doesn't).
- **Restarts** — queued launches and running agents are stored in the
  database; a restarted server resumes the queue and keeps counting agents
  that are still working.
- **Loop guard** — a reactive rule firing on the same card more than 5 times
  in 10 minutes is skipped and reported.
- **Kill switch** — ⏸ in the top bar or `POST /api/automation/pause
  {"paused": true}`; running agents finish, nothing new starts.

## Security

The script runs on the kanban server (= localhost). There's no sandboxing —
`cmd` and `args` execute as-is. Don't put anything in `rules.json` you
wouldn't run from your own shell, and keep `KANBAN_ALLOWED_TOOLS` as narrow
as the work allows.

## Your own launcher

You can replace `launch-claude.sh` with anything else — `launch-opencode.sh`,
`launch-aider.sh`, a wrapper around another LLM. The contract:

1. Stay in the foreground while the agent works (the kanban tracks the
   process; it runs in its own session and survives kanban restarts).
2. Leave the card in a truthful column before exiting — and when that isn't
   where the agent should have put it, say why in a comment.
3. Exit 75 for "try again later" (rate limits, service down).
4. Send `X-Kanban-Actor: <your name>` with REST calls so the history shows
   who moved the card.
