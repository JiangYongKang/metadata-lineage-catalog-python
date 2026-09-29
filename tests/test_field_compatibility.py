"""字段新增/删除/改名的兼容规则与可区分拒绝原因。"""
from __future__ import annotations

from .conftest import headers

BASE = {
    "entity_id": "t", "name": "t", "entity_type": "table", "owner": "alice",
    "fields": [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "price", "field_type": "float", "nullable": True},
        {"name": "label", "field_type": "string", "nullable": True},
    ],
}


def _update(client, fields):
    payload = {k: BASE[k] for k in ("entity_id", "name", "entity_type", "owner")}
    payload["fields"] = fields
    return client.post("/entities", headers=headers("alice"), json=payload)


def test_add_field_is_accepted(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, BASE["fields"] + [{"name": "note", "field_type": "string"}])
    print(f"\n[输入] 新增字段 note:string")
    print(f"[判定] {r.status_code} revision={r.json()['revision']} "
          f"字段={sorted(f['name'] for f in r.json()['fields'])} -> 新增默认允许")
    assert r.status_code == 201
    assert {f["name"] for f in r.json()["fields"]} == {"id", "price", "label", "note"}


def test_type_widening_integer_to_float_accepted(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "float", "nullable": False},
        {"name": "price", "field_type": "float"},
        {"name": "label", "field_type": "string"},
    ])
    print("\n[输入] id integer -> float（widening）")
    print(f"[判定] {r.status_code} -> 数值放宽兼容")
    assert r.status_code == 201


def test_type_narrowing_float_to_integer_rejected(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "price", "field_type": "integer"},
        {"name": "label", "field_type": "string"},
    ])
    body = r.json()
    print("\n[输入] price float -> integer（narrowing）")
    print(f"[判定] {r.status_code} error={body['error']} cause={body['detail']['cause']} "
          f"from={body['detail']['from_type']} to={body['detail']['to_type']}")
    assert r.status_code == 422
    assert body["error"] == "incompatible_field_change"
    assert body["detail"]["cause"] == "type_not_compatible"
    assert body["detail"]["field"] == "price"


def test_type_change_string_to_integer_rejected(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "price", "field_type": "float"},
        {"name": "label", "field_type": "integer"},
    ])
    print("\n[输入] label string -> integer（跨类型族）")
    print(f"[判定] {r.status_code} cause={r.json()['detail']['cause']}")
    assert r.status_code == 422
    assert r.json()["detail"]["cause"] == "type_not_compatible"


def test_nullable_tightening_rejected(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "price", "field_type": "float", "nullable": False},
        {"name": "label", "field_type": "string"},
    ])
    print("\n[输入] price nullable true -> false（收紧约束）")
    print(f"[判定] {r.status_code} cause={r.json()['detail']['cause']}")
    assert r.status_code == 422
    assert r.json()["detail"]["cause"] == "nullable_tightened"


def test_nullable_relaxation_accepted(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "integer", "nullable": True},
        {"name": "price", "field_type": "float"},
        {"name": "label", "field_type": "string"},
    ])
    print("\n[输入] id nullable false -> true（放开约束）")
    print(f"[判定] {r.status_code}")
    assert r.status_code == 201


def test_duplicate_field_in_submission_rejected(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "id", "field_type": "string"},
        {"name": "price", "field_type": "float"},
        {"name": "label", "field_type": "string"},
    ])
    print("\n[输入] 同一提交内 id 出现两次")
    print(f"[判定] {r.status_code} error={r.json()['error']} field={r.json()['detail']['field']}")
    assert r.status_code == 422
    assert r.json()["error"] == "duplicate_field"


def test_explicit_rename_migrates_field(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "price", "field_type": "float"},
        {"name": "name", "field_type": "string", "rename_from": "label"},
    ])
    print("\n[输入] label 显式改名为 name（rename_from='label'）")
    print(f"[判定] {r.status_code} 字段={sorted(f['name'] for f in r.json()['fields'])}")
    assert r.status_code == 201
    assert {f["name"] for f in r.json()["fields"]} == {"id", "price", "name"}


def test_rename_with_missing_source_rejected(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "price", "field_type": "float"},
        {"name": "label", "field_type": "string"},
        {"name": "ghost", "field_type": "string", "rename_from": "nonexistent"},
    ])
    print("\n[输入] 改名源 nonexistent 不存在")
    print(f"[判定] {r.status_code} cause={r.json()['detail']['cause']}")
    assert r.status_code == 422
    assert r.json()["detail"]["cause"] == "rename_source_missing"


def test_swap_style_rename_rejected(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    # id 改名为 price，但 price 当前仍存在（交换式/目标冲突）
    r = _update(client, [
        {"name": "price", "field_type": "integer", "nullable": False, "rename_from": "id"},
        {"name": "price2", "field_type": "float", "rename_from": "price"},
        {"name": "label", "field_type": "string"},
    ])
    print("\n[输入] id->price 但目标 price 当前仍存在（交换式改名）")
    print(f"[判定] {r.status_code} cause={r.json()['detail']['cause']}")
    assert r.status_code == 422
    assert r.json()["detail"]["cause"] == "rename_target_exists"


def test_delete_unreferenced_field_accepted(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = _update(client, [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "price", "field_type": "float"},
    ])
    print("\n[输入] 删除无血缘引用的字段 label")
    print(f"[判定] {r.status_code} 剩余字段={sorted(f['name'] for f in r.json()['fields'])}")
    assert r.status_code == 201
    assert "label" not in {f["name"] for f in r.json()["fields"]}


def test_delete_referenced_field_rejected(client):
    # t1.id -> t2.pk，删除 t1.id 必须被拒绝
    client.post("/entities", headers=headers("alice"), json={
        "entity_id": "t1", "name": "t1", "entity_type": "table", "owner": "alice",
        "fields": [{"name": "id", "field_type": "integer", "nullable": False}],
    })
    client.post("/entities", headers=headers("bob"), json={
        "entity_id": "t2", "name": "t2", "entity_type": "table", "owner": "bob",
        "fields": [{"name": "pk", "field_type": "integer", "nullable": False}],
    })
    client.post("/lineage", headers=headers("bob"), json={
        "source_id": "t1", "target_id": "t2",
        "field_mappings": [{"source_field": "id", "target_field": "pk"}],
    })
    r = client.post("/entities", headers=headers("alice"), json={
        "entity_id": "t1", "name": "t1", "entity_type": "table", "owner": "alice",
        "fields": [],
    })
    print("\n[输入] 删除被血缘引用的字段 t1.id")
    print(f"[判定] {r.status_code} error={r.json()['error']} referenced={r.json()['detail']['referenced']}")
    assert r.status_code == 422
    assert r.json()["detail"]["referenced"] == {"id": ["t2.pk"]}


def test_entity_type_immutable(client):
    client.post("/entities", headers=headers("alice"), json=BASE)
    r = client.post("/entities", headers=headers("alice"), json={
        "entity_id": "t", "name": "t", "entity_type": "topic", "owner": "alice",
        "fields": [],
    })
    print("\n[输入] 把已登记 table 重新登记为 topic")
    print(f"[判定] {r.status_code} error={r.json()['error']}")
    assert r.status_code == 409
    assert r.json()["error"] == "entity_spec_immutable"
