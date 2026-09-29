"""测试公共夹具与辅助函数。

每个用例前后都清空进程内 store，保证用例相互隔离。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.state import store


def headers(caller: str, admin: bool = False) -> dict[str, str]:
    h = {"X-Caller-Id": caller}
    if admin:
        h["X-Caller-Roles"] = "admin"
    return h


@pytest.fixture
def client():
    store.reset()
    with TestClient(app) as c:
        yield c
    store.reset()
