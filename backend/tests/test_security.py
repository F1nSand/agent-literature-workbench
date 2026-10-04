from fastapi.testclient import TestClient

from app.main import app


def test_rejects_cross_origin_browser_writes() -> None:
    response = TestClient(app).post(
        "/api/v1/tasks",
        headers={"Origin": "https://attacker.example"},
        content="not-json",
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "请求来源不被允许"}


def test_allows_local_workbench_origin_and_non_browser_clients() -> None:
    client = TestClient(app)
    body = "not-json"

    local_origin = client.post(
        "/api/v1/tasks",
        headers={"Origin": "http://127.0.0.1:5174"},
        content=body,
    )
    cli_request = client.post("/api/v1/tasks", content=body)

    assert local_origin.status_code == 422
    assert cli_request.status_code == 422


def test_rejects_fetch_marked_cross_site_when_origin_is_absent() -> None:
    response = TestClient(app).post(
        "/api/v1/tasks",
        headers={"Sec-Fetch-Site": "cross-site"},
        content="not-json",
    )

    assert response.status_code == 403
