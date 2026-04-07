import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    imap_host: str
    imap_port: int
    imap_user: str
    imap_password: str
    target_email: str
    poll_interval: int
    approved_senders: list[str]
    approval_keywords: list[str]
    gocd_base_url: str
    gocd_token: str
    gocd_stage_map: dict[str, str]   # pipeline_name → execute_stage
    gocd_default_stage: str          # fallback if pipeline not in map
    gocd_dry_run: bool
    db_path: str
    feishu_webhook_url: str
    skip_keywords: list[str]
    lookback_days: int
    gocd_status_timeout_minutes: int
    smtp_host: str
    smtp_port: int

    def get_execute_stage(self, pipeline_name: str) -> str:
        """Return the execute stage name for a given pipeline, falling back to default."""
        return self.gocd_stage_map.get(pipeline_name, self.gocd_default_stage)


def load_config() -> Config:
    def require(key: str) -> str:
        val = os.getenv(key)
        if not val:
            raise ValueError(f"Missing required env var: {key}")
        return val

    def csv_list(key: str, default: str) -> list[str]:
        raw = os.getenv(key, default)
        return [item.strip() for item in raw.split(",") if item.strip()]

    def parse_stage_map(raw: str) -> dict[str, str]:
        """Parse 'PIPELINE_A:stage-a,PIPELINE_B:stage-b' into a dict."""
        result = {}
        for entry in raw.split(","):
            entry = entry.strip()
            if ":" in entry:
                pipeline, stage = entry.split(":", 1)
                result[pipeline.strip()] = stage.strip()
        return result

    return Config(
        imap_host=require("IMAP_HOST"),
        imap_port=int(os.getenv("IMAP_PORT", "993")),
        imap_user=require("IMAP_USER"),
        imap_password=require("IMAP_PASSWORD"),
        target_email=os.getenv("TARGET_EMAIL", "otr-devops@inspiregroup.com"),
        poll_interval=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
        approved_senders=csv_list("APPROVED_SENDERS", ""),
        approval_keywords=csv_list("APPROVAL_KEYWORDS", "approve,approved,ok,同意,lgtm,好的,yes,+1"),
        gocd_base_url=require("GOCD_BASE_URL"),
        gocd_token=require("GOCD_TOKEN"),
        gocd_stage_map=parse_stage_map(os.getenv(
            "GOCD_STAGE_MAP",
            "ES-DATA-UPDATE-PROD:es-data-update,POD-CURL-PROD:pod-curl",
        )),
        gocd_default_stage=os.getenv("GOCD_DEFAULT_STAGE", ""),
        gocd_dry_run=os.getenv("GOCD_DRY_RUN", "false").lower() == "true",
        db_path=os.getenv("DB_PATH", "./state.db"),
        feishu_webhook_url=os.getenv("FEISHU_WEBHOOK_URL", ""),
        skip_keywords=csv_list("SKIP_KEYWORDS", "done,已完成,已执行,手动执行,manual"),
        lookback_days=int(os.getenv("LOOKBACK_DAYS", "3")),
        gocd_status_timeout_minutes=int(os.getenv("GOCD_STATUS_TIMEOUT_MINUTES", "60")),
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "465")),
    )
