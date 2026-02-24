from __future__ import annotations

import time

from app import create_app


def test_admin_status_ok():
    app = create_app()
    client = app.test_client()

    resp = client.get("/api/v1/admin/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 200
    assert "config" in body["data"]
    assert "artifacts" in body["data"]


def test_admin_train_task_lifecycle():
    app = create_app()
    client = app.test_client()

    resp = client.post("/api/v1/admin/train", json={"mode": "full"})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["code"] == 200
    task_id = body["data"]["task_id"]
    assert isinstance(task_id, str) and task_id

    # task should be queryable
    resp2 = client.get(f"/api/v1/admin/tasks/{task_id}")
    assert resp2.status_code == 200
    body2 = resp2.get_json()
    assert body2["code"] == 200
    assert body2["data"]["id"] == task_id

    # give it a tiny moment to finish (should be fast in default config)
    for _ in range(10):
        resp3 = client.get(f"/api/v1/admin/tasks/{task_id}")
        body3 = resp3.get_json()
        if body3["data"]["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.05)

    assert body3["data"]["status"] in {"succeeded", "failed"}
