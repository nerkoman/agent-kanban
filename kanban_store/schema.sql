-- Kanban schema (SQLite)
-- v2: added projects + tasks.project_id (migration in Store._migrate_v2).
-- v5: tasks.archived_at + rule_firings + command_runs + command_jobs (Store._migrate_v5).

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,                    -- 'finops', 'kanban-dev', ...
    name        TEXT NOT NULL,                       -- 'FinOps', 'Kanban Dev'
    color       TEXT NOT NULL DEFAULT '#F10D30',     -- accent color of the project (hex)
    icon        TEXT NOT NULL DEFAULT '',            -- 1-2 chars: 'F', 'KB', 'AI'
    sort_order  INTEGER NOT NULL DEFAULT 0,          -- order in the project switcher
    archived    INTEGER NOT NULL DEFAULT 0,          -- 0/1
    path        TEXT,                                -- Claude Code project directory (optional)
    created_at  TEXT NOT NULL                        -- ISO8601
);

CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,                -- T-001, T-002, ...
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'backlog', -- backlog/approved/analyst/in_progress/testing/uat/done/blocked/cancelled
    priority        TEXT NOT NULL DEFAULT 'normal',  -- high/normal/low
    size            TEXT NOT NULL DEFAULT 'M',       -- S/M/L
    assignee        TEXT,                            -- user / agent:<name> / NULL
    description     TEXT NOT NULL DEFAULT '',
    acceptance      TEXT NOT NULL DEFAULT '',
    external_blocker TEXT,                           -- "DevOps: roles monitoring.viewer"
    created_at      TEXT NOT NULL,                   -- ISO8601
    moved_at        TEXT NOT NULL,                   -- ISO8601, last status change
    column_order    INTEGER NOT NULL DEFAULT 0,      -- order within the column (for drag-drop)
    project_id      TEXT NOT NULL DEFAULT 'default', -- FK -> projects.id
    archived_at     TEXT                             -- ISO8601; NULL = visible on the board
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, column_order);
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(assignee);
-- idx_tasks_project_status is created in Store._migrate_v2 (after ALTER TABLE for older databases).

CREATE TABLE IF NOT EXISTS task_links (
    task_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    type     TEXT NOT NULL,                          -- memory/file/pr/url/plan
    value    TEXT NOT NULL,
    PRIMARY KEY (task_id, type, value)
);

CREATE TABLE IF NOT EXISTS task_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,                      -- user/agent:<name>/automation
    action       TEXT NOT NULL,                      -- create/move/comment/assign/update/archive/unarchive
    from_status  TEXT,
    to_status    TEXT,
    comment      TEXT
);

CREATE INDEX IF NOT EXISTS idx_history_task ON task_history(task_id, ts);

CREATE TABLE IF NOT EXISTS task_blockers (
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    blocker_id  TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, blocker_id),
    CHECK (task_id != blocker_id)
);

CREATE TABLE IF NOT EXISTS project_sources (
    project_id    TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,              -- 'plan_md' | 'git'
    config        TEXT NOT NULL,              -- JSON: {file, repo_url, ...}
    last_sync_at  TEXT,                       -- ISO8601
    created_at    TEXT NOT NULL
);

-- One row per (rule, task, episode): a polling rule acts on a task at most
-- once per stay in a column. ``anchor`` is the task's moved_at at the time
-- the rule fired, so moving the task out and back starts a new episode.
CREATE TABLE IF NOT EXISTS rule_firings (
    rule_key  TEXT NOT NULL,
    task_id   TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    anchor    TEXT NOT NULL,
    fired_at  TEXT NOT NULL,
    PRIMARY KEY (rule_key, task_id, anchor)
);

-- run_command launches per (rule, task), for the max_runs safety limit.
-- Reset when the task is moved out of 'blocked' (a human says "try again").
CREATE TABLE IF NOT EXISTS command_runs (
    rule_key         TEXT NOT NULL,
    task_id          TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    runs             INTEGER NOT NULL DEFAULT 0,
    last_started_at  TEXT,
    last_rc          INTEGER,
    PRIMARY KEY (rule_key, task_id)
);

-- run_command jobs that are queued or running, so a server restart neither
-- forgets queued launches nor loses track of agents still working.
CREATE TABLE IF NOT EXISTS command_jobs (
    rule_key    TEXT NOT NULL,
    task_id     TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    state       TEXT NOT NULL,              -- queued | running
    rule_name   TEXT NOT NULL,
    ctx         TEXT NOT NULL,              -- JSON placeholders (task_id, title, ...)
    pid         INTEGER,
    proc_start  TEXT,                       -- `ps -o lstart=` of pid: tells a reused pid apart
    queued_at   TEXT NOT NULL,
    started_at  TEXT,
    PRIMARY KEY (rule_key, task_id)
);

-- meta for migrations
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '4');
INSERT OR IGNORE INTO meta(key, value) VALUES ('next_id', '1');

-- Default project — read from env ``KANBAN_DEFAULT_PROJECT_ID`` / ``..._NAME``
-- (see Store._seed_default_project). If not provided, 'default'/'Default' is created.
-- Existing tasks receive this project_id via _migrate_v2().
