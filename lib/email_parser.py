import re
import logging

from bs4 import BeautifulSoup

from .config import Config
from .imap_client import RawEmail
from .state_store import RequestData, make_request_id

logger = logging.getLogger(__name__)

# Matches: https://ci.otr.mercedes-benz.com.cn/go/pipelines/{name}/{counter}/{stage}/{run}
PIPELINE_URL_RE = re.compile(
    r"^https://ci\.otr\.mercedes-benz\.com\.cn/go/pipelines/([\w-]+)/(\d+)/([\w-]+)/(\d+)$"
)

# Non-anchored variant for scanning free-form plain text
PIPELINE_URL_RE_INLINE = re.compile(
    r"https://ci\.otr\.mercedes-benz\.com\.cn/go/pipelines/([\w-]+)/(\d+)/([\w-]+)/(\d+)"
)

# Keywords indicating deferred/scheduled execution — bot should NOT auto-trigger.
# If any of these appear in the unquoted email body, require manual handling.
_DEFERRED_KEYWORDS = [
    "下班后", "下班时", "明天", "后天", "大后天",
    "指定时间", "维护窗口", "窗口期", "维护时间",
    "凌晨", "夜间", "深夜", "晚上执行", "夜里",
    "周末", "节假日", "假期后", "节后",
    "等通知", "等确认", "通知后", "确认后", "待定",
    "scheduled", "after hours", "maintenance window", "off hours",
    "release", "hotfix",
]


def has_deferred_execution_hint(text: str) -> str:
    """
    Returns the matched keyword if the text contains a deferred-execution hint,
    otherwise returns empty string.
    """
    lower = text.lower()
    for kw in _DEFERRED_KEYWORDS:
        if kw.lower() in lower:
            return kw
    return ""

# Required columns that identify a CI request table.
# Both must be present (as substrings of normalized column headers).
REQUIRED_COLS = ("问题单", ("pipline", "pipeline"))


def _extract_pipeline_info(url: str) -> tuple[str, str] | None:
    m = PIPELINE_URL_RE.match(url.strip())
    if not m:
        return None
    return m.group(1), m.group(2)


def _normalize_col(text: str) -> str:
    return text.strip().lower().replace("\xa0", " ").replace("\u00a0", " ")


def _is_ci_request_table(col_map: dict[str, int]) -> bool:
    """
    Returns True only if the table header looks like a CI request table.
    Must have 问题单 AND a pipeline column.
    """
    has_issue = any("问题单" in k for k in col_map)
    has_pipeline = any("pipline" in k or "pipeline" in k for k in col_map)
    return has_issue and has_pipeline


def parse_request_emails(raw: RawEmail) -> list[RequestData]:
    """
    Parse all CI pipeline requests from a single email.
    Returns one RequestData per valid table row (email may contain multiple CI links).
    Returns empty list if the email is not a CI request email.
    """
    # Must be addressed to (or CC'd) the group
    combined_recipients = (raw.to_addrs + " " + raw.cc_addrs).lower()
    if "otr-devops@inspiregroup.com" not in combined_recipients:
        return []

    if not raw.html_body:
        return []

    soup = BeautifulSoup(raw.html_body, "html.parser")

    results = []

    # An email can in theory have multiple tables; scan all of them
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        # Build column index map from header row
        header_cells = rows[0].find_all(["th", "td"])
        col_map: dict[str, int] = {
            _normalize_col(cell.get_text()): i
            for i, cell in enumerate(header_cells)
        }

        # Strict check: only process tables that look like CI request tables
        if not _is_ci_request_table(col_map):
            continue

        # Locate column indices
        pipeline_col = next(
            (col_map[k] for k in col_map if "pipline" in k or "pipeline" in k), None
        )
        pr_col = col_map.get("pr")
        issue_col = next((col_map[k] for k in col_map if "问题单" in k), None)

        if pipeline_col is None:
            continue

        # Process every data row — each valid row is a separate CI request
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= pipeline_col:
                continue

            link = cells[pipeline_col].find("a")
            if not link:
                continue
            pipeline_url = link.get("href", "").strip()

            info = _extract_pipeline_info(pipeline_url)
            if not info:
                logger.debug("Row skipped — pipeline URL invalid: %s", pipeline_url)
                continue

            pipeline_name, pipeline_counter = info

            pr_url = ""
            if pr_col is not None and len(cells) > pr_col:
                a = cells[pr_col].find("a")
                if a:
                    pr_url = a.get("href", "").strip()

            issue_number = ""
            if issue_col is not None and len(cells) > issue_col:
                issue_number = cells[issue_col].get_text(strip=True)

            results.append(RequestData(
                id=make_request_id(raw.message_id, pipeline_name, pipeline_counter),
                email_message_id=raw.message_id,
                subject=raw.subject,
                pipeline_url=pipeline_url,
                pipeline_name=pipeline_name,
                pipeline_counter=pipeline_counter,
                pr_url=pr_url,
                issue_number=issue_number,
                email_from=raw.from_addr,
                email_to=raw.to_addrs,
                email_cc=raw.cc_addrs,
                email_body_html=raw.html_body,
            ))

    return results


