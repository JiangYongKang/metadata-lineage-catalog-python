"""测试夹具：每个用例前清空全局存储，并登记稳定的调用方名册。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.deps import entity_service, register_caller, reset_all
from app.models import Caller, EntityKind, EntityUpsertRequest, FieldDefIn
from main import app


@pytest.fixture(autouse=True)
def _clean_store() -> None:
    reset_all()
    yield
    reset_all()


@pytest.fixture
def client():
    return TestClient(app)


def make_entity(
    cid: str,
    scope: str = "finance",
    name: str | None = None,
    fields: list[FieldDefIn] | None = None,
    caller: Caller | None = None,
) -> None:
    """直接走服务层造数据，避免测试前置依赖 HTTP。"""
    entity_service.upsert(
        caller or Caller(id="admin", role="admin", scopes=[]),
        EntityUpsertRequest(
            id=cid,
            name=name or cid,
            kind=EntityKind.TABLE,
            scope=scope,
            fields=fields if fields is not None else [],
        ),
    )


# 名册里额外加几个跨 scope 的身份，供越权测试使用
register_caller(Caller(id="reader-marketing", role="reader", scopes=["marketing"]))
register_caller(Caller(id="reader-hr", role="reader", scopes=["hr"]))
register_caller(Caller(id="owner-hr", role="owner", scopes=["hr"]))
