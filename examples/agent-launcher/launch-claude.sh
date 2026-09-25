#!/usr/bin/env bash
# launch-claude.sh — agent launcher for agent-kanban (v2).
#
# Invoked by the rule engine when a card lands in a chosen column:
#
#   {
#     "name": "Claude on approved",
#     "trigger": {"type": "task_moved", "to_status": "approved", "project_id": "myproj"},
#     "action": {
#       "type": "run_command",
#       "cmd": "/abs/path/to/agent-kanban/examples/agent-launcher/launch-claude.sh",
#       "args": ["{task_id}", "{project_id}"],
#       "max_concurrent": 1,
#       "max_runs": 3
#     }
#   }
#
# The script stays in the foreground while the agent works, so the kanban
# knows the launch is alive (one run per card, max_concurrent queueing).
#
# What it does, in order:
#   1. Preflight: board reachable, card still in approved/analyst, `claude`
#      found and logged in. A failed preflight blocks the card with the reason
#      instead of failing silently in a log nobody reads.
#   2. Per-card lock (mkdir — portable, no flock on macOS).
#   3. Runs `claude -p` in the project directory with:
#        --mcp-config <generated> --strict-mcp-config  — the kanban server is
#          passed explicitly: no dependency on the project's .mcp.json alias,
#          nothing to approve interactively;
#        --permission-mode dontAsk + --allowedTools    — anything not allowed
#          is denied immediately; a headless run never waits on a prompt.
#   4. A hard timeout (AGENT_TIMEOUT_SEC): a live process is not proof of work.
#   5. Afterwards checks the card. If the agent left it in approved/analyst/
#      in_progress, the launcher moves it to blocked with the exit code, the
#      reason and the log path. Optional KANBAN_GATE_CMD (e.g. the test
#      suite) must pass before a card stays in testing.
#
# Exit codes: 0 done (whatever the outcome, it is on the card); 75 the model's
# usage limit was hit — the kanban does not count it against max_runs;
# 1 the launcher itself could not run.
#
# Environment (all optional):
#   KANBAN_URL            http://localhost:7777
#   KANBAN_LAUNCHER_ACTOR agent:launcher       — name in the card history
#   AGENT_TIMEOUT_SEC     3600
#   CLAUDE_BIN            path to the claude CLI (auto-detected otherwise)
#   CLAUDE_MODEL_S/M/L    model per card size, e.g. CLAUDE_MODEL_S=claude-haiku-4-5
#   KANBAN_PERMISSION_MODE dontAsk             — or acceptEdits / bypassPermissions
#   KANBAN_ALLOWED_TOOLS  "Bash Read Edit Write Grep Glob TodoWrite" (+ kanban tools)
#   KANBAN_STRICT_MCP     1 — 0 also loads the project's own .mcp.json servers
#   KANBAN_GATE_CMD       shell command run in the project dir after the agent
#   KANBAN_RUNS_DIR       ~/Library/Logs/agent-kanban/runs — per-card run folders

set -uo pipefail

# launchd starts services with a tiny PATH and no locale.
export PATH="$HOME/.local/bin:$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [[ -d "$HOME/.nvm/versions/node" ]]; then
    _node_dir="$(/bin/ls -1d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | tail -1)"
    [[ -n "$_node_dir" ]] && export PATH="$_node_dir:$PATH"
fi
export LANG="${LANG:-en_US.UTF-8}"

TASK_ID="${1:?usage: launch-claude.sh <task_id> [project_id]}"
PROJECT_ID="${2:-}"
KANBAN_URL="${KANBAN_URL:-http://localhost:7777}"
ACTOR="${KANBAN_LAUNCHER_ACTOR:-agent:launcher}"
TIMEOUT="${AGENT_TIMEOUT_SEC:-3600}"
PERMISSION_MODE="${KANBAN_PERMISSION_MODE:-dontAsk}"
STRICT_MCP="${KANBAN_STRICT_MCP:-1}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
KANBAN_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PY="$KANBAN_ROOT/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

RUNS_DIR="${KANBAN_RUNS_DIR:-$HOME/Library/Logs/agent-kanban/runs}"
RUN_DIR="$RUNS_DIR/$TASK_ID"
mkdir -p "$RUN_DIR"
LOG="$RUN_DIR/launcher.log"

log() { echo "[$(date -u +%FT%TZ)] $TASK_ID: $*" | tee -a "$LOG" >&2; }

# --- tiny REST helpers ------------------------------------------------------

api_get() {  # path
    curl -fsS -m 20 "$KANBAN_URL$1"
}

api_post() {  # path json
    curl -fsS -m 20 -X POST "$KANBAN_URL$1" \
        -H 'Content-Type: application/json' -H "X-Kanban-Actor: $ACTOR" \
        -d "$2" >/dev/null
}

