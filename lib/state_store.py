import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


@dataclass
class RequestData:
    id: str               # unique: email_message_id::pipeline_name::pipeline_counter
    email_message_id: str # original email Message-ID (for thread correlation)
    subject: str
    pipeline_url: str
    pipeline_name: str
    pipeline_counter: str
    pr_url: str
    issue_number: str
    execute_stage: str = ""          # resolved at save time from config stage map
    status: str = "pending"
    # pending | approved | triggered | executed | failed | manually_handled | timeout
    approved_by: str = ""
    created_at: str = ""
    approved_at: str = ""
    triggered_at: str = ""
    executed_at: str = ""
    error_message: str = ""
    # original email recipients — used for reply-all on completion
    email_from: str = ""
    email_to: str = ""
    email_cc: str = ""
    email_body_html: str = ""
    approved_email_html: str = ""  # HTML body of the approver's reply email


def make_request_id(email_message_id: str, pipeline_name: str, pipeline_counter: str) -> str:
    return f"{email_message_id}::{pipeline_name}::{pipeline_counter}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, db_path: str):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        self._migrate()

    def _migrate(self):
        """Apply schema migrations for columns added after initial release."""
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(requests)").fetchall()
        }
        if "execute_stage" not in existing:
            self._conn.execute(
                "ALTER TABLE requests ADD COLUMN execute_stage TEXT NOT NULL DEFAULT ''"
            )
        for col in ("email_from", "email_to", "email_cc", "email_body_html", "approved_email_html"):
            if col not in existing:
                self._conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        self._conn.commit()

    def _create_tables(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS requests (
                id                TEXT PRIMARY KEY,
                email_message_id  TEXT NOT NULL,
                subject           TEXT,
                pipeline_url      TEXT NOT NULL,
                pipeline_name     TEXT NOT NULL,
                pipeline_counter  TEXT NOT NULL,
                pr_url            TEXT,
                issue_number      TEXT,
                execute_stage     TEXT NOT NULL DEFAULT '',
                status            TEXT NOT NULL DEFAULT 'pending',
                -- pending | approved | triggered | executed | failed | manually_handled | timeout
                triggered_at      TEXT,
                approved_by       TEXT,
                created_at        TEXT NOT NULL,
                approved_at       TEXT,
                executed_at       TEXT,
                error_message     TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_requests_email_message_id
                ON requests(email_message_id);

            CREATE TABLE IF NOT EXISTS processed_emails (
                message_id  TEXT PRIMARY KEY,
                handled_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        self._conn.commit()

    # ── Request CRUD ──────────────────────────────────────────────────────────

    def save_request(self, data: RequestData) -> bool:
        """
        Save a new pending request. Returns False (and skips) if an active request
        (pending/approved/triggered) already exists for the same pipeline counter,
        regardless of which email it came from.
        """
        existing = self._conn.execute(
            "SELECT 1 FROM requests WHERE pipeline_name=? AND pipeline_counter=?"
            " AND status IN ('pending', 'approved', 'triggered')",
            (data.pipeline_name, data.pipeline_counter),
        ).fetchone()
        if existing:
            return False
        self._conn.execute("""
            INSERT OR IGNORE INTO requests
            (id, email_message_id, subject, pipeline_url, pipeline_name,
             pipeline_counter, pr_url, issue_number, execute_stage, status, created_at,
             email_from, email_to, email_cc, email_body_html)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)
        """, (data.id, data.email_message_id, data.subject, data.pipeline_url,
              data.pipeline_name, data.pipeline_counter,
              data.pr_url, data.issue_number, data.execute_stage, _now(),
              data.email_from, data.email_to, data.email_cc, data.email_body_html))
        self._conn.commit()
        return True

    def mark_approved(self, request_id: str, approved_by: str, approved_email_html: str = ""):
        self._conn.execute("""
            UPDATE requests SET status='approved', approved_by=?, approved_at=?, approved_email_html=?
            WHERE id=? AND status='pending'
        """, (approved_by, _now(), approved_email_html, request_id))
        self._conn.commit()

    def mark_manually_handled(self, request_id: str):
        self._conn.execute("""
            UPDATE requests SET status='manually_handled', executed_at=?
            WHERE id=? AND status IN ('pending', 'approved')
        """, (_now(), request_id))
        self._conn.commit()

    def mark_triggered(self, request_id: str):
        """GoCD API call succeeded; waiting to confirm stage result."""
        self._conn.execute(
            "UPDATE requests SET status='triggered', triggered_at=? WHERE id=?",
            (_now(), request_id),
        )
        self._conn.commit()

    def mark_executed(self, request_id: str):
        """GoCD stage completed successfully (Passed)."""
        self._conn.execute(
            "UPDATE requests SET status='executed', executed_at=? WHERE id=?",
            (_now(), request_id),
        )
        self._conn.commit()

    def mark_failed(self, request_id: str, error: str):
        self._conn.execute(
            "UPDATE requests SET status='failed', error_message=?, executed_at=? WHERE id=?",
            (error, _now(), request_id),
        )
        self._conn.commit()

    def mark_timeout(self, request_id: str):
        self._conn.execute(
            "UPDATE requests SET status='timeout', executed_at=? WHERE id=?",
            (_now(), request_id),
        )
        self._conn.commit()

    def get_triggered_requests(self, timeout_minutes: int) -> tuple[list, list]:
        """
        Returns (still_running, timed_out) based on how long ago they were triggered.
        """
        from datetime import datetime, timezone, timedelta
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE status='triggered'"
        ).fetchall()
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=timeout_minutes)
        still_running, timed_out = [], []
        for row in rows:
            req = _row_to_data(row)
            triggered_at_raw = row["triggered_at"]
            if triggered_at_raw:
                triggered_at = datetime.fromisoformat(triggered_at_raw)
                if triggered_at.tzinfo is None:
                    triggered_at = triggered_at.replace(tzinfo=timezone.utc)
            else:
                triggered_at = datetime.now(timezone.utc)
            if triggered_at < cutoff:
                timed_out.append(req)
            else:
                still_running.append(req)
        return still_running, timed_out

    def has_any_running_pipeline(self) -> bool:
        """Returns True if any pipeline is currently triggered (running). All pipelines are serial."""
        return self._conn.execute(
            "SELECT 1 FROM requests WHERE status='triggered'",
        ).fetchone() is not None

    def get_pending_by_thread(self, thread_ids: list[str]) -> list[RequestData]:
        """All pending requests whose original email is in the thread."""
        if not thread_ids:
            return []
        placeholders = ",".join("?" * len(thread_ids))
        rows = self._conn.execute(
            f"SELECT * FROM requests WHERE email_message_id IN ({placeholders})"
            f" AND status='pending'",
            thread_ids,
        ).fetchall()
        return [_row_to_data(r) for r in rows]

    def get_actionable_by_thread(self, thread_ids: list[str]) -> list[RequestData]:
        """Pending or approved requests — used for done-reply detection."""
        if not thread_ids:
            return []
        placeholders = ",".join("?" * len(thread_ids))
        rows = self._conn.execute(
            f"SELECT * FROM requests WHERE email_message_id IN ({placeholders})"
            f" AND status IN ('pending', 'approved')",
            thread_ids,
        ).fetchall()
        return [_row_to_data(r) for r in rows]

    def get_requests_by_email_message_id(self, email_message_id: str) -> list[RequestData]:
        """All requests originating from the same email."""
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE email_message_id=?",
            (email_message_id,),
        ).fetchall()
        return [_row_to_data(r) for r in rows]

    def get_approved_requests(self) -> list[RequestData]:
        rows = self._conn.execute(
            "SELECT * FROM requests WHERE status='approved' ORDER BY approved_at ASC"
        ).fetchall()
        return [_row_to_data(r) for r in rows]

    # ── Processed-email deduplication ─────────────────────────────────────────

    def is_email_processed(self, message_id: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM processed_emails WHERE message_id=?", (message_id,)
        ).fetchone() is not None

    def mark_email_processed(self, message_id: str):
        self._conn.execute(
            "INSERT OR IGNORE INTO processed_emails (message_id, handled_at) VALUES (?, ?)",
            (message_id, _now()),
        )
        self._conn.commit()

    # ── Last-check date ────────────────────────────────────────────────────────

    def get_last_uid(self) -> int:
        """Return the highest IMAP UID processed so far, or 0 on first run."""
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key='last_uid'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def update_last_uid(self, uid: int):
        self._conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('last_uid', ?)",
            (str(uid),),
        )
        self._conn.commit()

    def get_lookback_date(self, lookback_days: int) -> datetime:
        """Fallback date used only on first run (last_uid == 0)."""
        return datetime.now(timezone.utc) - timedelta(days=lookback_days)

    def close(self):
        self._conn.close()


def _row_to_data(row: sqlite3.Row) -> RequestData:
    return RequestData(
        id=row["id"],
        email_message_id=row["email_message_id"],
        subject=row["subject"] or "",
        pipeline_url=row["pipeline_url"],
        pipeline_name=row["pipeline_name"],
        pipeline_counter=row["pipeline_counter"],
        pr_url=row["pr_url"] or "",
        issue_number=row["issue_number"] or "",
        execute_stage=row["execute_stage"] or "",
        status=row["status"],
        approved_by=row["approved_by"] or "",
        created_at=row["created_at"],
        approved_at=row["approved_at"] or "",
        triggered_at=row["triggered_at"] or "",
        executed_at=row["executed_at"] or "",
        error_message=row["error_message"] or "",
        email_from=row["email_from"] or "",
        email_to=row["email_to"] or "",
        email_cc=row["email_cc"] or "",
        email_body_html=row["email_body_html"] or "",
        approved_email_html=row["approved_email_html"] or "",
    )
