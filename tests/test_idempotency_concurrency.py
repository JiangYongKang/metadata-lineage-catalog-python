"""重复与并发上报场景。

每个用例都会 print 输入与判定依据，方便复核（pytest -s 可见）。
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .conftest import headers


def _payload(extra_fields=None):
    fields = [
        {"name": "id", "field_type": "integer", "nullable": False},
        {"name": "amount", "field_type": "float"},
    ]
    if extra_fields:
        fields += extra_fields
    return {
        "entity_id": "orders",
        "name": "订单表",
        "entity_type": "table",
        "owner": "alice",
        "description": "raw orders",
        "fields": fields,
    }


def test_duplicate_register_is_idempotent(client):
    payload = _payload()
    r1 = client.post("/entities", headers=headers("alice"), json=payload)
    r2 = client.post("/entities", headers=headers("alice"), json=payload)
    print(f"\n[输入] 相同 payload 提交两次: {payload['entity_id']}")
    print(f"[判定] 第一次 {r1.status_code} revision={r1.json()['revision']}; "
          f"第二次 {r2.status_code} revision={r2.json()['revision']}")
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["revision"] == 1
    # 幂等：重复提交不增加 revision、不更新 updated_at 语义
    assert r2.json()["revision"] == 1
    assert r1.json()["created_at"] == r2.json()["created_at"]
    assert r1.json()["fields"] == r2.json()["fields"]


def test_field_order_insensitive_idempotency(client):
    p1 = _payload()
    p2 = _payload()
    p2["fields"] = list(reversed(p2["fields"]))
    r1 = client.post("/entities", headers=headers("alice"), json=p1)
    r2 = client.post("/entities", headers=headers("alice"), json=p2)
    print(f"\n[输入] 字段顺序不同的两次提交: {[f['name'] for f in p1['fields']]} vs "
          f"{[f['name'] for f in p2['fields']]}")
    print(f"[判定] 两次 revision 均为 {r1.json()['revision']}/{r2.json()['revision']} "
          f"-> 字段集合按 name 归一，顺序无关")
    assert r2.json()["revision"] == 1


def test_concurrent_identical_registers(client):
    payload = _payload()
    print(f"\n[输入] 20 个并发相同注册请求: entity={payload['entity_id']} owner={payload['owner']}")

    def submit(_):
        return client.post("/entities", headers=headers("alice"), json=payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(submit, range(20)))
    statuses = [r.status_code for r in responses]
    revisions = {r.json().get("revision") for r in responses}
    print(f"[判定] HTTP 状态集合={set(statuses)}; revision 集合={revisions} "
          f"（服务端串行化判定+写入，只有首次创建生效，其余幂等命中）")
    assert set(statuses) == {201}
    assert revisions == {1}


def test_concurrent_competing_changes_one_wins(client):
    """并发但互不兼容的字段变更：恰好一个成功，另一个拿到明确的拒绝原因。

    初始 id: integer。A 想放宽为 float（兼容），B 想改为 string（不兼容）。
    无论锁先后顺序如何：A 成功、B 以 type_not_compatible 被拒，最终类型
    恒为 float，不会出现两种变更都被接受或状态错乱。
    """
    client.post("/entities", headers=headers("alice"), json=_payload())

    def payload_for_type(t):
        p = _payload()
        p["fields"] = [
            {"name": "id", "field_type": t, "nullable": False},
            {"name": "amount", "field_type": "float"},
        ]
        return p

    def submit(target_type):
        return client.post(
            "/entities", headers=headers("alice"), json=payload_for_type(target_type)
        )

    print("\n[输入] 两个并发类型变更：id integer->float（A，兼容放宽）"
          " 与 id integer->string（B，不兼容）")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futs = [pool.submit(submit, "float"), pool.submit(submit, "string")]
        ra, rb = [f.result() for f in futs]
    results = [(r.status_code, r.json().get("detail", {}).get("cause")) for r in (ra, rb)]
    accepted = [r for r in (ra, rb) if r.status_code == 201]
    rejected = [r for r in (ra, rb) if r.status_code == 422]
    print(f"[判定] 结果={results}；接受数={len(accepted)} 拒绝数={len(rejected)}，"
          f"拒绝 cause={[r.json()['detail'].get('cause') for r in rejected]}")
    assert len(accepted) == 1
    assert len(rejected) == 1
    assert rejected[0].json()["error"] == "incompatible_field_change"
    final = client.get("/entities/orders", headers=headers("alice")).json()
    id_field = next(f for f in final["fields"] if f["name"] == "id")
    print(f"[判定] 最终 id 类型={id_field['field_type']}（与执行顺序无关，恒为 float）")
    assert id_field["field_type"] == "float"


def test_concurrent_lineage_same_edge(client):
    client.post("/entities", headers=headers("alice"), json=_payload())
    client.post("/entities", headers=headers("bob"), json={
        "entity_id": "dwd", "name": "dwd", "entity_type": "table", "owner": "bob",
        "fields": [{"name": "id", "field_type": "integer"}],
    })
    edge = {
        "source_id": "orders", "target_id": "dwd",
        "field_mappings": [{"source_field": "id", "target_field": "id"}],
    }
    print(f"\n[输入] 10 个并发相同血缘上报: {edge['source_id']} -> {edge['target_id']}")

    def submit(_):
        return client.post("/lineage", headers=headers("bob"), json=edge)

    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(submit, range(10)))
    created = [r for r in responses if r.json().get("status") == "created"]
    dupes = [r for r in responses if r.json().get("status") == "duplicate"]
    print(f"[判定] created={len(created)} duplicate={len(dupes)} "
          f"（恰好一条边建立，其余明确标记为 duplicate，而非静默覆盖）")
    assert len(created) == 1 and len(dupes) == 9


def _reason_counts(responses):
    counts: dict[str, int] = {}
    for r in responses:
        try:
            key = r.json().get("error", f"http_{r.status_code}")
        except Exception:
            key = f"http_{r.status_code}"
        counts[key] = counts.get(key, 0) + 1
    return counts
