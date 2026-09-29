"""内存存储与全部领域操作。

并发口径：进程内一把可重入锁，每个公开方法内的"判定 + 写入"整体原子，
因此并发提交只有一个提交能写入，其余提交要么幂等命中、要么拿到稳定的
拒绝原因，不会出现交错的中间态。
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

from .compat import plan_field_changes
from .errors import (
    CyclicDependencyError,
    DuplicateLineageError,
    EntityDeprecatedError,
    EntityNotFoundError,
    EntitySpecImmutableError,
    ForbiddenError,
    LineageFieldNotFoundError,
)
from .errors import IncompatibleFieldChangeError
from .models import (
    CreateLineageRequest,
    DeprecateEntityRequest,
    EntityStatus,
    EntityType,
    EntityView,
    FieldSpec,
    FieldView,
    ImpactAnalysisResult,
    ImpactNode,
    LineageEdgeView,
    LineageResult,
    Principal,
    RegisterEntityRequest,
    RenameEntityRequest,
)

ADMIN_ROLE = "admin"


def is_admin(principal: Principal) -> bool:
    return ADMIN_ROLE in principal.roles


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class _Entity:
    __slots__ = (
        "entity_id", "name", "entity_type", "owner", "description",
        "status", "fields", "created_at", "updated_at", "revision",
        "renamed_from", "deprecate_reason",
    )

    def __init__(self, req: RegisterEntityRequest):
        now = _now()
        self.entity_id = req.entity_id
        self.name = req.name
        self.entity_type = req.entity_type
        self.owner = req.owner
        self.description = req.description
        self.status = EntityStatus.ACTIVE
        self.fields = {f.name: f for f in req.fields}
        self.created_at = now
        self.updated_at = now
        self.revision = 1
        self.renamed_from: str | None = None
        self.deprecate_reason = ""


class _Edge:
    __slots__ = (
        "source_id", "target_id", "mappings", "description",
        "created_at", "updated_at",
    )

    def __init__(self, req: CreateLineageRequest):
        now = _now()
        self.source_id = req.source_id
        self.target_id = req.target_id
        # 排序后去重存放，作为"同一上报"的规范化形态
        self.mappings = sorted({(m.source_field, m.target_field) for m in req.field_mappings})
        self.description = req.description
        self.created_at = now
        self.updated_at = now


class CatalogStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entities: dict[str, _Entity] = {}
        # (source_id, target_id) -> _Edge
        self._edges: dict[tuple[str, str], _Edge] = {}
        self._down: dict[str, set[str]] = {}
        self._up: dict[str, set[str]] = {}
        # entity_id -> set(grantee)
        self._grants: dict[str, set[str]] = {}

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _get_active(self, entity_id: str) -> _Entity:
        ent = self._entities.get(entity_id)
        if ent is None:
            raise EntityNotFoundError(
                f"entity not found: {entity_id}", {"entity_id": entity_id}
            )
        return ent

    def _require_active(self, entity_id: str) -> _Entity:
        ent = self._get_active(entity_id)
        if ent.status is EntityStatus.DEPRECATED:
            raise EntityDeprecatedError(
                f"entity is deprecated: {entity_id}",
                {"entity_id": entity_id, "reason": ent.deprecate_reason},
            )
        return ent

    def _ensure_owner_or_admin(self, ent: _Entity, principal: Principal) -> None:
        if is_admin(principal) or ent.owner == principal.caller_id:
            return
        raise ForbiddenError(
            "mutation requires owner or admin",
            {"entity_id": ent.entity_id, "owner": ent.owner, "caller": principal.caller_id},
        )

    @staticmethod
    def _specs_same(ent: _Entity, req: RegisterEntityRequest) -> bool:
        """提交与现存实体在语义上完全一致（忽略字段顺序），用于幂等短路。"""
        if len(ent.fields) != len(req.fields):
            return False
        for f in req.fields:
            old = ent.fields.get(f.name)
            if old is None:
                return False
            if (old.field_type, old.nullable, old.description) != (
                f.field_type, f.nullable, f.description,
            ):
                return False
        return True

    def _to_view(self, ent: _Entity) -> EntityView:
        return EntityView(
            entity_id=ent.entity_id,
            name=ent.name,
            entity_type=ent.entity_type,
            owner=ent.owner,
            description=ent.description,
            status=ent.status,
            fields=[
                FieldView(
                    name=f.name, field_type=f.field_type,
                    nullable=f.nullable, description=f.description,
                )
                for f in (ent.fields[k] for k in sorted(ent.fields))
            ],
            created_at=ent.created_at,
            updated_at=ent.updated_at,
            revision=ent.revision,
            renamed_from=ent.renamed_from,
        )

    def _edge_view(self, edge: _Edge) -> LineageEdgeView:
        from .models import FieldMapping

        return LineageEdgeView(
            source_id=edge.source_id,
            target_id=edge.target_id,
            field_mappings=[FieldMapping(source_field=s, target_field=t) for s, t in edge.mappings],
            description=edge.description,
            created_at=edge.created_at,
            updated_at=edge.updated_at,
        )

    def _name_owned(self, name: str, exclude_id: str | None = None) -> str | None:
        for eid, ent in self._entities.items():
            if eid != exclude_id and ent.name == name:
                return eid
        return None

    # ------------------------------------------------------------------ #
    # 实体登记 / 更新
    # ------------------------------------------------------------------ #
    def register_entity(
        self, req: RegisterEntityRequest, principal: Principal
    ) -> EntityView:
        with self._lock:
            # 登记时声明的 owner 必须就是调用方本人；admin 代登记也必须
            # 显式以 req.owner 作为归属，不能冒用他人身份
            if req.owner != principal.caller_id and not is_admin(principal):
                raise ForbiddenError(
                    "cannot register an entity owned by another caller",
                    {"submitted_owner": req.owner, "caller": principal.caller_id},
                )
            existing = self._entities.get(req.entity_id)
            if existing is None:
                # 新建：entity_id 新，但 name 全局唯一（含已下线实体）
                clash = self._name_owned(req.name)
                if clash is not None:
                    raise EntitySpecImmutableError(
                        f"entity name already in use by '{clash}'",
                        {"name": req.name, "entity_id": clash},
                    )
                # index_fields 会拒绝重名字段
                from .compat import index_fields

                index_fields(req.fields)
                ent = _Entity(req)
                self._entities[ent.entity_id] = ent
                self._down.setdefault(ent.entity_id, set())
                self._up.setdefault(ent.entity_id, set())
                return self._to_view(ent)

            # 已存在：先做写权限判定（幂等提交同样要过闸门，结论才稳定）
            self._ensure_owner_or_admin(existing, principal)
            if existing.owner != req.owner:
                raise EntitySpecImmutableError(
                    "owner is immutable",
                    {"field": "owner", "existing": existing.owner, "submitted": req.owner},
                )
            # 幂等 / 更新判定
            if existing.status is EntityStatus.DEPRECATED:
                raise EntityDeprecatedError(
                    f"entity is deprecated: {req.entity_id}",
                    {"entity_id": req.entity_id},
                )
            if existing.entity_type is not req.entity_type:
                raise EntitySpecImmutableError(
                    "entity_type is immutable",
                    {"field": "entity_type", "existing": existing.entity_type.value,
                     "submitted": req.entity_type.value},
                )
            # 幂等短路：语义完全相同的重复提交（name/description/fields 均一致）
            if (
                existing.name == req.name
                and existing.description == req.description
                and self._specs_same(existing, req)
            ):
                return self._to_view(existing)
            if existing.name != req.name:
                raise EntitySpecImmutableError(
                    "name cannot change via register; use rename endpoint",
                    {"field": "name", "existing": existing.name, "submitted": req.name},
                )
            # 字段变更（含显式改名、删除引用检查、类型/可空性兼容）
            plan = plan_field_changes(existing.fields, req.fields)
            self._reject_referenced_deletions(existing.entity_id, plan.deletions)
            self._apply_field_plan(existing, plan)
            existing.description = req.description
            existing.updated_at = _now()
            existing.revision += 1
            return self._to_view(existing)

    def _reject_referenced_deletions(self, entity_id: str, deletions: list[str]) -> None:
        """被血缘字段映射引用的字段不得静默删除。"""
        referenced: dict[str, list[str]] = {}
        for (s, t), edge in self._edges.items():
            if s == entity_id:
                for src_f, tgt_f in edge.mappings:
                    if src_f in deletions:
                        referenced.setdefault(src_f, []).append(
                            f"{t}.{tgt_f}"
                        )
            if t == entity_id:
                for src_f, tgt_f in edge.mappings:
                    if tgt_f in deletions:
                        referenced.setdefault(tgt_f, []).append(
                            f"{s}.{src_f}"
                        )
        if referenced:
            raise IncompatibleFieldChangeError(
                "deleted fields are still referenced by lineage mappings",
                {"entity_id": entity_id, "referenced": referenced},
            )

    def _apply_field_plan(self, ent: _Entity, plan) -> None:
        # 改名：迁移字段对象与血缘映射引用
        for old_name, new_name in plan.renames:
            spec = ent.fields.pop(old_name)
            spec.name = new_name
            spec.rename_from = None
            ent.fields[new_name] = spec
            self._rewrite_field_refs(ent.entity_id, old_name, new_name)
        # 删除（引用已在调用前拒绝）
        for name in plan.deletions:
            ent.fields.pop(name, None)
        # 新增 / 更新
        for name, spec in plan.new_index.items():
            stored = FieldSpec(
                name=name, field_type=spec.field_type,
                nullable=spec.nullable, description=spec.description,
            )
            ent.fields[name] = stored

    def _rewrite_field_refs(self, entity_id: str, old_name: str, new_name: str) -> None:
        for (s, t), edge in self._edges.items():
            changed = False
            new_mappings = []
            for src_f, tgt_f in edge.mappings:
                if s == entity_id and src_f == old_name:
                    new_mappings.append((new_name, tgt_f))
                    changed = True
                elif t == entity_id and tgt_f == old_name:
                    new_mappings.append((src_f, new_name))
                    changed = True
                else:
                    new_mappings.append((src_f, tgt_f))
            if changed:
                edge.mappings = sorted(set(new_mappings))
                edge.updated_at = _now()

    # ------------------------------------------------------------------ #
    # 查询 / 改名 / 下线
    # ------------------------------------------------------------------ #
    def get_entity(self, entity_id: str) -> EntityView:
        with self._lock:
            return self._to_view(self._get_active(entity_id))

    def rename_entity(
        self, entity_id: str, req: RenameEntityRequest, principal: Principal
    ) -> EntityView:
        with self._lock:
            ent = self._require_active(entity_id)
            self._ensure_owner_or_admin(ent, principal)
            if ent.name == req.new_name:
                return self._to_view(ent)  # 改名幂等
            clash = self._name_owned(req.new_name, exclude_id=entity_id)
            if clash is not None:
                from .errors import RenameTargetExistsError

                raise RenameTargetExistsError(
                    f"name already in use by '{clash}'",
                    {"new_name": req.new_name, "entity_id": clash},
                )
            old_name = ent.name
            ent.renamed_from = old_name
            ent.name = req.new_name
            ent.updated_at = _now()
            ent.revision += 1
            return self._to_view(ent)

    def deprecate_entity(
        self, entity_id: str, req: DeprecateEntityRequest, principal: Principal
    ) -> EntityView:
        with self._lock:
            ent = self._get_active(entity_id)
            self._ensure_owner_or_admin(ent, principal)
            if ent.status is EntityStatus.DEPRECATED:
                # 重复下线幂等：原因取已有值
                return self._to_view(ent)
            ent.status = EntityStatus.DEPRECATED
            ent.deprecate_reason = req.reason
            ent.updated_at = _now()
            ent.revision += 1
            return self._to_view(ent)

    # ------------------------------------------------------------------ #
    # 血缘
    # ------------------------------------------------------------------ #
    def create_lineage(
        self, req: CreateLineageRequest, principal: Principal
    ) -> dict:
        with self._lock:
            if req.source_id == req.target_id:
                raise CyclicDependencyError(
                    "self lineage is not allowed",
                    {"cycle": [req.source_id, req.source_id]},
                )
            source = self._require_active(req.source_id)
            target = self._require_active(req.target_id)
            # 血缘由下游（target）登记自己的数据来源，因此只校验 target 的写权限
            self._ensure_owner_or_admin(target, principal)
            # 字段映射必须在两端字段上真实存在
            pairs = sorted({(m.source_field, m.target_field) for m in req.field_mappings})
            for src_f, tgt_f in pairs:
                if src_f not in source.fields:
                    raise LineageFieldNotFoundError(
                        f"source field not found: {req.source_id}.{src_f}",
                        {"side": "source", "entity_id": req.source_id, "field": src_f},
                    )
                if tgt_f not in target.fields:
                    raise LineageFieldNotFoundError(
                        f"target field not found: {req.target_id}.{tgt_f}",
                        {"side": "target", "entity_id": req.target_id, "field": tgt_f},
                    )

            key = (req.source_id, req.target_id)
            existing = self._edges.get(key)
            if existing is not None:
                if existing.mappings == pairs and existing.description == req.description:
                    return {
                        "status": "duplicate",
                        "edge": self._edge_view(existing).model_dump(),
                    }
                raise DuplicateLineageError(
                    "lineage edge already exists with a different report; "
                    "conflicting duplicates are rejected, not merged",
                    {
                        "source_id": req.source_id,
                        "target_id": req.target_id,
                        "existing_mappings": [list(p) for p in existing.mappings],
                        "submitted_mappings": [list(p) for p in pairs],
                    },
                )

            # 环检测：若 target 已经能（沿下游方向）到达 source，则成环
            cycle = self._find_path(req.target_id, req.source_id)
            if cycle is not None:
                raise CyclicDependencyError(
                    "adding this edge would create a cycle",
                    {"cycle": cycle + [req.target_id]},
                )

            edge = _Edge(req)
            self._edges[key] = edge
            self._down.setdefault(req.source_id, set()).add(req.target_id)
            self._up.setdefault(req.target_id, set()).add(req.source_id)
            self._down.setdefault(req.target_id, set())
            self._up.setdefault(req.source_id, set())
            return {"status": "created", "edge": self._edge_view(edge).model_dump()}

    def _find_path(self, start: str, goal: str) -> list[str] | None:
        """沿下游方向 BFS，返回 start -> goal 的实体 id 路径（不含回到 start 的边）。"""
        if start == goal:
            return [start]
        queue: list[list[str]] = [[start]]
        seen = {start}
        while queue:
            path = queue.pop(0)
            node = path[-1]
            for nxt in sorted(self._down.get(node, ())):
                if nxt in seen:
                    continue
                new_path = path + [nxt]
                if nxt == goal:
                    return new_path
                seen.add(nxt)
                queue.append(new_path)
        return None

    def get_lineage(self, entity_id: str, principal: Principal) -> LineageResult:
        with self._lock:
            self._get_active(entity_id)  # 不存在 -> 404（可见性已在路由层闸门处理）
            up_edges = [
                self._edge_view(self._edges[(s, entity_id)])
                for s in sorted(self._up.get(entity_id, ()))
                if (s, entity_id) in self._edges and self._visible_for(principal, s)
            ]
            down_edges = [
                self._edge_view(self._edges[(entity_id, t)])
                for t in sorted(self._down.get(entity_id, ()))
                if (entity_id, t) in self._edges and self._visible_for(principal, t)
            ]
            return LineageResult(
                entity_id=entity_id, upstream=up_edges, downstream=down_edges
            )

    def impact_analysis(
        self, entity_id: str, direction: str, principal: Principal
    ) -> ImpactAnalysisResult:
        with self._lock:
            self._get_active(entity_id)
            if direction not in ("upstream", "downstream"):
                raise ValueError("direction must be 'upstream' or 'downstream'")
            neighbor_map = self._down if direction == "downstream" else self._up
            impacted: list[ImpactNode] = []
            cycles = self._detect_cycles()
            # BFS；不可见实体在遍历中被整体剪枝（含其出边），隐藏实体
            # 无法通过影响分析的节点、距离或字段被推断出来
            queue: list[tuple[str, int]] = [(entity_id, 0)]
            seen = {entity_id}
            while queue:
                node, dist = queue.pop(0)
                for nxt in sorted(neighbor_map.get(node, ())):
                    if nxt in seen:
                        continue
                    seen.add(nxt)
                    if not self._visible_for(principal, nxt):
                        continue
                    ent = self._entities[nxt]
                    via = self._via_fields(node, nxt, direction)
                    impacted.append(
                        ImpactNode(
                            entity_id=nxt, name=ent.name, entity_type=ent.entity_type,
                            distance=dist + 1, via_fields=via,
                        )
                    )
                    queue.append((nxt, dist + 1))
            return ImpactAnalysisResult(
                entity_id=entity_id, direction=direction,  # type: ignore[arg-type]
                impacted=impacted, cycles=cycles,
            )

    def _via_fields(self, node: str, nxt: str, direction: str) -> list[str]:
        if direction == "downstream":
            edge = self._edges.get((node, nxt))
            return sorted({t for _, t in edge.mappings}) if edge else []
        edge = self._edges.get((nxt, node))
        return sorted({s for s, _ in edge.mappings}) if edge else []

    def _detect_cycles(self) -> list[list[str]]:
        """返回图中所有简单环（DFS 去重，环以最小 id 旋转归一）。"""
        cycles: set[tuple[str, ...]] = set()

        def rotate(cycle: list[str]) -> tuple[str, ...]:
            i = cycle.index(min(cycle))
            rotated = tuple(cycle[i:] + cycle[:i])
            return rotated

        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in self._entities}
        stack: list[str] = []

        def dfs(u: str) -> None:
            color[u] = GRAY
            stack.append(u)
            for v in sorted(self._down.get(u, ())):
                if color.get(v, WHITE) == GRAY:
                    idx = stack.index(v)
                    cycles.add(rotate(stack[idx:]))
                elif color.get(v, WHITE) == WHITE:
                    dfs(v)
            stack.pop()
            color[u] = BLACK

        for n in sorted(self._entities):
            if color[n] == WHITE:
                dfs(n)
        return [list(c) for c in sorted(cycles)]

    # ------------------------------------------------------------------ #
    # 权限 / 可见性
    # ------------------------------------------------------------------ #
    def is_visible(self, principal: Principal, entity_id: str) -> bool:
        with self._lock:
            ent = self._entities.get(entity_id)
            if ent is None:
                return False
            return self._visible_for(principal, entity_id)

    def ensure_visible(self, principal: Principal, entity_id: str) -> None:
        with self._lock:
            ent = self._entities.get(entity_id)
            if ent is None:
                raise EntityNotFoundError(
                    f"entity not found: {entity_id}", {"entity_id": entity_id}
                )
            if not self._visible_for(principal, entity_id):
                raise ForbiddenError(
                    "entity is not visible to this caller",
                    {"entity_id": entity_id, "caller": principal.caller_id},
                )

    def _visible_for(self, principal: Principal, entity_id: str) -> bool:
        ent = self._entities.get(entity_id)
        if ent is None:
            return False
        if is_admin(principal):
            return True
        if ent.owner == principal.caller_id:
            return True
        return principal.caller_id in self._grants.get(entity_id, ())

    def search_entities(
        self,
        principal: Principal,
        query: str,
        entity_type: EntityType | None,
        status: EntityStatus | None,
    ) -> list[EntityView]:
        """枚举类通道：隐藏实体在结果中直接缺席，也不影响总数语义。

        与按 ID 直查不同：搜索不区分"不存在"与"不可见"，两者都不出现，
        从而无法通过搜索推断隐藏实体的存在。
        """
        q = query.strip().lower()
        with self._lock:
            out: list[EntityView] = []
            for eid in sorted(self._entities):
                ent = self._entities[eid]
                if not self._visible_for(principal, eid):
                    continue
                if entity_type is not None and ent.entity_type is not entity_type:
                    continue
                if status is not None and ent.status is not status:
                    continue
                if q:
                    haystack = f"{ent.entity_id} {ent.name} {ent.description}".lower()
                    if q not in haystack:
                        continue
                out.append(self._to_view(ent))
            return out

    def grant_visibility(self, principal: Principal, entity_id: str, grantee: str) -> dict:
        with self._lock:
            if not is_admin(principal):
                raise ForbiddenError(
                    "only admin can grant visibility",
                    {"caller": principal.caller_id},
                )
            self._get_active(entity_id)  # 对不存在的实体授权 -> 404
            grants = self._grants.setdefault(entity_id, set())
            created = grantee not in grants
            grants.add(grantee)
            return {
                "entity_id": entity_id,
                "grantee": grantee,
                "status": "created" if created else "already_granted",
            }

    def list_grants(self, principal: Principal) -> list[dict]:
        with self._lock:
            if not is_admin(principal):
                raise ForbiddenError(
                    "only admin can list grants", {"caller": principal.caller_id}
                )
            return [
                {"entity_id": eid, "grantees": sorted(gs)}
                for eid, gs in sorted(self._grants.items())
            ]

    def reset(self) -> None:
        with self._lock:
            self._entities.clear()
            self._edges.clear()
            self._down.clear()
            self._up.clear()
            self._grants.clear()

