# Email Approval Bot

A lightweight Python daemon that monitors a Feishu (Lark) mailbox for data-operation approval requests and automatically triggers GoCD pipeline stages when an authorized reviewer replies with an approval keyword.

## How it works

1. **Poll** — The bot polls your IMAP inbox every `POLL_INTERVAL_SECONDS` seconds.
2. **Detect** — Incoming emails that contain a GoCD pipeline URL are saved as `pending` requests.
3. **Approve** — When an authorized sender replies to one of those threads with an approval keyword (e.g. `approve`, `lgtm`, `同意`), the request is marked `approved`.
4. **Trigger** — The bot calls the GoCD REST API to run the configured pipeline stage, then marks the request `executed`.
5. **Notify** — Optionally sends a Feishu group-bot message and an SMTP "Done" reply email.

```
IMAP inbox ──► email_parser ──► state_store (SQLite)
                                      │
                              approved requests
                                      │
                              gocd_client (REST API)
                                      │
                         feishu_notifier + smtp_client
```

## Requirements

- Python 3.11+
- A Feishu/Lark mailbox with IMAP enabled and an app password
- GoCD API token with permission to trigger pipeline stages

## Setup

### 1. Clone and install

```bash
git clone git@github.com:sunzhencode/email-approval-bot.git
cd email-approval-bot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Open `.env` and fill in your credentials. The key variables are:

| Variable | Description |
|---|---|
| `IMAP_HOST` / `IMAP_USER` / `IMAP_PASSWORD` | Feishu IMAP credentials |
| `APPROVED_SENDERS` | Comma-separated list of email addresses allowed to approve |
| `APPROVAL_KEYWORDS` | Keywords that count as approval (default: `approve,lgtm,同意,ok,yes,+1`) |
| `GOCD_BASE_URL` | Base URL of your GoCD server |
| `GOCD_TOKEN` | GoCD personal access token |
| `GOCD_STAGE_MAP` | `PIPELINE_NAME:stage-name` pairs (comma-separated) |
| `FEISHU_WEBHOOK_URL` | *(Optional)* Feishu group-bot webhook for notifications |
| `SMTP_HOST` | *(Optional)* SMTP host for "Done" reply emails (leave empty to disable) |
| `GOCD_DRY_RUN` | Set to `true` to log API calls without actually triggering anything |

See [`.env.example`](.env.example) for the full list of options with descriptions.

### 3. Run

```bash
./start.sh
```

`start.sh` will create the virtual environment, install dependencies, and start the bot. Logs are written to `logs/bot.log` (rotating, 10 MB × 5 files).

Or run directly:

```bash
.venv/bin/python main.py
```

## MCP Server (optional)

An [MCP](https://modelcontextprotocol.io) server is included so AI assistants can query approval status:

```bash
.venv/bin/python mcp_server.py
```

Available tools:

| Tool | Description |
|---|---|
| `list_pending_requests` | Lists all requests waiting for approval |
| `get_request_status` | Gets full details of a request by ID |
| `list_recent_requests` | Lists recent requests filtered by status |
| `get_statistics` | Summary counts by status |

To use it with VS Code Copilot, add an entry to your MCP config pointing to the server's stdio command.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `60` | Seconds between inbox polls |
| `LOOKBACK_DAYS` | `3` | Days to look back on first startup |
| `GOCD_DEFAULT_STAGE` | *(empty)* | Fallback stage if pipeline not in `GOCD_STAGE_MAP` |
| `GOCD_STATUS_TIMEOUT_MINUTES` | `60` | Minutes before a triggered stage is marked `timeout` |
| `SKIP_KEYWORDS` | `done,已完成,手动执行,manual` | If you reply with one of these, the request is marked `manually_handled` |
| `DB_PATH` | `./state.db` | SQLite database path |
| `SMTP_PORT` | `465` | SMTP port (SSL) |

> [!NOTE]
> `IMAP_USER` and `IMAP_PASSWORD` are reused for SMTP authentication when sending Done replies.

## Project structure

```
main.py               — Polling loop, wires all components
mcp_server.py         — Optional MCP server for AI tool access
lib/
  config.py           — .env loader, Config dataclass
  imap_client.py      — IMAP SSL connection, fetches unread emails
  email_parser.py     — HTML table parsing, approval detection, thread matching
  state_store.py      — SQLite persistence (requests table)
  gocd_client.py      — GoCD REST API trigger + status polling
  feishu_notifier.py  — Feishu group-bot webhook notifications
  smtp_client.py      — SMTP "Done" reply emails
```
