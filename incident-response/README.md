# Incident responder

`POST /alerts` (port 8001) receives Grafana webhook alerts, saves evidence under
`incidents/<timestamp>-<fingerprint>/` and starts the coding assistant
(`claude -p`) headlessly in the repo root.

```bash
cd incident-response
uv run uvicorn responder.main:app --host 127.0.0.1 --port 8001
```

Per incident it writes: `alert.json`, `evidence.md` (endpoint, failing paths, latest
error), `logs.json` (Loki), `traces/*.json` (Tempo), `prompt.md`, `agent-output.jsonl`
(full stream-json transcript), `agent-response.md`, `result.json` (incl. last line),
`status.json`.

Guardrails: one agent run at a time; same alert+endpoint is suppressed for
`COOLDOWN_SECONDS` (600); `AGENT_TIMEOUT` (900s) and `AGENT_MAX_TURNS` (40) cap a run;
resolved alerts are only recorded; test alerts (`labels.test=true`) get a read-only,
3-turn agent; the agent may not `git commit/push`, `docker compose down`, `rm`, or use
the web; evidence is passed as untrusted data; the server binds to localhost only.

Settings (env): `AGENT_COMMAND`, `REPO_ROOT`, `INCIDENTS_DIR`, `LOKI_URL`, `TEMPO_URL`,
`PROMETHEUS_URL`, `APP_URL`, `LOOKBACK_MINUTES`.

Tests: `uv run pytest -q` (uses a fake agent; no Claude or Docker needed).
