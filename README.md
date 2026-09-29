# 本地元数据目录服务（metadata-lineage-catalog）

一个纯本地、内存存储的 FastAPI 服务，用于：

- 登记/更新数据实体（table / topic / api / dashboard）及其字段；
- 维护上下游字段级血缘，做影响分析；
- 按调用方身份做服务端可见性控制；
- 对重复/并发上报、不兼容字段变更、环形血缘、越权访问给出**稳定且可区分**的结论。

数据只存进程内存，重启即清空，定位为本地验证与规则演练，不是生产存储。

---

## 1. 本地运行与验证

需要 Python 3.14（项目已提供 `uv.lock`）。

```bash
# 安装依赖（uv 会按 lock 建虚拟环境）
uv sync

# 启动服务
uv run python main.py
# -> http://127.0.0.1:8080，交互文档 /docs

# 运行测试（用例会 print 输入与判定依据，加 -s 可复核）
uv run pytest -s
uv run pytest -s tests/test_idempotency_concurrency.py
```

不使用 uv 时：`pip install fastapi[standard] pytest httpx && pytest -s`。

### 调用方身份怎么传

本地用请求头模拟"已通过网关认证"后的身份，**服务端只从这两个头取身份**：

| 头 | 含义 |
| --- | --- |
| `X-Caller-Id` | 调用方唯一标识；缺失 -> `401 unauthenticated` |
| `X-Caller-Roles` | 逗号分隔角色；当前识别 `admin` |

请求体、查询参数里自称的身份（如 `?caller_id=alice`）一律不读取，不能用来绕过权限。

---

## 2. HTTP 接口

| 方法 & 路径 | 说明 | 写权限 |
| --- | --- | --- |
| `POST /entities` | 登记或全量更新实体（幂等） | 登记：`owner` 必须是本人（admin 除外）；更新：owner 或 admin |
| `GET /entities/{id}` | 按 ID 查询 | 可见即可 |
| `POST /entities/{id}/rename` | 实体改名（全局唯一名） | owner 或 admin |
| `POST /entities/{id}/deprecate` | 实体下线 | owner 或 admin |
| `POST /entities/search` | 关键字搜索（枚举通道，隐藏实体直接缺席） | 已认证 |
| `POST /lineage` | 建立上下游血缘 | **下游（target）owner** 或 admin |
| `GET /lineage/{id}` | 查询一跳上下游血缘 | 可见即可，邻接边同样按可见性过滤 |
| `GET /impact/{id}?direction=downstream|upstream` | 传递闭包影响分析（BFS 跳数） | 对起点可见 |
| `POST /admin/grants` | 给某调用方授予实体可见性 | 仅 admin |
| `GET /admin/grants` | 查看全部授权 | 仅 admin |

所有业务拒绝都返回统一结构：

```json
{ "error": "稳定的错误码", "reason": "人类可读说明", "detail": { ... } }
```

| HTTP | error | 触发场景 |
| --- | --- | --- |
| 401 | `unauthenticated` | 缺少 `X-Caller-Id` |
| 403 | `forbidden` | 已认证但无权（越权改/查、非 admin 授权、血缘非下游 owner 登记） |
| 404 | `entity_not_found` | 直查/操作的实体不存在 |
| 400 | `invalid_argument` | 参数取值非法（如 direction） |
| 422 | `duplicate_field` | 同一次提交里字段重名 |
| 422 | `incompatible_field_change` | 字段变更不兼容（见第 3 节，`detail.cause` 细分） |
| 422 | `lineage_field_not_found` | 血缘映射引用了不存在的字段 |
| 409 | `entity_spec_immutable` | 改 `entity_type`、改 `owner`、通过登记接口改名、name 冲突 |
| 409 | `rename_target_exists` | 实体改名目标名已被占用 |
| 409 | `entity_deprecated` | 对已下线实体登记/更新/挂血缘 |
| 409 | `cyclic_dependency` | 自环或新增边会形成环 |
| 409 | `duplicate_lineage` | 同一对实体的第二次上报与已有边**不一致**（冲突重复，拒绝而非合并） |

> 直查（按 ID）时"不存在(404)"与"存在但无权(403)"原因可区分；
> 枚举类通道（search / lineage 邻接边 / impact 节点）对两者不做区分，
> 隐藏内容直接缺席，避免通过枚举推断隐藏实体的存在。

---

## 3. 字段兼容规则

`POST /entities` 是**全量字段集**提交：服务端拿新集合与存量字段按 `name`
做身份对齐后判定。判定与写入在同一把锁内原子完成。

### 3.1 幂等

- 字段集合按 `name` 归一比较，**字段顺序无关**。
- 实体名、描述、字段集合（名/类型/nullable/描述）全部相同的重复提交：
  原样返回，`revision` 不增加。
- 并发相同提交：服务端串行化，只有第一次创建/生效，其余幂等命中。

### 3.2 新增与删除

- **新增**字段（出现新名字）：允许，`revision + 1`。
- **删除**字段（旧名字在新集合中缺失）：允许，但**被血缘字段映射引用的字段
  不得删除**，否则 `422 incompatible_field_change`，
  `detail.referenced` 列出仍在引用它的对端字段（如 `{"id": ["t2.pk"]}`）。

### 3.3 改名（语义确定，禁止猜）

改名**必须显式声明**：新字段上带 `"rename_from": "旧字段名"`。
不允许用"删掉旧字段 + 新增同语义字段"让系统猜改名。以下情形全部
`422 incompatible_field_change`，用 `detail.cause` 区分：

