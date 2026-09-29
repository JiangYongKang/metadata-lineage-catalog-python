"""领域模型与错误码（类型定义层）。"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- 错误码

class ErrorCode(str, Enum):
    # 认证 / 授权
    UNAUTHENTICATED = "UNAUTHENTICATED"          # 缺少或无法识别调用方身份
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"          # 身份合法但可见范围不足
    FORBIDDEN_NOT_OWNER = "FORBIDDEN_NOT_OWNER"  # 非属主且非管理员执行属主操作
    # 实体登记 / 变更
    ENTITY_NOT_FOUND = "ENTITY_NOT_FOUND"
    FIELD_REQUIRED_MISSING = "FIELD_REQUIRED_MISSING"        # 非法：必填字段无 default
    FIELD_REMOVED = "FIELD_REMOVED"                          # 破坏性：删除字段
    FIELD_RENAMED = "FIELD_RENAMED"                          # 破坏性：未声明改名即换名
    FIELD_TYPE_INCOMPATIBLE = "FIELD_TYPE_INCOMPATIBLE"      # 非法：不兼容类型变更
    FIELD_OPTIONAL_TO_REQUIRED = "FIELD_OPTIONAL_TO_REQUIRED"  # 破坏性
    RENAMED_FIELD_NOT_FOUND = "RENAMED_FIELD_NOT_FOUND"      # 非法：renames 指向不存在的旧字段
    RENAMED_FIELD_TARGET_EXISTS = "RENAMED_FIELD_TARGET_EXISTS"  # 非法：新名已被占用
    VERSION_CONFLICT = "VERSION_CONFLICT"                    # 并发提交基于过期版本
    ENTITY_ALREADY_RETIRED = "ENTITY_ALREADY_RETIRED"        # 对已下线实体做变更
    ENTITY_ACTIVE = "ENTITY_ACTIVE"                          # 已存在同名活跃实体
    # 血缘
    UPSTREAM_NOT_FOUND = "UPSTREAM_NOT_FOUND"
    DOWNSTREAM_NOT_FOUND = "DOWNSTREAM_NOT_FOUND"
    SELF_EDGE_FORBIDDEN = "SELF_EDGE_FORBIDDEN"              # 非法：自环
    DUPLICATE_EDGE = "DUPLICATE_EDGE"                        # 重复上报
    CYCLE_DETECTED = "CYCLE_DETECTED"                        # 环形依赖
    EDGE_NOT_FOUND = "EDGE_NOT_FOUND"
    EDGE_ON_RETIRED_ENTITY = "EDGE_ON_RETIRED_ENTITY"        # 非法：端点含已下线实体


# ---------------------------------------------------------------- 枚举

class FieldKind(str, Enum):
    STRING = "string"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    DATE = "date"
    TIMESTAMP = "timestamp"
    JSON = "json"


class EntityKind(str, Enum):
    TABLE = "table"
    TOPIC = "topic"
    API = "api"
    VIEW = "view"


class EntityStatus(str, Enum):
    ACTIVE = "active"
    RETIRED = "retired"


class EdgeStatus(str, Enum):
    ACTIVE = "active"
    DELETED = "deleted"


# ---------------------------------------------------------------- 实体 / 字段

class FieldDef(BaseModel):
    name: str
    kind: FieldKind
    required: bool = False
    default: str | None = None
    description: str = ""


class FieldDefIn(BaseModel):
    name: str
    kind: FieldKind
    required: bool = False
    default: str | None = None
    description: str = ""


class EntityDef(BaseModel):
    id: str
    name: str
    kind: EntityKind
    scope: str
    description: str = ""
    version: int = 1
    status: EntityStatus = EntityStatus.ACTIVE
    aliases: list[str] = Field(default_factory=list)
    fields: list[FieldDef] = Field(default_factory=list)
    created_at: str
    updated_at: str


class EntityUpsertRequest(BaseModel):
    id: str
    name: str
    kind: EntityKind
    scope: str
    description: str = ""
    fields: list[FieldDefIn] = Field(default_factory=list)
    # 期望的前置版本；缺省表示仅新建。重复上报（内容一致）不受版本约束。
    expected_version: int | None = None
    # 声明的字段改名映射：old_name -> new_name
    renames: dict[str, str] = Field(default_factory=dict)
    # 显式确认接受破坏性变更（删除/改名/收紧必填）
    allow_breaking: bool = False


class EntityRetireRequest(BaseModel):
    expected_version: int | None = None


# ---------------------------------------------------------------- 血缘

class EdgeDef(BaseModel):
    upstream_id: str
    downstream_id: str
    transform: str = ""
    status: EdgeStatus = EdgeStatus.ACTIVE
    created_at: str


class EdgeUpsertRequest(BaseModel):
    upstream_id: str
    downstream_id: str
    transform: str = ""


class EdgeDeleteRequest(BaseModel):
    upstream_id: str
    downstream_id: str


class NodeRef(BaseModel):
    id: str
    name: str
    kind: EntityKind
    status: EntityStatus


class LineageView(BaseModel):
    entity: NodeRef
    upstream: list[NodeRef]
    downstream: list[NodeRef]
    edges: list[EdgeDef]


class ImpactReport(BaseModel):
    origin: NodeRef
    downstream_entities: list[NodeRef]
    paths: list[list[str]]
    affected_downstream_count: int


class SearchResult(BaseModel):
    items: list[EntityDef]
    total: int


# ---------------------------------------------------------------- 调用方

class Caller(BaseModel):
    id: str
    role: str           # admin / owner / reader
    scopes: list[str]   # 可见数据范围；admin 视为全部
