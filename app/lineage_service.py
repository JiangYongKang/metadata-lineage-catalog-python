"""上下游血缘登记、查询与影响分析。

口径摘要（详见 docs）：
- 边的身份是 (upstream_id, downstream_id)；重复上报同一对节点得到明确的
  DUPLICATE_EDGE 错误（或幂等返回同一条边），不会静默覆盖。
- 任何会使图成环的新增都被拒绝，错误码 CYCLE_DETECTED，并带回环路径作为判定依据。
- 端点必须存在且为活跃状态；自环直接拒绝。
- 删除边为软删除（保留历史），不影响已经给出的历史结论。
- 查询与影响分析只在调用方可见子图内进行：不可见实体及其邻边被整体剪枝，
  无法通过路径或计数推断隐藏实体的存在。
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from app.errors import CatalogError
from app.models import (
    Caller,
    EdgeDef,
    EdgeStatus,
    EdgeUpsertRequest,
    EntityDef,
    EntityStatus,
    ErrorCode,
    ImpactReport,
    LineageView,
    NodeRef,
)
from app.security import can_modify_entity, can_see_entity
from app.store import Store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_ref(entity: EntityDef) -> NodeRef:
    return NodeRef(id=entity.id, name=entity.name, kind=entity.kind, status=entity.status)


class LineageService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ---------------------------------------------------------------- 登记边

    def add_edge(self, caller: Caller, req: EdgeUpsertRequest) -> tuple[EdgeDef, bool]:
        if req.upstream_id == req.downstream_id:
            raise CatalogError(
                ErrorCode.SELF_EDGE_FORBIDDEN,
                f"自环不被接受：{req.upstream_id} -> {req.downstream_id}",
                reason="血缘边必须连接两个不同实体",
            )

        with self.store.lock:
            upstream = self.store.get_entity(req.upstream_id)
            downstream = self.store.get_entity(req.downstream_id)
            if upstream is None:
                raise CatalogError(
                    ErrorCode.UPSTREAM_NOT_FOUND,
                    f"上游实体不存在：{req.upstream_id}",
                    reason="登记血缘前两端实体都必须先登记",
                )
            if downstream is None:
                raise CatalogError(
                    ErrorCode.DOWNSTREAM_NOT_FOUND,
                    f"下游实体不存在：{req.downstream_id}",
                    reason="登记血缘前两端实体都必须先登记",
                )
            # 服务端权限判定：两端都必须可见，且对下游（数据消费方）有属主级权限
            if not can_see_entity(caller, upstream):
                raise CatalogError(
                    ErrorCode.FORBIDDEN_SCOPE,
                    f"caller '{caller.id}' 无权引用上游 scope '{upstream.scope}' 的实体",
                    reason=f"服务端按名册判定：role={caller.role}, scopes={caller.scopes}",
                )
            if not can_see_entity(caller, downstream):
                raise CatalogError(
                    ErrorCode.FORBIDDEN_SCOPE,
                    f"caller '{caller.id}' 无权向下游 scope '{downstream.scope}' 登记血缘",
                    reason=f"服务端按名册判定：role={caller.role}, scopes={caller.scopes}",
                )
            # 建边代表"下游消费上游"，登记方必须对下游实体有属主级权限；
            # reader 即使两端可见也不能建边。
            if not can_modify_entity(caller, downstream):
                raise CatalogError(
                    ErrorCode.FORBIDDEN_NOT_OWNER,
                    f"caller '{caller.id}' 无权作为下游 '{downstream.id}' 登记血缘",
                    reason=f"建边要求下游实体的属主或管理员权限；"
                    f"服务端按名册判定：role={caller.role}, scopes={caller.scopes}",
                )
            if (
                upstream.status == EntityStatus.RETIRED
                or downstream.status == EntityStatus.RETIRED
            ):
                raise CatalogError(
                    ErrorCode.EDGE_ON_RETIRED_ENTITY,
                    "血缘端点包含已下线实体，不能新增边",
                    reason=(
                        f"upstream.status={upstream.status.value}, "
                        f"downstream.status={downstream.status.value}；"
                        "历史边保留可查，但下线实体不能参与新血缘"
                    ),
                )

            existing = self.store.get_edge(req.upstream_id, req.downstream_id)
            if existing is not None and existing.status == EdgeStatus.ACTIVE:
                # 重复上报：明确标记，不静默覆盖 transform
                if existing.transform == req.transform:
                    return existing, False  # 完全相同的重复提交：幂等
                raise CatalogError(
                    ErrorCode.DUPLICATE_EDGE,
                    f"血缘边已存在：{req.upstream_id} -> {req.downstream_id}",
                    reason=f"已登记 transform={existing.transform!r}；"
                    f"本次 transform={req.transform!r} 与之不同。"
                    "同一对节点只允许一条边；重复上报不会静默覆盖，"
                    "如需调整请先删除再重新登记",
                )

            active = {
                (e.upstream_id, e.downstream_id)
                for e in self.store.active_edges()
            }
            cycle = self._would_cycle(active, req.upstream_id, req.downstream_id)
            if cycle is not None:
                raise CatalogError(
                    ErrorCode.CYCLE_DETECTED,
                    f"新增边将形成环：{' -> '.join(cycle)}",
                    reason="血缘图必须为 DAG；检测到下游可经现有边回到上游，"
                    f"环路径为 {' -> '.join(cycle)}",
                )

            # 若之前被软删除，则同身份边复活
            edge = EdgeDef(
                upstream_id=req.upstream_id,
                downstream_id=req.downstream_id,
                transform=req.transform,
                status=EdgeStatus.ACTIVE,
                created_at=_now(),
            )
            self.store.put_edge(edge)
            return edge, True

    def delete_edge(self, caller: Caller, req: EdgeUpsertRequest) -> None:
        with self.store.lock:
            upstream = self.store.get_entity(req.upstream_id)
            downstream = self.store.get_entity(req.downstream_id)
            # 先做权限判定，避免无权方借助"边是否存在"的响应差异推断隐藏信息
            if upstream is not None and downstream is not None and not (
                can_modify_entity(caller, downstream)
                or can_modify_entity(caller, upstream)
            ):
                raise CatalogError(
                    ErrorCode.FORBIDDEN_NOT_OWNER,
                    f"caller '{caller.id}' 无权删除该血缘边",
                    reason="只有上游或下游实体的属主/管理员可以删除边",
                )
            edge = self.store.get_edge(req.upstream_id, req.downstream_id)
            if edge is None or edge.status == EdgeStatus.DELETED:
                raise CatalogError(
                    ErrorCode.EDGE_NOT_FOUND,
                    f"活跃血缘边不存在：{req.upstream_id} -> {req.downstream_id}",
                    reason="删除要求边当前处于活跃状态（重复删除不被静默接受）",
                )
            edge.status = EdgeStatus.DELETED
            self.store.put_edge(edge)

    # ---------------------------------------------------------------- 查询

    def _resolve_visible(self, caller: Caller, ref: str) -> EntityDef:
        entity = self.store.get_entity(ref) or self.store.find_by_name(ref)
        if entity is None:
            raise CatalogError(
                ErrorCode.ENTITY_NOT_FOUND,
                f"实体不存在：{ref}",
                reason="按稳定 ID、当前名与历史别名均未找到",
            )
        if not can_see_entity(caller, entity):
            raise CatalogError(
                ErrorCode.FORBIDDEN_SCOPE,
                f"caller '{caller.id}' 无权查看 scope '{entity.scope}' 的实体",
                reason=f"服务端按名册判定：role={caller.role}, scopes={caller.scopes}",
            )
        return entity

    def lineage(self, caller: Caller, ref: str) -> LineageView:
        with self.store.lock:
            entity = self._resolve_visible(caller, ref)
            upstream: list[NodeRef] = []
            downstream: list[NodeRef] = []
            visible_edges: list[EdgeDef] = []
            for edge in self.store.active_edges():
                if edge.upstream_id == entity.id:
                    other = self.store.get_entity(edge.downstream_id)
                    if other is not None and can_see_entity(caller, other):
                        downstream.append(_to_ref(other))
                        visible_edges.append(edge)
                elif edge.downstream_id == entity.id:
                    other = self.store.get_entity(edge.upstream_id)
                    if other is not None and can_see_entity(caller, other):
                        upstream.append(_to_ref(other))
                        visible_edges.append(edge)
            upstream.sort(key=lambda n: n.id)
            downstream.sort(key=lambda n: n.id)
            return LineageView(
                entity=_to_ref(entity),
                upstream=upstream,
                downstream=downstream,
                edges=visible_edges,
            )

    def impact(self, caller: Caller, ref: str) -> ImpactReport:
        """下游影响分析（BFS，仅在可见子图内传播）。

        - 只沿"两端均可见"的活跃边前进，隐藏实体阻断传播且不出现在路径里。
        - 返回所有可达下游实体、去重后的简单传播路径与计数。
        - 实体改名/下线后以稳定 ID 计算，历史结论保持自洽。
        """
        with self.store.lock:
            origin = self._resolve_visible(caller, ref)

            # 构建调用方可见的活跃邻接表
            visible_ids = {
                eid
                for eid, ent in self.store.entities.items()
                if can_see_entity(caller, ent)
            }
            adj: dict[str, list[str]] = {eid: [] for eid in visible_ids}
            for edge in self.store.active_edges():
                if edge.upstream_id in visible_ids and edge.downstream_id in visible_ids:
                    adj[edge.upstream_id].append(edge.downstream_id)
            for lst in adj.values():
                lst.sort()

            reached: dict[str, list[list[str]]] = {}
            # BFS 记录路径；路径长度有界（简单路径），图为 DAG 故不会无限
            queue = deque([[origin.id]])
            while queue:
                path = queue.popleft()
                node = path[-1]
                for nxt in adj.get(node, []):
                    new_path = path + [nxt]
                    if nxt == origin.id:
                        continue  # 理论上不成环，防御性处理
                    if nxt not in path:
                        reached.setdefault(nxt, [])
                        # 保留至多 16 条简单路径，避免超大图爆炸
                        if len(reached[nxt]) < 16:
                            reached[nxt].append(new_path)
                        queue.append(new_path)

            nodes: list[NodeRef] = []
            all_paths: list[list[str]] = []
            for eid in sorted(reached):
                ent = self.store.get_entity(eid)
                nodes.append(_to_ref(ent))
                all_paths.extend(reached[eid])
            all_paths.sort(key=lambda p: (len(p), p))
            return ImpactReport(
                origin=_to_ref(origin),
                downstream_entities=nodes,
                paths=all_paths,
                affected_downstream_count=len(nodes),
            )

    # ---------------------------------------------------------------- 环检测

    @staticmethod
    def _would_cycle(
        active: set[tuple[str, str]], upstream_id: str, downstream_id: str
    ) -> list[str] | None:
        """加入 upstream->downstream 后若成环，返回环上的节点序列（含首尾），否则 None。

        成环条件：当前图中存在 downstream_id -> ... -> upstream_id 的路径。
        """
        # 邻接表
        adj: dict[str, list[str]] = {}
        for u, d in active:
            adj.setdefault(u, []).append(d)
        # BFS 从 downstream 找回到 upstream 的路径
        queue = deque([[downstream_id]])
        visited = {downstream_id}
        while queue:
            path = queue.popleft()
            node = path[-1]
            if node == upstream_id:
                return path + [downstream_id]
            for nxt in adj.get(node, []):
                if nxt not in visited:
                    visited.add(nxt)
                    queue.append(path + [nxt])
        return None