json_field() {  # json field  → prints the field (empty if missing)
    printf '%s' "$1" | "$PY" -c 'import json,sys; d=json.load(sys.stdin); v=d.get(sys.argv[1]); print("" if v is None else v)' "$2"
}

json_str() {  # text → JSON string literal
    printf '%s' "$1" | "$PY" -c 'import json,sys; print(json.dumps(sys.stdin.read()))'
}

comment() {  # text
    api_post "/api/tasks/$TASK_ID/comment" "{\"text\": $(json_str "$1")}" || log "could not comment: $1"
}

move() {  # to_status comment [expected_from]
    local exp=""
    [[ -n "${3:-}" ]] && exp=", \"expected_from\": \"$3\""
    api_post "/api/tasks/$TASK_ID/move" \
        "{\"to_status\": \"$1\", \"comment\": $(json_str "$2")$exp}" \
        || log "could not move to $1 (someone moved the card meanwhile?)"
}

card_status() {
    json_field "$(api_get "/api/tasks/$TASK_ID?history_limit=0" 2>/dev/null || echo '{}')" status
}

block() {  # reason — preflight only: the card was just seen in $STATUS
    log "BLOCK: $1"
    move blocked "launcher: $1" "$STATUS"
}

# --- 1. preflight -----------------------------------------------------------

TASK_JSON="$(api_get "/api/tasks/$TASK_ID?history_limit=0")" || {
    log "kanban not reachable at $KANBAN_URL"; exit 1; }
STATUS="$(json_field "$TASK_JSON" status)"
case "$STATUS" in
    approved|analyst) ;;
    *) log "card is in '$STATUS' now, not approved — nothing to do"; exit 0 ;;
esac
TITLE="$(json_field "$TASK_JSON" title)"
DESC="$(json_field "$TASK_JSON" description)"
ACCEPTANCE="$(json_field "$TASK_JSON" acceptance)"
SIZE="$(json_field "$TASK_JSON" size)"
[[ -z "$PROJECT_ID" ]] && PROJECT_ID="$(json_field "$TASK_JSON" project_id)"

PROJ_PATH="$(api_get /api/projects | "$PY" -c '
import json, sys
ps = json.load(sys.stdin)["projects"]
m = [p for p in ps if p["id"] == sys.argv[1]]
print((m[0].get("path") or "") if m else "")' "$PROJECT_ID")"
if [[ -z "$PROJ_PATH" || ! -d "$PROJ_PATH" ]]; then
    block "project '$PROJECT_ID' has no existing directory (set it in the UI: ⋯ → Project directory)"
    exit 0
fi
DB_PATH="$(json_field "$(api_get /api/automation/status)" db)"

_resolve_claude() {
    [[ -n "${CLAUDE_BIN:-}" ]] && { echo "$CLAUDE_BIN"; return; }
    local p
    p="$(command -v claude 2>/dev/null || true)"
    [[ -n "$p" && -x "$p" ]] && { echo "$p"; return; }
    # macOS desktop app bundles the CLI; the ~/.local/bin symlink can go stale.
    local mac_root="$HOME/Library/Application Support/Claude/claude-code" latest
    if [[ -d "$mac_root" ]]; then
        latest="$(/bin/ls -1t "$mac_root" 2>/dev/null | head -1)"
        [[ -n "$latest" && -x "$mac_root/$latest/claude.app/Contents/MacOS/claude" ]] \
            && { echo "$mac_root/$latest/claude.app/Contents/MacOS/claude"; return; }
    fi
    echo ""
}
CLAUDE="$(_resolve_claude)"
if [[ -z "$CLAUDE" ]]; then
    block "claude CLI not found (PATH=$PATH). Set CLAUDE_BIN in the rule's env."
    exit 0
fi
if ! "$CLAUDE" auth status 2>/dev/null | "$PY" -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("loggedIn") else 1)'; then
    block "claude CLI is not logged in — run \`claude auth login\` (or press C in the board's top bar), then move the card back to Approved."
    exit 0
fi

# --- 2. per-card lock -------------------------------------------------------

LOCK="$RUN_DIR/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    old_pid="$(cat "$LOCK/pid" 2>/dev/null || true)"
    if [[ -n "$old_pid" ]] && kill -0 "$old_pid" 2>/dev/null; then
        log "already running (pid $old_pid) — not starting a second agent"
        exit 0
    fi
    rm -rf "$LOCK" && mkdir "$LOCK"
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# --- 3. prompt, MCP config, model -------------------------------------------

