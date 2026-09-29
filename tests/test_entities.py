"""实体登记：重复/并发上报、字段兼容与改名、越权。

每个用例都会打印请求输入与服务端判定依据（reason），方便人工复核。
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.deps import entity_service
from app.models import (
    Caller,
    EntityKind,
    EntityUpsertRequest,
    FieldDefIn,
    FieldKind,
)

ADMIN = {"X-Caller-Id": "admin"}
OWNER_F = {"X-Caller-Id": "owner-finance"}
OWNER_M = {"X-Caller-Id": "owner-marketing"}
READER_F = {"X-Caller-Id": "reader-finance"}
READER_M = {"X-Caller-Id": "reader-marketing"}


def _show(title: str, payload: dict, resp) -> None:
    print(f"\n[{title}]")
    print("  输入:", json.dumps(payload, ensure_ascii=False))
    body = resp.json() if resp.content else {}
    print("  判定: HTTP", resp.status_code, json.dumps(body, ensure_ascii=False)[:600])


BASE_FIELDS = [
    {"name": "id", "kind": "int", "required": True},
    {"name": "amount", "kind": "int"},
]


def _payload(**overrides) -> dict:
    p = {"id": "orders", "name": "orders", "kind": "table",
         "scope": "finance", "fields": BASE_FIELDS}
    p.update(overrides)
    return p


# ---------------------------------------------------------------- 幂等

def test_duplicate_submission_is_idempotent(client):
    """同一条记录重复提交：第二次 changed=false，版本不增长。"""
    payload = _payload()
    r1 = client.post("/api/v1/entities", json=payload, headers=ADMIN)
    _show("首次登记", payload, r1)
    assert r1.status_code == 200 and r1.json()["changed"] is True
    assert r1.json()["entity"]["version"] == 1

    # 重复提交即使携带过期的 expected_version，只要内容一致也幂等
    payload2 = _payload(expected_version=1)
    r2 = client.post("/api/v1/entities", json=payload2, headers=ADMIN)
    _show("重复上报(带相同版本号)", payload2, r2)
    assert r2.status_code == 200
    assert r2.json()["changed"] is False
    assert r2.json()["entity"]["version"] == 1


def test_concurrent_identical_submissions_all_succeed(client):
    """并发重复上报：全部成功，实体只创建一次，版本停留在 1。"""
    payload = _payload()

    def submit(_):
        r = client.post("/api/v1/entities", json=payload, headers=ADMIN)
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(submit, range(8)))
    print("\n[并发 8 个相同上报]")
    for i, (status, body) in enumerate(results):
        changed = body.get("changed")
        version = body.get("entity", {}).get("version")
        print(f"  worker{i}: HTTP {status} changed={changed} version={version}")

    assert all(status == 200 for status, _ in results)
    created = [body for _, body in results if body.get("changed") is True]
    assert len(created) == 1, "只允许一次真实创建"
    assert {body["entity"]["version"] for _, body in results} == {1}


def test_concurrent_conflicting_updates_one_wins(client):
    """并发的不一致变更：乐观锁只放行一个，其余得到可区分的拒绝码。

    每个 worker 都新增各自的可选字段、基于 v1 提交。只有一个能成功；
    其余要么 VERSION_CONFLICT（版本过期），要么 FIELD_REMOVED（相对最新版本
    等于要删掉别人已登记的字段）。两种拒绝都不会被静默接受。
    """
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)

    def add_field(suffix: str):
        # 每人独立字段，避免共享可变对象
        fields = [dict(f) for f in BASE_FIELDS] + [
            {"name": f"f_{suffix}", "kind": "string"}
        ]
        p = _payload(expected_version=1, fields=fields)
        r = client.post("/api/v1/entities", json=p, headers=ADMIN)
        return r.status_code, r.json()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(add_field, [f"w{i}" for i in range(6)]))
    print("\n[并发 6 个互斥字段变更]")
    codes = []
    for i, (status, body) in enumerate(results):
        code = body.get("error", {}).get("code") or "OK"
        codes.append(code)
        print(f"  worker{i}: HTTP {status} code={code} "
              f"reason={body.get('error', {}).get('reason', '')[:90]}")

    assert codes.count("OK") == 1, f"只应一个成功，实际：{codes}"
    rejected = [c for c in codes if c != "OK"]
    assert set(rejected) <= {"VERSION_CONFLICT", "FIELD_REMOVED"}, codes
    assert len(rejected) == 5

    # 失败后读取最新版本再提交：可以串行成功，证明结论稳定、可恢复
    latest = client.get("/api/v1/entities/orders", headers=ADMIN).json()["entity"]
    fields = [{"name": f["name"], "kind": f["kind"], "required": f["required"]}
              for f in latest["fields"]]
    fields.append({"name": "f_retry", "kind": "string"})
    r = client.post("/api/v1/entities", json=_payload(
        expected_version=latest["version"], fields=fields), headers=ADMIN)
    assert r.status_code == 200 and r.json()["changed"] is True


# ---------------------------------------------------------------- 字段兼容

def test_add_optional_field_compatible(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1,
                 fields=BASE_FIELDS + [{"name": "note", "kind": "string"}])
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("新增可选字段", p, r)
    assert r.status_code == 200 and r.json()["entity"]["version"] == 2


def test_add_required_field_without_default_rejected(client):
    """非法变更：新增无 default 的必填字段，allow_breaking 也无法绕过。"""
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1, allow_breaking=True,
                 fields=BASE_FIELDS + [{"name": "ssn", "kind": "string", "required": True}])
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("新增无默认必填字段(即使 allow_breaking)", p, r)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "FIELD_REQUIRED_MISSING"
    assert r.json()["entity" if False else "error"]["reason"]  # reason 必填，便于复核


def test_remove_field_requires_explicit_confirmation(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1, fields=[BASE_FIELDS[0]])
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("未确认直接删除字段", p, r)
    assert r.status_code == 409 and r.json()["error"]["code"] == "FIELD_REMOVED"

    p_ok = _payload(expected_version=1, allow_breaking=True, fields=[BASE_FIELDS[0]])
    r2 = client.post("/api/v1/entities", json=p_ok, headers=ADMIN)
    _show("显式确认后删除字段", p_ok, r2)
    assert r2.status_code == 200
    assert [f["name"] for f in r2.json()["entity"]["fields"]] == ["id"]


def test_undeclared_rename_treated_as_remove_and_add(client):
    """未在 renames 声明的换名 = 删除旧字段，按删除规则拒绝。"""
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1,
                 fields=[{"name": "id", "kind": "int", "required": True},
                         {"name": "amount_cny", "kind": "int"}])
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("未声明改名(amount->amount_cny)", p, r)
    assert r.status_code == 409 and r.json()["error"]["code"] == "FIELD_REMOVED"


def test_declared_rename_needs_confirmation_then_keeps_identity(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1, renames={"amount": "amount_cny"},
                 fields=[{"name": "id", "kind": "int", "required": True},
                         {"name": "amount_cny", "kind": "int"}])
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("已声明改名但未确认", p, r)
    assert r.status_code == 409 and r.json()["error"]["code"] == "FIELD_RENAMED"

    p_ok = {**p, "allow_breaking": True}
    r2 = client.post("/api/v1/entities", json=p_ok, headers=ADMIN)
    _show("显式确认改名", p_ok, r2)
    assert r2.status_code == 200
    assert [f["name"] for f in r2.json()["entity"]["fields"]] == ["id", "amount_cny"]


def test_incompatible_type_change_always_rejected(client):
    """非法变更：string 与 int 互不兼容，allow_breaking 也不接受。"""
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1, allow_breaking=True,
                 fields=[{"name": "id", "kind": "string", "required": True},
                         {"name": "amount", "kind": "int"}])
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("int->string 不兼容类型变更", p, r)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "FIELD_TYPE_INCOMPATIBLE"


def test_int_to_float_is_compatible(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1,
                 fields=[{"name": "id", "kind": "int", "required": True},
                         {"name": "amount", "kind": "float"}])
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("int->float 兼容放宽", p, r)
    assert r.status_code == 200


def test_optional_to_required_without_default_is_breaking(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1,
                 fields=[{"name": "id", "kind": "int", "required": True},
                         {"name": "amount", "kind": "int", "required": True}])
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("可选收紧为必填(无默认值)", p, r)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "FIELD_OPTIONAL_TO_REQUIRED"


def test_renames_referencing_unknown_field_rejected(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1, renames={"nope": "x"},
                 fields=BASE_FIELDS)
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("renames 指向不存在的旧字段", p, r)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RENAMED_FIELD_NOT_FOUND"


def test_stale_version_on_real_change_conflicts(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    # 先推进到 v2
    client.post("/api/v1/entities", json=_payload(
        expected_version=1, description="v2 desc"), headers=ADMIN)
    # 再拿 v1 做真实变更 -> 冲突
    p = _payload(expected_version=1, fields=BASE_FIELDS, description="late")
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("基于过期版本提交真实变更", p, r)
    assert r.status_code == 409 and r.json()["error"]["code"] == "VERSION_CONFLICT"


# ---------------------------------------------------------------- 改名 / 下线

def test_entity_rename_keeps_id_and_historical_alias(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1, name="orders_v2")
    r = client.post("/api/v1/entities", json=p, headers=ADMIN)
    _show("实体改名 orders->orders_v2", p, r)
    assert r.status_code == 200
    assert r.json()["entity"]["id"] == "orders"
    assert r.json()["entity"]["aliases"] == ["orders"]

    r_old = client.get("/api/v1/entities/orders", headers=ADMIN)
    r_new = client.get("/api/v1/entities/orders_v2", headers=ADMIN)
    assert r_old.status_code == r_new.status_code == 200
    assert r_old.json()["entity"]["version"] == r_new.json()["entity"]["version"]


def test_retire_is_explicit_and_repeat_gives_stable_error(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    r1 = client.post("/api/v1/entities/orders/retire",
                     json={"expected_version": 1}, headers=ADMIN)
    print("\n[首次下线]", r1.status_code, r1.json()["entity"]["status"])
    assert r1.status_code == 200 and r1.json()["entity"]["status"] == "retired"

    r2 = client.post("/api/v1/entities/orders/retire",
                     json={"expected_version": 2}, headers=ADMIN)
    print("[重复下线]", r2.status_code, r2.json()["error"]["code"],
          r2.json()["error"]["reason"])
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "ENTITY_ALREADY_RETIRED"

    # 下线后不能继续变更
    r3 = client.post("/api/v1/entities", json=_payload(expected_version=2,
                     description="x"), headers=ADMIN)
    assert r3.status_code == 409
    assert r3.json()["error"]["code"] == "ENTITY_ALREADY_RETIRED"


# ---------------------------------------------------------------- 越权

def test_missing_or_unknown_identity_is_unauthenticated(client):
    r1 = client.get("/api/v1/entities")
    print("\n[无身份头]", r1.status_code, r1.json()["error"]["code"])
    assert r1.status_code == 401 and r1.json()["error"]["code"] == "UNAUTHENTICATED"

    r2 = client.get("/api/v1/entities", headers={"X-Caller-Id": "ghost"})
    print("[未知身份]", r2.status_code, r2.json()["error"]["code"])
    assert r2.status_code == 401


def test_scope_is_enforced_server_side(client):
    """跨 scope 读取/搜索被拒；请求体内的 scope/role 标记无效。"""
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)

    r = client.get("/api/v1/entities/orders", headers=READER_M)
    _show("marketing reader 读取 finance 实体", {"ref": "orders"}, r)
    assert r.status_code == 403 and r.json()["error"]["code"] == "FORBIDDEN_SCOPE"

    r2 = client.get("/api/v1/entities", headers=READER_M)
    print("[marketing 搜索列表] total =", r2.json().get("total"))
    assert r2.status_code == 200 and r2.json()["total"] == 0

    # reader 不能登记；即便在 body 里伪造 role/scope 也无效
    p = _payload(id="ghost-entity", scope="marketing", role="admin")
    r3 = client.post("/api/v1/entities", json=p, headers=READER_M)
    _show("reader 伪造 role=admin 登记", p, r3)
    assert r3.status_code == 403
    assert r3.json()["error"]["code"] == "FORBIDDEN_SCOPE"


def test_owner_cannot_touch_other_scope(client):
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    p = _payload(expected_version=1, description="cross-scope write")
    r = client.post("/api/v1/entities", json=p, headers=OWNER_M)
    _show("marketing owner 修改 finance 实体", p, r)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN_NOT_OWNER"


def test_scope_filter_cannot_widen_visibility(client):
    """显式 scope=finance 过滤也不能让 marketing 调用方看到 finance 数据。"""
    client.post("/api/v1/entities", json=_payload(), headers=ADMIN)
    r = client.get("/api/v1/entities?scope=finance", headers=OWNER_M)
    body = r.json()
    print("\n[跨 scope 强制过滤参数]", body)
    assert r.status_code == 200 and body["total"] == 0 and body["items"] == []
