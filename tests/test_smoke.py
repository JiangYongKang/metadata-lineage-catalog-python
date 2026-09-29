"""骨架冒烟测试。"""
from fastapi.testclient import TestClient

from app.main import app


def test_healthz():
    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_openapi_available():
    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
