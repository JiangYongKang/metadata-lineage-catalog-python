"""服务端权限模型：越权访问拒绝且原因可区分，隐藏实体不可被推断。"""
from __future__ import annotations

import json

from tests.conftest import make_entity

ADMIN = {"X-Caller-Id": "admin"}
OWNER_F = {"X-Caller-Id": "owner-finance"}
OWNER_M = {"X-Caller-Id": "owner-marketing"}
READER_F = {"X-Caller-Id": "reader-finance"}
READER_M = {"X-Caller-Id": "reader-marketing"}
READER_HR = {"X-Caller-Id": "reader-hr"}
OWNER_HR = {"X-Caller-Id": "owner-hr"}


def _edge(u, d, transform=""):
    return {"upstream_id": u, "downstream_id": d, "transform": transform}


def _show(title, resp, payload=None):
    print(f"\n[{title}]")
    if payload is not None:
        print("  输入:", json.dumps(payload, ensure_ascii=False))
    print("  判定: HTTP", resp.status_code,
          json.dumps(resp.json(), ensure_ascii=False)[:500])


def test_unauthenticated_distinct_from_forbidden(client):
    # 无身份 / 未知身份：401 UNAUTHENTICATED
    r1 = client.get("/api/v1/entities/anything")
    assert r1.status_code == 401 and r1.json()["error"]["code"] == "UNAUTHENTICATED"
    r2 = client.get("/api/v1/entities/anything", headers={"X-Caller-Id": "nobody"})
    assert r2.status_code == 401 and r2.json()["error"]["code"] == "UNAUTHENTICATED"
    print("\n[未认证] 401 UNAUTHENTICATED（与 403 越权明确区分）")


def test_forbidden_scope_and_not_owner_are_distinguishable(client):
    make_entity("orders", "finance")
    # 合法身份但跨 scope 查看：403 FORBIDDEN_SCOPE
    r1 = client.get("/api/v1/entities/orders", headers=READER_M)
    _show("marketing reader 查看 finance 实体", r1)
    assert r1.status_code == 403 and r1.json()["error"]["code"] == "FORBIDDEN_SCOPE"

    # 同操作、同资源，owner 跨 scope 修改：403 FORBIDDEN_NOT_OWNER
    p = {"id": "orders", "name": "orders", "kind": "table", "scope": "finance",
         "expected_version": 1, "fields": []}
    r2 = client.post("/api/v1/entities", json=p, headers=OWNER_M)
    _show("marketing owner 修改 finance 实体", r2, p)
    assert r2.status_code == 403 and r2.json()["error"]["code"] == "FORBIDDEN_NOT_OWNER"


def test_request_body_claims_cannot_grant_access(client):
    """请求体里携带 role/admin/scopes 等任何标记都不能提升权限。"""
    make_entity("orders", "finance")
    # pydantic 模型会忽略多余字段；即使客户端伪造也无效
    for forged in [
        {"role": "admin"},
        {"admin": True},
        {"scopes": ["finance"]},
        {"role": "owner", "scope": "finance"},
    ]:
        p = {"id": "x", "name": "x", "kind": "table", "scope": "marketing",
             "fields": [], **forged}
        r = client.post("/api/v1/entities", json=p, headers=READER_M)
        assert r.status_code == 403, (forged, r.json())
        assert r.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    print("\n[请求体伪造权限标记] 4 种伪造全部 403 FORBIDDEN_SCOPE")


def test_reader_cannot_write_even_within_own_scope(client):
    p = {"id": "new_t", "name": "new_t", "kind": "table", "scope": "finance", "fields": []}
    r = client.post("/api/v1/entities", json=p, headers=READER_F)
    _show("finance reader 在本 scope 登记实体", r, p)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN_SCOPE"


def test_hidden_entities_absent_from_search(client):
    make_entity("orders", "finance")
    make_entity("budget", "finance", name="budget")
    make_entity("campaign", "marketing", name="campaign")

    r = client.get("/api/v1/entities", headers=READER_M)
    ids = [i["id"] for i in r.json()["items"]]
    print("\n[marketing 视角搜索]", ids)
    assert ids == ["campaign"]  # 仅本 scope；finance 实体彻底不出现
    r2 = client.get("/api/v1/entities?name_contains=ord", headers=READER_M)
    assert r2.json()["items"] == [] and r2.json()["total"] == 0