def parse_inline_request(raw: RawEmail, config: Config) -> list[RequestData]:
    """
    Parse CI pipeline requests from plain-text inline reply emails.
    Used for supplementary requests where the operator pastes a pipeline URL
    in free-form text rather than an HTML table.
    """
    combined_recipients = (raw.to_addrs + " " + raw.cc_addrs).lower()
    if config.target_email.lower() not in combined_recipients:
        return []

    # Approvers send approvals, not requests — never treat their emails as new requests
    sender = _extract_email_address(raw.from_addr)
    if sender in config.approved_senders:
        return []

    # Scan both plain text and HTML-derived text, stripping quoted/forwarded content
    # to avoid re-registering pipeline URLs that only appear in reply quotes.
    full_text = _strip_text_quotes(raw.text_body) + " " + _html_to_text_no_quotes(raw.html_body)

    # Extract issue number from patterns like 工单号：CS0903906 or 工单号:CS0903906
    issue_match = re.search(r"工单号[：:]\s*(\S+)", full_text)
    issue_number = issue_match.group(1).strip() if issue_match else ""

    results = []
    seen_urls: set[str] = set()

    for m in PIPELINE_URL_RE_INLINE.finditer(full_text):
        pipeline_url = m.group(0)
        if pipeline_url in seen_urls:
            continue
        seen_urls.add(pipeline_url)

        pipeline_name = m.group(1)
        pipeline_counter = m.group(2)

        results.append(RequestData(
            id=make_request_id(raw.message_id, pipeline_name, pipeline_counter),
            email_message_id=raw.message_id,
            subject=raw.subject,
            pipeline_url=pipeline_url,
            pipeline_name=pipeline_name,
            pipeline_counter=pipeline_counter,
            pr_url="",
            issue_number=issue_number,
            email_from=raw.from_addr,
            email_to=raw.to_addrs,
            email_cc=raw.cc_addrs,
            email_body_html=raw.html_body,
        ))

    return results


def is_approval_email(raw: RawEmail, config: Config) -> tuple[bool, str]:
    sender = _extract_email_address(raw.from_addr)
    if sender not in config.approved_senders:
        return False, ""
    body = (raw.text_body + " " + _html_to_text(raw.html_body)).lower()
    for keyword in config.approval_keywords:
        if keyword.lower() in body:
            return True, sender
    return False, ""


def is_done_email(raw: RawEmail, config: Config, my_email: str) -> bool:
    body = (_strip_text_quotes(raw.text_body) + " " + _html_to_text_no_quotes(raw.html_body)).lower()
    return any(kw.lower() in body for kw in config.skip_keywords)


def get_thread_ids(raw: RawEmail) -> list[str]:
    """Message-IDs this email is replying to (In-Reply-To + References)."""
    ids: set[str] = set()
    if raw.in_reply_to:
        ids.add(raw.in_reply_to.strip("<>"))
    for ref in raw.references.split():
        cleaned = ref.strip("<>")
        if cleaned:
            ids.add(cleaned)
    return list(ids)


def _extract_email_address(from_header: str) -> str:
    m = re.search(r"<([^>]+)>", from_header)
    if m:
        return m.group(1).strip().lower()
    return from_header.strip().lower()


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    return BeautifulSoup(html, "html.parser").get_text(separator=" ")


def _html_to_text_no_quotes(html: str) -> str:
    """Like _html_to_text but strips <blockquote> sections first."""
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for bq in soup.find_all("blockquote"):
        bq.decompose()
    return soup.get_text(separator=" ")


def _strip_text_quotes(text: str) -> str:
    """Remove quoted lines (lines starting with >) from plain text."""
    return "\n".join(
        line for line in text.splitlines()
        if not line.lstrip().startswith(">")
    )
