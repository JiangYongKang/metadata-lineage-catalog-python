# 本地元数据目录服务（Metadata & Lineage Catalog）

一个基于 FastAPI 的**本地**元数据目录：登记数据实体及其字段、维护上下游血缘、
提供查询与影响分析，并在服务端完成调用方可见范围控制。所有判定都有稳定的
错误码与文字化判定依据（`reason`），重复上报、字段变更、环形血缘、越权访问
都不会被静默接受。

- 运行形态：单进程、内存存储、全局互斥锁串行化写操作（无需外部数据库）
- 稳定身份：实体 ID 与血缘 `(upstream_id, downstream_id)` 永不随改名变化
- 权限模型：身份只来自服务端名册与 `X-Caller-Id` 请求头，请求体中的任何
  角色/范围声明一律忽略

---

## 1. 本地验证方法

```bash
# 1) 安装依赖（需要 uv）
uv sync

# 2) 运行全部测试（36 个用例，覆盖重复/并发、字段兼容、环形血缘、越权推断）
uv run pytest -v

# 用例会打印每次请求的输入与服务端判定依据，例如：
#   [未确认直接删除字段]
#     输入: {"id": "orders", ... "fields": [{"name": "id", ...}]}
#     判定: HTTP 409 {"error": {"code": "FIELD_REMOVED", "reason": "..."}}

# 3) 启动服务
uv run uvicorn main:app --reload
# 打开 http://127.0.0.1:8000/docs 可交互调试
```

### 预置调用方名册（`app/deps.py`）

| X-Caller-Id        | 角色  | 可见/可管范围        |
|--------------------|-------|----------------------|
| `admin`            | admin | 全部                 |
| `owner-finance`    | owner | finance（可登记/改） |
| `owner-marketing`  | owner | marketing（可登记/改）|
| `reader-finance`   | reader| finance（只读）      |

所有请求都必须带头 `X-Caller-Id: <名册中的 ID>`；缺失或无法识别 → `401 UNAUTHENTICATED`。

### API 一览（前缀 `/api/v1`）

| 方法   | 路径                          | 说明                         |
|--------|-------------------------------|------------------------------|
| POST   | `/entities`                   | 登记/更新实体（幂等 upsert） |
| GET    | `/entities/{ref}`             | 按 ID / 当前名 / 历史别名查  |
| GET    | `/entities?name_contains=&scope=` | 搜索（只返回可见活跃实体） |
| POST   | `/entities/{id}/retire`       | 下线实体（终态）             |
| POST   | `/lineage/edges`              | 登记血缘边                   |
| DELETE | `/lineage/edges`              | 软删除血缘边                 |
| GET    | `/lineage/{ref}`              | 直接上下游                   |
| GET    | `/impact/{ref}`               | 下游影响分析（实体+路径）    |

错误统一形如：

```json
{"error": {"code": "FIELD_REMOVED",
           "message": "请求删除字段：amount",
           "reason": "删除字段会使存量数据失去对应列；如确认接受请显式传 allow_breaking=true；若实为改名请在 renames 中声明 old->new"}}
```

---

## 2. 字段兼容规则

更新已有实体时，服务端先对**字段集差异分类**，再决定接受/拒绝。
变更分为两类：**非法变更**（任何情况下都拒绝）和**破坏性变更**
（必须显式 `allow_breaking: true` 才接受）。

### 2.1 兼容变更（直接接受，版本 +1）

- 新增**可选**字段，或新增带 `default` 的必填字段
- 删除字段的"必填"约束（required → optional，放宽）
- 字段类型 `int -> float`（唯一允许的类型变更，值域放宽）
- 仅修改 `description` / `default`（不改结构）

### 2.2 非法变更（`422`，传 `allow_breaking` 也拒绝）

| 错误码 | 触发条件 |
|--------|----------|
| `FIELD_REQUIRED_MISSING` | 给已存在实体新增**无 default 的必填字段**（存量数据无法满足约束）；同名字段重复登记 |
| `FIELD_TYPE_INCOMPATIBLE` | 类型变更不兼容（除 int→float 外一律拒绝，如 int→string、bool→date） |
| `RENAMED_FIELD_NOT_FOUND` | `renames` 的键不是当前版本存在的字段名，或新旧同名 |
| `RENAMED_FIELD_TARGET_EXISTS` | 改名目标字段名已被占用，或新旧名同时出现在新字段集中 |

### 2.3 破坏性变更（`409`，需 `allow_breaking: true`）

| 错误码 | 触发条件 |
|--------|----------|
| `FIELD_REMOVED` | 字段从新字段集中消失（未在 `renames` 中声明的换名也按"删除+新增"处理，先报删除） |
| `FIELD_RENAMED` | `renames` 声明了字段改名（语义确定，但仍属破坏性，需确认） |
| `FIELD_OPTIONAL_TO_REQUIRED` | 可选字段收紧为必填且未提供 default |

### 2.4 字段改名的确定语义

- 改名**必须**在请求的 `renames: {旧字段名: 新字段名}` 中显式声明；
  不声明就直接换新名字，等价于"删除旧字段 + 新增字段"，按删除规则拒绝。
- 改名后字段身份按"新名"延续：类型/必填约束以新字段集为准继续参与校验。

### 2.5 实体改名与下线

- **实体 ID 是稳定身份**。改 `name` 不会新建实体：旧名自动进入 `aliases`，
  用旧名仍可查到同一实体及其血缘。
- `retire` 是终态：下线后实体冻结（`ENTITY_ALREADY_RETIRED`），不能再变更，
  也不能作为端点参与新血缘（`EDGE_ON_RETIRED_ENTITY`）；但其历史字段、
  历史血缘和影响分析结论继续保留可查。

### 2.6 幂等与并发

