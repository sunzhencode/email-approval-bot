# CLAUDE.md

## Project Overview

Email Approval Bot monitors a personal Feishu mailbox (via IMAP) for OTR+ data deletion approval requests sent to `otr-devops@inspiregroup.com`. When an approved sender replies with a configured keyword, it automatically triggers the GoCD `es-data-update` pipeline stage via REST API.

## Running

```bash
# Install dependencies
.venv/bin/pip install -r requirements.txt

# Copy and fill in config
cp .env.example .env

# Run
.venv/bin/python main.py
```

Always use `.venv/bin/python` instead of system `python`.

## Architecture

```
main.py               — Polling loop, wires all components
lib/config.py         — Loads .env, Config dataclass
lib/imap_client.py    — IMAP SSL connection, fetches unread emails
lib/email_parser.py   — Parses HTML table, detects approvals, extracts thread IDs
lib/state_store.py    — SQLite persistence (requests table)
lib/gocd_client.py    — GoCD REST API trigger
```

## Workflow

1. Poll IMAP inbox every `POLL_INTERVAL_SECONDS` seconds
2. For each unread email:
   - If it's a request email (has GoCD pipeline URL in HTML table) → save as `pending`
   - If it's an approval reply (sender in whitelist, body matches keyword, In-Reply-To matches a pending request) → mark as `approved`
3. For each `approved` request → call GoCD API → mark as `executed` or `failed`

## GoCD API

`POST /go/api/stages/{pipeline_name}/{pipeline_counter}/{stage_name}/run`

Set `GOCD_DRY_RUN=true` to test URL construction without firing real calls.
