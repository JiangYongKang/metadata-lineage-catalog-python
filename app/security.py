"""服务端鉴权。

身份边界（本地服务口径）：调用方身份只从请求头 X-Caller-Id /
X-Caller-Roles 解析，用于模拟已通过网关认证后的下游服务。请求体或查询
参数里自称的身份一律不读取、不可信。

角色：
- admin：可查看全部实体、可下发可见性授权；
- 普通调用方：默认只能看到 owner 为自己、或被显式授权（grant）的实体。

可见性判定本身委托给 store（数据在存储层），路由只调用
ensure_can_view，不允许各自实现一套判定。
"""
from __future__ import annotations

from fastapi import Header

from .errors import UnauthenticatedError
from .models import Principal
from .state import store

ADMIN_ROLE = "admin"


async def require_principal(
    x_caller_id: str | None = Header(default=None),
    x_caller_roles: str | None = Header(default=None),
) -> Principal:
    """从请求头解析调用方身份；缺失身份一律 401 unauthenticated。"""
    if not x_caller_id or not x_caller_id.strip():
        raise UnauthenticatedError(
            "missing caller identity",
            {"required_header": "X-Caller-Id"},
        )
    roles = [r.strip() for r in (x_caller_roles or "").split(",") if r.strip()]
    return Principal(caller_id=x_caller_id.strip(), roles=roles)


def is_admin(principal: Principal) -> bool:
    return ADMIN_ROLE in principal.roles


def can_view(principal: Principal, entity_id: str) -> bool:
    """仅做布尔判定（存在性与可见性合并后的结果），不暴露原因。"""
    return store.is_visible(principal, entity_id)


def ensure_can_view(principal: Principal, entity_id: str) -> None:
    """可见性闸门：不存在 -> entity_not_found；存在但不可见 -> forbidden。

    直接按 ID 访问时 403 与 404 原因可区分；枚举类通道（搜索、影响
    分析）在 store 内直接过滤，不会透出隐藏实体。
    """
    store.ensure_visible(principal, entity_id)