- **内容完全一致**的重复提交（含并发）永远幂等：返回当前实体、`changed=false`，
  即使带了过期的 `expected_version` 也不报冲突。
- 存在**实际差异**时启用乐观并发控制：`expected_version` 必须等于当前版本，
  否则 `409 VERSION_CONFLICT`，调用方读取最新版本后重试。
- 所有写操作在进程内互斥锁中完成"读取-判定-写入"，并发互斥变更恰好一个成功，
  其余得到 `VERSION_CONFLICT` 或 `FIELD_REMOVED`（相对最新版本等于删除他人字段），
  二者都是明确拒绝而非静默覆盖。

---

## 3. 血缘与影响分析口径

### 3.1 边的登记

- 边身份是 `(upstream_id, downstream_id)`，语义为"下游消费上游"。
- 前置校验顺序：非自环 → 两端实体存在 → 调用方对两端可见且对下游有属主权限
  → 两端均为活跃状态 → 非重复 → 不成环。
- **自环**：`422 SELF_EDGE_FORBIDDEN`。
- **重复上报**：
  - 与现有活跃边完全相同（含 `transform`）→ 幂等返回，`created=false`；
  - 节点对相同但 `transform` 不同 → `409 DUPLICATE_EDGE`，**不静默覆盖**，
    需先删除再重新登记。
- **环形依赖**：血缘图必须是 DAG。若新边会使图成环，返回
  `409 CYCLE_DETECTED`，并在 `message`/`reason` 中给出闭合环路径
  （从下游节点起、回到上游再到下游，例如已存在 `a->b` 时登记 `b->a`
  返回环 `a -> b -> a`）。被拒的边不产生任何副作用。
- 删除边是**软删除**：保留边记录用于历史复核；重复删除返回
  `404 EDGE_NOT_FOUND`；同一身份的边之后可以重新登记。

### 3.2 查询与影响分析

- `GET /lineage/{ref}`：返回直接上游、直接下游以及调用方可见的邻边。
- `GET /impact/{ref}`：从该实体出发，沿**活跃边**做图遍历，给出
  - `downstream_entities`：所有可达下游实体（含已下线实体，带 `status=retired`）
  - `paths`：简单传播路径（如 `["a","b","c"]`，超大图每实体最多保留 16 条）
  - `affected_downstream_count`：去重后的下游实体数
- 实体改名/下线后以稳定 ID 计算，历史查询与影响分析结论保持自洽
  （下线只阻断"新边登记"，不抹除历史传播路径）。

---

## 4. 权限模型

### 4.1 身份与判定位置

- 身份**只**由 `X-Caller-Id` 请求头携带，并由**服务端名册**解析
  （`app/security.py: CallerDirectory`）。名册在服务启动时配置，客户端无法修改。
- 请求体即使携带 `role`、`admin`、`scopes` 等字段也会被模型忽略，
  **不能靠请求里的标记提权**（有测试固定该行为）。

### 4.2 角色与范围

| 能力 | admin | owner（持某 scope） | reader |
|------|-------|--------------------|--------|
| 查看本 scope 实体 | ✔ 全部 | ✔ | ✔ |
| 搜索/影响分析 | 全 scope | 仅本 scope | 仅本 scope |
| 登记/更新/下线实体 | ✔ | 仅本 scope | ✘ |
| 登记血缘边 | ✔ | 两端可见且为下游属主 | ✘ |
| 删除血缘边 | ✔ | 上游或下游属主 | ✗ |

### 4.3 拒绝码（原因可区分）

| 场景 | HTTP | 错误码 |
|------|------|--------|
| 无头 / 身份不在名册 | 401 | `UNAUTHENTICATED` |
| 合法身份但资源不在可见 scope（读/搜/引用） | 403 | `FORBIDDEN_SCOPE` |
| 可见但缺少属主权限（写实体/建边/删边） | 403 | `FORBIDDEN_NOT_OWNER` |

### 4.4 防推断保证

隐藏实体不仅是"打不开详情"，而是在所有输出面都被剪枝：

- **搜索**：不可见实体不出现在结果与 `total` 计数中（下线实体也不进搜索，
  只能按 ID/别名显式查）。
- **影响分析**：只在调用方的**可见子图**内沿边遍历。任何一端不可见的边都被
  整体剪除——隐藏节点既不出现在 `downstream_entities`，也不会作为路径的中间跳，
  计数也不包含它，因此无法通过"为什么少一跳/计数对不上"推断其存在。
- **直接血缘**：不可见邻点与邻边都不返回。
- **边存在性探测**：删除跨 scope 边时先做权限判定再查边，无权方稳定得到 403，
  无法利用 404/403 的差异探测隐藏边是否存在。
- 403 响应体只含错误码与判定依据，不含被访问实体的任何字段。

---

## 5. 代码结构

```
main.py                    # FastAPI 入口
app/models.py              # 领域模型、字段类型枚举、稳定错误码
app/errors.py              # CatalogError（code + message + reason）
app/store.py               # 带 RLock 的内存存储
app/security.py            # 名册、可见性/属主判定（唯一授权入口）
app/entity_service.py      # 登记/幂等/字段兼容/改名/下线 + 字段差异算法
app/lineage_service.py     # 建边/DAG 环检测/血缘视图/可见子图影响分析
app/api.py                 # HTTP 路由、X-Caller-Id 解析、错误码->HTTP 映射
app/deps.py                # 装配与预置名册
tests/                     # 36 个用例，打印输入与判定依据
```

## 6. 边界与说明

- 内存存储重启即清空，定位为本地/CI 验证服务；持久化可在 `Store` 接口后替换实现。
- 影响分析面向血缘深度有限的目录场景，对每个终点保留至多 16 条简单路径以防超大图爆炸。
