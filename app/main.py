"""FastAPI 入口：元数据目录服务的 HTTP 路由。

骨架阶段仅完成装配：鉴权依赖、统一异常处理、路由 -> store 的委托。
"""
from __future__ import annotations

from fastapi import Depends, FastAPI

from .errors import RejectedError
from .models import (
    CreateLineageRequest,
    DeprecateEntityRequest,
    GrantVisibilityRequest,
    ImpactAnalysisResult,
    LineageResult,
    Principal,
    RegisterEntityRequest,
    RenameEntityRequest,
    SearchEntitiesRequest,
)
from .security import ensure_can_view, require_principal
from .state import store

app = FastAPI(title="metadata-lineage-catalog", version="0.1.0")


@app.exception_handler(RejectedError)
async def rejected_handler(_request, exc: RejectedError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=exc.http_status,
        content={"error": exc.reason, "reason": exc.message, "detail": exc.detail},
    )


@app.exception_handler(ValueError)
async def value_error_handler(_request, exc: ValueError):
    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=400,
        content={"error": "invalid_argument", "reason": str(exc), "detail": {}},
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


# -- 实体 --------------------------------------------------------------------
@app.post("/entities", status_code=201)
def register_entity(req: RegisterEntityRequest, principal: Principal = Depends(require_principal)):
    return store.register_entity(req, principal)


@app.get("/entities/{entity_id}")
def get_entity(entity_id: str, principal: Principal = Depends(require_principal)):
    ensure_can_view(principal, entity_id)
    return store.get_entity(entity_id)


@app.post("/entities/{entity_id}/rename")
def rename_entity(
    entity_id: str, req: RenameEntityRequest, principal: Principal = Depends(require_principal)
):
    return store.rename_entity(entity_id, req, principal)


@app.post("/entities/{entity_id}/deprecate")
def deprecate_entity(
    entity_id: str, req: DeprecateEntityRequest, principal: Principal = Depends(require_principal)
):
    return store.deprecate_entity(entity_id, req, principal)


@app.post("/entities/search")
def search_entities(req: SearchEntitiesRequest, principal: Principal = Depends(require_principal)):
    return store.search_entities(principal, req.query, req.entity_type, req.status)


# -- 血缘 --------------------------------------------------------------------
@app.post("/lineage", status_code=201)
def create_lineage(req: CreateLineageRequest, principal: Principal = Depends(require_principal)):
    return store.create_lineage(req, principal)


@app.get("/lineage/{entity_id}")
def get_lineage(entity_id: str, principal: Principal = Depends(require_principal)) -> LineageResult:
    ensure_can_view(principal, entity_id)
    return store.get_lineage(entity_id, principal)


@app.get("/impact/{entity_id}")
def impact_analysis(
    entity_id: str,
    direction: str = "downstream",
    principal: Principal = Depends(require_principal),
) -> ImpactAnalysisResult:
    ensure_can_view(principal, entity_id)
    return store.impact_analysis(entity_id, direction, principal)


# -- 权限管理 ----------------------------------------------------------------
@app.post("/admin/grants", status_code=201)
def grant_visibility(req: GrantVisibilityRequest, principal: Principal = Depends(require_principal)):
    return store.grant_visibility(principal, req.entity_id, req.grantee)


@app.get("/admin/grants")
def list_grants(principal: Principal = Depends(require_principal)):
    return store.list_grants(principal)
