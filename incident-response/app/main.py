"""Receives Grafana alert webhooks, saves incident context, and triggers a headless coding assistant."""
import asyncio
import json
import logging
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://tempo:3200")
INCIDENTS_DIR = Path(os.getenv("INCIDENTS_DIR", "/data/incidents"))
REPO_DIR = os.getenv("REPO_DIR", "/workspace")
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "15"))

AUTO_INVOKE_ASSISTANT = os.getenv("AUTO_INVOKE_ASSISTANT", "true").lower() == "true"
ASSISTANT_COMMAND = os.getenv("ASSISTANT_COMMAND", "copilot")
# Empty by default: inherits whatever model the CLI/account defaults to. Set to a
# specific model name (see `copilot help config`) to pin cost/capability, e.g. a
# cheaper tier like "gpt-5-mini" or "claude-haiku-4.5".
ASSISTANT_MODEL = os.getenv("ASSISTANT_MODEL", "")
ASSISTANT_TIMEOUT_SECONDS = int(os.getenv("ASSISTANT_TIMEOUT_SECONDS", "1800"))
ASSISTANT_COOLDOWN_SECONDS = int(os.getenv("ASSISTANT_COOLDOWN_SECONDS", "600"))

WEBHOOK_USERNAME = os.getenv("ALERT_WEBHOOK_USERNAME", "grafana")
WEBHOOK_PASSWORD = os.getenv("ALERT_WEBHOOK_PASSWORD")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("incident_response")

if not WEBHOOK_PASSWORD:
    logger.warning("ALERT_WEBHOOK_PASSWORD is not set; /alerts is unauthenticated")

app = FastAPI(title="Incident Response", telemetry={"auto_configure": False})
security = HTTPBasic(auto_error=False)

# Tracks the last time the assistant ran per alert fingerprint, to avoid piling up
# overlapping runs while an alert is still flapping.
_last_invocation: dict[str, float] = {}


def _check_auth(credentials: Optional[HTTPBasicCredentials]) -> None:
    if not WEBHOOK_PASSWORD:
        return
    if (
        credentials is None
        or credentials.username != WEBHOOK_USERNAME
        or credentials.password != WEBHOOK_PASSWORD
    ):
        raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Basic"})


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "incident"


async def _fetch_logs(limit: int = 200) -> list[dict[str, Any]]:
    """Recent log lines for the service, for the operator/assistant to read."""
    start_ns = int((time.time() - LOOKBACK_MINUTES * 60) * 1e9)
    params = {"query": '{service_name="order-tracker"}', "limit": limit, "start": start_ns}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{LOKI_URL}/loki/api/v1/query_range", params=params)
            response.raise_for_status()
            return response.json().get("data", {}).get("result", [])
        except httpx.HTTPError as exc:
            logger.warning("Loki query failed: %s", exc)
            return []


async def _fetch_traces(endpoint: str, limit: int = 20) -> dict[str, Any]:
    """Recent traces for the service, optionally narrowed to the affected route."""
    tags = 'service.name="order-tracker"'
    if endpoint and endpoint != "unknown":
        tags += f' http.route="{endpoint}"'
    start = int(time.time() - LOOKBACK_MINUTES * 60)
    params = {"tags": tags, "limit": limit, "start": start, "end": int(time.time())}
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{TEMPO_URL}/api/search", params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            logger.warning("Tempo query failed: %s", exc)
            return {}


def _build_summary(alert: dict[str, Any], endpoint: str, logs: list[Any], traces: dict[str, Any]) -> str:
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    return "\n".join(
        [
            f"# Incident: {labels.get('alertname', 'unknown-alert')}",
            "",
            f"- **Endpoint**: `{endpoint}`",
            f"- **Severity**: {labels.get('severity', 'unknown')}",
            f"- **Status**: {alert.get('status', 'unknown')}",
            f"- **Started at**: {alert.get('startsAt', 'unknown')}",
            f"- **Dashboard**: {annotations.get('dashboard_url', 'n/a')}",
            f"- **Summary**: {annotations.get('summary', 'n/a')}",
            f"- **Description**: {annotations.get('description', 'n/a')}",
            "",
            f"## Logs (last {LOOKBACK_MINUTES}m, {len(logs)} streams captured)",
            "Raw data in `logs.json`.",
            "",
            f"## Traces (last {LOOKBACK_MINUTES}m, {len(traces.get('traces', []))} traces captured)",
            "Raw data in `traces.json`.",
        ]
    )


