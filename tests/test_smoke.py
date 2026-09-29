"""骨架冒烟测试。"""
from fastapi.testclient import TestClient

from main import app


def test_root():
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
