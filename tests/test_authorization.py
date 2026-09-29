"""服务端鉴权：越权拒绝原因可区分，隐藏实体不可被枚举/推断。"""
from __future__ import annotations

from .conftest import headers


def _setup(client):
    client.post("/entities", headers=headers("alice"), json={
        "entity_id": "secret", "name": "secret_table", "entity_type": "table",
        "owner": "alice", "description": "sensitive",
        "fields": [{"name": "id", "field_type": "integer"}],
    })
    client.post("/entities", headers=headers("bob"), json={
        "entity_id": "pub", "name": "pub_table", "entity_type": "table", "owner": "bob",
        "fields": [{"name": "id", "field_type": "integer"}],
    })


def test_missing_identity_is_unauthenticated(client):
    r = client.get("/entities/pub")
    print(f"\n[输入] 不带 X-Caller-Id 直查实体")
    print(f"[判定] {r.status_code} error={r.json()['error']}（与越权 403 原因可区分）")
    assert r.status_code == 401
    assert r.json()["error"] == "unauthenticated"


def test_forbidden_vs_not_found_distinguishable(client):
    _setup(client)
    r_hidden = client.get("/entities/secret", headers=headers("bob"))
    r_missing = client.get("/entities/ghost", headers=headers("bob"))
    print(f"\n[输入] bob 直查存在但不可见的 secret 与不存在的 ghost")
    print(f"[判定] secret -> {r_hidden.status_code} {r_hidden.json()['error']}；"
          f"ghost -> {r_missing.status_code} {r_missing.json()['error']}（直查场景原因可区分）")
    assert r_hidden.status_code == 403
    assert r_hidden.json()["error"] == "forbidden"
    assert r_missing.status_code == 404
    assert r_missing.json()["error"] == "entity_not_found"


def test_identity_in_body_cannot_bypass(client):
    _setup(client)
    # 请求体/参数里自称 alice 不应被采信：身份只取自请求头
    r = client.get("/entities/secret?caller_id=alice", headers=headers("bob"))
    print(f"\n[输入] bob 在查询参数自称 caller_id=alice")
    print(f"[判定] {r.status_code} {r.json()['error']}（服务端只认 X-Caller-Id 头）")
    assert r.status_code == 403


def test_cannot_register_entity_owned_by_other(client):
    r = client.post("/entities", headers=headers("bob"), json={
        "entity_id": "x", "name": "x", "entity_type": "table", "owner": "alice", "fields": []})
    print(f"\n[输入] bob 登记 owner=alice 的实体")
    print(f"[判定] {r.status_code} {r.json()['error']}")
    assert r.status_code == 403


def test_non_owner_cannot_mutate(client):
    _setup(client)
    r = client.post("/entities/secret/deprecate",
                    headers=headers("bob"), json={"reason": "x"})
    print(f"\n[输入] bob 下线 alice 的 secret")
    print(f"[判定] {r.status_code} {r.json()['error']}")
    assert r.status_code == 403
    r2 = client.post("/entities/secret/rename",
                     headers=headers("bob"), json={"new_name": "hacked"})
    assert r2.status_code == 403


def test_non_admin_cannot_grant(client):
    _setup(client)
    r = client.post("/admin/grants", headers=headers("alice"),
                    json={"entity_id": "pub", "grantee": "alice"})
    print(f"\n[输入] 非 admin 尝试下发授权")
    print(f"[判定] {r.status_code} {r.json()['error']}")
    assert r.status_code == 403


def test_grant_opens_visibility(client):
    _setup(client)
    g = client.post("/admin/grants", headers=headers("root", admin=True),
                    json={"entity_id": "secret", "grantee": "bob"})
    print(f"\n[输入] admin 给 bob 授予 secret 可见性 -> {g.json()['status']}")
    r = client.get("/entities/secret", headers=headers("bob"))
    print(f"[判定] 授权后 bob 直查 secret: {r.status_code}")
    assert r.status_code == 200
    # 重复授权幂等
    g2 = client.post("/admin/grants", headers=headers("root", admin=True),
                     json={"entity_id": "secret", "grantee": "bob"})
    assert g2.json()["status"] == "already_granted"


def test_hidden_entity_absent_from_search(client):
    _setup(client)
    r = client.post("/entities/search", headers=headers("bob"), json={"query": "secret"})
    print(f"\n[输入] bob 用关键字 'secret' 搜索")
    print(f"[判定] 结果={[e['entity_id'] for e in r.json()]}（隐藏实体直接缺席，"
          f"无法区分不存在/不可见，存在性不可推断）")
    assert [e["entity_id"] for e in r.json()] == []
    # owner 自己能搜到
    r2 = client.post("/entities/search", headers=headers("alice"), json={"query": "secret"})
    assert [e["entity_id"] for e in r2.json()] == ["secret"]


def test_hidden_entity_absent_from_impact_and_lineage(client):
    _setup(client)
    # 血缘 secret -> pub（由 pub 的 owner bob 登记）
    client.post("/lineage", headers=headers("bob"), json={
        "source_id": "secret", "target_id": "pub",
        "field_mappings": [{"source_field": "id", "target_field": "id"}]})
    # bob 对 pub 查上游：secret 不可见，必须整体剪枝
    impact = client.get("/impact/pub?direction=upstream", headers=headers("bob")).json()
    lineage = client.get("/lineage/pub", headers=headers("bob")).json()
    print(f"\n[输入] 存在 secret->pub，但 bob 对 secret 无可见性")
    print(f"[判定] bob 的上游影响分析={[n['entity_id'] for n in impact['impacted']]}；"
          f"上游血缘边数={len(lineage['upstream'])}（隐藏实体及其出边均不可推断）")
    assert impact["impacted"] == []
    assert lineage["upstream"] == []
    # admin 可见完整链路
    impact_admin = client.get(
        "/impact/pub?direction=upstream", headers=headers("root", admin=True)).json()
    assert [n["entity_id"] for n in impact_admin["impacted"]] == ["secret"]


def test_admin_sees_everything(client):
    _setup(client)
    r = client.post("/entities/search", headers=headers("root", admin=True), json={"query": ""})
    ids = sorted(e["entity_id"] for e in r.json())
    print(f"\n[输入] admin 无过滤搜索")
    print(f"[判定] 可见实体={ids}")
    assert ids == ["pub", "secret"]