MCP_ALIAS="agent-kanban"
MCP_CONFIG="$RUN_DIR/mcp.json"
"$PY" - "$MCP_CONFIG" "$KANBAN_ROOT" "$DB_PATH" "$PROJECT_ID" "$PY" <<'PYEOF'
import json, sys
out, root, db, project, py = sys.argv[1:6]
json.dump({"mcpServers": {"agent-kanban": {
    "type": "stdio", "command": py, "args": ["-m", "kanban_mcp"], "cwd": root,
    "env": {"PYTHONPATH": root, "KANBAN_DB": db, "KANBAN_PROJECT_ID": project,
            "KANBAN_ACTOR": "claude"},
}}}, open(out, "w"), indent=2)
PYEOF

KANBAN_TOOLS=""
for t in columns get history pull move comment link create update search my_active board blockers; do
    KANBAN_TOOLS="$KANBAN_TOOLS mcp__${MCP_ALIAS}__kanban_$t"
done
ALLOWED="${KANBAN_ALLOWED_TOOLS:-Bash Read Edit Write Grep Glob TodoWrite}$KANBAN_TOOLS"

case "$(echo "$SIZE" | tr '[:lower:]' '[:upper:]')" in
    S) MODEL="${CLAUDE_MODEL_S:-}" ;;
    L|XL) MODEL="${CLAUDE_MODEL_L:-}" ;;
    *) MODEL="${CLAUDE_MODEL_M:-}" ;;
esac

PROMPT="$RUN_DIR/prompt.md"
cat > "$PROMPT" <<EOF
You're connected to agent-kanban via MCP (server "$MCP_ALIAS"). Take task $TASK_ID and complete it.

Workflow:
  1. kanban_pull(task_id="$TASK_ID") — claim it (approved -> analyst).
  2. Plan, and write the plan as a comment via kanban_comment.
  3. kanban_move(task_id="$TASK_ID", to_status="in_progress") before editing files.
  4. Implement it. Along the way: kanban_comment for progress, kanban_link for PRs/files.
  5. Verify against the acceptance criteria. Claims of "done" must be backed by
     command output (tests, a run), quoted in the final comment.
  6. kanban_move(task_id="$TASK_ID", to_status="testing", comment="<what was done, how it was verified>")
  7. Stop. Do not move the card to "uat" / "done" — a human decides that.

If the task is ambiguous or you are blocked, add a kanban_comment with the
specific question and kanban_move the card to "blocked". Do not guess.
Change only what the card asks for.

=== Task ===
ID:     $TASK_ID
Title:  $TITLE

Description:
$DESC

Acceptance criteria:
$ACCEPTANCE
=== End ===
EOF

# --- 4. run the agent with a hard timeout -----------------------------------

CMD=("$CLAUDE" -p "$(cat "$PROMPT")" --output-format json
     --mcp-config "$MCP_CONFIG" --permission-mode "$PERMISSION_MODE"
     --allowedTools $ALLOWED)
[[ "$STRICT_MCP" == "1" ]] && CMD+=(--strict-mcp-config)
[[ -n "$MODEL" ]] && CMD+=(--model "$MODEL")

RESULT="$RUN_DIR/result.json"
AGENT_LOG="$RUN_DIR/agent.log"
: > "$RESULT"
log "starting claude in $PROJ_PATH (timeout ${TIMEOUT}s${MODEL:+, model $MODEL}); run dir $RUN_DIR"
cd "$PROJ_PATH" || { block "cannot cd to $PROJ_PATH"; exit 0; }
# Own session/process group: on timeout the whole tree goes — claude, the
# MCP servers it started, and whatever its Bash tool left running.
"$PY" -c 'import os, sys; os.setsid(); os.execvp(sys.argv[1], sys.argv[1:])' "${CMD[@]}" \
    > "$RESULT" 2> "$AGENT_LOG" < /dev/null &
AGENT_PID=$!
echo "$AGENT_PID" > "$RUN_DIR/agent.pid"
# Timeout guard. Its sleeps run in the background + `wait`, so the TERM we
# send when the agent finishes first interrupts them instead of leaving an
# orphaned `sleep` around for an hour.
(
    trap 'kill "$sp" 2>/dev/null; exit 0' TERM
    sleep "$TIMEOUT" & sp=$!; wait "$sp"
    if kill -0 "$AGENT_PID" 2>/dev/null; then
        echo "timeout" > "$RUN_DIR/timed_out"
        kill -TERM -- "-$AGENT_PID" 2>/dev/null || kill -TERM "$AGENT_PID" 2>/dev/null
        i=0
        while [ "$i" -lt 15 ] && kill -0 -- "-$AGENT_PID" 2>/dev/null; do
            sleep 1; i=$((i + 1))
        done
        kill -KILL -- "-$AGENT_PID" 2>/dev/null
    fi
) > /dev/null 2>&1 &
GUARD_PID=$!
wait "$AGENT_PID"; RC=$?
kill "$GUARD_PID" 2>/dev/null
wait "$GUARD_PID" 2>/dev/null
rm -f "$RUN_DIR/agent.pid"
# Whatever is left in the agent's process group (a child that ignored TERM,
# a server its Bash tool started) goes too: the guard may have been stopped
# before its SIGKILL step.
if kill -0 -- "-$AGENT_PID" 2>/dev/null; then
    kill -TERM -- "-$AGENT_PID" 2>/dev/null
    i=0
    while [ "$i" -lt 5 ] && kill -0 -- "-$AGENT_PID" 2>/dev/null; do
        sleep 1; i=$((i + 1))
    done
    kill -KILL -- "-$AGENT_PID" 2>/dev/null
