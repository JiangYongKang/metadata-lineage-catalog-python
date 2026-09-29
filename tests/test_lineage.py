"""血缘登记 / 环形依赖 / 重复上报 / 影响分析 / 改名下线后的历史自洽。"""
from __future__ import annotations

import json

from tests.conftest import make_entity

ADMIN = {"X-Caller-Id": "admin"}
OWNER_F = {"X-Caller-Id": "owner-finance"}
OWNER_M = {"X-Caller-Id": "owner-marketing"}
READER_F = {"X-Caller-Id": "reader-finance"}
READER_M = {"X-Caller-Id": "reader-marketing"}


def _edge(u: str, d: str, transform: str = "") -> dict:
    return {"upstream_id": u, "downstream_id": d, "transform": transform}


def _show(title: str, payload: dict, resp) -> None:
    print(f"\n[{title}]")
    print("  输入:", json.dumps(payload, ensure_ascii=False))
    print("  判定: HTTP", resp.status_code,
          json.dumps(resp.json(), ensure_ascii=False)[:600])


def test_duplicate_edge_is_flagged_not_silently_merged(client):
    make_entity("a", "finance")
    make_entity("b", "finance")

    r1 = client.post("/api/v1/lineage/edges", json=_edge("a", "b", "daily_etl"), headers=ADMIN)
    _show("首次登记 a->b", _edge("a", "b", "daily_etl"), r1)
    assert r1.status_code == 200 and r1.json()["created"] is True

    # 完全相同的重复：幂等
    r2 = client.post("/api/v1/lineage/edges", json=_edge("a", "b", "daily_etl"), headers=ADMIN)
    _show("完全相同的重复上报", _edge("a", "b", "daily_etl"), r2)
    assert r2.status_code == 200 and r2.json()["created"] is False

    # transform 冲突的重复：明确拒绝，不静默覆盖
    r3 = client.post("/api/v1/lineage/edges", json=_edge("a", "b", "other_job"), headers=ADMIN)
    _show("同一边不同 transform 的重复上报", _edge("a", "b", "other_job"), r3)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "DUPLICATE_EDGE"
    # 原 transform 未被覆盖
    r4 = client.get("/api/v1/lineage/a", headers=ADMIN)
    assert r4.json()["edges"][0]["transform"] == "daily_etl"


def test_self_loop_rejected(client):
    make_entity("a", "finance")
    r = client.post("/api/v1/lineage/edges", json=_edge("a", "a"), headers=ADMIN)
    _show("自环 a->a", _edge("a", "a"), r)
    assert r.status_code == 422 and r.json()["error"]["code"] == "SELF_EDGE_FORBIDDEN"


def test_cycle_detected_and_marked(client):
    """a->b->c 已存在时再登记 c->a，必须明确报 CYCLE_DETECTED 并给出环路径。"""
    for cid in ("a", "b", "c"):
        make_entity(cid, "finance")
    client.post("/api/v1/lineage/edges", json=_edge("a", "b"), headers=ADMIN)
    client.post("/api/v1/lineage/edges", json=_edge("b", "c"), headers=ADMIN)

    r = client.post("/api/v1/lineage/edges", json=_edge("c", "a"), headers=ADMIN)
    _show("登记 c->a（会成环）", _edge("c", "a"), r)
    assert r.status_code == 409
    body = r.json()
    assert body["error"]["code"] == "CYCLE_DETECTED"
    # 环路径作为判定依据返回，且包含全部三个节点
    assert "a" in body["error"]["message"] and "b" in body["error"]["message"]

    # 被拒绝的边没有产生副作用：a 的上游仍然为空
    view = client.get("/api/v1/lineage/a", headers=ADMIN).json()
    assert view["upstream"] == []


def test_endpoints_must_exist_and_be_active(client):
    make_entity("a", "finance")
    r1 = client.post("/api/v1/lineage/edges", json=_edge("a", "ghost"), headers=ADMIN)
    assert r1.status_code == 404 and r1.json()["error"]["code"] == "DOWNSTREAM_NOT_FOUND"
    r2 = client.post("/api/v1/lineage/edges", json=_edge("ghost", "a"), headers=ADMIN)
    assert r2.status_code == 404 and r2.json()["error"]["code"] == "UPSTREAM_NOT_FOUND"

    # 下线后不能新增边
    client.post("/api/v1/entities/a/retire", json={}, headers=ADMIN)
    make_entity("b", "finance")
    r3 = client.post("/api/v1/lineage/edges", json=_edge("b", "a"), headers=ADMIN)
    _show("以下线实体为端点登记新边", _edge("b", "a"), r3)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "EDGE_ON_RETIRED_ENTITY"


