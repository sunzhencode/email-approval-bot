import logging
import logging.handlers
import os
import time

from lib.config import load_config
from lib.imap_client import ImapClient
from lib.email_parser import (parse_request_emails, is_approval_email, is_done_email,
                              get_thread_ids, parse_inline_request, has_deferred_execution_hint,
                              _strip_text_quotes, _html_to_text_no_quotes)
from lib.state_store import StateStore
from lib import gocd_client
from lib import feishu_notifier
from lib import smtp_client


def _setup_logging(log_dir: str = "logs") -> None:
    os.makedirs(log_dir, exist_ok=True)
    fmt = logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Console — same as before
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # Rotating file — 10 MB per file, keep 5
    fh = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "bot.log"),
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)


logger = logging.getLogger(__name__)


_TERMINAL_STATUSES = frozenset({"executed", "failed", "timeout", "manually_handled"})


def _maybe_send_done_reply(config, store: StateStore, imap: ImapClient, req) -> None:
    """Send the Done reply email, but only when all pipelines from the same
    source email have reached a terminal state (for multi-pipeline emails)."""
    siblings = store.get_requests_by_email_message_id(req.email_message_id)
    if len(siblings) > 1:
        if not all(s.status in _TERMINAL_STATUSES for s in siblings):
            return  # still waiting for other pipelines
    latest_html = imap.fetch_latest_in_thread(req.email_message_id)
    smtp_client.send_done_reply(config, req, latest_html)


