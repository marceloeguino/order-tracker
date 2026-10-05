# Homework 4 runbook (run in a Terminal, in this repo)

Repo: https://github.com/marceloeguino/order-tracker

## Q1 - run the starter
Use the original app only: `git stash` is not needed, the health endpoint is unchanged.
```bash
docker compose up --build -d --wait
curl http://localhost:8000/healthz        # {"status":"ok"}
```

## Q2 - OpenTelemetry in the app (console exporters)
```bash
docker compose up --build -d --wait app
curl -i http://localhost:8000/api/orders/standard-1001   # HTTP 200
sleep 6; docker compose logs app | grep -E 'http.server.requests|http.response.status_code' | tail
```
The `http.server.requests` metric carries `http.route=/api/orders/{order_id}` and
`http.response.status_code=200`. Spans (`GET /api/orders/{order_id}`, `order.lookup`) and a log record
are printed too.

## Q3 - Collector, Prometheus, Loki, Tempo, Grafana
```bash
docker compose up --build -d --wait
curl -i http://localhost:8000/api/orders/standard-1002   # HTTP 404
open http://localhost:3000/d/order-tracker               # dashboard (no login)
```
Prometheus shows `http_server_requests_total{http_response_status_code="404"}`.

## Q4 - alert on 5xx
Rule `Order Tracker 5xx errors` (Alerting -> Alert rules). After the 404 lookup above the state
stays **Normal** (404 is not a 5xx; no-data is also Normal).

## Q5 - responder
```bash
cd incident-response && uv run uvicorn responder.main:app --host 127.0.0.1 --port 8001
# other terminal:
curl -sS -X POST http://localhost:8001/alerts -H 'Content-Type: application/json' -d '<ResponderTest payload from the homework>'
```
Read `incident-response/incidents/*/agent-response.md` (last line = `STATUS: TEST_OK`).

## Q6 - end to end
```bash
curl -i http://localhost:8000/api/orders/express-1002    # HTTP 500, repeat a few times if needed
```
Grafana fires -> webhook -> responder -> agent fixes `app/main.py`, rebuilds `app`, re-checks the
request. Root cause: the express delivery estimate adds 2 to the day number
(`placed_at.replace(day=placed_at.day + 2)`), which does not exist at the end of a month.
