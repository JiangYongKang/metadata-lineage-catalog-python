"""实体与字段的登记、幂等更新、改名与下线。

口径摘要（详见 docs）：
- 稳定身份是实体 ID；改名只改展示名，旧名进入 aliases，历史血缘不断。
- 字段改名必须在 renames 中显式声明，否则按"删除+新增"处理。
- 变更分两类：
  * 非法变更（无论是否确认都拒绝）：不兼容类型、renames 引用错误、
    给已存在实体新增没有 default 的必填字段。
  * 破坏性变更（需 allow_breaking=true 才接受）：删除字段、
    未声明的改名（删旧增新）、可选收紧为必填且无 default。
- expected_version 用于乐观并发控制；但对"内容完全一致"的重复提交保持幂等，
  不报版本冲突。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.errors import CatalogError
from app.models import (
    Caller,
    EntityDef,
    EntityKind,
    EntityRetireRequest,
    EntityStatus,
    EntityUpsertRequest,
    FieldDef,
    FieldDefIn,
    FieldKind,
    ErrorCode,
    SearchResult,
)
from app.security import (
    can_create_in_scope,
    can_modify_entity,
    can_see_entity,
)
from app.store import Store

# 允许的类型收窄/放宽：只有 int -> float 被认为兼容
_COMPATIBLE_TYPE_CHANGES: set[tuple[FieldKind, FieldKind]] = {
    (FieldKind.INT, FieldKind.FLOAT),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class FieldChanges:
    added: list[FieldDef] = field(default_factory=list)
    removed: list[FieldDef] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (old, new)
    type_changed: list[tuple[FieldDef, FieldDef]] = field(default_factory=list)
    tightened_required: list[FieldDef] = field(default_factory=list)
    description_changed: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.added
            or self.removed
            or self.renamed
            or self.type_changed
            or self.tightened_required
            or self.description_changed
        )


class EntityService:
    def __init__(self, store: Store) -> None:
        self.store = store

    # ---------------------------------------------------------------- 登记/更新

    def upsert(self, caller: Caller, req: EntityUpsertRequest) -> tuple[EntityDef, bool]:
        self._validate_request_shape(req)

        with self.store.lock:
            existing = self.store.get_entity(req.id)
            if existing is None:
                return self._create(caller, req)
            return self._update(caller, existing, req)

    def _create(self, caller: Caller, req: EntityUpsertRequest) -> tuple[EntityDef, bool]:
        if not can_create_in_scope(caller, req.scope):
            raise CatalogError(
                ErrorCode.FORBIDDEN_SCOPE,
                f"caller '{caller.id}' 不允许在 scope '{req.scope}' 下登记实体",
                reason=f"服务端按名册判定：role={caller.role}, scopes={caller.scopes}；"
                f"请求体中的任何角色/范围标记均不被采信",
            )
        # 同 ID 不存在，但同名（含别名）可能已属于另一个实体
        clash = self.store.find_by_name(req.name)
        if clash is not None and clash.status == EntityStatus.ACTIVE:
            raise CatalogError(
                ErrorCode.ENTITY_ACTIVE,
                f"名称 '{req.name}' 已被活跃实体 {clash.id} 占用",
                reason="实体 ID 是稳定身份，改名请对原 ID 做更新，而不是用新 ID 重报",
            )
        now = _now()
        entity = EntityDef(
            id=req.id,
            name=req.name,
            kind=req.kind,
            scope=req.scope,
            description=req.description,
            version=1,
            status=EntityStatus.ACTIVE,
            fields=[FieldDef(**f.model_dump()) for f in req.fields],
            created_at=now,
            updated_at=now,
        )
        self.store.put_entity(entity)
        return entity, True

    def _update(
        self, caller: Caller, existing: EntityDef, req: EntityUpsertRequest
    ) -> tuple[EntityDef, bool]:
        if existing.status == EntityStatus.RETIRED:
            raise CatalogError(
                ErrorCode.ENTITY_ALREADY_RETIRED,
                f"实体 {existing.id} 已下线，不能再登记变更",
                reason="下线为终态；其历史血缘与查询仍保留，但实体本身冻结",
            )
        if not can_modify_entity(caller, existing):
            raise CatalogError(
                ErrorCode.FORBIDDEN_NOT_OWNER,
                f"caller '{caller.id}' 无权修改 scope '{existing.scope}' 的实体",
                reason=f"服务端按名册判定：role={caller.role}, scopes={caller.scopes}",
            )
        if req.scope != existing.scope:
            raise CatalogError(
                ErrorCode.FORBIDDEN_SCOPE,
                f"实体 scope 不可变更：{existing.scope} -> {req.scope}",
                reason="scope 是实体归属，归属迁移需管理员另行处理",
            )

        new_fields = [FieldDef(**f.model_dump()) for f in req.fields]
        changes = self._compute_field_changes(existing.fields, new_fields, req.renames)

        # 1) 非法变更：无论如何都拒绝 ----
        for old_f, new_f in changes.type_changed:
            if (old_f.kind, new_f.kind) not in _COMPATIBLE_TYPE_CHANGES:
                raise CatalogError(
                    ErrorCode.FIELD_TYPE_INCOMPATIBLE,
                    f"字段 '{old_f.name}' 类型不兼容变更：{old_f.kind.value} -> {new_f.kind.value}",
                    reason=f"允许的类型变更仅为 int->float；字段名={old_f.name}",
                )
        for f in changes.added:
            if f.required and f.default is None:
                raise CatalogError(
                    ErrorCode.FIELD_REQUIRED_MISSING,
                    f"新增字段 '{f.name}' 为必填且未提供 default",
                    reason="对已存在实体新增无默认值的必填字段，存量数据无法满足约束，"
                    "属于非法变更（allow_breaking 也不接受）；请先以可选字段或带默认值登记",
                )
        for f in changes.tightened_required:
            if f.default is None:
                # 可选 -> 必填 且没有默认值：破坏性
                if not req.allow_breaking:
                    raise CatalogError(
                        ErrorCode.FIELD_OPTIONAL_TO_REQUIRED,
                        f"字段 '{f.name}' 由可选收紧为必填且无 default",
                        reason="存量数据可能缺失该字段；如确认接受请显式传 allow_breaking=true",
                    )

        # 2) 破坏性变更：需要显式确认 ----
        breaking: list[str] = []
        for f in changes.removed:
            breaking.append(f"删除字段 '{f.name}'")
        if changes.removed and not changes.renamed and not req.allow_breaking:
            pass  # 下方统一处理，便于给出聚合原因
        for old_name, new_name in changes.renamed:
            breaking.append(f"字段改名 '{old_name}' -> '{new_name}'（已在 renames 声明）")
        if changes.removed and not req.allow_breaking:
            names = ", ".join(f.name for f in changes.removed)
            raise CatalogError(
                ErrorCode.FIELD_REMOVED,
                f"请求删除字段：{names}",
                reason="删除字段会使存量数据失去对应列；如确认接受请显式传 allow_breaking=true；"
                "若实为改名请在 renames 中声明 old->new",
            )
        if changes.renamed and not req.allow_breaking:
            pairs = ", ".join(f"{o}->{n}" for o, n in changes.renamed)
            raise CatalogError(
                ErrorCode.FIELD_RENAMED,
                f"请求声明字段改名：{pairs}",
                reason="改名同样属于破坏性变更；如确认接受请显式传 allow_breaking=true",
            )

        # 3) 幂等判断：字段无差异、描述性属性也无差异 -> 原样返回 ----
        meta_same = (
            existing.name == req.name
            and existing.description == req.description
            and existing.kind == req.kind
        )
        if changes.is_empty() and meta_same:
            # 重复上报：即使 expected_version 过期也返回当前实体，changed=False
            return existing, False

        # 4) 乐观并发控制：有实际差异时版本必须匹配 ----
        if req.expected_version is not None and req.expected_version != existing.version:
            raise CatalogError(
                ErrorCode.VERSION_CONFLICT,
                f"版本冲突：期望 {req.expected_version}，当前 {existing.version}",
                reason="实体已被其他提交更新，请读取最新版本后携带新的 expected_version 重试；"
                "内容完全相同的重复提交不受此限制",
            )

        # 5) 接受变更 ----
        if existing.name != req.name:
            if existing.name not in existing.aliases:
                existing.aliases.append(existing.name)
        existing.name = req.name
        existing.description = req.description
        existing.kind = req.kind
        existing.fields = new_fields
        existing.version += 1
        existing.updated_at = _now()
        self.store.put_entity(existing)
        return existing, True

    def _validate_request_shape(self, req: EntityUpsertRequest) -> None:
        """请求自身的静态合法性：字段名唯一、renames 自洽。"""
        names = [f.name for f in req.fields]
        dup = {n for n in names if names.count(n) > 1}
        if dup:
            raise CatalogError(
                ErrorCode.FIELD_REQUIRED_MISSING,
                f"字段名重复：{sorted(dup)}",
                reason="同一实体的字段名必须唯一",
            )
        new_names = set(names)
        for old_name, new_name in req.renames.items():
            if old_name == new_name:
                raise CatalogError(
                    ErrorCode.RENAMED_FIELD_NOT_FOUND,
                    f"改名映射无效：{old_name} -> {new_name} 新旧同名",
                    reason="renames 的键值必须不同",
                )
            if new_name in new_names and old_name in new_names:
                # 新名和旧名都出现在新字段集里：无法判定改名目标
                raise CatalogError(
                    ErrorCode.RENAMED_FIELD_TARGET_EXISTS,
                    f"改名目标冲突：'{new_name}' 与旧名 '{old_name}' 同时存在于新字段集中",
                    reason="改名后旧名字段不应再出现在 fields 列表中",
                )

    # ---------------------------------------------------------------- 查询

    def get(self, caller: Caller, ref: str) -> EntityDef:
        with self.store.lock:
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

    def search(
        self, caller: Caller, name_contains: str | None, scope: str | None
    ) -> SearchResult:
        with self.store.lock:
            candidates = list(self.store.entities.values())
        items: list[EntityDef] = []
        for entity in candidates:
            if not can_see_entity(caller, entity):
                continue  # 隐藏实体直接剔除，搜索结果不泄露其存在性
            if scope is not None and entity.scope != scope:
                continue
            if name_contains is not None and name_contains not in entity.name:
                continue
            if entity.status == EntityStatus.RETIRED:
                # 搜索默认只暴露活跃实体；下线实体只能按 ID/别名显式查询
                continue
            items.append(entity)
        items.sort(key=lambda e: e.id)
        return SearchResult(items=items, total=len(items))

    def retire(self, caller: Caller, entity_id: str, req: EntityRetireRequest) -> EntityDef:
        with self.store.lock:
            entity = self.store.get_entity(entity_id)
            if entity is None:
                raise CatalogError(
                    ErrorCode.ENTITY_NOT_FOUND,
                    f"实体不存在：{entity_id}",
                    reason="下线操作要求实体已登记",
                )
            if not can_modify_entity(caller, entity):
                raise CatalogError(
                    ErrorCode.FORBIDDEN_NOT_OWNER,
                    f"caller '{caller.id}' 无权下线 scope '{entity.scope}' 的实体",
                    reason=f"服务端按名册判定：role={caller.role}, scopes={caller.scopes}",
                )
            if entity.status == EntityStatus.RETIRED:
                raise CatalogError(
                    ErrorCode.ENTITY_ALREADY_RETIRED,
                    f"实体 {entity_id} 已处于下线状态",
                    reason="下线幂等口径：重复下线得到明确的已下线错误，而不是被静默接受",
                )
            if req.expected_version is not None and req.expected_version != entity.version:
                raise CatalogError(
                    ErrorCode.VERSION_CONFLICT,
                    f"版本冲突：期望 {req.expected_version}，当前 {entity.version}",
                    reason="下线同样走乐观并发控制",
                )
            entity.status = EntityStatus.RETIRED
            entity.version += 1
            entity.updated_at = _now()
            self.store.put_entity(entity)
            return entity

    # ---------------------------------------------------------------- 字段差异

    @staticmethod
    def _compute_field_changes(
        old: list[FieldDef], new: list[FieldDef], renames: dict[str, str]
    ) -> FieldChanges:
        """计算字段差异。

        步骤：先用 renames 把旧字段视图改名，再与新字段集按名比对。
        - renames 声明但旧字段不存在 / 新名已被其他旧字段占用：报错（非法）。
        - 旧字段消失且未在 renames 中声明：removed（破坏性删除）。
        - 新字段出现且不是任何改名目标：added。
        - 同名：比较类型与 required。
        """
        changes = FieldChanges()
        old_by_name = {f.name: f for f in old}
        new_by_name = {f.name: f for f in new}

        # --- 校验并应用 renames ---
        renamed_old: dict[str, FieldDef] = {}  # new_name -> old field
        for old_name, new_name in renames.items():
            if old_name not in old_by_name:
                raise CatalogError(
                    ErrorCode.RENAMED_FIELD_NOT_FOUND,
                    f"renames 引用的旧字段 '{old_name}' 不存在",
                    reason="改名映射的键必须是当前版本中存在的字段名",
                )
            if new_name in old_by_name:
                raise CatalogError(
                    ErrorCode.RENAMED_FIELD_TARGET_EXISTS,
                    f"改名目标 '{new_name}' 已被现有字段占用",
                    reason="不能改名到一个已经存在的字段名上",
                )
            old_f = old_by_name[old_name]
            renamed_old[new_name] = old_f.model_copy(update={"name": new_name})
            changes.renamed.append((old_name, new_name))

        # 改名后参与比对的旧字段视图
        effective_old: dict[str, FieldDef] = {}
        for name, f in old_by_name.items():
            if name in renames:
                effective_old[renames[name]] = f.model_copy(update={"name": renames[name]})
            else:
                effective_old[name] = f

        for name, new_f in new_by_name.items():
            old_f = effective_old.get(name)
            if old_f is None:
                changes.added.append(new_f)
                continue
            if old_f.kind != new_f.kind:
                changes.type_changed.append((old_f, new_f))
            if not old_f.required and new_f.required:
                changes.tightened_required.append(new_f)
            elif old_f.required and not new_f.required:
                pass  # 必填放宽为可选：兼容
            if old_f.description != new_f.description or old_f.default != new_f.default:
                changes.description_changed.append(name)

        new_names = set(new_by_name)
        for name, old_f in effective_old.items():
            if name not in new_names:
                # 还原为原始旧字段，便于错误信息展示
                changes.removed.append(old_by_name.get(name, old_f))

        return changes
