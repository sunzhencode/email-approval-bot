import logging
import httpx

from .state_store import RequestData

logger = logging.getLogger(__name__)


def notify_executed(webhook_url: str, req: RequestData) -> None:
    """Send a success notification card to the Feishu group bot."""
    if not webhook_url:
        return

    pipeline_link = f"{req.pipeline_name}/{req.pipeline_counter}"
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": "✅ CI 自动执行成功", "tag": "plain_text"},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": False,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**邮件标题**\n{req.subject or '-'}",
                            },
                        },
                    ],
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**Pipeline**\n{pipeline_link}",
                            },
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**批准人**\n{req.approved_by or '-'}",
                            },
                        },
                    ],
                },
                *(_pr_button(req.pr_url) if req.pr_url else []),
                *(_pipeline_button(req.pipeline_url) if req.pipeline_url else []),
            ],
        },
    }
    _send(webhook_url, card)


def notify_failed(webhook_url: str, req: RequestData, error: str) -> None:
    """Send a failure notification card to the Feishu group bot."""
    if not webhook_url:
        return

    pipeline_link = f"{req.pipeline_name}/{req.pipeline_counter}"
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"content": "❌ CI 执行失败，需要人工介入", "tag": "plain_text"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": False,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**邮件标题**\n{req.subject or '-'}",
                            },
                        },
                    ],
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**Pipeline**\n{pipeline_link}",
                            },
                        },
                    ],
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**错误信息**\n```\n{error[:300]}\n```",
                    },
                },
                *(_pipeline_button(req.pipeline_url) if req.pipeline_url else []),
            ],
        },
    }
    _send(webhook_url, card)


def _pr_button(pr_url: str) -> list:
    return [
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看 GitHub PR"},
                    "url": pr_url,
                    "type": "default",
                }
            ],
        }
    ]


def _pipeline_button(pipeline_url: str) -> list:
    return [
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看 GoCD Pipeline"},
                    "url": pipeline_url,
                    "type": "primary",
                }
            ],
        }
    ]


def _send(webhook_url: str, payload: dict) -> None:
    try:
        resp = httpx.post(webhook_url, json=payload, timeout=10)
        if not resp.is_success:
            logger.warning("Feishu webhook returned %s: %s", resp.status_code, resp.text[:200])
        else:
            data = resp.json()
            if data.get("code", 0) != 0:
                logger.warning("Feishu webhook error: %s", data)
    except Exception as e:
        logger.error("Failed to send Feishu notification: %s", e)
