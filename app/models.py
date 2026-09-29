"""类型定义：数据实体、字段、血缘、权限相关的请求与响应模型。

本文件只包含数据结构定义，不包含业务逻辑。
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 基础枚举
# ---------------------------------------------------------------------------
class EntityType(str, Enum):
    TABLE = "table"
    TOPIC = "topic"
    API = "api"
    DASHBOARD = "dashboard"


class EntityStatus(str, Enum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


# 字段类型使用受限的字符串字面量，便于做兼容性判定
FieldTypeName = Literal["string", "integer", "float", "boolean", "date", "timestamp", "bytes"]


# ---------------------------------------------------------------------------
# 字段与实体
# ---------------------------------------------------------------------------
class FieldSpec(BaseModel):
    """登记/更新时单个字段的声明。"""

    name: str = Field(min_length=1)
    field_type: FieldTypeName
    nullable: bool = True
    description: str = ""
    # 显式改名声明：本字段由同实体的旧字段 rename_from 改名而来；
    # 不允许靠"删除旧字段 + 新增同名字段"隐式推断改名。
    rename_from: str | None = None


class FieldView(BaseModel):
    """对外暴露的字段视图（不含提交专用的 rename_from）。"""

    name: str
    field_type: FieldTypeName
    nullable: bool
    description: str = ""


class RegisterEntityRequest(BaseModel):
    entity_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    entity_type: EntityType
    owner: str = Field(min_length=1)
    description: str = ""
    fields: list[FieldSpec] = Field(default_factory=list)


class EntityView(BaseModel):
    entity_id: str
    name: str
    entity_type: EntityType
    owner: str
    description: str
    status: EntityStatus
    fields: list[FieldView]
    created_at: str
    updated_at: str
    revision: int
    renamed_from: str | None = None


# ---------------------------------------------------------------------------
# 血缘
# ---------------------------------------------------------------------------
class FieldMapping(BaseModel):
    """上游字段到下游字段的映射。"""

    source_field: str = Field(min_length=1)
    target_field: str = Field(min_length=1)


class CreateLineageRequest(BaseModel):
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    field_mappings: list[FieldMapping] = Field(default_factory=list)
    description: str = ""


class LineageEdgeView(BaseModel):
    source_id: str
    target_id: str
    field_mappings: list[FieldMapping]
    description: str
    created_at: str
    updated_at: str


class LineageResult(BaseModel):
    entity_id: str
    upstream: list[LineageEdgeView] = Field(default_factory=list)
    downstream: list[LineageEdgeView] = Field(default_factory=list)


class ImpactNode(BaseModel):
    entity_id: str
    name: str
    entity_type: EntityType
    distance: int
    via_fields: list[str] = Field(default_factory=list)


class ImpactAnalysisResult(BaseModel):
    entity_id: str
    direction: Literal["upstream", "downstream"]
    impacted: list[ImpactNode] = Field(default_factory=list)
    cycles: list[list[str]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 实体改名 / 下线 / 搜索 / 权限
# ---------------------------------------------------------------------------
class RenameEntityRequest(BaseModel):
    new_name: str = Field(min_length=1)


class DeprecateEntityRequest(BaseModel):
    reason: str = ""


class SearchEntitiesRequest(BaseModel):
    query: str = ""
    entity_type: EntityType | None = None
    status: EntityStatus | None = None


class Principal(BaseModel):
    """经过服务端鉴权后得到的调用方身份。请求里自称的身份不可信。"""

    caller_id: str
    roles: list[str] = Field(default_factory=list)
    grants: list[str] = Field(default_factory=list)


class GrantVisibilityRequest(BaseModel):
    entity_id: str = Field(min_length=1)
    grantee: str = Field(min_length=1)


class ErrorResponse(BaseModel):
    error: str
    reason: str
    detail: dict = Field(default_factory=dict)