def test_hidden_entities_cannot_be_inferred_via_lineage(client):
    """a(finance) -> h(marketing) -> c(finance)：finance 视角看不到 h，
    也不能从影响分析的路径/计数推断 h 的存在。"""
    make_entity("a", "finance")
    make_entity("c", "finance")
    make_entity("h", "marketing")
    client.post("/api/v1/lineage/edges", json=_edge("a", "h"), headers=ADMIN)
    client.post("/api/v1/lineage/edges", json=_edge("h", "c"), headers=ADMIN)

    # finance owner 视角：a 没有任何可见下游（h 被剪枝，c 不可达）
    r = client.get("/api/v1/impact/a", headers=OWNER_F)
    body = r.json()
    print("\n[finance 视角的 a 影响分析（图中实际隔着 marketing 节点）]")
    print(json.dumps(body, ensure_ascii=False, indent=2))
    assert body["affected_downstream_count"] == 0
    assert body["downstream_entities"] == []
    assert body["paths"] == []

    # 直接血缘视图也不暴露 h
    v = client.get("/api/v1/lineage/a", headers=OWNER_F).json()
    assert v["downstream"] == [] and v["edges"] == []

    # 直接请求隐藏实体：403，且响应不含任何实体字段
    r2 = client.get("/api/v1/entities/h", headers=OWNER_F)
    _show("finance owner 直接请求 marketing 实体", r2)
    assert r2.status_code == 403 and "entity" not in r2.json()

    # 建边引用不可见端点也被拒，错误码与"不存在"可区分但不泄露
    p = _edge("a", "h")
    r3 = client.post("/api/v1/lineage/edges", json=p, headers=OWNER_F)
    _show("finance owner 试图登记到隐藏实体 h 的边", r3, p)
    assert r3.status_code == 403 and r3.json()["error"]["code"] == "FORBIDDEN_SCOPE"

    # 对照组：admin 看到完整链路
    admin_body = client.get("/api/v1/impact/a", headers=ADMIN).json()
    assert {n["id"] for n in admin_body["downstream_entities"]} == {"h", "c"}


def test_hidden_entity_cannot_be_probed_by_edge_existence(client):
    """无权方不能借助"边不存在/已删除"的响应差异探测跨 scope 的边。"""
    make_entity("a", "finance")
    make_entity("m", "marketing")
    client.post("/api/v1/lineage/edges", json=_edge("a", "m"), headers=ADMIN)
    # finance reader 想删除这条跨 scope 边：先报权限（而非 EDGE_NOT_FOUND）
    r = client.request("DELETE", "/api/v1/lineage/edges", json=_edge("a", "m"),
                       headers=READER_F)
    _show("reader 删除跨 scope 边（探测存在性）", r)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN_NOT_OWNER"


def test_third_scope_reader_sees_neither_side(client):
    """hr 调用方对 finance/marketing 之间的图完全无感知。"""
    make_entity("a", "finance")
    make_entity("m", "marketing")
    client.post("/api/v1/lineage/edges", json=_edge("a", "m"), headers=ADMIN)

    assert client.get("/api/v1/entities", headers=READER_HR).json()["total"] == 0
    for ref in ("a", "m"):
        r = client.get(f"/api/v1/impact/{ref}", headers=READER_HR)
        assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN_SCOPE"
    print("\n[第三方 scope] 搜索为空、影响分析 403：无法推断任何邻接信息")


def test_admin_sees_everything_and_owner_scope_is_exact(client):
    make_entity("a", "finance")
    make_entity("m", "marketing")
    admin_items = client.get("/api/v1/entities", headers=ADMIN).json()["items"]
    f_items = client.get("/api/v1/entities", headers=READER_F).json()["items"]
    m_items = client.get("/api/v1/entities", headers=READER_M).json()["items"]
    print("\n[可见范围]", "admin:", [i["id"] for i in admin_items],
          "finance:", [i["id"] for i in f_items],
          "marketing:", [i["id"] for i in m_items])
    assert {i["id"] for i in admin_items} == {"a", "m"}
    assert [i["id"] for i in f_items] == ["a"]
    assert [i["id"] for i in m_items] == ["m"]
