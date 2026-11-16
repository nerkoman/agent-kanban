# agent-kanban

> Local-first kanban for AI-agent workflows. SQLite, FastAPI, no auth, no cloud.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

A self-hosted kanban board designed to be driven by AI coding agents
(Claude Code, Cline, opencode, Open WebUI, any function-calling LLM).
Drag-drop in browser, REST + MCP for agents. Multi-project, light/dark
theme, markdown plan-file import, automation rules.

![Board — light theme](docs/screenshots/board-light.png)

## Docker Setup

### Prerequisites

- Docker and Docker Compose installed
- At least 1GB free disk space

### Quick Start with Docker

1. **Clone the repository**
   ```bash
   git clone https://github.com/<your-user>/agent-kanban.git
   cd agent-kanban
   ```

2. **Start the container**
   ```bash
   docker-compose up -d
   ```

3. **Access the application**
   Open your browser and navigate to `http://localhost:7777`

4. **View logs**
   ```bash
   docker-compose logs -f
   ```

5. **Stop the container**
   ```bash
   docker-compose down
   ```

6. **Stop and remove volumes**
   ```bash
   docker-compose down -v
   ```

### Docker Compose Configuration

The `docker-compose.yml` file configures:

- **Port**: 7777 (host) → 7777 (container)
- **Data persistence**: `kanban_data` volume for SQLite database, inbox, rules, and webhooks
- **Environment variables**: All configuration via environment variables (see below)

### Environment Variables

| Variable | Default | Purpose |
|---|---|---|
| `KANBAN_HOST` | `0.0.0.0` | Bind address (0.0.0.0 for all interfaces) |
| `KANBAN_PORT` | `7777` | Port |
| `KANBAN_DB` | `/app/kanban_data/tasks.db` | SQLite database path |
| `KANBAN_DEFAULT_PROJECT_ID` | `default` | ID of seed project on first launch |
| `KANBAN_DEFAULT_PROJECT_NAME` | `Default` | Display name of seed project |
| `KANBAN_DEFAULT_PROJECT_COLOR` | `#F10D30` | Accent color of seed project |
| `KANBAN_INBOX_DIR` | `/app/kanban_data/inbox` | Inbox watcher folder |
| `KANBAN_RULES_FILE` | `/app/kanban_data/rules.json` | Automation rules file |
| `KANBAN_WEBHOOKS_FILE` | `/app/kanban_data/webhooks.json` | Outbound webhooks file |
| `KANBAN_AUTOMATION_INTERVAL` | `60` | Rule engine interval (seconds) |
| `KANBAN_EVENTS_INTERVAL` | `1` | Event feed check interval (seconds) |
| `KANBAN_EVENTS_QUIET_ACTORS` | `plan-import,maintenance` | Bulk writers that don't trigger rules/webhooks |
| `KANBAN_RULE_LOOP_LIMIT` | `5` | Max rule firings per card per 10 min |
| `KANBAN_INBOX_INTERVAL` | `5` | Inbox poll interval (seconds) |

### Building from Source

If you want to build the image manually:

```bash
docker build -t agent-kanban .
docker run -p 7777:7777 agent-kanban
```

### Running with Custom Configuration

Create a `.env` file in the project root:

```env
KANBAN_HOST=0.0.0.0
KANBAN_PORT=7777
KANBAN_DEFAULT_PROJECT_NAME=MyProject
KANBAN_DEFAULT_PROJECT_COLOR=#007BFF
```

Then run:

```bash
docker-compose up -d
```

### Data Persistence

All data is stored in the `kanban_data` volume:

- `tasks.db` - SQLite database
- `inbox/` - Watched folder for `.md` files
- `rules.json` - Automation rules
- `webhooks.json` - Webhook configurations
- `logs/` - Rule execution logs

### Updating

To update to a newer version:

```bash
docker-compose pull
docker-compose up -d
```

### Troubleshooting

**Container won't start**
```bash
docker-compose logs
```

**Reset to factory defaults**
```bash
docker-compose down -v
docker-compose up -d
```

**Check database health**
```bash
docker exec -it agent-kanban python -m kanban_store.maintenance doctor
```

## Features

- 9 workflow columns: Backlog → Approved → Analyst → In progress → Testing → UAT → Done, plus Blocked / Cancelled.
- Multiple projects in one DB, URL-routed (`/p/{slug}`), each with optional Claude Code-project directory binding.
- Light/dark theme switcher (`t`), density toggle (`d`), collapsible columns, sidebar.
- Search + filter chips, keyboard shortcuts (`/`, `n`, `r`, `\`, `Esc`).
- Inbox watcher: drop a `.md` into `kanban_data/inbox/`, get a card in 5 sec.
- PLAN.md import (loose-mode parser, one-shot): any `## Section` → cards in backlog with section-name in description.
- Automation rules (`kanban_data/rules.json`): `task_moved`, `task_idle`, `blockers_done`, `task_count_in_status` → move, comment, assign, set priority, archive, run a command.
- **Agent launcher** ([`examples/agent-launcher/`](examples/agent-launcher/)): card lands in Approved → headless Claude Code works it to Testing.
- **Outbound webhooks** (`kanban_data/webhooks.json`): POST to Slack / Telegram / any HTTP endpoint.
- **MCP server** (`kanban_mcp/`) with 16 tools for Claude Code / Cline.
- **One event stream**: every change lands in the task history.
- **REST API + auto-generated OpenAPI** for opencode / Open WebUI / any LLM.
- Archive instead of delete: archived cards leave the board but keep their status.

## AI agent integrations

See [`docs/INTEGRATION.md`](docs/INTEGRATION.md) for detailed integration guides for:
- Claude Code (MCP stdio)
- Cline (MCP stdio / HTTP)
- Cursor (HTTP MCP)
- opencode (REST / OpenAPI)
- Open WebUI (OpenAPI Tool Server)
- Generic LLMs (function-calling + REST)

## License

[MIT](LICENSE)
