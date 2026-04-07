import logging
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from .config import Config
from .state_store import RequestData

logger = logging.getLogger(__name__)

_ADDR_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _extract_addresses(header: str) -> list[str]:
    """Return all email addresses found in a header string."""
    return [m.lower() for m in _ADDR_RE.findall(header)]


def _reply_all_recipients(config: Config, req: RequestData) -> tuple[list[str], list[str]]:
    """
    Build To / CC lists for a reply-all, excluding the bot's own address.

    Reply-all convention:
      To  = original From  (minus self)
      CC  = original To + original CC (minus self, minus already-in-To)

    Fallback: if To ends up empty (e.g. old DB record with no stored From),
    the approver is used as the sole To recipient.
    """
    me = config.imap_user.lower()

    orig_from = _extract_addresses(req.email_from)
    orig_to   = _extract_addresses(req.email_to)
    orig_cc   = _extract_addresses(req.email_cc)

    to_addrs = [a for a in orig_from if a != me]

    in_to: set[str] = set(to_addrs)
    cc_addrs = [
        a for a in (orig_to + orig_cc)
        if a != me and a not in in_to
    ]

    # Always include the approver
    if req.approved_by:
        approver = req.approved_by.lower()
        if approver != me and approver not in in_to and approver not in cc_addrs:
            if to_addrs:
                cc_addrs.append(approver)
            else:
                to_addrs.append(approver)

    # If To is still empty, promote CC to To
    if not to_addrs and cc_addrs:
        to_addrs, cc_addrs = cc_addrs, []

    return to_addrs, cc_addrs


def _build_reply_body(req: RequestData, latest_thread_html: str = "") -> tuple[str, str]:
    """
    Build (plain_text, html) bodies for the Done reply.
    Prefers the latest thread email (which includes the approver's reply) as the
    quoted block; falls back to the original request email HTML.
    """
    plain = "Done"
    html_reply = "<div>Done</div>"

    quote_html = latest_thread_html or req.approved_email_html or req.email_body_html
    if quote_html:
        plain = "Done\n\n"
        html_reply = (
            "<div>Done</div>"
            "<br>"
            '<blockquote style="margin:0 0 0 .8ex;border-left:1px #ccc solid;padding-left:1ex">'
            f"{quote_html}"
            "</blockquote>"
        )

    return plain, html_reply


def send_done_reply(config: Config, req: RequestData, latest_thread_html: str = "") -> None:
    """Reply-all 'Done' once the GoCD stage passes."""
    if not config.smtp_host:
        return

    to_addrs, cc_addrs = _reply_all_recipients(config, req)
    all_recipients = to_addrs + cc_addrs
    if not all_recipients:
        logger.warning("[Reply] No recipients resolved for request %s", req.id)
        return

    subject = req.subject if req.subject.startswith("Re:") else f"Re: {req.subject}"

    plain_body, html_body = _build_reply_body(req, latest_thread_html)

    msg = MIMEMultipart("alternative")
    msg["From"] = config.imap_user
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["CC"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    if req.email_message_id:
        bracketed = (f"<{req.email_message_id}>"
                     if not req.email_message_id.startswith("<")
                     else req.email_message_id)
        msg["In-Reply-To"] = bracketed
        msg["References"] = bracketed

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    if config.gocd_dry_run:
        logger.info("[DRY RUN] Would send Done reply  to=%s  cc=%s  subject=%r",
                    to_addrs, cc_addrs, subject)
        return

    try:
        with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=15) as smtp:
            smtp.login(config.imap_user, config.imap_password)
            smtp.sendmail(config.imap_user, all_recipients, msg.as_bytes())
        logger.info("[Reply] Done sent  to=%s  cc=%s  subject=%r", to_addrs, cc_addrs, subject)
    except Exception as e:
        logger.error("[Reply] Failed to send Done reply: %s", e)
