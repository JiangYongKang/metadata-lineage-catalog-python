"""共享装配：存储、服务、调用方名册。"""
from __future__ import annotations

from app.entity_service import EntityService
from app.lineage_service import LineageService
from app.models import Caller
from app.security import CallerDirectory
from app.store import Store

store = Store()
entity_service = EntityService(store)
lineage_service = LineageService(store)

# 本地默认名册：测试通过 register_caller 扩展。
caller_directory = CallerDirectory(
    {
        "admin": Caller(id="admin", role="admin", scopes=[]),
        "owner-finance": Caller(id="owner-finance", role="owner", scopes=["finance"]),
        "owner-marketing": Caller(id="owner-marketing", role="owner", scopes=["marketing"]),
        "reader-finance": Caller(id="reader-finance", role="reader", scopes=["finance"]),
    }
)


def register_caller(caller: Caller) -> None:
    caller_directory.register(caller)


def reset_all() -> None:
    """测试辅助：清空全部数据。"""
    store.reset()
