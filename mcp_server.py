"""MCP server for email-approval-bot — exposes approval status queries as AI tools."""

import os
from datetime import datetime, timezone, timedelta

from mcp.server.fastmcp import FastMCP

from lib.state_store import StateStore

DB_PATH = os.getenv("DB_PATH", "./state.db")
store = StateStore(DB_PATH)

mcp = FastMCP("email-approval-bot")


@mcp.tool(name="list_pending_requests")
def list_pending_requests() -> str:
    """List all pending approval requests that are waiting for approval."""
    rows = store._conn.execute(
        "SELECT id, subject, pipeline_name, pipeline_counter, created_at "
        "FROM requests WHERE status='pending' ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        return "No pending requests."
    lines = []
    for r in rows:
        lines.append(
            f"- [{r['pipeline_name']}/{r['pipeline_counter']}] {r['subject']}\n"
            f"  ID: {r['id']} | Created: {r['created_at']}"
        )
    return f"Pending requests ({len(rows)}):\n\n" + "\n".join(lines)


@mcp.tool(name="get_request_status")
def get_request_status(request_id: str) -> str:
    """Get detailed status of a specific approval request by its ID."""
    row = store._conn.execute(
        "SELECT * FROM requests WHERE id=?", (request_id,)
    ).fetchone()
    if not row:
        return f"Request '{request_id}' not found."
    return (
        f"Request: {row['id']}\n"
        f"Subject: {row['subject']}\n"
        f"Pipeline: {row['pipeline_name']}/{row['pipeline_counter']}\n"
        f"Status: {row['status']}\n"
        f"Created: {row['created_at']}\n"
        f"Approved by: {row['approved_by'] or 'N/A'}\n"
        f"Approved at: {row['approved_at'] or 'N/A'}\n"
        f"Triggered at: {row['triggered_at'] or 'N/A'}\n"
        f"Executed at: {row['executed_at'] or 'N/A'}\n"
        f"Error: {row['error_message'] or 'None'}"
    )


@mcp.tool(name="list_recent_executions")
def list_recent_executions(hours: int = 24) -> str:
    """List approval requests that were executed or failed in the last N hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    rows = store._conn.execute(
        "SELECT id, subject, pipeline_name, pipeline_counter, status, executed_at, error_message "
        "FROM requests WHERE status IN ('executed', 'failed', 'triggered') "
        "AND (executed_at >= ? OR triggered_at >= ?) ORDER BY executed_at DESC",
        (cutoff, cutoff),
    ).fetchall()
    if not rows:
        return f"No executions in the last {hours} hours."
    lines = []
    for r in rows:
        status_icon = "✅" if r["status"] == "executed" else "❌" if r["status"] == "failed" else "⏳"
        line = f"- {status_icon} [{r['pipeline_name']}/{r['pipeline_counter']}] {r['subject']}"
        if r["error_message"]:
            line += f"\n  Error: {r['error_message']}"
        lines.append(line)
    return f"Recent executions ({len(rows)} in last {hours}h):\n\n" + "\n".join(lines)


@mcp.tool(name="retry_failed_request")
def retry_failed_request(request_id: str) -> str:
    """Retry a failed request by resetting its status to 'approved'."""
    row = store._conn.execute(
        "SELECT status FROM requests WHERE id=?", (request_id,)
    ).fetchone()
    if not row:
        return f"Request '{request_id}' not found."
    if row["status"] != "failed":
        return f"Cannot retry: request status is '{row['status']}', not 'failed'."
    store._conn.execute(
        "UPDATE requests SET status='approved', error_message='', executed_at=NULL WHERE id=?",
        (request_id,),
    )
    store._conn.commit()
    return f"Request '{request_id}' has been reset to 'approved' and will be retried."


@mcp.tool(name="get_bot_statistics")
def get_bot_statistics() -> str:
    """Get overall statistics of the email approval bot."""
    rows = store._conn.execute(
        "SELECT status, COUNT(*) as cnt FROM requests GROUP BY status"
    ).fetchall()
    total = sum(r["cnt"] for r in rows)
    stats = {r["status"]: r["cnt"] for r in rows}
    return (
        f"Bot Statistics:\n"
        f"  Total requests: {total}\n"
        f"  Pending: {stats.get('pending', 0)}\n"
        f"  Approved: {stats.get('approved', 0)}\n"
        f"  Triggered: {stats.get('triggered', 0)}\n"
        f"  Executed: {stats.get('executed', 0)}\n"
        f"  Failed: {stats.get('failed', 0)}\n"
        f"  Timeout: {stats.get('timeout', 0)}\n"
        f"  Manually handled: {stats.get('manually_handled', 0)}"
    )


if __name__ == "__main__":
    mcp.run()
