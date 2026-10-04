from fastapi.testclient import TestClient

from app.main import app


def test_live_health() -> None:
    response = TestClient(app).get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_has_versioned_health_and_task_routes() -> None:
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/health/ready" in paths
    assert "/api/v1/tasks" in paths


def test_ready_health_returns_generic_error_without_database(monkeypatch) -> None:
    from app.api.v1 import health
    from sqlalchemy.exc import OperationalError

    class BrokenConnection:
        def __enter__(self):
            raise OperationalError("connect", {}, Exception("password=do-not-leak"))

        def __exit__(self, *_args):
            return False

    class BrokenEngine:
        def connect(self):
            return BrokenConnection()

    monkeypatch.setattr(health, "get_engine", lambda: BrokenEngine())
    response = TestClient(app).get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "database unavailable"}
    assert "do-not-leak" not in response.text


def test_ready_health_with_database(monkeypatch) -> None:
    from app.api.v1 import health

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _statement):
            return 1

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(health, "get_engine", lambda: Engine())
    response = TestClient(app).get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}
