"""POST /alerts: receive a Grafana webhook, save evidence, start the coding assistant."""

import json
import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request

from responder import agent, evidence
from responder.config import Settings

log = logging.getLogger("responder")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def is_test(alert: dict) -> bool:
    return str(alert.get("labels", {}).get("test", "")).lower() == "true"


class Responder:
    def __init__(self, settings: Settings, background: bool = True):
        self.settings = settings
        self.background = background
        self._busy = threading.Lock()  # at most one agent run at a time
        self._last_run: dict[str, float] = {}  # dedup key -> monotonic start time

    def handle(self, payload: dict) -> list[dict]:
        default_status = payload.get("status") or "firing"
        return [self._accept({"status": default_status, **alert}) for alert in payload.get("alerts", [])]

    def _accept(self, alert: dict) -> dict:
        test = is_test(alert)
        endpoint = evidence.endpoint_of(alert)
        key = f"{alert.get('labels', {}).get('alertname', 'alert')}|{endpoint}|test={test}"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        incident_id = f"{stamp}-{(alert.get('fingerprint') or 'nofp')[:8]}"
        out_dir = self.settings.incidents_dir / incident_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "alert.json").write_text(json.dumps(alert, indent=2))

        def finish(state: str, reason: str = "") -> dict:
            record = {"incident": incident_id, "state": state, "endpoint": endpoint, "test": test, "reason": reason}
            (out_dir / "status.json").write_text(json.dumps(record, indent=2))
            log.info("alert %s -> %s %s", incident_id, state, reason)
            return record

        if alert.get("status") != "firing":
            return finish("ignored", "alert is not firing (resolved)")
        recent = time.monotonic() - self._last_run.get(key, -1e9)
        if recent < self.settings.cooldown_seconds:
            return finish("suppressed", f"same alert handled {int(recent)}s ago (cooldown)")
        if not self._busy.acquire(blocking=False):
            return finish("busy", "another incident run is in progress")
        self._last_run[key] = time.monotonic()
        if self.background:
            threading.Thread(target=self._run, args=(alert, out_dir, test, finish), daemon=True).start()
            return finish("accepted", "evidence + agent running in background")
        return self._run(alert, out_dir, test, finish)

    def _run(self, alert, out_dir, test, finish):
        try:
            summary = evidence.collect(alert, out_dir, self.settings)
            prompt = agent.build_prompt(alert, summary, out_dir, test)
            result = agent.run(self.settings, prompt, out_dir, test)
            state = "agent_done" if result.get("exit_code") == 0 else "agent_failed"
            (out_dir / "result.json").write_text(json.dumps(result, indent=2))
            return finish(state, f"last line: {result['last_line']!r}")
        except Exception as exc:  # never leave the lock held
            log.exception("incident run crashed")
            return finish("error", f"{type(exc).__name__}: {exc}")
        finally:
            self._busy.release()


def create_app(settings: Settings | None = None, background: bool = True) -> FastAPI:
    responder = Responder(settings or Settings(), background)
    app = FastAPI(title="Incident responder")
    app.state.responder = responder

    @app.get("/healthz")
    def health():
        return {"status": "ok"}

    @app.post("/alerts")
    async def alerts(request: Request):
        try:
            payload = await request.json()
        except ValueError:
            raise HTTPException(400, "body must be JSON")
        if not isinstance(payload, dict) or not isinstance(payload.get("alerts"), list):
            raise HTTPException(400, "expected a Grafana webhook payload with an 'alerts' list")
        return {"received": len(payload["alerts"]), "results": responder.handle(payload)}

    return app


app = create_app()
