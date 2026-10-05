import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture
def client(tmp_path, monkeypatch, telemetry_sink):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "orders.db")
    # The express seed order is placed on the last day of the previous month.
    # raise_server_exceptions=False -> the client sees the 500 like curl would.
    with TestClient(main.app, raise_server_exceptions=False) as test_client:
        yield test_client


def request_points(reader):
    data = reader.get_metrics_data()
    points = {}
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == "http.server.requests":
                    for point in metric.data.data_points:
                        key = (
                            point.attributes["http.route"],
                            point.attributes["http.response.status_code"],
                        )
                        points[key] = points.get(key, 0) + point.value
    return points


def test_successful_lookup_records_200(client, telemetry_sink):
    reader, spans = telemetry_sink
    assert client.get("/api/orders/standard-1001").status_code == 200
    assert request_points(reader)[("/api/orders/{order_id}", 200)] == 1
    names = {span.name for span in spans.get_finished_spans()}
    assert {"GET /api/orders/{order_id}", "order.lookup"} <= names


def test_unknown_order_records_404(client, telemetry_sink):
    reader, _ = telemetry_sink
    assert client.get("/api/orders/standard-1002").status_code == 404
    assert request_points(reader)[("/api/orders/{order_id}", 404)] == 1


def test_error_series_is_seeded_at_zero(client, telemetry_sink):
    reader, _ = telemetry_sink
    # Lifespan already ran when the client started, but with the *previous*
    # telemetry object; seed explicitly on the test one and check shape.
    main.telemetry.seed_error_series([("GET", "/api/orders/{order_id}")])
    assert request_points(reader)[("/api/orders/{order_id}", 500)] == 0


def test_server_error_is_recorded_as_500_with_exception(client, telemetry_sink, monkeypatch):
    reader, spans = telemetry_sink

    def boom(_row):
        raise ValueError("day is out of range for month")

    monkeypatch.setattr(main, "order_detail", boom)
    assert client.get("/api/orders/standard-1001").status_code == 500
    assert request_points(reader)[("/api/orders/{order_id}", 500)] == 1
    request_span = next(s for s in spans.get_finished_spans() if s.name.startswith("GET /api"))
    assert request_span.status.status_code.name == "ERROR"
    assert any(event.name == "exception" for event in request_span.events)
