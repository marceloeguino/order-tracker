import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field

from app.telemetry import create_telemetry


DB_PATH = Path(os.getenv("ORDER_DB_PATH", "data/orders.db"))
STATUSES = {"received", "preparing", "shipped", "delivered"}

# Traces, metrics and logs. Console exporters are always on (visible in
# `docker compose logs app`); OTLP export to the Collector turns on when
# OTEL_EXPORTER_OTLP_ENDPOINT is set. Tests swap this object out.
telemetry = create_telemetry()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with connect() as db:
        db.execute(
            """CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                customer TEXT NOT NULL,
                item TEXT NOT NULL,
                priority TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        if db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0:
            now = datetime.now(timezone.utc)
            previous_month_end = now.replace(day=1) - timedelta(days=1)
            for order in (
                ("standard-1001", "Avery", "Notebook", "standard", "received", now),
                ("express-1002", "Sam", "Headphones", "express", "preparing", previous_month_end),
                ("standard-1003", "Riley", "Water bottle", "standard", "shipped", now),
            ):
                db.execute(
                    "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
                    (*order[:5], order[5].isoformat()),
                )


def as_dict(row):
    return dict(row) if row else None


def order_detail(row):
    order = as_dict(row)
    if order["priority"] == "express":
        placed_at = datetime.fromisoformat(order["created_at"])
        estimated_at = placed_at.replace(day=placed_at.day + 2)
        order["estimated_delivery"] = estimated_at.date().isoformat()
    return order


class NewOrder(BaseModel):
    customer: str = Field(min_length=1, max_length=80)
    item: str = Field(min_length=1, max_length=120)
    priority: str = "standard"


class StatusUpdate(BaseModel):
    status: str


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    telemetry.seed_error_series(
        [
            (method, route.path)
            for route in _app.routes
            for method in sorted(getattr(route, "methods", None) or ())
            if method not in {"HEAD", "OPTIONS"} and route.path.startswith(("/api", "/healthz"))
        ]
    )
    yield
    telemetry.shutdown()


app = FastAPI(title="Order Tracker", lifespan=lifespan)


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    """One server span, one log line and the request metrics per HTTP request."""
    started = time.perf_counter()
    status_code = 500
    failure = None
    if request.url.path == "/healthz":
        # Probes (Compose healthcheck every 5s) count in the metrics but stay
        # out of spans and logs, which would otherwise drown real traffic.
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            telemetry.record_request(
                request.method, "/healthz", status_code, time.perf_counter() - started
            )
    span_cm = telemetry.tracer.start_as_current_span(
        f"{request.method} {request.url.path}",
        context=extract(dict(request.headers)),
        kind=SpanKind.SERVER,
        attributes={"http.request.method": request.method, "url.path": request.url.path},
        record_exception=False,
        set_status_on_exception=False,
    )
    with span_cm as span:
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception as exc:  # unhandled -> uvicorn answers 500
            failure = exc
            span.record_exception(exc)
            raise
        finally:
            route_obj = request.scope.get("route")
            route = getattr(route_obj, "path", None) or "unmatched"
            elapsed = time.perf_counter() - started
            span.update_name(f"{request.method} {route}")
            span.set_attribute("http.route", route)
            span.set_attribute("http.response.status_code", status_code)
            if status_code >= 500:
                span.set_status(Status(StatusCode.ERROR))
            telemetry.record_request(request.method, route, status_code, elapsed)
            fields = {
                "http.request.method": request.method,
                "http.route": route,
                "url.path": request.url.path,
                "http.response.status_code": status_code,
                "http.server.duration_ms": round(elapsed * 1000, 2),
            }
            if failure is not None:
                telemetry.logger.error(
                    "request failed: %s: %s",
                    type(failure).__name__,
                    failure,
                    exc_info=(type(failure), failure, failure.__traceback__),
                    extra=fields,
                )
            else:
                telemetry.logger.info(
                    "%s %s -> %s", request.method, request.url.path, status_code, extra=fields
                )


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent.parent / "static" / "index.html")


@app.get("/healthz")
def health():
    with connect() as db:
        db.execute("SELECT 1")
    return {"status": "ok"}


@app.get("/api/orders")
def list_orders():
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created_at DESC").fetchall()
    return [as_dict(row) for row in rows]


@app.get("/api/orders/{order_id}")
def get_order(order_id: str):
    with telemetry.tracer.start_as_current_span("order.lookup") as span:
        span.set_attribute("order.id", order_id)
        with connect() as db:
            row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        span.set_attribute("order.found", row is not None)
        if row is None:
            telemetry.logger.warning("order not found", extra={"order.id": order_id})
            raise HTTPException(404, "Order not found")
        span.set_attribute("order.priority", row["priority"])
        return order_detail(row)


@app.post("/api/orders", status_code=201)
def create_order(order: NewOrder):
    if order.priority not in {"standard", "express"}:
        raise HTTPException(422, "Priority must be standard or express")
    order_id = str(uuid4())
    with connect() as db:
        db.execute(
            "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, order.customer, order.item, order.priority, "received",
             datetime.now(timezone.utc).isoformat()),
        )
    return get_order(order_id)


@app.patch("/api/orders/{order_id}")
def update_status(order_id: str, update: StatusUpdate):
    if update.status not in STATUSES:
        raise HTTPException(422, "Invalid status")
    with connect() as db:
        cursor = db.execute(
            "UPDATE orders SET status = ? WHERE id = ?",
            (update.status, order_id),
        )
    if cursor.rowcount == 0:
        raise HTTPException(404, "Order not found")
    return get_order(order_id)
