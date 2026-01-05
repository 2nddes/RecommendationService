from __future__ import annotations

from app import create_app


def test_health():
    app = create_app()
    client = app.test_client()

    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 200
    assert body["data"]["status"] == "ok"
