"""可区分的业务错误。"""
from __future__ import annotations

from app.models import ErrorCode


class CatalogError(Exception):
    """业务异常：携带稳定错误码、可读信息与判定依据。"""

    def __init__(self, code: ErrorCode, message: str, reason: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.reason = reason or message
