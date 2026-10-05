import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from responder import agent, evidence
from responder.config import Settings
from responder.main import create_app

FAKE = f"{sys.executable} {Path(__file__).parent / 'fake_agent.py'}"

TEST_ALERT = {
    "receiver": "incident-responder", "status": "firing",
    "alerts": [{
        "status": "firing", "fingerprint": "abc12345",
        "labels": {"alertname": "ResponderTest", "test": "true"},
        "annotations": {"summary": "Test notification; no incident to fix"},
    }],
}
REAL_ALERT = {
    "status": "firing",
    "alerts": [{
        "status": "firing", "fingerprint": "feed0001",
        "labels": {"alertname": "Order Tracker 5xx errors", "http_route": "/api/orders/{order_id}"},
        "annotations": {"summary": "5xx errors on /api/orders/{order_id}", "endpoint": "/api/orders/{order_id}"},
        "dashboardURL": "http://localhost:3000/d/order-tracker",
    }],
}


@pytest.fixture
def settings(tmp_path):
    # Backends point at a closed port: evidence collection must degrade, not crash.
    return Settings(repo_root=tmp_path, incidents_dir=tmp_path / "incidents", agent_command=FAKE,
                    loki_url="http://127.0.0.1:9", tempo_url="http://127.0.0.1:9", cooldown_seconds=600)


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings, background=False))


def only_incident(settings):
    (path,) = [p for p in settings.incidents_dir.iterdir() if p.is_dir()]
    return path


def test_rejects_bad_payload(client):
    assert client.post("/alerts", content="not json").status_code == 400
    assert client.post("/alerts", json={"nope": 1}).status_code == 400


def test_test_alert_runs_agent_and_records_last_line(client, settings):
    response = client.post("/alerts", json=TEST_ALERT)
    assert response.status_code == 200
    (result,) = response.json()["results"]
    assert result["state"] == "agent_done" and result["test"] is True
    incident = only_incident(settings)
    for name in ("alert.json", "evidence.md", "prompt.md", "agent-output.jsonl", "agent-response.md", "result.json"):
        assert (incident / name).exists(), name
    assert "STATUS: TEST_OK" in (incident / "agent-response.md").read_text()
    assert "do not read or change any files" in (incident / "prompt.md").read_text().lower()


def test_incident_prompt_points_at_evidence_and_forbids_commit(client, settings):
    client.post("/alerts", json=REAL_ALERT)
    incident = only_incident(settings)
    prompt = (incident / "prompt.md").read_text()
    assert "/api/orders/{order_id}" in prompt and str(incident) in prompt
    assert "do not commit or push" in prompt.lower()
    assert "Fixed it." in (incident / "agent-response.md").read_text()
    evidence_md = (incident / "evidence.md").read_text()
    assert "Endpoint" in evidence_md and "collection problems" in evidence_md


def test_command_restricts_tools(settings):
    command = agent.build_command(settings, test=False)
    allowed = command[command.index("--allowedTools") + 1]
    denied = command[command.index("--disallowedTools") + 1]
    assert "Bash(git commit:*)" in denied and "Bash(git push:*)" in denied
    assert "Bash(docker compose down:*)" in denied
    assert "git commit" not in allowed and "git push" not in allowed
    assert agent.build_command(settings, test=True)[command.index("--allowedTools") + 1] == "Read,Grep,Glob"


def test_cooldown_suppresses_duplicate_and_resolved_is_ignored(client):
    first = client.post("/alerts", json=REAL_ALERT).json()["results"][0]
    second = client.post("/alerts", json=REAL_ALERT).json()["results"][0]
    resolved = client.post("/alerts", json={"alerts": [{**REAL_ALERT["alerts"][0], "status": "resolved",
                                                        "labels": {"alertname": "other"}}]}).json()["results"][0]
    assert first["state"] == "agent_done"
    assert second["state"] == "suppressed"
    assert resolved["state"] == "ignored"


def test_busy_lock_blocks_concurrent_run(settings):
    app = create_app(settings, background=False)
    responder = app.state.responder
    assert responder._busy.acquire(blocking=False)
    result = responder.handle(REAL_ALERT)[0]
    assert result["state"] == "busy"


def test_evidence_collects_logs_and_traces(tmp_path, settings):
    def handler(request: httpx.Request):
        if "query_range" in request.url.path:
            return httpx.Response(200, json={"data": {"result": [{
                "stream": {"detected_level": "error", "http_response_status_code": "500",
                           "url_path": "/api/orders/express-1002", "http_route": "/api/orders/{order_id}"},
                "values": [["1", "request failed: ValueError: day is out of range for month"]]}]}})
        if request.url.path == "/api/search":
            return httpx.Response(200, json={"traces": [{"traceID": "t1"}]})
        return httpx.Response(200, json={"batches": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    summary = evidence.collect(REAL_ALERT["alerts"][0], tmp_path / "inc", settings, client)
    assert summary["failing_request_paths"] == ["/api/orders/express-1002"]
    assert summary["trace_ids"] == ["t1"] and not summary["collection_errors"]
    assert (tmp_path / "inc" / "traces" / "t1.json").exists()
    assert "day is out of range" in (tmp_path / "inc" / "evidence.md").read_text()