| cause | 含义 |
| --- | --- |
| `rename_source_missing` | `rename_from` 指向的旧字段不存在 |
| `rename_target_exists` | 改名目标名当前仍存在（含交换式改名），拒绝歧义 |
| `rename_to_self` | `rename_from` 与新名字相同 |
| `rename_source_ambiguous` | 多个字段声明从同一旧字段改名 |

改名成功后：该字段上的**血缘映射引用会自动迁移**到新名字，历史血缘不断链。

### 3.4 类型与可空性（只接受向后兼容的放宽）

- 类型变化只允许 widening：`integer -> float` 兼容；
  其它一切类型变化（含 `float -> integer`、跨类型族如 `string -> integer`）
  都是 `cause=type_not_compatible`。
- `nullable` 只允许放开：`false -> true` 允许；`true -> false`（收紧）
  为 `cause=nullable_tightened`。
- 同一次提交内字段重名：`422 duplicate_field`。

### 3.5 实体级不可变项

- `entity_type`、`owner` 不可变：`409 entity_spec_immutable`。
- 实体显示名 `name` 全局唯一（含已下线实体），改名只能走
  `POST /entities/{id}/rename`；改名后实体保留 `renamed_from`。
  通过登记接口改名为 `409`，占用名为 `409 rename_target_exists`。
- `revision` 仅在发生实质变更时 +1，便于并发上报时确认到底哪次生效。

---

## 4. 血缘与影响分析口径

### 4.1 建立血缘

- 请求：`{source_id, target_id, field_mappings[], description}`。
- 登记方：**下游（target）的 owner 或 admin**（下游登记"我的数据来自谁"）。
- 约束：
  - 不允许自环（`source == target`）；
  - 两端实体都必须存在且处于 `active`（已下线 -> `409 entity_deprecated`）；
  - 每条 `(source_field, target_field)` 必须在两端真实存在，否则
    `422 lineage_field_not_found`；
  - 新增边前做环检测：若 target 已能沿下游方向到达 source，则
    `409 cyclic_dependency`，`detail.cycle` 给出具体成环路径。
- 重复上报（同一 `source -> target`）：
  - 映射集合（排序去重后）与描述**完全相同** -> 返回
    `201 {"status": "duplicate", ...}`，物理上只有一条边，**明确标记**而不静默；
  - 内容**不一致** -> `409 duplicate_lineage`，`detail` 同时给出已有与本次
    的映射，冲突重复不合并、不覆盖、不任取一条。

### 4.2 查询与影响分析

- `GET /lineage/{id}`：返回**一跳** `upstream` / `downstream` 边。
- `GET /impact/{id}`：沿血缘方向做 BFS，返回传递闭包内的全部受影响实体，
  `distance` 为跳数，`via_fields` 为该跳涉及的目标/来源字段名。
- 历史自洽：实体改名（`renamed_from` 保留旧名）、字段改名（映射引用自动
  迁移）、实体下线（status=deprecated，边保留）之后，按实体 ID 的历史查询
  与影响分析结论仍然成立；已下线实体不能再挂新血缘，但既有的上下游链路
  与影响结论继续可见、可追溯。

### 4.3 可见性如何作用于血缘

隐藏（对当前调用方不可见）的实体会被**整体剪枝，包括它的连边**：

- 搜索结果中缺席；
- 一跳血缘的邻接边中缺席；
- 影响分析遍历时，既不会出现在节点列表，也不会作为"跳板"把它后面可见的
  实体暴露出来——因此无法从距离、跳数或字段名反推出隐藏实体。

`cycles` 字段返回当前血缘图中检测到的简单环（用于观测；正常写入路径下
环无法产生，自历史脏数据或外部导入时才可能出现）。

---

## 5. 权限模型

- 身份来源：仅 `X-Caller-Id` / `X-Caller-Roles` 请求头（模拟网关注入），
  判定全部在服务端完成。
- 角色：
  - `admin`：可见全部实体（含 deprecated）、可下发/查看授权；
  - 普通调用方：默认可见 **owner 是自己** 或 **被 admin 显式 grant** 的实体。
- 写权限：
  - 登记实体时 `owner` 必须等于本人（admin 可代登记，但归属以 `owner` 字段为准）；
  - 更新/改名/下线实体：owner 或 admin；
  - 建立血缘：下游 target 的 owner 或 admin；
  - 授权与列举授权：仅 admin。
- 越权写/直查 -> `403 forbidden`；不存在 -> `404 entity_not_found`，两类原因
  可区分；枚举通道不泄露存在性（见 4.3）。

---

## 6. 测试

```bash
uv run pytest -s
```

| 文件 | 覆盖场景 |
| --- | --- |
| `tests/test_idempotency_concurrency.py` | 重复上报幂等、字段顺序无关、并发相同登记、并发冲突变更、并发相同血缘 |
| `tests/test_field_compatibility.py` | 新增/删除、widening/narrowing、nullable 放宽/收紧、重名、显式改名各类拒绝、删除被引用字段、entity_type 不可变 |
| `tests/test_lineage_impact.py` | 重复/冲突血缘标记、自环、成环路径、映射字段不存在、BFS 距离、改名与下线后自洽 |
| `tests/test_authorization.py` | 401/403/404 区分、请求体内自称身份无效、冒用 owner、非 owner 改写、非 admin 授权、grant 生效、搜索/血缘/影响中的隐藏实体剪枝 |

每个用例都通过 `print` 输出"输入 + 判定依据"，用 `pytest -s` 可逐条复核。