def poll_once(imap: ImapClient, store: StateStore, config) -> None:
    last_uid = store.get_last_uid()
    since = store.get_lookback_date(config.lookback_days)
    emails, max_uid = imap.fetch_new_emails(last_uid, since)

    # ── Email processing ──────────────────────────────────────────────────────
    n_saved = n_approved = n_deferred = n_manual = 0

    if emails:
        emails.sort(key=lambda e: int(e.uid) if e.uid.isdigit() else 0)
        new_emails = [e for e in emails if e.message_id and not store.is_email_processed(e.message_id)]
        my_email = config.imap_user.lower()

        if last_uid == 0:
            logger.info("[Poll] First run — %d email(s) since %s (lookback %dd)",
                        len(emails), since.date(), config.lookback_days)
        elif new_emails:
            logger.info("[Poll] %d new email(s)", len(new_emails))

        for raw in new_emails:
            # 1. Done reply — any sender; skip regardless of DB state
            if is_done_email(raw, config, my_email):
                thread_ids = get_thread_ids(raw)
                targets = store.get_actionable_by_thread(thread_ids)
                for req in targets:
                    store.mark_manually_handled(req.id)
                    n_manual += 1
                    logger.info("[Manual] %s/%s  issue=%s",
                                req.pipeline_name, req.pipeline_counter, req.issue_number or "-")
                store.mark_email_processed(raw.message_id)
                continue

            # 2. Approval reply — checked before request parsing so that a reply from
            #    an approved sender (which may quote the original CI table in HTML)
            #    is not misidentified as a new CI request.
            approved, sender = is_approval_email(raw, config)
            if approved:
                thread_ids = get_thread_ids(raw)
                pending = store.get_pending_by_thread(thread_ids)
                if pending:
                    for req in pending:
                        store.mark_approved(req.id, sender, raw.html_body)
                        n_approved += 1
                        logger.info("[Approved] %s/%s  issue=%s  by=%s",
                                    req.pipeline_name, req.pipeline_counter,
                                    req.issue_number or "-", sender)
                else:
                    logger.warning("[Approval?] %s approved but no matching pending request", sender)
                store.mark_email_processed(raw.message_id)
                continue

            # 3. CI request (table + inline, merged)
            requests = parse_request_emails(raw, config.target_email)
            inline_requests = parse_inline_request(raw, config)
            existing_counters = {(r.pipeline_name, r.pipeline_counter) for r in requests}
            for r in inline_requests:
                if (r.pipeline_name, r.pipeline_counter) not in existing_counters:
                    requests.append(r)

            if requests:
                unquoted = _strip_text_quotes(raw.text_body) + " " + _html_to_text_no_quotes(raw.html_body)
                deferred_kw = has_deferred_execution_hint(unquoted)
                if deferred_kw:
                    n_deferred += 1
                    logger.warning("[Deferred] Manual review required — keyword=%r  subject=%r",
                                   deferred_kw, raw.subject)
                    store.mark_email_processed(raw.message_id)
                    continue

                for req in requests:
                    req.execute_stage = config.get_execute_stage(req.pipeline_name)
                    if not req.execute_stage:
                        logger.warning("[Ignored] Pipeline not in GOCD_STAGE_MAP: %s/%s",
                                       req.pipeline_name, req.pipeline_counter)
                        continue
                    saved = store.save_request(req)
                    if saved:
                        n_saved += 1
                        logger.info("[+Request] %s/%s  issue=%s  stage=%s  pr=%s",
                                    req.pipeline_name, req.pipeline_counter,
                                    req.issue_number or "-", req.execute_stage,
                                    req.pr_url or "-")
                store.mark_email_processed(raw.message_id)
                continue

            # Unrecognised — mark processed silently
            store.mark_email_processed(raw.message_id)

        if max_uid > last_uid:
            store.update_last_uid(max_uid)

        if any([n_saved, n_approved, n_deferred, n_manual]):
            logger.info("[Poll summary] saved=%d  approved=%d  deferred=%d  manual=%d",
                        n_saved, n_approved, n_deferred, n_manual)

    # ── Trigger approved requests ─────────────────────────────────────────────
    # All pipelines are serial — only one can run at a time across all pipeline names.
    running_list, _ = store.get_triggered_requests(config.gocd_status_timeout_minutes)
    if running_list:
        current = running_list[0]
        approved_queue = store.get_approved_requests()
        if approved_queue:
            logger.info("[Running] %s/%s  (%d queued)",
                        current.pipeline_name, current.pipeline_counter, len(approved_queue))
    else:
        approved_queue = store.get_approved_requests()

    for req in ([] if running_list else approved_queue):
        if store.has_any_running_pipeline():
            break  # triggered by a previous iteration in this same loop
        # Check if the stage already completed before we trigger it.
        pre_result = gocd_client.get_stage_result(req.pipeline_name, req.pipeline_counter,
                                                   req.execute_stage, config)
        if pre_result == gocd_client.RESULT_PASSED:
            store.mark_executed(req.id)
            logger.info("[AlreadyDone] %s/%s  issue=%s — stage already Passed, skipping trigger",
                        req.pipeline_name, req.pipeline_counter, req.issue_number or "-")
            feishu_notifier.notify_executed(config.feishu_webhook_url, req)
            _maybe_send_done_reply(config, store, imap, req)
            continue
        if pre_result in (gocd_client.RESULT_FAILED, gocd_client.RESULT_CANCELLED):
            store.mark_failed(req.id, f"GoCD stage already {pre_result} before trigger")
            logger.warning("[AlreadyFailed] %s/%s  issue=%s — stage already %s, skipping trigger",
                           req.pipeline_name, req.pipeline_counter, req.issue_number or "-", pre_result)
            feishu_notifier.notify_failed(config.feishu_webhook_url, req,
                                          f"GoCD stage already {pre_result} before trigger")
            continue
        try:
            gocd_client.trigger_stage(req.pipeline_name, req.pipeline_counter,
                                      req.execute_stage, config)
            store.mark_triggered(req.id)
            logger.info("[Triggered] %s/%s  issue=%s  stage=%s  approved_by=%s",
                        req.pipeline_name, req.pipeline_counter,
                        req.issue_number or "-", req.execute_stage, req.approved_by)
        except Exception as e:
            store.mark_failed(req.id, str(e))
            logger.error("[TriggerFailed] %s/%s — %s", req.pipeline_name, req.pipeline_counter, e)
            feishu_notifier.notify_failed(config.feishu_webhook_url, req, str(e))

    # ── Check triggered requests ──────────────────────────────────────────────
    running, timed_out = store.get_triggered_requests(config.gocd_status_timeout_minutes)

    for req in timed_out:
        store.mark_timeout(req.id)
        logger.warning("[Timeout] %s/%s  running >%dm with no result",
                       req.pipeline_name, req.pipeline_counter, config.gocd_status_timeout_minutes)
        feishu_notifier.notify_failed(config.feishu_webhook_url, req,
                                      f"Timed out after {config.gocd_status_timeout_minutes} minutes")

    for req in running:
        result = gocd_client.get_stage_result(req.pipeline_name, req.pipeline_counter,
                                               req.execute_stage, config)
        if result == gocd_client.RESULT_PASSED:
            store.mark_executed(req.id)
            logger.info("[✓ Passed] %s/%s  issue=%s", req.pipeline_name, req.pipeline_counter,
                        req.issue_number or "-")
            feishu_notifier.notify_executed(config.feishu_webhook_url, req)
            _maybe_send_done_reply(config, store, imap, req)
        elif result in (gocd_client.RESULT_FAILED, gocd_client.RESULT_CANCELLED):
            store.mark_failed(req.id, f"GoCD stage result: {result}")
            logger.error("[✗ %s] %s/%s  issue=%s", result, req.pipeline_name, req.pipeline_counter,
                         req.issue_number or "-")
            feishu_notifier.notify_failed(config.feishu_webhook_url, req, f"GoCD stage result: {result}")
        else:
            logger.debug("[Running] %s/%s  still in progress", req.pipeline_name, req.pipeline_counter)


def main():
    _setup_logging()
    config = load_config()
    store = StateStore(config.db_path)
    imap = ImapClient(config)

    logger.info("Bot starting  imap=%s  approvers=%s  interval=%ds  dry_run=%s",
                config.imap_user,
                ",".join(config.approved_senders) or "(none)",
                config.poll_interval,
                config.gocd_dry_run)

    try:
        while True:
            try:
                poll_once(imap, store, config)
            except Exception as e:
                logger.error("Poll cycle error: %s", e)
            time.sleep(config.poll_interval)
    finally:
        imap.close()
        store.close()


if __name__ == "__main__":
    main()