def test_impact_analysis_reports_all_downstream_and_paths(client):
    """a->b->d, a->c->d：影响分析给出全部下游与传播路径。"""
    for cid in ("a", "b", "c", "d"):
        make_entity(cid, "finance")
    client.post("/api/v1/lineage/edges", json=_edge("a", "b"), headers=ADMIN)
    client.post("/api/v1/lineage/edges", json=_edge("a", "c"), headers=ADMIN)
    client.post("/api/v1/lineage/edges", json=_edge("b", "d"), headers=ADMIN)
    client.post("/api/v1/lineage/edges", json=_edge("c", "d"), headers=ADMIN)

    r = client.get("/api/v1/impact/a", headers=ADMIN)
    body = r.json()
    print("\n[a 的影响分析]", json.dumps(body, ensure_ascii=False, indent=2))
    assert body["affected_downstream_count"] == 3
    assert [n["id"] for n in body["downstream_entities"]] == ["b", "c", "d"]
    assert ["a", "b", "d"] in body["paths"]
    assert ["a", "c", "d"] in body["paths"]

    # d 是末端：无下游
    assert client.get("/api/v1/impact/d", headers=ADMIN).json()[
        "affected_downstream_count"] == 0


def test_history_stays_consistent_after_rename_and_retire(client):
    """改名后按旧名仍可查到血缘；下线后历史血缘与影响分析仍自洽。"""
    for cid in ("a", "b", "c"):
        make_entity(cid, "finance")
    client.post("/api/v1/lineage/edges", json=_edge("a", "b"), headers=ADMIN)
    client.post("/api/v1/lineage/edges", json=_edge("b", "c"), headers=ADMIN)

    # a 改名为 alpha：旧名查询血缘仍命中同一节点
    client.post("/api/v1/entities", json={
        "id": "a", "name": "alpha", "kind": "table", "scope": "finance",
        "expected_version": 1, "fields": []}, headers=ADMIN)
    r_old = client.get("/api/v1/lineage/a", headers=ADMIN)
    r_alias = client.get("/api/v1/lineage/alpha", headers=ADMIN)
    print("\n[改名后旧名查询血缘]", r_old.json()["entity"]["name"],
          "/", r_alias.json()["entity"]["name"])
    assert r_old.json()["entity"]["id"] == "a"
    assert r_alias.json()["entity"]["name"] == "alpha"
    assert [n["id"] for n in r_old.json()["downstream"]] == ["b"]

    # 下线 b：历史边仍在，影响分析仍能沿 a->b->c 给出结论，b 带 retired 标记
    client.post("/api/v1/entities/b/retire", json={}, headers=ADMIN)
    impact = client.get("/api/v1/impact/a", headers=ADMIN).json()
    ids = {n["id"]: n["status"] for n in impact["downstream_entities"]}
    print("[b 下线后 a 的影响分析]", ids)
    assert ids == {"b": "retired", "c": "active"}


def test_edge_soft_delete_then_recreate(client):
    make_entity("a", "finance")
    make_entity("b", "finance")
    client.post("/api/v1/lineage/edges", json=_edge("a", "b"), headers=ADMIN)

    r = client.request("DELETE", "/api/v1/lineage/edges", json=_edge("a", "b"), headers=ADMIN)
    print("\n[删除边]", r.status_code, r.json())
    assert r.status_code == 200
    # 重复删除得到明确错误
    r2 = client.request("DELETE", "/api/v1/lineage/edges", json=_edge("a", "b"), headers=ADMIN)
    assert r2.status_code == 404 and r2.json()["error"]["code"] == "EDGE_NOT_FOUND"
    # 影响分析不再沿已删除边传播
    assert client.get("/api/v1/impact/a", headers=ADMIN).json()[
        "affected_downstream_count"] == 0
    # 同身份边可以重新登记
    r3 = client.post("/api/v1/lineage/edges", json=_edge("a", "b"), headers=ADMIN)
    assert r3.status_code == 200 and r3.json()["created"] is True
