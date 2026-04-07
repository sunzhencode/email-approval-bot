import imaplib
import email
import logging
from dataclasses import dataclass
from datetime import datetime
from email.header import decode_header
from email.message import Message

from .config import Config

logger = logging.getLogger(__name__)


@dataclass
class RawEmail:
    uid: str
    message_id: str
    subject: str
    from_addr: str
    to_addrs: str
    cc_addrs: str
    in_reply_to: str
    references: str
    html_body: str
    text_body: str


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)


def _extract_bodies(msg: Message) -> tuple[str, str]:
    html_body = ""
    text_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html" and not html_body:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                html_body = payload.decode(charset, errors="replace")
            elif ctype == "text/plain" and not text_body:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                text_body = payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or "utf-8"
        body = payload.decode(charset, errors="replace") if payload else ""
        if msg.get_content_type() == "text/html":
            html_body = body
        else:
            text_body = body
    return html_body, text_body


class ImapClient:
    def __init__(self, config: Config):
        self._config = config
        self._conn: imaplib.IMAP4_SSL | None = None

    def _connect(self):
        self._conn = imaplib.IMAP4_SSL(self._config.imap_host, self._config.imap_port)
        self._conn.login(self._config.imap_user, self._config.imap_password)
        self._conn.select("INBOX")
        logger.info("IMAP connected to %s", self._config.imap_host)

    def _ensure_connected(self):
        try:
            if self._conn is None:
                self._connect()
                return
            self._conn.noop()
        except Exception:
            logger.warning("IMAP connection lost, reconnecting...")
            self._connect()

    def fetch_new_emails(self, last_uid: int, since: datetime) -> tuple[list[RawEmail], int]:
        """
        Fetch emails newer than last_uid.

        On first run (last_uid=0), falls back to SINCE {date} to load historical emails.
        Returns (emails, max_uid_seen) so the caller can persist the high-water mark.

        Does NOT modify read/unread state in the mailbox.
        """
        self._ensure_connected()

        if last_uid > 0:
            # Normal case: only fetch emails with UID strictly greater than last seen
            search_criterion = f"UID {last_uid + 1}:*"
            _, data = self._conn.uid("search", None, search_criterion)
        else:
            # First run: use SINCE as a date-level filter for historical backfill
            date_str = since.strftime("%d-%b-%Y")
            _, data = self._conn.uid("search", None, f"SINCE {date_str}")

        uid_list = data[0].split()
        if not uid_list:
            return [], last_uid

        results = []
        max_uid = last_uid

        for uid_bytes in uid_list:
            uid_str = uid_bytes.decode()
            uid_int = int(uid_str)

            # Guard: IMAP UID {N}:* always includes N even if no newer mail exists
            if last_uid > 0 and uid_int <= last_uid:
                continue

            try:
                _, msg_data = self._conn.uid("fetch", uid_bytes, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                html_body, text_body = _extract_bodies(msg)

                results.append(RawEmail(
                    uid=uid_str,
                    message_id=_decode_header_value(msg.get("Message-ID", "")).strip("<>"),
                    subject=_decode_header_value(msg.get("Subject", "")),
                    from_addr=_decode_header_value(msg.get("From", "")),
                    to_addrs=_decode_header_value(msg.get("To", "")),
                    cc_addrs=_decode_header_value(msg.get("CC", "")),
                    in_reply_to=_decode_header_value(msg.get("In-Reply-To", "")).strip("<>"),
                    references=_decode_header_value(msg.get("References", "")),
                    html_body=html_body,
                    text_body=text_body,
                ))
                max_uid = max(max_uid, uid_int)
            except Exception as e:
                logger.error("Failed to fetch email uid=%s: %s", uid_str, e)

        return results, max_uid

    def fetch_latest_in_thread(self, original_message_id: str) -> str:
        """
        Return the HTML body of the most-recent email in the thread rooted at
        original_message_id.  Searches for any email whose In-Reply-To or
        References header contains that ID.  Falls back to empty string on any
        error so callers can safely ignore failures.
        """
        self._ensure_connected()
        mid = original_message_id.strip("<>")
        if not mid:
            return ""
        bracketed = f"<{mid}>"

        uid_sets: list[bytes] = []
        for criterion in (
            f'HEADER In-Reply-To "{bracketed}"',
            f'HEADER References "{bracketed}"',
        ):
            try:
                _, data = self._conn.uid("search", None, criterion)
                if data and data[0]:
                    uid_sets.extend(data[0].split())
            except Exception as e:
                logger.debug("fetch_latest_in_thread search failed criterion=%r: %s", criterion, e)

        if not uid_sets:
            return ""

        # Iterate from highest UID downward, skip emails sent by the bot itself
        for uid in sorted(set(uid_sets), key=lambda u: int(u), reverse=True):
            try:
                _, msg_data = self._conn.uid("fetch", uid, "(RFC822.HEADER)")
                raw_headers = msg_data[0][1]
                hdr = email.message_from_bytes(raw_headers)
                from_addr = _decode_header_value(hdr.get("From", "")).lower()
                if self._config.imap_user.lower() in from_addr:
                    logger.debug("fetch_latest_in_thread skipping bot's own email uid=%s", uid)
                    continue
                # Found a non-bot email — fetch full body
                _, msg_data = self._conn.uid("fetch", uid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)
                html_body, _ = _extract_bodies(msg)
                return html_body
            except Exception as e:
                logger.warning("fetch_latest_in_thread failed uid=%s: %s", uid, e)
                continue
        return ""

    def close(self):
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
