"""HTTP 路由：服务端身份解析 + 统一错误码响应。

约定：
- 调用方身份只通过 X-Caller-Id 请求头传递，由服务端名册解析；
  请求体中的 role/scope 等字段一律不参与授权。
- 所有业务错误返回统一结构 {"error": {"code", "message", "reason"}}，
  code 为稳定字符串，reason 给出判定依据，便于复核。
- 不存在 / 无权可见统一映射：未认证 401；合法身份但不可见 403，
  响应中不包含实体任何字段（隐藏实体不可从响应差异中被稳定推断）。
"""
from __future__ import annotations

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

from app.deps import caller_directory, entity_service, lineage_service
from app.errors import CatalogError
from app.models import (
    EdgeUpsertRequest,
    EntityRetireRequest,
    EntityUpsertRequest,
    ErrorCode,
)

router = APIRouter(prefix="/api/v1")

_STATUS_BY_CODE: dict[ErrorCode, int] = {
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN_SCOPE: 403,
    ErrorCode.FORBIDDEN_NOT_OWNER: 403,
    ErrorCode.ENTITY_NOT_FOUND: 404,
    ErrorCode.UPSTREAM_NOT_FOUND: 404,
    ErrorCode.DOWNSTREAM_NOT_FOUND: 404,
    ErrorCode.EDGE_NOT_FOUND: 404,
    ErrorCode.ENTITY_ALREADY_RETIRED: 409,
    ErrorCode.ENTITY_ACTIVE: 409,
    ErrorCode.FIELD_REQUIRED_MISSING: 422,
    ErrorCode.FIELD_REMOVED: 409,
    ErrorCode.FIELD_RENAMED: 409,
    ErrorCode.FIELD_TYPE_INCOMPATIBLE: 422,
    ErrorCode.FIELD_OPTIONAL_TO_REQUIRED: 409,
    ErrorCode.RENAMED_FIELD_NOT_FOUND: 422,
    ErrorCode.RENAMED_FIELD_TARGET_EXISTS: 422,
    ErrorCode.VERSION_CONFLICT: 409,
    ErrorCode.SELF_EDGE_FORBIDDEN: 422,
    ErrorCode.DUPLICATE_EDGE: 409,
    ErrorCode.CYCLE_DETECTED: 409,
    ErrorCode.EDGE_ON_RETIRED_ENTITY: 409,
}


def _require_caller(x_caller_id: str | None):
    caller = caller_directory.lookup((x_caller_id or "").strip() or None)
    if caller is None:
        raise CatalogError(
            ErrorCode.UNAUTHENTICATED,
            "缺少或无法识别的调用方身份",
            reason="请通过 X-Caller-Id 头传递服务端名册中已登记的调用方 ID；"
            "请求体中的任何身份/角色声明均不被采信",
        )
    return caller


def _error_response(err: CatalogError) -> JSONResponse:
    status = _STATUS_BY_CODE.get(err.code, 400)
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "code": err.code.value,
                "message": err.message,
                "reason": err.reason,
            }
        },
    )


# ---------------------------------------------------------------- 实体

@router.post("/entities")
def upsert_entity(req: EntityUpsertRequest, x_caller_id: str | None = Header(default=None)):
    try:
        caller = _require_caller(x_caller_id)
        entity, changed = entity_service.upsert(caller, req)
        return {"entity": entity.model_dump(mode="json"), "changed": changed}
    except CatalogError as err:
        return _error_response(err)


@router.get("/entities/{ref}")
def get_entity(ref: str, x_caller_id: str | None = Header(default=None)):
    try:
        caller = _require_caller(x_caller_id)
        return {"entity": entity_service.get(caller, ref).model_dump(mode="json")}
    except CatalogError as err:
        return _error_response(err)


@router.get("/entities")
def search_entities(
    name_contains: str | None = None,
    scope: str | None = None,
    x_caller_id: str | None = Header(default=None),
):
    try:
        caller = _require_caller(x_caller_id)
        result = entity_service.search(caller, name_contains, scope)
        return result.model_dump(mode="json")
    except CatalogError as err:
        return _error_response(err)


@router.post("/entities/{entity_id}/retire")
def retire_entity(
    entity_id: str,
    req: EntityRetireRequest,
    x_caller_id: str | None = Header(default=None),
):
    try:
        caller = _require_caller(x_caller_id)
        entity = entity_service.retire(caller, entity_id, req)
        return {"entity": entity.model_dump(mode="json")}
    except CatalogError as err:
        return _error_response(err)


# ---------------------------------------------------------------- 血缘

@router.post("/lineage/edges")
def add_edge(req: EdgeUpsertRequest, x_caller_id: str | None = Header(default=None)):
    try:
        caller = _require_caller(x_caller_id)
        edge, created = lineage_service.add_edge(caller, req)
        return {"edge": edge.model_dump(mode="json"), "created": created}
    except CatalogError as err:
        return _error_response(err)


@router.delete("/lineage/edges")
def delete_edge(req: EdgeUpsertRequest, x_caller_id: str | None = Header(default=None)):
    try:
        caller = _require_caller(x_caller_id)
        lineage_service.delete_edge(caller, req)
        return {"deleted": True}
    except CatalogError as err:
        return _error_response(err)


@router.get("/lineage/{ref}")
def get_lineage(ref: str, x_caller_id: str | None = Header(default=None)):
    try:
        caller = _require_caller(x_caller_id)
        return lineage_service.lineage(caller, ref).model_dump(mode="json")
    except CatalogError as err:
        return _error_response(err)


@router.get("/impact/{ref}")
def get_impact(ref: str, x_caller_id: str | None = Header(default=None)):
    try:
        caller = _require_caller(x_caller_id)
        return lineage_service.impact(caller, ref).model_dump(mode="json")
    except CatalogError as err:
        return _error_response(err)
