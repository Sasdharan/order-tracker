import base64
import json

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "INCIDENTS_DIR", tmp_path)
    monkeypatch.setattr(main, "WEBHOOK_PASSWORD", None)
    monkeypatch.setattr(main, "_last_invocation", {})

    invoked = []

    async def fake_invoke(incident_dir, endpoint):
        invoked.append((incident_dir, endpoint))

    async def fake_logs(limit=200):
        return [{"stream": {}, "values": [["1", "log line"]]}]

    async def fake_traces(endpoint, limit=20):
        return {"traces": [{"traceID": "abc123"}]}

    monkeypatch.setattr(main, "_invoke_assistant", fake_invoke)
    monkeypatch.setattr(main, "_fetch_logs", fake_logs)
    monkeypatch.setattr(main, "_fetch_traces", fake_traces)

    with TestClient(main.app) as test_client:
        test_client.invoked = invoked
        yield test_client


def _alert(status="firing", **labels):
    return {
        "status": status,
        "labels": {"alertname": "Order Tracker - 5xx errors by route", "http_route": "/api/orders/{order_id}", **labels},
        "annotations": {"summary": "route is failing", "dashboard_url": "http://localhost:3000/d/x"},
        "startsAt": "2026-09-30T00:00:00Z",
        "fingerprint": "fp1",
    }


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_firing_alert_saves_incident_and_invokes_assistant(client):
    response = client.post("/alerts", json={"alerts": [_alert()]})
    assert response.status_code == 200
    body = response.json()
    assert body["received"] == 1
    assert len(body["handled"]) == 1
    assert body["handled"][0]["endpoint"] == "/api/orders/{order_id}"
    assert body["handled"][0]["assistant_invoked"] is True

    incident_dirs = list(main.INCIDENTS_DIR.iterdir())
    assert len(incident_dirs) == 1
    incident_dir = incident_dirs[0]
    assert (incident_dir / "alert.json").exists()
    assert (incident_dir / "logs.json").exists()
    assert (incident_dir / "traces.json").exists()
    assert (incident_dir / "summary.md").exists()
    assert json.loads((incident_dir / "logs.json").read_text())

    assert len(client.invoked) == 1


def test_resolved_alert_is_not_handled(client):
    response = client.post("/alerts", json={"alerts": [_alert(status="resolved")]})
    assert response.status_code == 200
    assert response.json() == {"received": 1, "handled": []}
    assert list(main.INCIDENTS_DIR.iterdir()) == []


def test_cooldown_skips_repeat_invocation(client):
    client.post("/alerts", json={"alerts": [_alert()]})
    client.post("/alerts", json={"alerts": [_alert()]})
    assert len(client.invoked) == 1


def test_auth_required_when_password_set(client, monkeypatch):
    monkeypatch.setattr(main, "WEBHOOK_PASSWORD", "secret")

    unauthenticated = client.post("/alerts", json={"alerts": [_alert()]})
    assert unauthenticated.status_code == 401

    creds = base64.b64encode(b"grafana:secret").decode()
    authenticated = client.post(
        "/alerts",
        json={"alerts": [_alert()]},
        headers={"Authorization": f"Basic {creds}"},
    )
    assert authenticated.status_code == 200
