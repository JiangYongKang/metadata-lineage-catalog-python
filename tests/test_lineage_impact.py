"""血缘建立、环检测、重复上报标记、影响分析与历史自洽。"""
from __future__ import annotations

from .conftest import headers


def _entity(client, eid, owner, fields=None):
    fields = fields or [{"name": "id", "field_type": "integer", "nullable": False}]
    return client.post("/entities", headers=headers(owner), json={
        "entity_id": eid, "name": eid, "entity_type": "table", "owner": owner,
        "fields": fields,
    })


def test_duplicate_lineage_marked_not_deduplicated(client):
    _entity(client, "a", "alice")
    _entity(client, "b", "bob")
    edge = {"source_id": "a", "target_id": "b",
            "field_mappings": [{"source_field": "id", "target_field": "id"}]}
    r1 = client.post("/lineage", headers=headers("bob"), json=edge)
    r2 = client.post("/lineage", headers=headers("bob"), json=edge)
    print(f"\n[输入] 相同血缘上报两次 a->b")
    print(f"[判定] 第一次 status={r1.json()['status']}；第二次 status={r2.json()['status']}"
          f"（明确标记 duplicate，不静默取一条）")
    assert r1.json()["status"] == "created"
    assert r2.json()["status"] == "duplicate"
    # 物理上仍只有一条边
    lin = client.get("/lineage/b", headers=headers("admin", admin=True))
    assert len(lin.json()["upstream"]) == 1


def test_conflicting_duplicate_lineage_rejected(client):
    _entity(client, "a", "alice", [
        {"name": "id", "field_type": "integer"},
        {"name": "v", "field_type": "integer"},
    ])
    _entity(client, "b", "bob", [
        {"name": "id", "field_type": "integer"},
        {"name": "w", "field_type": "integer"},
    ])
    e1 = {"source_id": "a", "target_id": "b",
          "field_mappings": [{"source_field": "id", "target_field": "id"}]}
    e2 = {"source_id": "a", "target_id": "b",
          "field_mappings": [{"source_field": "v", "target_field": "w"}]}
    client.post("/lineage", headers=headers("bob"), json=e1)
    r = client.post("/lineage", headers=headers("bob"), json=e2)
    print(f"\n[输入] 同一对实体 a->b 的第二次上报使用不同字段映射")
    print(f"[判定] {r.status_code} error={r.json()['error']}（冲突重复不合并、不覆盖）")
    assert r.status_code == 409
    assert r.json()["error"] == "duplicate_lineage"


def test_self_lineage_rejected_as_cycle(client):
    _entity(client, "a", "alice")
    r = client.post("/lineage", headers=headers("alice"), json={
        "source_id": "a", "target_id": "a", "field_mappings": []})
    print(f"\n[输入] a -> a 自环")
    print(f"[判定] {r.status_code} error={r.json()['error']} cycle={r.json()['detail']['cycle']}")
    assert r.status_code == 409
    assert r.json()["error"] == "cyclic_dependency"


def test_cycle_detected(client):
    for e in "abc":
        _entity(client, e, "alice")
    client.post("/lineage", headers=headers("alice"), json={"source_id": "a", "target_id": "b"})
    client.post("/lineage", headers=headers("alice"), json={"source_id": "b", "target_id": "c"})
    r = client.post("/lineage", headers=headers("alice"), json={"source_id": "c", "target_id": "a"})
    print(f"\n[输入] 已存在 a->b->c，再提交 c->a")
    print(f"[判定] {r.status_code} error={r.json()['error']} 成环路径={r.json()['detail']['cycle']}")
    assert r.status_code == 409
    assert r.json()["error"] == "cyclic_dependency"
    assert r.json()["detail"]["cycle"] == ["a", "b", "c", "a"]
    # 被拒绝的边没有落库
    lin = client.get("/lineage/c", headers=headers("admin", admin=True))
    assert lin.json()["downstream"] == []


def test_lineage_field_must_exist(client):
    _entity(client, "a", "alice")
    _entity(client, "b", "bob")
    r = client.post("/lineage", headers=headers("bob"), json={
        "source_id": "a", "target_id": "b",
        "field_mappings": [{"source_field": "ghost", "target_field": "id"}]})
    print(f"\n[输入] 血缘引用了上游不存在的字段 a.ghost")
    print(f"[判定] {r.status_code} error={r.json()['error']} side={r.json()['detail']['side']}")
    assert r.status_code == 422
    assert r.json()["error"] == "lineage_field_not_found"


def test_downstream_impact_distances(client):
    # a -> b -> c 链式
    for e in "abc":
        _entity(client, e, "admin")
    client.post("/lineage", headers=headers("admin", admin=True),
                json={"source_id": "a", "target_id": "b"})
    client.post("/lineage", headers=headers("admin", admin=True),
                json={"source_id": "b", "target_id": "c"})
    r = client.get("/impact/a?direction=downstream", headers=headers("admin", admin=True))
    by_id = {n["entity_id"]: n["distance"] for n in r.json()["impacted"]}
    print(f"\n[输入] 对 a 做下游影响分析，链 a->b->c")
    print(f"[判定] 距离表={by_id}（BFS 跳数）")
    assert by_id == {"b": 1, "c": 2}
    assert r.json()["cycles"] == []


def test_impact_still_consistent_after_rename_and_deprecate(client):
    _entity(client, "a", "alice", [{"name": "id", "field_type": "integer"}])
    _entity(client, "b", "bob", [{"name": "pk", "field_type": "integer"}])
    client.post("/lineage", headers=headers("bob"), json={
        "source_id": "a", "target_id": "b",
        "field_mappings": [{"source_field": "id", "target_field": "pk"}]})
    # 实体改名
    client.post("/entities/a/rename", headers=headers("alice"), json={"new_name": "a_new"})
    # 字段改名
    client.post("/entities", headers=headers("alice"), json={
        "entity_id": "a", "name": "a_new", "entity_type": "table", "owner": "alice",
        "fields": [{"name": "id2", "field_type": "integer", "rename_from": "id"}]})
    # 下线
    client.post("/entities/a/deprecate", headers=headers("alice"), json={"reason": "gone"})

    lin = client.get("/lineage/b", headers=headers("admin", admin=True)).json()
    mapping = [(m["source_field"], m["target_field"])
               for e in lin["upstream"] for m in e["field_mappings"]]
    impact = client.get("/impact/b?direction=upstream",
                        headers=headers("admin", admin=True)).json()
    print(f"\n[输入] a 改名 a_new、字段 id 改名 id2、随后 a 下线")
    print(f"[判定] b 的上游血缘映射={mapping}；影响分析仍指向上游={[n['entity_id'] for n in impact['impacted']]}")
    assert mapping == [("id2", "pk")]
    assert [n["entity_id"] for n in impact["impacted"]] == ["a"]
    node = impact["impacted"][0]
    assert node["name"] == "a_new"
    assert node["via_fields"] == ["id2"]


def test_invalid_direction_is_bad_request(client):
    _entity(client, "a", "alice")
    r = client.get("/impact/a?direction=sideways", headers=headers("admin", admin=True))
    print(f"\n[输入] direction=sideways")
    print(f"[判定] {r.status_code} error={r.json()['error']}（参数错误 400，不与越权 403 混淆）")
    assert r.status_code == 400
    assert r.json()["error"] == "invalid_argument"
