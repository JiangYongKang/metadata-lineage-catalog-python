"""线程安全的内存存储（单进程本地服务）。

所有结构性变更（登记实体、增删血缘边）都必须在持有 ``lock`` 的情况下完成，
以保证并发提交串行化，配合版本号实现幂等。
"""
from __future__ import annotations

import threading

from app.models import EdgeDef, EntityDef


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.entities: dict[str, EntityDef] = {}
        # (upstream_id, downstream_id) -> EdgeDef（含已删除边，保留历史）
        self.edges: dict[tuple[str, str], EdgeDef] = {}

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ----- 实体 -----
    def get_entity(self, entity_id: str) -> EntityDef | None:
        return self.entities.get(entity_id)

    def find_by_name(self, name: str) -> EntityDef | None:
        """按当前名或历史别名精确查找。"""
        for entity in self.entities.values():
            if entity.name == name or name in entity.aliases:
                return entity
        return None

    def put_entity(self, entity: EntityDef) -> None:
        self.entities[entity.id] = entity

    # ----- 血缘 -----
    def get_edge(self, upstream_id: str, downstream_id: str) -> EdgeDef | None:
        return self.edges.get((upstream_id, downstream_id))

    def put_edge(self, edge: EdgeDef) -> None:
        self.edges[(edge.upstream_id, edge.downstream_id)] = edge

    def active_edges(self) -> list[EdgeDef]:
        return [e for e in self.edges.values() if e.status.value == "active"]

    def reset(self) -> None:
        with self._lock:
            self.entities.clear()
            self.edges.clear()
