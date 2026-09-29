"""字段集合变更的兼容性规则（纯函数，不碰存储）。

规则口径：
1. 字段以 name 作为身份；新增：新名字出现；删除：旧名字缺失。
2. 改名必须显式声明 rename_from，禁止靠"一删一增"猜测改名。
3. 类型只允许 widening：integer -> float 兼容，其余类型变化均拒绝；
   nullable 只允许放开（false -> true），收紧（true -> false）拒绝。
4. 同一提交内字段名重复 -> duplicate_field；改名源不存在、改名目标
   已存在、多个字段指向同一改名源 -> incompatible_field_change。
5. 删除字段是否被血缘引用由存储层判定（compat 不知道血缘）。

调用方（store）应先做幂等短路，再调用 plan_field_changes。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import DuplicateFieldError, IncompatibleFieldChangeError
from .models import FieldSpec

# 允许的类型放宽（widening）：key 可安全变更为 value 集合中的类型
WIDENINGS: dict[str, frozenset[str]] = {
    "integer": frozenset({"float"}),
}


def index_fields(fields: list[FieldSpec]) -> dict[str, FieldSpec]:
    """字段列表 -> {name: spec}；重名立即拒绝。"""
    indexed: dict[str, FieldSpec] = {}
    for spec in fields:
        if spec.name in indexed:
            raise DuplicateFieldError(
                f"field name duplicated in submission: {spec.name}",
                {"field": spec.name},
            )
        indexed[spec.name] = spec
    return indexed


@dataclass
class FieldChangePlan:
    new_index: dict[str, FieldSpec]
    additions: list[str] = field(default_factory=list)
    deletions: list[str] = field(default_factory=list)
    # (old_name, new_name)
    renames: list[tuple[str, str]] = field(default_factory=list)


def _incompatible(field: str, cause: str, **extra) -> IncompatibleFieldChangeError:
    detail = {"field": field, "cause": cause}
    detail.update(extra)
    return IncompatibleFieldChangeError(f"incompatible change on field '{field}': {cause}", detail)


def is_type_compatible(old_type: str, new_type: str) -> bool:
    if old_type == new_type:
        return True
    return new_type in WIDENINGS.get(old_type, frozenset())


def plan_field_changes(
    old_fields: dict[str, FieldSpec], new_fields: list[FieldSpec]
) -> FieldChangePlan:
    """对比旧字段集合与新提交，产出变更计划；不兼容即抛异常。

    注意：本函数假定调用方已经排除了"新旧语义完全相同"的幂等场景，
    因此任何 rename_from 指向不存在字段的情况都会被拒绝。
    """
    new_index = index_fields(new_fields)

    renames: list[tuple[str, str]] = []
    seen_sources: set[str] = set()
    for spec in new_fields:
        source = spec.rename_from
        if source is None:
            continue
        if source == spec.name:
            raise _incompatible(spec.name, "rename_to_self")
        if source not in old_fields:
            raise _incompatible(spec.name, "rename_source_missing", rename_from=source)
        if source in seen_sources:
            raise _incompatible(spec.name, "rename_source_ambiguous", rename_from=source)
        # 改名目标若当前仍存在（含交换式改名），不确定性过大，拒绝
        if spec.name in old_fields:
            raise _incompatible(spec.name, "rename_target_exists", existing=spec.name)
        seen_sources.add(source)
        renames.append((source, spec.name))

    renamed_sources = {old for old, _ in renames}

    # 删除 = 旧名字在新提交中缺失，且没有被某次改名消费
    deletions = [name for name in old_fields if name not in new_index and name not in renamed_sources]

    # 新增 = 新名字在旧集合中不存在，且不是某次改名的目标
    renamed_targets = {new for _, new in renames}
    additions = [name for name in new_index if name not in old_fields and name not in renamed_targets]

    # 针对"保留字段"和"改名字段"做类型/可空性兼容校验
    def check_spec_compat(name: str, old_spec: FieldSpec, new_spec: FieldSpec) -> None:
        if not is_type_compatible(old_spec.field_type, new_spec.field_type):
            raise _incompatible(
                name,
                "type_not_compatible",
                from_type=old_spec.field_type,
                to_type=new_spec.field_type,
            )
        if old_spec.nullable and not new_spec.nullable:
            raise _incompatible(
                name,
                "nullable_tightened",
                from_nullable=True,
                to_nullable=False,
            )

    for old_name, new_name in renames:
        check_spec_compat(new_name, old_fields[old_name], new_index[new_name])
    for name, spec in new_index.items():
        if name in old_fields:
            check_spec_compat(name, old_fields[name], spec)

    return FieldChangePlan(
        new_index=new_index,
        additions=sorted(additions),
        deletions=sorted(deletions),
        renames=sorted(renames),
    )
