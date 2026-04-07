import logging
import httpx

from .config import Config

logger = logging.getLogger(__name__)

RESULT_PASSED = "Passed"
RESULT_FAILED = "Failed"
RESULT_CANCELLED = "Cancelled"
RESULT_RUNNING = "Running"  # stage is actively executing
RESULT_UNKNOWN = "Unknown"  # not started yet or indeterminate


def trigger_stage(pipeline_name: str, pipeline_counter: str, execute_stage: str,
                  config: Config) -> None:
    """
    Trigger a specific execute stage for a pipeline instance.
    Raises RuntimeError on non-2xx response.
    """
    url = (f"{config.gocd_base_url.rstrip('/')}/go/api/stages"
           f"/{pipeline_name}/{pipeline_counter}/{execute_stage}/run")

    if config.gocd_dry_run:
        logger.info("[DRY RUN] Would POST %s", url)
        return

    headers = {
        "Accept": "application/vnd.go.cd.v2+json",
        "Authorization": f"Bearer {config.gocd_token}",
        "X-GoCD-Confirm": "true",
    }

    logger.info("Triggering GoCD: POST %s", url)
    response = httpx.post(url, headers=headers, verify=False, timeout=30)

    if not response.is_success:
        raise RuntimeError(
            f"GoCD API returned {response.status_code}: {response.text[:500]}"
        )


def get_stage_result(pipeline_name: str, pipeline_counter: str, execute_stage: str,
                     config: Config) -> str:
    """
    Query the result of a specific stage instance.
    Returns: "Passed", "Failed", "Cancelled", or "Unknown" (still running).
    """
    url = (f"{config.gocd_base_url.rstrip('/')}/go/api/stages"
           f"/{pipeline_name}/{pipeline_counter}/{execute_stage}/1")

    if config.gocd_dry_run:
        logger.info("[DRY RUN] Would GET %s → returning Passed", url)
        return RESULT_PASSED

    headers = {
        "Accept": "application/vnd.go.cd.v2+json",
        "Authorization": f"Bearer {config.gocd_token}",
    }

    try:
        response = httpx.get(url, headers=headers, verify=False, timeout=15)
    except Exception as e:
        logger.warning("GoCD status check failed (network): %s", e)
        return RESULT_UNKNOWN

    if response.status_code == 404:
        return RESULT_UNKNOWN

    if not response.is_success:
        logger.warning("GoCD status check returned %s: %s",
                       response.status_code, response.text[:200])
        return RESULT_UNKNOWN

    data = response.json()
    result = data.get("result", RESULT_UNKNOWN)
    status = data.get("status", "")
    logger.debug("GoCD %s/%s/%s → result=%s status=%s",
                 pipeline_name, pipeline_counter, execute_stage, result, status)
    # GoCD sets result="Unknown" while the stage is still building;
    # use the status field to distinguish "running" from "not started".
    if result == RESULT_UNKNOWN and status in ("Building", "Scheduled", "Preparing"):
        return RESULT_RUNNING
    return result
