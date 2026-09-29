"""进程内共享单例的唯一来源，避免跨模块导入产生多份存储。"""
from __future__ import annotations

from .store import CatalogStore

store = CatalogStore()