fi

# --- 5. check what the agent left behind ------------------------------------

# Line 1: OK | LIMIT (the model's usage limit, judged from the CLI's error
# output and an error result only — never from token counts, costs or ids
# that happen to contain "429"). Line 2: summary. Line 3: last words.
ANALYSIS="$("$PY" - "$RESULT" "$AGENT_LOG" "$RC" <<'PYEOF'
import json, re, sys
res_path, log_path, rc = sys.argv[1], sys.argv[2], int(sys.argv[3])
try:
    d = json.load(open(res_path))
    d = d if isinstance(d, dict) else {}
except Exception:
    d = {}
try:
    err = open(log_path, errors="replace").read()[-2000:]
except OSError:
    err = ""
bits = []
if d.get("num_turns") is not None: bits.append(f"{d['num_turns']} turns")
if isinstance(d.get("total_cost_usd"), (int, float)): bits.append(f"${d['total_cost_usd']:.2f}")
if d.get("session_id"): bits.append(f"session {d['session_id']}")
result_text = str(d.get("result") or "")
if d.get("is_error"): bits.append("error: " + result_text[:200])
failed = rc != 0 or bool(d.get("is_error"))
text = (result_text if d.get("is_error") else "") + "\n" + err
limit = failed and re.search(
    r"usage limit reached|hit your [a-z0-9 -]*limit|limit will reset"
    r"|\brate[ _]limit(ed)?[ _](error|exceeded)\b|\btoo many requests\b|\boverloaded_error\b",
    text, re.I)
last = (result_text if d.get("is_error") or not err.strip() else err).strip()
print("LIMIT" if limit else "OK")
print(", ".join(bits) or "no result")
print(" ".join(last.split())[-300:])
PYEOF
)"
VERDICT="$(printf '%s\n' "$ANALYSIS" | sed -n 1p)"
SUMMARY="$(printf '%s\n' "$ANALYSIS" | sed -n 2p)"
LAST_WORDS="$(printf '%s\n' "$ANALYSIS" | sed -n 3p)"
log "claude exited rc=$RC ($SUMMARY)"

NOW="$(card_status)"

# Block the card only while it still sits in an agent column, and only if
# nobody moved it meanwhile (expected_from); otherwise just leave a note.
block_if_working() {  # status reason
    case "$1" in
        approved|analyst|in_progress)
            log "BLOCK: $2"
            move blocked "launcher: $2" "$1" ;;
        *)
            comment "launcher: $2 (the card is in '$1' — left as is)" ;;
    esac
}

if [[ -f "$RUN_DIR/timed_out" ]]; then
    rm -f "$RUN_DIR/timed_out"
    block_if_working "$NOW" "agent timed out after ${TIMEOUT}s ($SUMMARY). Log: $RUN_DIR"
    exit 0
fi

if [[ "$VERDICT" == "LIMIT" ]]; then
    comment "launcher: the model's usage limit was hit ($SUMMARY). The card stays where it is; retry later. Log: $RUN_DIR"
    exit 75
fi

case "$NOW" in
    approved|analyst|in_progress)
        block_if_working "$NOW" "agent exited (rc=$RC, $SUMMARY) but left the card in '$NOW'. Log: $RUN_DIR — $LAST_WORDS"
        exit 0 ;;
esac

if [[ "$NOW" == "testing" && -n "${KANBAN_GATE_CMD:-}" ]]; then
    log "gate: $KANBAN_GATE_CMD"
    if ! (cd "$PROJ_PATH" && bash -c "$KANBAN_GATE_CMD") > "$RUN_DIR/gate.log" 2>&1; then
        move blocked "launcher: gate failed after the agent's run — \`$KANBAN_GATE_CMD\`: $(tail -5 "$RUN_DIR/gate.log" | tr '\n' ' ' | cut -c1-400). Log: $RUN_DIR/gate.log" testing
        exit 0
    fi
    comment "launcher: gate passed — \`$KANBAN_GATE_CMD\` ($(tail -1 "$RUN_DIR/gate.log" | cut -c1-200))"
fi

comment "launcher: agent run finished in '$NOW' ($SUMMARY)."
exit 0
