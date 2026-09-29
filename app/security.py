"""服务端调用方身份与可见范围判定（不信任请求体中的任何权限标记）。

身份与角色只来自服务端名册（CallerDirectory），请求里即使携带 role/scopes
字段也一律忽略，因此无法靠请求标记越权。
"""
from __future__ import annotations

from app.models import Caller, EntityDef


class CallerDirectory:
    """调用方名册：服务端持有，启动时配置。"""

    def __init__(self, callers: dict[str, Caller] | None = None) -> None:
        self._callers = dict(callers or {})

    def register(self, caller: Caller) -> None:
        self._callers[caller.id] = caller

    def lookup(self, caller_id: str | None) -> Caller | None:
        if not caller_id:
            return None
        return self._callers.get(caller_id)


def is_admin(caller: Caller) -> bool:
    return caller.role == "admin"


def can_see_entity(caller: Caller, entity: EntityDef) -> bool:
    """可见性判定的唯一入口：admin 全可见，其余按 scope 精确匹配。"""
    if is_admin(caller):
        return True
    return entity.scope in caller.scopes


def can_modify_entity(caller: Caller, entity: EntityDef) -> bool:
    """登记/更新：admin 或与实体同 scope 的 owner。reader 永远只读。"""
    if is_admin(caller):
        return True
    return caller.role == "owner" and entity.scope in caller.scopes


def can_create_in_scope(caller: Caller, scope: str) -> bool:
    """在指定 scope 下新建实体：admin 或持有该 scope 的 owner。"""
    if is_admin(caller):
        return True
    return caller.role == "owner" and scope in caller.scopes