def _should_invoke(fingerprint: str) -> bool:
    now = time.monotonic()
    last = _last_invocation.get(fingerprint)
    if last is not None and now - last < ASSISTANT_COOLDOWN_SECONDS:
        return False
    _last_invocation[fingerprint] = now
    return True


async def _invoke_assistant(incident_dir: Path, endpoint: str) -> None:
    prompt = (
        f"Grafana alert fired for order-tracker, endpoint {endpoint}. Incident context is in "
        f"{incident_dir} (summary.md, alert.json, logs.json, traces.json) - skim it briefly, "
        "don't restate it back. Reproduce the bug with the minimum necessary commands, find the "
        "root cause, then immediately apply the smallest correct code fix in this repository. "
        "Do not ask clarifying questions. Skip writing a long report - after applying the fix, "
        f"append a 3-5 line summary (root cause + fix) to {incident_dir}/investigation.md and stop."
    )
    cmd = [
        ASSISTANT_COMMAND,
        "-p", prompt,
        "--allow-all-tools",
        "--no-ask-user",
        "-C", REPO_DIR,
        "--add-dir", str(INCIDENTS_DIR),
        "--log-dir", str(incident_dir / "assistant-logs"),
        "--share", str(incident_dir / "session.md"),
    ]
    if ASSISTANT_MODEL:
        cmd += ["--model", ASSISTANT_MODEL]
    logger.info("Starting coding assistant for incident %s", incident_dir.name)
    try:
        with (incident_dir / "assistant.log").open("w") as log_file:
            process = await asyncio.create_subprocess_exec(
                *cmd, stdout=log_file, stderr=subprocess.STDOUT, cwd=REPO_DIR
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=ASSISTANT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning("Assistant run for %s timed out; killing it", incident_dir.name)
                process.kill()
    except FileNotFoundError:
        logger.error("Assistant command %r not found; skipping auto-invoke", ASSISTANT_COMMAND)
    except Exception:
        logger.exception("Assistant invocation failed for %s", incident_dir.name)


async def _handle_alert(alert: dict[str, Any]) -> dict[str, Any]:
    labels = alert.get("labels", {})
    alertname = labels.get("alertname", "unknown-alert")
    endpoint = labels.get("http_route", "unknown")
    fingerprint = alert.get("fingerprint") or f"{alertname}:{endpoint}"

    incident_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{_slug(alertname)}"
    incident_dir = INCIDENTS_DIR / incident_id
    incident_dir.mkdir(parents=True, exist_ok=True)

    (incident_dir / "alert.json").write_text(json.dumps(alert, indent=2))

    logs = await _fetch_logs()
    (incident_dir / "logs.json").write_text(json.dumps(logs, indent=2))

    traces = await _fetch_traces(endpoint)
    (incident_dir / "traces.json").write_text(json.dumps(traces, indent=2))

    (incident_dir / "summary.md").write_text(_build_summary(alert, endpoint, logs, traces))

    logger.info("Saved incident report for %s (%s) to %s", alertname, endpoint, incident_dir)

    invoked = False
    if AUTO_INVOKE_ASSISTANT and _should_invoke(fingerprint):
        asyncio.create_task(_invoke_assistant(incident_dir, endpoint))
        invoked = True

    return {"incident_id": incident_id, "endpoint": endpoint, "assistant_invoked": invoked}


@app.get("/healthz")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/alerts")
async def receive_alert(
    request: Request, credentials: Optional[HTTPBasicCredentials] = Depends(security)
) -> dict[str, Any]:
    _check_auth(credentials)
    payload = await request.json()
    alerts = payload.get("alerts", [])
    handled = [await _handle_alert(alert) for alert in alerts if alert.get("status") == "firing"]
    return {"received": len(alerts), "handled": handled}
