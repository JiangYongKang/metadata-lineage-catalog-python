"""服务端拒绝请求时使用的错误类型。

每一类业务拒绝对应一个稳定的 reason 字符串，HTTP 状态码在
ERROR_STATUS 中集中声明，保证对外结论明确且稳定。
"""
from __future__ import annotations


class RejectedError(Exception):
    """所有可预期业务拒绝的基类。"""

    reason: str = "rejected"
    http_status: int = 400

    def __init__(self, message: str = "", detail: dict | None = None):
        super().__init__(message or self.reason)
        self.message = message or self.reason
        self.detail = detail or {}


class UnauthenticatedError(RejectedError):
    reason = "unauthenticated"
    http_status = 401


class ForbiddenError(RejectedError):
    reason = "forbidden"
    http_status = 403


class EntityNotFoundError(RejectedError):
    reason = "entity_not_found"
    http_status = 404


class IncompatibleFieldChangeError(RejectedError):
    reason = "incompatible_field_change"
    http_status = 422


class DuplicateFieldError(RejectedError):
    reason = "duplicate_field"
    http_status = 422


class RenameTargetExistsError(RejectedError):
    reason = "rename_target_exists"
    http_status = 409


class EntityDeprecatedError(RejectedError):
    reason = "entity_deprecated"
    http_status = 409


class CyclicDependencyError(RejectedError):
    reason = "cyclic_dependency"
    http_status = 409


class DuplicateLineageError(RejectedError):
    reason = "duplicate_lineage"
    http_status = 409


class EntitySpecImmutableError(RejectedError):
    reason = "entity_spec_immutable"
    http_status = 409


class LineageFieldNotFoundError(RejectedError):
    reason = "lineage_field_not_found"
    http_status = 422


# reason -> HTTP 状态码的总表（供统一异常处理器使用）
ERROR_STATUS: dict[str, int] = {
    cls.reason: cls.http_status
    for cls in (
        UnauthenticatedError,
        ForbiddenError,
        EntityNotFoundError,
        IncompatibleFieldChangeError,
        DuplicateFieldError,
        RenameTargetExistsError,
        EntityDeprecatedError,
        CyclicDependencyError,
        DuplicateLineageError,
        EntitySpecImmutableError,
        LineageFieldNotFoundError,
    )
}
