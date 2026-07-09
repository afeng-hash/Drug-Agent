# AI Agent 运营平台 — 后台管理 API 接口文档

> **Base URL**: `http://<host>:<port>/api/v1/admin`
>
> **版本**: v1.1 (Phase 1)
>
> **更新日期**: 2026-07-07

---

## 目录

1. [通用约定](#1-通用约定)
2. [仪表盘 / 用户分析](#2-仪表盘--用户分析-analytics)
3. [对话管理](#3-对话管理-conversations)
4. [用户管理](#4-用户管理-users)
5. [LLM 用量与模型配置](#5-llm-用量与模型配置-llm)
6. [药品数据库管理](#6-药品数据库管理-database)
7. [知识图谱管理](#7-知识图谱管理-kg)
8. [Skill / SOP 编排](#8-skill--sop-编排-skills)
9. [工具管理](#9-工具管理-tools)
10. [Prompt 版本管理](#10-prompt-版本管理-prompts)
11. [Web Search 配置](#11-web-search-配置-web-search)
12. [用户反馈](#12-用户反馈-feedback)
13. [审计日志](#13-审计日志-audit)
14. [高风险关键字监控](#14-高风险关键字监控-risk-keywords--risk-alerts)
15. [链路追踪](#15-链路追踪-traces)
16. [系统配置](#16-系统配置-config)
17. [前端对接注意事项](#17-前端对接注意事项)

---

## 1. 通用约定

### 1.1 请求格式

- Content-Type: `application/json`（GET 请求参数走 query string）
- 字符编码: UTF-8

### 1.2 分页响应格式

所有列表接口返回统一分页结构：

```json
{
  "items": [...],
  "total": 120,
  "page": 1,
  "page_size": 20
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `items` | `array` | 当前页数据列表 |
| `total` | `int` | 总记录数 |
| `page` | `int` | 当前页码（从 1 开始） |
| `page_size` | `int` | 每页条数 |

> **前端计算总页数**: `Math.ceil(total / page_size)` — 后端 `PaginatedResponse` 的 `total_pages` 是 Python `@property`，不会序列化到 JSON 中，前端需要自行计算。

### 1.3 分页请求参数约定

| 参数 | 类型 | 默认值 | 范围 | 说明 |
|------|------|--------|------|------|
| `page` | `int` | `1` | `>=1` | 页码 |
| `page_size` | `int` | `20` | `1-100`（部分接口上限 200） | 每页条数 |

### 1.4 日期参数格式

所有日期参数使用 ISO 8601 格式：
- 日期: `"2026-07-01"`
- 日期时间: `"2026-07-01T14:30:00"`

### 1.5 HTTP 状态码约定

| 状态码 | 含义 |
|--------|------|
| `200` | 成功（GET/PUT/DELETE-软删除） |
| `201` | 创建成功（POST） |
| `204` | 物理删除成功（无响应体，仅 KG 关系等非核心资源） |
| `400` | 请求参数错误 |
| `404` | 资源不存在 |
| `409` | 资源冲突（如外键依赖不可删除） |
| `500` | 服务器内部错误 |
| `501` | 功能未实现（stub 占位） |
| `503` | 依赖服务不可用（如 Neo4j 未连接） |

> **注意**: Drug/Inventory/PromptTemplate/HighRiskKeyword 四个核心资源使用**软删除**策略（`deleted_at`），DELETE 返回 **200** + JSON body（`{"success": true, ...}`），而非 204。仅 KG 关系等非核心资源 DELETE 返回 204。

### 1.6 认证

> **当前 Phase 1 未启用认证**。所有接口无需 Token，可直接调用。
>
> `AdminUser` 表和密码哈希字段已就位，认证中间件将在 Phase 2 接入。

### 1.7 空值 / null 字段

- `datetime` 类型字段为 `null` 时序列化为 JSON `null`
- `str | None` 类型字段为 `None` 时序列化为 JSON `null`
- `dict | None` 类型字段为 `None` 时序列化为 JSON `null`
- `int | None` 类型字段为 `None` 时序列化为 JSON `null`

---

## 2. 仪表盘 / 用户分析 (`/analytics`)

### 2.1 概览统计

```
GET /api/v1/admin/analytics/overview?days=30
```

**请求参数**:

| 参数 | 类型 | 默认 | 范围 | 说明 |
|------|------|------|------|------|
| `days` | `int` | `30` | `1-365` | 统计最近 N 天 |

**响应示例**:

```json
{
  "total_sessions": 1580,
  "active_sessions": 12,
  "total_messages": 8230,
  "avg_messages_per_session": 5.2,
  "safety_block_rate": 0.023
}
```

| 字段 | 说明 |
|------|------|
| `total_sessions` | 统计周期内总会话数 |
| `active_sessions` | 当前 `status='active'` 的会话数 |
| `total_messages` | 统计周期内总消息数 |
| `avg_messages_per_session` | 平均每个会话的消息数 |
| `safety_block_rate` | 安全拦截率（BLOCK / 总安全评估数） |

### 2.2 按天趋势

```
GET /api/v1/admin/analytics/trends?days=30
```

**请求参数**: 同概览（`days`）

**响应示例**:

```json
[
  {"date": "2026-06-08", "sessions": 45, "messages": 210, "recommendations": 38},
  {"date": "2026-06-09", "sessions": 52, "messages": 245, "recommendations": 44}
]
```

> **注意**: 返回严格按天的完整时间序列（共 `days` 条），缺失日期各字段值为 `0`，前端可直接渲染折线图无需补 0。

### 2.3 Intent 分布

```
GET /api/v1/admin/analytics/intents?days=30
```

**响应示例**:

```json
[
  {"intent": "describe_symptom", "count": 420},
  {"intent": "ask_drug", "count": 310},
  {"intent": "give_up", "count": 25}
]
```

### 2.4 转化漏斗

```
GET /api/v1/admin/analytics/conversion?days=30
```

**响应示例**:

```json
{
  "total_sessions": 1580,
  "with_symptoms": 1420,
  "recommendations_given": 980,
  "with_ai_response": 1350
}
```

| 字段 | 说明 |
|------|------|
| `total_sessions` | 总会话 |
| `with_symptoms` | 有用户消息的会话（用户至少发了一条消息） |
| `recommendations_given` | 有推荐结果的会话（state_snapshot 非空） |
| `with_ai_response` | 有 AI 回复的会话 |

### 2.5 Top 推荐药品

```
GET /api/v1/admin/analytics/top-drugs?days=30&limit=10
```

**请求参数**:

| 参数 | 类型 | 默认 | 范围 | 说明 |
|------|------|------|------|------|
| `days` | `int` | `30` | `1-365` | 统计周期 |
| `limit` | `int` | `10` | `1-50` | 返回 Top N |

**响应示例**:

```json
[
  {"drug_name": "布洛芬", "count": 285},
  {"drug_name": "对乙酰氨基酚", "count": 198}
]
```

> **注意**: 该接口在 Python 内存中聚合 `state_snapshot` JSON 字段，数据量大时可能较慢。

---

## 3. 对话管理 (`/conversations`)

### 3.1 会话列表

```
GET /api/v1/admin/conversations?page=1&page_size=20&status=active&keyword=头痛
```

**请求参数**:

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `page` | `int` | `1` | 页码 |
| `page_size` | `int` | `20` | 每页条数（最大 100） |
| `status` | `str` | — | 筛选状态: `active` / `expired` / `closed` |
| `date_from` | `str` | — | ISO 日期起始（如 `"2026-07-01"`） |
| `date_to` | `str` | — | ISO 日期截止 |
| `user_id` | `int` | — | 按用户 ID 筛选 |
| `keyword` | `str` | — | 搜索消息内容（模糊匹配） |

**响应示例**:

```json
{
  "items": [
    {
      "session_id": "a1b2c3d4-...",
      "user_id": 5,
      "user_nickname": "张先生",
      "status": "active",
      "message_count": 12,
      "first_message": "我头疼，有点发热...",
      "last_message_at": "2026-07-07T10:30:00",
      "intents": ["describe_symptom", "ask_drug"],
      "recommendation_count": 3,
      "created_at": "2026-07-07T09:00:00"
    }
  ],
  "total": 150,
  "page": 1,
  "page_size": 20
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `session_id` | `str` | 会话 UUID（用于跳转详情） |
| `user_id` | `int\|null` | 关联用户 ID（匿名用户为 null） |
| `user_nickname` | `str\|null` | 用户昵称 |
| `status` | `str` | `active` / `expired` / `closed` |
| `message_count` | `int` | 会话消息总数 |
| `first_message` | `str` | 用户第一条消息（截断至 100 字） |
| `last_message_at` | `str\|null` | 最后消息时间 |
| `intents` | `list[str]` | 该会话中出现过的所有 intent |
| `recommendation_count` | `int` | 推荐药品数（从 state_snapshot 提取） |
| `created_at` | `str` | 会话创建时间 |

### 3.2 会话详情

```
GET /api/v1/admin/conversations/{session_id}
```

> **注意**: `{session_id}` 是 UUID 字符串（如 `"a1b2c3d4-e5f6-..."`），**不是数据库自增 ID**。

**响应示例**:

```json
{
  "session_id": "a1b2c3d4-...",
  "user_id": 5,
  "user_nickname": "张先生",
  "status": "active",
  "created_at": "2026-07-07T09:00:00",
  "expires_at": "2026-07-07T09:30:00",
  "updated_at": "2026-07-07T10:30:00",
  "messages": [
    {
      "role": "user",
      "content": "我头疼，有点发热",
      "intent": "describe_symptom",
      "metadata": {"phase": "intake"},
      "timestamp": "2026-07-07T09:00:05"
    },
    {
      "role": "assistant",
      "content": "您好，请问您头痛持续多久了？",
      "intent": null,
      "metadata": {"phase": "consult", "node": "consult"},
      "timestamp": "2026-07-07T09:00:08"
    }
  ],
  "state_snapshot": {
    "consult_slots": {"symptoms": ["头痛", "发热"], "duration": "2天"},
    "recommendations": [
      {"generic_name": "布洛芬", "score": 0.92, "inventory": [...]}
    ],
    "phase": "end",
    "consult_rounds": 3,
    "safety_result": {"verdict": "PASS"}
  }
}
```

| 字段 | 说明 |
|------|------|
| `messages[].intent` | 仅 user 消息有此字段，assistant 消息为 `null` |
| `messages[].metadata` | 节点运行元数据，如 `{"phase": "recommending", "node": "recommend"}` |
| `state_snapshot` | 会话结束时保存的完整状态快照，会话进行中时为 `null` |

### 3.3 导出会话

```
GET /api/v1/admin/conversations/{session_id}/export?format=json
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `format` | `json` | `json` 或 `csv` |

- JSON 格式：返回完整会话数据（含 state_snapshot）
- CSV 格式：只包含消息列表 (role, content, intent, timestamp)
- 响应头包含 `Content-Disposition: attachment; filename=conversation_{session_id}.{format}`

---

## 4. 用户管理 (`/users`)

### 4.1 用户列表

```
GET /api/v1/admin/users?page=1&page_size=20&search=张三
```

**请求参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `page` / `page_size` | `int` | 分页 |
| `search` | `str` | 搜索 `external_id` 或 `nickname`（模糊匹配） |

**响应示例**:

```json
{
  "items": [
    {
      "id": 5,
      "external_id": "13800138000",
      "nickname": "张先生",
      "session_count": 8,
      "last_active_at": "2026-07-07T15:30:00",
      "created_at": "2026-06-01T09:00:00"
    }
  ],
  "total": 45,
  "page": 1,
  "page_size": 20
}
```

### 4.2 用户详情

```
GET /api/v1/admin/users/{user_id}
```

> **注意**: `{user_id}` 是数据库自增整数 ID。

**响应示例**:

```json
{
  "id": 5,
  "external_id": "13800138000",
  "nickname": "张先生",
  "health_profile": {
    "allergies": ["青霉素"],
    "chronic_conditions": ["高血压"],
    "age": 45,
    "special_population": null
  },
  "session_count": 8,
  "last_active_at": "2026-07-07T15:30:00",
  "created_at": "2026-06-01T09:00:00",
  "recent_sessions": [
    {"session_id": "a1b2-...", "status": "active", "created_at": "2026-07-07T09:00:00"}
  ]
}
```

> **注意**: `health_profile` 是自由格式 JSON，字段可能不完整（部分用户无健康画像），前端注意 null-safe 访问。

### 4.3 用户的会话列表

```
GET /api/v1/admin/users/{user_id}/sessions?page=1&page_size=20
```

**响应**: `PaginatedResponse<{session_id, status, created_at, message_count}>`

---

## 5. LLM 用量与模型配置 (`/llm`)

### 5.1 用量概览

```
GET /api/v1/admin/llm/overview?date_from=2026-07-01&date_to=2026-07-07
```

**请求参数**:

| 参数 | 类型 | 说明 |
|------|------|------|
| `date_from` | `str` | ISO 日期起始（可选） |
| `date_to` | `str` | ISO 日期截止（可选） |

**响应示例**:

```json
{
  "total_calls": 12500,
  "total_prompt_tokens": 3200000,
  "total_completion_tokens": 850000,
  "avg_latency_ms": 1230.5,
  "p95_latency_ms": 4500.0,
  "error_rate": 0.012,
  "date_from": "2026-07-01",
  "date_to": "2026-07-07"
}
```

### 5.2 时间趋势

```
GET /api/v1/admin/llm/trends?days=7
```

**响应**:

```json
[
  {"date": "2026-07-01", "calls": 1800, "prompt_tokens": 450000, "completion_tokens": 120000},
  {"date": "2026-07-02", "calls": 1950, "prompt_tokens": 480000, "completion_tokens": 130000}
]
```

> 返回最近 N 天完整序列（含无数据的日期，值为 0），按日期升序。

### 5.3 按节点分解

```
GET /api/v1/admin/llm/by-node?date_from=2026-07-01&date_to=2026-07-07
```

**响应**:

```json
[
  {"node": "react", "calls": 4500, "prompt_tokens": 1200000, "completion_tokens": 320000, "avg_latency_ms": 2500.5},
  {"node": "consult", "calls": 3500, "prompt_tokens": 850000, "completion_tokens": 220000, "avg_latency_ms": 800.2},
  {"node": "dispatcher", "calls": 2000, "prompt_tokens": 500000, "completion_tokens": 130000, "avg_latency_ms": 450.1},
  {"node": "recommend", "calls": 1500, "prompt_tokens": 400000, "completion_tokens": 100000, "avg_latency_ms": 600.3},
  {"node": "classifier", "calls": 1000, "prompt_tokens": 250000, "completion_tokens": 80000, "avg_latency_ms": 350.8}
]
```

> 按调用次数降序排列。节点含义: `dispatcher`（对话调度）、`consult`（症状收集）、`react`（药品问答）、`recommend`（推荐生成）、`classifier`（症状标准化）、`generator`（SOP 回复生成）。

### 5.4 调用明细

```
GET /api/v1/admin/llm/calls?page=1&page_size=20&node=react&model=qwen-plus&session_id=xxx&turn_id=xxx
```

**请求参数**:

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页 |
| `session_id` | 按会话 UUID 筛选 |
| `turn_id` | 按 turn 筛选（精确匹配） |
| `node` | 按调用节点筛选 |
| `model` | 按模型名筛选 |
| `date_from` / `date_to` | 日期范围 |

**响应**:

```json
{
  "items": [
    {
      "id": 1001,
      "session_id": "a1b2-...",
      "turn_id": "a1b2-...:3:a1b2c3d4",
      "node": "react",
      "model": "qwen-plus",
      "prompt_tokens": 2500,
      "completion_tokens": 800,
      "latency_ms": 3200.5,
      "success": true,
      "error_message": null,
      "created_at": "2026-07-07T10:30:00"
    }
  ],
  "total": 5000,
  "page": 1,
  "page_size": 20
}
```

> **LLM 调用日志采集机制**: 系统通过 Python `contextvars.ContextVar` 在 Graph 执行入口自动设置 `session_id` 和 `turn_id`，LLMClient 的 `_schedule_log()` 方法自动从协程上下文中补全这两个字段。调用方无需显式传参。`session_id` 和 `turn_id` 字段已正常填充。

### 5.5 模型配置列表

```
GET /api/v1/admin/llm/models
```

**响应**:

```json
[
  {
    "id": 1,
    "role": "dispatcher",
    "model_name": "qwen-turbo",
    "temperature": 0.1,
    "max_tokens": 512,
    "is_active": true,
    "description": "默认调度器配置",
    "updated_at": "2026-07-01T09:00:00"
  }
]
```

> **role 枚举**: `dispatcher` / `consult` / `react` / `recommend` / `classifier` / `generator`

### 5.6 更新模型配置

```
PUT /api/v1/admin/llm/models/{role}
```

**请求体** (所有字段可选):

```json
{
  "model_name": "qwen-max",
  "temperature": 0.3,
  "max_tokens": 2048,
  "description": "升级到 qwen-max"
}
```

| 字段 | 类型 | 约束 | 说明 |
|------|------|------|------|
| `model_name` | `str` | — | 模型名 |
| `temperature` | `float` | `0.0 - 2.0` | 采样温度 |
| `max_tokens` | `int` | `>=1` | 最大输出 token |
| `description` | `str` | — | 变更说明 |

> **注意**: `{role}` 必须在 DB 中已有 `is_active=true` 的配置，否则返回 404。

---

## 6. 药品数据库管理 (`/database`)

> **删除策略**: Drug 和 Inventory 使用**软删除**（`deleted_at` 时间戳）。DELETE 返回 200 + JSON body 而非 204。`deleted_at IS NULL` 的记录才出现在列表/详情中。物理删除只发生在 KG 关系等非核心资源。

### 6.1 药品列表

```
GET /api/v1/admin/database/drugs?page=1&page_size=20&search=布洛芬&category=感冒退烧&otc_type=甲类
```

**请求参数**:

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页 |
| `search` | 搜索 `generic_name`（模糊） |
| `category` | 按类别筛选 |
| `otc_type` | 按 OTC 类别筛选 `甲类` / `乙类` |

> 列表只显示未删除的记录（`deleted_at IS NULL`）。

**响应**: `PaginatedResponse<Drug>`，Drug 对象字段：

```json
{
  "id": 1,
  "generic_name": "布洛芬",
  "brand_names": ["芬必得", "美林"],
  "category": "感冒退烧",
  "active_ingredients": ["布洛芬"],
  "dosage_form": "片剂",
  "strength": "0.3g",
  "otc_type": "甲类",
  "indication_summary": "用于缓解轻至中度疼痛...",
  "usage_adult": "一次1粒，一日2次",
  "usage_child": "儿童遵医嘱",
  "usage_elderly": "老人酌情减量"
}
```

### 6.2 药品详情（含库存）

```
GET /api/v1/admin/database/drugs/{drug_id}
```

> 仅返回未删除的记录（`deleted_at IS NULL`）。

在药品详情基础上增加了 `inventory` 数组：

```json
{
  "id": 1,
  "generic_name": "布洛芬",
  "...": "...",
  "inventory": [
    {
      "id": 10,
      "product_name": "布洛芬缓释胶囊",
      "manufacturer": "中美天津史克",
      "specification": "0.3g×24粒",
      "stock_quantity": 150,
      "price": 25.80,
      "shelf_location": "A-3-2",
      "is_available": true
    }
  ]
}
```

### 6.3 创建药品

```
POST /api/v1/admin/database/drugs
```

**请求体**:

```json
{
  "generic_name": "对乙酰氨基酚",
  "brand_names": ["泰诺", "扑热息痛"],
  "category": "感冒退烧",
  "active_ingredients": ["对乙酰氨基酚"],
  "dosage_form": "片剂",
  "strength": "500mg",
  "otc_type": "乙类",
  "indication_summary": "用于普通感冒引起的发热...",
  "usage_adult": "一次1片，一日不超过4次",
  "usage_child": "儿童遵医嘱",
  "usage_elderly": "老人遵医嘱"
}
```

| 字段 | 类型 | 必填 | 约束 |
|------|------|------|------|
| `generic_name` | `str` | **是** | 1-200 字符，不可重复 |
| `brand_names` | `list[str]` | 否 | 默认 `[]` |
| `category` | `str` | 否 | 1-50 字符，默认 `"感冒退烧"` |
| `active_ingredients` | `list[str]` | 否 | 默认 `[]` |
| `dosage_form` | `str` | 否 | 最长 50 字符 |
| `strength` | `str` | 否 | 最长 100 字符 |
| `otc_type` | `str` | 否 | 最长 20 字符，默认 `"甲类"` |
| `indication_summary` | `str` | 否 | 最长 500 字符 |
| `usage_adult` | `str` | 否 | 最长 1000 字符 |
| `usage_child` | `str\|null` | 否 | 最长 1000 字符 |
| `usage_elderly` | `str\|null` | 否 | 最长 1000 字符 |

### 6.4 更新药品

```
PUT /api/v1/admin/database/drugs/{drug_id}
```

请求体同创建，但**所有字段可选**（PATCH 语义）。只传需要更新的字段，未传字段保持原值。

### 6.5 删除药品（软删除）

```
DELETE /api/v1/admin/database/drugs/{drug_id}
```

> **软删除**: 设置 `deleted_at` 时间戳并设 `is_active=False`。已删除的药品不会出现在列表中，但数据保留在数据库中。
>
> 返回 **200**（非 204）:
> ```json
> {"success": true, "message": "Drug '布洛芬' soft-deleted", "id": 1}
> ```

### 6.6 库存列表

```
GET /api/v1/admin/database/inventory?page=1&drug_id=1&is_available=true&stock_low=true
```

**请求参数**:

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `page` / `page_size` | `int` | `1/20` | 分页 |
| `drug_id` | `int` | — | 按药品筛选 |
| `is_available` | `bool` | — | `true` 仅可售 / `false` 仅下架 |
| `stock_low` | `bool` | `false` | `true` 仅显示库存紧张（`0 < stock < 10`） |

> 列表只显示未删除的记录（`deleted_at IS NULL`）。`stock_low=true` 时自动筛选库存数量在 1-9 之间的 SKU。

**响应示例**:

```json
{
  "items": [
    {
      "id": 10,
      "drug_id": 1,
      "product_name": "布洛芬缓释胶囊",
      "manufacturer": "中美天津史克",
      "specification": "0.3g×24粒",
      "stock_quantity": 150,
      "price": 25.80,
      "shelf_location": "A-3-2",
      "is_available": true
    }
  ],
  "total": 35,
  "page": 1,
  "page_size": 20
}
```

### 6.7 创建库存

```
POST /api/v1/admin/database/inventory
```

```json
{
  "drug_id": 1,
  "product_name": "布洛芬缓释胶囊",
  "manufacturer": "中美天津史克",
  "specification": "0.3g×24粒",
  "stock_quantity": 150,
  "price": 25.80,
  "shelf_location": "A-3-2",
  "is_available": true
}
```

| 字段 | 类型 | 必填 | 约束 |
|------|------|------|------|
| `drug_id` | `int` | **是** | 关联药品 |
| `product_name` | `str` | **是** | 1-200 字符 |
| `manufacturer` | `str` | **是** | 1-200 字符 |
| `specification` | `str` | 否 | 最长 100 字符 |
| `stock_quantity` | `int` | 否 | `>=0`，默认 0 |
| `price` | `float` | 否 | `>=0`，默认 0 |
| `shelf_location` | `str` | 否 | 最长 50 字符 |
| `is_available` | `bool` | 否 | 默认 `true` |

### 6.8 更新库存

```
PUT /api/v1/admin/database/inventory/{inv_id}
```

请求体同创建但所有字段可选（除 `drug_id` — **库存更新接口不允许修改 drug_id**）。

### 6.9 删除库存（软删除）

```
DELETE /api/v1/admin/database/inventory/{inv_id}
```

> **软删除**: 设置 `deleted_at` 时间戳。返回 **200**（非 204）:
> ```json
> {"success": true, "message": "Inventory #10 soft-deleted", "id": 10}
> ```

### 6.10 权重配置列表

```
GET /api/v1/admin/database/weights
```

**响应**:

```json
[
  {
    "id": 1,
    "version": "3.2.1",
    "policy": "balanced",
    "weights": {"symptom_match": 0.50, "symptom_focus_ratio": 0.15, "safety": 0.20, "inventory_availability": 0.10, "price": 0.05},
    "scoring_version": "v2",
    "is_active": true,
    "description": "均衡策略默认权重",
    "created_at": "2026-07-01T09:00:00"
  }
]
```

> `scoring_version`: `v1`（几何加权平均）或 `v2`（层级乘法模型）。`v2` 的 `weights` 含义不同（指数映射而非归一化权重）。详见评分模块开发文档。

### 6.11 创建权重版本

```
POST /api/v1/admin/database/weights
```

```json
{
  "version": "3.3.0",
  "policy": "safety_first",
  "weights": {"symptom_match": 0.40, "safety": 0.35, "symptom_focus_ratio": 0.10, "inventory_availability": 0.10, "price": 0.05},
  "scoring_version": "v2",
  "description": "安全优先策略"
}
```

| 字段 | 约束 |
|------|------|
| `version` | **必填**，格式 `x.y.z` |
| `policy` | 默认 `"balanced"` |
| `weights` | 自由 JSON |
| `scoring_version` | `"v1"` 或 `"v2"` |

### 6.12 激活权重版本

```
PUT /api/v1/admin/database/weights/{wc_id}/activate
```

> 自动停用其他所有版本，激活指定版本。响应 `{"success": true, "activated": "v3.3.0"}`。

---

## 7. 知识图谱管理 (`/kg`)

> **依赖**: Neo4j 数据库。不可用时所有接口返回 `503` 或 `available: false`。

### 7.1 图谱统计

```
GET /api/v1/admin/kg/stats
```

**Neo4j 可用时**:

```json
{
  "total_nodes": 500,
  "total_relationships": 1200,
  "node_types": {"Drug": 200, "Symptom": 150, "Ingredient": 100, "Condition": 50},
  "relationship_types": {"TREATS": 400, "HAS_INGREDIENT": 300, "HAS_SIDE_EFFECT": 200},
  "available": true
}
```

**Neo4j 不可用时**:

```json
{"total_nodes": 0, "total_relationships": 0, "node_types": {}, "relationship_types": {}, "available": false}
```

> **前端注意**: Neo4j 不可用不会返回 HTTP 错误，而是 `available: false`。请根据此字段判断是否展示 KG 功能入口。

### 7.2 节点搜索

```
GET /api/v1/admin/kg/nodes?page=1&page_size=20&type=Drug&search=布洛芬
```

| 参数 | 说明 |
|------|------|
| `type` | 节点类型: `Drug` / `Symptom` / `Condition` / `Ingredient` / `Population` / `Category` |
| `search` | 搜索 `generic_name` 或 `name` 属性 |

**响应**:

```json
{
  "items": [
    {
      "id": "4:abc123:0",
      "labels": ["Drug"],
      "properties": {"generic_name": "布洛芬", "category": "解热镇痛"}
    }
  ],
  "total": 1,
  "page": 1,
  "page_size": 20
}
```

> **注意**: `id` 是 Neo4j 内部 `elementId`，不是数字。`labels` 是数组（一个节点可以有多个标签）。`properties` 是自由 JSON。

### 7.3 节点详情

```
GET /api/v1/admin/kg/nodes/{node_id}
```

> `{node_id}` 是 Neo4j elementId 字符串（如 `"4:abc123:0"`），需要做 URL 编码。

**响应**:

```json
{
  "id": "4:abc123:0",
  "labels": ["Drug"],
  "properties": {"generic_name": "布洛芬", "category": "解热镇痛"},
  "relations": [
    {
      "rel_id": "5:xyz789:0",
      "type": "TREATS",
      "target_id": "5:def456:0",
      "target_labels": ["Symptom"],
      "target_props": {"name": "头痛"}
    }
  ]
}
```

### 7.4 创建节点 / 删除节点

```
POST /api/v1/admin/kg/nodes        # 创建 (201)
DELETE /api/v1/admin/kg/nodes/{node_id}  # 删除 (204, 物理删除)
```

**创建请求体**:

```json
{
  "labels": ["Drug"],
  "properties": {"generic_name": "阿司匹林", "category": "解热镇痛"}
}
```

> **安全**: `labels` 只允许 `[A-Za-z_][A-Za-z0-9_]*` 格式，`properties` 的 key 同样校验，防止 Cypher 注入。

### 7.5 创建 / 删除关系

```
POST /api/v1/admin/kg/relations              # 创建 (201)
DELETE /api/v1/admin/kg/relations/{rel_id}   # 删除 (204, 物理删除)
```

**创建请求体**:

```json
{
  "from_node_id": "4:abc123:0",
  "to_node_id": "4:def456:1",
  "type": "TREATS",
  "properties": {"confidence": 0.95}
}
```

> `type` 必须是白名单之一: `TREATS`, `CONTRAINDICATED_FOR`, `HAS_INGREDIENT`, `SIMILAR_TO`, `BELONGS_TO`, `HAS_SYMPTOM`, `HAS_SIDE_EFFECT`, `INTERACTS_WITH`, `SUITABLE_FOR`, `USED_FOR`

### 7.6 同步触发

```
POST /api/v1/admin/kg/sync
```

> **当前返回 501 Not Implemented**。Phase 2 实现。

---

## 8. Skill / SOP 编排 (`/skills`)

### 8.1 技能列表

```
GET /api/v1/admin/skills?page=1&page_size=20&status=active
```

| 参数 | 说明 |
|------|------|
| `status` | 筛选: `active` / `inactive` / `draft` |

**响应**:

```json
{
  "items": [
    {
      "id": 1,
      "name": "副作用查询",
      "task_type": "side_effects",
      "status": "active",
      "current_version": "v1.2.0",
      "description": "查询药品副作用信息",
      "created_at": "2026-06-01T09:00:00"
    }
  ],
  "total": 8,
  "page": 1,
  "page_size": 20
}
```

### 8.2 技能详情（含版本历史）

```
GET /api/v1/admin/skills/{skill_id}
```

**响应**:

```json
{
  "id": 1,
  "name": "副作用查询",
  "task_type": "side_effects",
  "status": "active",
  "current_version": "v1.2.0",
  "description": "查询药品副作用信息",
  "created_at": "2026-06-01T09:00:00",
  "updated_at": "2026-07-05T14:00:00",
  "versions": [
    {
      "id": 3,
      "version": "v1.2.0",
      "sop_steps": [
        {"order": 1, "tool_name": "search_manual", "args_template": {"query": "{drug_name} 副作用"}, "parallel_group": 0, "is_critical": true, "timeout_ms": 15000},
        {"order": 2, "tool_name": "search_web", "args_template": {"query": "{drug_name} adverse effects"}, "parallel_group": 0, "is_critical": false, "timeout_ms": 10000}
      ],
      "response_structure": "按系统-器官分类列出常见/严重副作用...",
      "mandatory_reminders": ["以上内容仅供参考，请仔细阅读药品说明书"],
      "fallback_response": "未能查询到 {drug_name} 的副作用信息...",
      "changelog": "新增 search_web 作为补充数据源",
      "is_active": true,
      "created_by": "admin",
      "created_at": "2026-07-05T14:00:00"
    }
  ]
}
```

### 8.3 创建技能

```
POST /api/v1/admin/skills
```

```json
{
  "name": "副作用查询",
  "task_type": "side_effects",
  "description": "查询药品副作用信息"
}
```

> `task_type` 必须是以下之一: `side_effects`, `contraindications`, `dosage`, `efficacy`, `drug_interaction`, `drug_comparison`, `special_population`, `general_consultation`

### 8.4 发布新版本

```
POST /api/v1/admin/skills/{skill_id}/versions
```

```json
{
  "version": "v1.3.0",
  "sop_steps": [
    {"order": 1, "tool_name": "search_manual", "args_template": {"query": "{drug_name} 副作用"}, "parallel_group": 0, "is_critical": true, "timeout_ms": 15000}
  ],
  "response_structure": "按系统-器官分类...",
  "mandatory_reminders": ["以上内容仅供参考，请仔细阅读药品说明书"],
  "fallback_response": "未能查询到 {drug_name} 的副作用信息，建议咨询药师。",
  "changelog": "优化回复结构",
  "created_by": "admin"
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `version` | `str` | 语义版本号 |
| `sop_steps` | `list[dict]` | SOP 步骤列表 |
| `response_structure` | `str` | 回复结构建议 |
| `mandatory_reminders` | `list[str]` | 强制性安全提醒 |
| `fallback_response` | `str` | 兜底回复模板（支持 `{drug_name}` 占位符） |
| `changelog` | `str` | 版本变更说明 |
| `created_by` | `str` | 创建人，默认 `"system"` |

### 8.5 激活版本

```
PUT /api/v1/admin/skills/{skill_id}/versions/{version_id}/activate
```

> 更新 `skill.current_version` 并设置 `skill.status = "active"`。

**响应**: `{"success": true, "activated": "副作用查询 v1.3.0"}`

### 8.6 模拟执行

```
POST /api/v1/admin/skills/{skill_id}/test
```

> **当前返回 501 Not Implemented**。Phase 2 接入 SOPEngine 模拟执行。

### 8.7 SOP 编排接口

```
GET    /api/v1/admin/skills/{skill_id}/versions/{version_id}/sop          # 查看 SOP
PUT    /api/v1/admin/skills/{skill_id}/versions/{version_id}/sop          # 整体替换 SOP
POST   /api/v1/admin/skills/{skill_id}/versions/{version_id}/steps        # 添加步骤
PUT    /api/v1/admin/skills/{skill_id}/versions/{version_id}/steps/{order}  # 编辑步骤
DELETE /api/v1/admin/skills/{skill_id}/versions/{version_id}/steps/{order}  # 删除步骤
POST   /api/v1/admin/skills/{skill_id}/versions/{version_id}/validate     # 校验 SOP
```

**SOP 步骤数据结构** (`SOPStepIn`):

```json
{
  "order": 1,
  "tool_name": "search_manual",
  "args_template": {"query": "{drug_name} 副作用"},
  "parallel_group": 0,
  "is_critical": true,
  "timeout_ms": 15000
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `order` | `int` | 步骤序号 |
| `tool_name` | `str` | 使用的工具名 |
| `args_template` | `dict` | 参数模板，支持 `{drug_name}` 等占位符 |
| `parallel_group` | `int` | 并行组号（0=串行，>0 同组并行） |
| `is_critical` | `bool` | 是否为关键步骤 |
| `timeout_ms` | `int\|null` | 超时（毫秒） |

**校验接口** (`POST .../validate`):

```json
// 响应
{"valid": false, "errors": ["Step 1: tool 'search_xxx' is not registered or inactive"]}
```

> 校验规则：工具名是否在 DB 中且状态为 `active`、参数占位符格式、并行组冲突。

---

## 9. 工具管理 (`/tools`)

### 9.1 工具列表

```
GET /api/v1/admin/tools?page=1&page_size=20&status=active
```

**响应**:

```json
{
  "items": [
    {
      "id": 1,
      "name": "search_manual",
      "display_name": "说明书检索",
      "description": "在药品说明书知识库中搜索信息",
      "capabilities": ["drug_manual", "rag"],
      "fallback_tools": ["search_web"],
      "timeout_ms": 15000,
      "retry_count": 1,
      "status": "active",
      "updated_at": "2026-07-01T09:00:00"
    }
  ],
  "total": 6,
  "page": 1,
  "page_size": 20
}
```

| 字段 | 说明 |
|------|------|
| `name` | 工具唯一标识名 |
| `display_name` | 展示名 |
| `capabilities` | 能力标签数组 |
| `fallback_tools` | 容错替代工具数组 |
| `status` | `active` / `inactive` / `deprecated` |

### 9.2 工具详情

```
GET /api/v1/admin/tools/{name}
```

> **注意**: `{name}` 是工具名（如 `"search_manual"`），不是数字 ID。

在列表字段基础上增加 `parameters_schema`（OpenAI function-calling 格式的 JSON Schema）。

### 9.3 更新工具元数据

```
PUT /api/v1/admin/tools/{name}
```

```json
{
  "display_name": "说明书检索（优化）",
  "description": "更新后的描述",
  "timeout_ms": 20000,
  "retry_count": 2
}
```

> **所有字段可选**（PATCH 语义）。

### 9.4 启用 / 停用工具

```
PUT /api/v1/admin/tools/{name}/status
```

```json
{"status": "inactive"}
```

> `status` 取值: `active` / `inactive` / `deprecated`

**响应**: `{"success": true, "tool": "search_manual", "previous_status": "active", "current_status": "inactive"}`

### 9.5 调用统计

```
GET /api/v1/admin/tools/{name}/stats
```

> **当前返回 0 值占位**。精确的工具级统计需要 `LLMCallLog` 增加 `tool_name` 字段（Phase 2）。

---

## 10. Prompt 版本管理 (`/prompts`)

> **删除策略**: Prompt 模板使用**软删除**（`deleted_at` 时间戳）。DELETE 返回 200 + JSON body。列表/详情/激活操作均过滤 `deleted_at IS NULL`。

### 10.1 Prompt 列表

```
GET /api/v1/admin/prompts?page=1&page_size=20&role=react
```

**请求参数**: `role` 可选筛选。

> 列表只显示未删除的记录（`deleted_at IS NULL`），不返回 `content` 字段（Prompt 全文）。

**响应**:

```json
{
  "items": [
    {
      "id": 1,
      "role": "react",
      "version": "v1.2.0",
      "is_active": true,
      "description": "优化工具选择逻辑",
      "updated_by": "admin",
      "created_at": "2026-07-05T14:00:00"
    }
  ],
  "total": 15,
  "page": 1,
  "page_size": 20
}
```

### 10.2 Prompt 详情

```
GET /api/v1/admin/prompts/{prompt_id}
```

> 仅返回未删除的记录。在列表字段基础上增加 `content` 字段（完整 Prompt 文本）。

### 10.3 新增 Prompt 版本

```
POST /api/v1/admin/prompts
```

```json
{
  "role": "react",
  "version": "v1.3.0",
  "content": "你是一个药品推荐助手...（完整 Prompt 文本）",
  "description": "增加多轮对话能力",
  "updated_by": "admin"
}
```

> `role` 必须是以下之一: `dispatcher`, `consult`, `react`, `recommend`, `classifier`, `generator`, `safety_block`

### 10.4 激活版本

```
PUT /api/v1/admin/prompts/{prompt_id}/activate
```

> 自动停用同 `role` 的其他版本，仅操作未删除记录。响应: `{"success": true, "activated": "react v1.3.0"}`

### 10.5 删除 Prompt（软删除）

```
DELETE /api/v1/admin/prompts/{prompt_id}
```

> **软删除**: 设置 `deleted_at` 时间戳并设 `is_active=False`。仅操作未删除记录，已删除的记录返回 404。
>
> 返回 **200**（非 204）:
> ```json
> {"success": true, "message": "Prompt 'react v1.3.0' soft-deleted", "id": 1, "deleted_at": "2026-07-07T15:00:00"}
> ```

---

## 11. Web Search 配置 (`/web-search`)

### 11.1 获取配置

```
GET /api/v1/admin/web-search/config
```

```json
{
  "enabled": true,
  "timeout_seconds": 15.0,
  "max_results": 5,
  "api_key_configured": true
}
```

> `api_key_configured`: 是否已配置 Tavily API Key（只返回 `true/false`，不暴露实际 key）。

### 11.2 更新配置

```
PUT /api/v1/admin/web-search/config
```

```json
{
  "enabled": true,
  "timeout_seconds": 20.0,
  "max_results": 10
}
```

| 字段 | 类型 | 约束 |
|------|------|------|
| `enabled` | `bool\|null` | — |
| `timeout_seconds` | `float\|null` | `0.5 - 60.0` |
| `max_results` | `int\|null` | `1 - 20` |

> 所有字段可选，只传需要更新的即可。

### 11.3 调用统计 / 调用明细

```
GET /api/v1/admin/web-search/stats    # 当前返回 0 值占位 (Phase 2)
GET /api/v1/admin/web-search/calls    # 当前返回空列表占位 (Phase 2)
```

### 11.4 测试搜索

```
POST /api/v1/admin/web-search/test?query=布洛芬+副作用
```

> 直接调用 Tavily API 预览搜索结果，用于验证配置。
>
> **注意**: 此接口参数 `query` 是 **query string** 不是 request body。

**响应**:

```json
{
  "status": "ok",
  "query": "布洛芬 副作用",
  "results": [
    {"title": "...", "url": "...", "content": "..."}
  ]
}
```

或错误:

```json
{"status": "error", "query": "...", "error": "Tavily API key not configured"}
```

---

## 12. 用户反馈 (`/feedback`)

### 12.1 反馈列表

```
GET /api/v1/admin/feedback?page=1&page_size=20&drug_id=1&rating=5
```

**请求参数**:

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页 |
| `drug_id` | 按药品筛选 |
| `rating` | 按评分筛选 (1-5) |

**响应**:

```json
{
  "items": [
    {
      "id": 100,
      "session_id": "a1b2-...",
      "drug_id": 1,
      "drug_name": "布洛芬",
      "rating": 5,
      "comment": "效果很好，吃完半小时就不疼了",
      "created_at": "2026-07-07T10:30:00"
    }
  ],
  "total": 200,
  "page": 1,
  "page_size": 20
}
```

### 12.2 按药品聚合评分

```
GET /api/v1/admin/feedback/stats?limit=20
```

```json
[
  {"drug_name": "布洛芬", "avg_rating": 4.5, "feedback_count": 85},
  {"drug_name": "对乙酰氨基酚", "avg_rating": 4.2, "feedback_count": 62}
]
```

> 按反馈数量降序，返回 Top N。

---

## 13. 审计日志 (`/audit`)

> **审计机制**: 系统通过 `AuditLogMiddleware`（ASGI 中间件）自动捕获所有 admin 写操作（POST/PUT/DELETE），仅记录 2xx 成功响应。审计日志在端点处理完成后异步写入，不影响请求响应时间。

### 13.1 审计日志列表

```
GET /api/v1/admin/audit?page=1&page_size=20&admin_user=admin&action=update&resource_type=drug&date_from=2026-07-01&date_to=2026-07-07
```

**请求参数**:

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页 |
| `admin_user` | 按操作人筛选 |
| `action` | 按操作类型筛选: `create` / `update` / `delete` / `activate` / `deactivate` |
| `resource_type` | 按资源类型筛选 |
| `date_from` / `date_to` | 日期范围 |

**响应**:

```json
{
  "items": [
    {
      "id": 5001,
      "admin_user": "admin",
      "action": "update",
      "resource_type": "drug",
      "resource_id": "1",
      "changes": {
        "method": "PUT",
        "path": "/api/v1/admin/database/drugs/1",
        "status_code": 200,
        "elapsed_ms": 45.2
      },
      "ip_address": "192.168.1.100",
      "created_at": "2026-07-07T14:00:00"
    }
  ],
  "total": 500,
  "page": 1,
  "page_size": 20
}
```

| 字段 | 说明 |
|------|------|
| `admin_user` | 操作人标识（Phase 1 固定为 `"admin"`，Phase 2 从 JWT 提取） |
| `changes` | 中间件记录的请求元信息（method/path/status_code/elapsed_ms）。端点内显式调用 `audit_log()` 可获得更精确的字段变更信息（Phase 2） |
| `ip_address` | 客户端 IP 地址 |

> **审计范围**: 中间件自动覆盖所有 admin 写操作（POST/PUT/DELETE）。`changes` 字段目前记录请求元信息；端点内显式调用 `audit_log()` 可记录精确的字段变更（Phase 2 逐步接入）。
>
> **隐私合规**: IP 地址采集依据《个人信息保护法》第 13 条（履行法定职责所必需），仅用于安全审计，不用于用户画像。

---

## 14. 高风险关键字监控 (`/risk-keywords` + `/risk-alerts`)

> **检测机制**: 系统在 `end_node` 中自动触发关键字检测（fire-and-forget，不阻塞 SSE 流）。检测分三层：
> 1. **Substring 快速匹配** — 忽略大小写
> 2. **正则词边界校验** — 防止"解毒"误匹配"毒品"
> 3. **Negative patterns 白名单** — 如关键字"毒品"的 negative_pattern = `"药品|解毒|消毒"`，命中则跳过
>
> 关键字列表每 60 秒从 DB 缓存刷新一次。

### 14.1 关键字列表

```
GET /api/v1/admin/risk-keywords?page=1&page_size=50&category=suicide&is_active=true
```

**请求参数**:

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页（最大 200） |
| `category` | 类别筛选: `suicide` / `severe_allergy` / `emergency` / `drug_abuse` / `other` |
| `is_active` | `true` 仅启用 / `false` 仅停用 |

**响应**:

```json
{
  "items": [
    {
      "id": 1,
      "keyword": "自杀",
      "category": "suicide",
      "severity": "critical",
      "negative_patterns": "游戏自杀|角色自杀",
      "is_active": true,
      "created_at": "2026-07-01T09:00:00"
    }
  ],
  "total": 25,
  "page": 1,
  "page_size": 50
}
```

| 字段 | 说明 |
|------|------|
| `negative_patterns` | 白名单正则（逗号分隔）。当关键字命中后，若内容也匹配此正则，则不告警。`null` 表示无白名单 |

### 14.2 新增关键字

```
POST /api/v1/admin/risk-keywords
```

```json
{
  "keyword": "自杀",
  "category": "suicide",
  "severity": "critical",
  "negative_patterns": "游戏自杀|角色自杀",
  "is_active": true
}
```

| 字段 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `keyword` | `str` | **必填** | 关键字内容 |
| `category` | `str` | `"other"` | 类别 |
| `severity` | `str` | `"medium"` | 严重程度: `low` / `medium` / `high` / `critical` |
| `negative_patterns` | `str\|null` | `null` | 白名单正则（逗号分隔），用于排除误匹配 |
| `is_active` | `bool` | `true` | 是否启用 |

### 14.3 编辑 / 删除关键字

```
PUT    /api/v1/admin/risk-keywords/{kw_id}        # 编辑
DELETE /api/v1/admin/risk-keywords/{kw_id}        # 删除（软删除, 200）
```

> 删除为**软删除**（设置 `deleted_at`），返回 200 + JSON body。

### 14.4 告警列表

```
GET /api/v1/admin/risk-alerts?page=1&page_size=20&is_reviewed=false&category=suicide
```

**请求参数**:

| 参数 | 说明 |
|------|------|
| `is_reviewed` | `true` 已处理 / `false` 未处理 |
| `category` | 按关键字类别筛选 |

**响应**:

```json
{
  "items": [
    {
      "id": 100,
      "session_id": "a1b2-...",
      "keyword_id": 1,
      "matched_content": "我最近很难受，有时候想自杀...",
      "is_reviewed": false,
      "reviewed_by": null,
      "review_notes": null,
      "created_at": "2026-07-07T15:30:00"
    }
  ],
  "total": 12,
  "page": 1,
  "page_size": 20
}
```

> `matched_content` 截断至 500 字。

### 14.5 标记告警已处理

```
PUT /api/v1/admin/risk-alerts/{alert_id}/review
```

```json
{
  "reviewed_by": "admin",
  "review_notes": "已联系用户，确认误触"
}
```

> 两个字段都有默认值（`reviewed_by="admin"`, `review_notes=""`），可以不传 body。

**响应**: `{"success": true, "alert_id": 100, "reviewed_by": "admin"}`

### 14.6 告警统计

```
GET /api/v1/admin/risk-alerts/stats?days=30
```

```json
{
  "total_alerts": 45,
  "reviewed_count": 30,
  "unreviewed_count": 15,
  "by_category": {"suicide": 12, "emergency": 20, "drug_abuse": 8, "other": 5},
  "by_severity": {"critical": 10, "high": 15, "medium": 18, "low": 2}
}
```

---

## 15. 链路追踪 (`/traces`)

> **采集机制**: 在 SSE Graph 执行循环中，为 9 个核心节点（intake/dispatcher/consult/safety_block/recommend/explain/inventory/react/end）自动采集 start/end/error 事件。节点开始时通过 `datetime.now(timezone.utc)` 记录精确时间戳，结束时计算 `duration_ms` 并提取节点特有元数据。

### 15.1 Trace 会话列表

```
GET /api/v1/admin/traces?page=1&page_size=20&status=error&keyword=头痛
```

**请求参数**:

| 参数 | 说明 |
|------|------|
| `page` / `page_size` | 分页 |
| `date_from` / `date_to` | 日期范围 |
| `status` | `completed` / `error` |
| `keyword` | 搜索消息内容 |

**响应**:

```json
{
  "items": [
    {
      "session_id": "a1b2-...",
      "turn_count": 5,
      "total_duration_ms": 12500.0,
      "error_count": 1,
      "first_node": "intake",
      "last_node": "end",
      "created_at": "2026-07-07T09:00:00"
    }
  ],
  "total": 300,
  "page": 1,
  "page_size": 20
}
```

### 15.2 Turn 完整链路

```
GET /api/v1/admin/traces/{turn_id}
```

> **`turn_id` 格式**: `"{session_id}:{user_message_count}:{random_hex8}"`（如 `"a1b2c3d4-...:3:a1b2c3d4"`）。第三段的 8 位 hex 用于防止高并发下的 turn_id 碰撞。
>
> **作为 URL 路径参数时**，冒号 `:` 必须编码为 `%3A`。示例: `/api/v1/admin/traces/a1b2c3d4-...%3A3%3Aa1b2c3d4`
>
> **向后兼容**: 查询时自动剥离末尾 uuid8 后缀（新格式→旧格式兼容）。先按新格式精确匹配，再按旧格式 `{sid}:{count}` 匹配，最后 fallback 到 session_id 级别查询。

**响应**:

```json
{
  "turn_id": "a1b2c3d4-...:3:a1b2c3d4",
  "session_id": "a1b2c3d4-...",
  "nodes": [
    {
      "node": "intake",
      "status": "completed",
      "duration_ms": 150.0,
      "metadata": null,
      "error_message": null,
      "started_at": "2026-07-07T09:00:00"
    },
    {
      "node": "dispatcher",
      "status": "completed",
      "duration_ms": 450.5,
      "metadata": {"route": "consult", "intent": "describe_symptom", "actions": null},
      "error_message": null,
      "started_at": "2026-07-07T09:00:01"
    },
    {
      "node": "recommend",
      "status": "completed",
      "duration_ms": 3200.0,
      "metadata": {"count": 3},
      "error_message": null,
      "started_at": "2026-07-07T09:00:05"
    }
  ],
  "llm_calls": [
    {
      "id": 1001,
      "node": "dispatcher",
      "model": "qwen-turbo",
      "prompt_tokens": 500,
      "completion_tokens": 80,
      "latency_ms": 450.5,
      "success": true,
      "error_message": null,
      "created_at": "2026-07-07T09:00:01"
    }
  ],
  "messages": [
    {"role": "user", "content": "我头疼...", "intent": "describe_symptom", "timestamp": "2026-07-07T09:00:00"}
  ]
}
```

**节点 metadata 说明**:

| 节点 | metadata 内容 |
|------|-------------|
| `dispatcher` | `route`（路由方向）、`intent`（用户意图）、`actions`（执行计划） |
| `consult` | `next_action`（下一步动作）、`rounds`（问诊轮数） |
| `safety_block` | `verdict`（PASS/BLOCK）、`triggered_rules`（触发的规则列表） |
| `recommend` | `count`（推荐药品数） |
| `intake/explain/inventory/react/end` | `null` 或 `{"events": [...]}` |

### 15.3 Trace 统计

```
GET /api/v1/admin/traces/stats?date_from=2026-07-01&date_to=2026-07-07
```

```json
{
  "total_turns": 8500,
  "error_rate": 0.015,
  "avg_duration_ms": 3200.5
}
```

---

## 16. 系统配置 (`/config`)

### 16.1 获取配置

```
GET /api/v1/admin/config
```

```json
[
  {
    "key": "max_consult_rounds",
    "value": "8",
    "description": "最大问诊轮数",
    "updated_by": "system",
    "updated_at": null
  },
  {
    "key": "web_search_enabled",
    "value": "true",
    "description": "联网搜索开关",
    "updated_by": "admin",
    "updated_at": "2026-07-05T10:00:00"
  },
  {
    "key": "session_expire_minutes",
    "value": "30",
    "description": "会话过期时间（分钟）",
    "updated_by": "system",
    "updated_at": null
  }
]
```

> **注意**: `value` 统一为字符串类型，前端需按 key 做类型转换（如 `"8"` → `8`, `"true"` → `true`）。
>
> **当前只有 3 个配置项**，新增 key 需改代码（`_RUNTIME_KEYS` 字典 + `Settings` 类）。

### 16.2 批量更新配置

```
PUT /api/v1/admin/config
```

```json
{
  "configs": [
    {"key": "max_consult_rounds", "value": "10", "description": "延长问诊", "updated_by": "admin"},
    {"key": "web_search_enabled", "value": "false"}
  ]
}
```

> `configs` 数组最少 1 条，最多 20 条。`description` 和 `updated_by` 可选。

**响应**: 更新后的完整配置列表（同 GET）。

---

## 17. 前端对接注意事项

### 17.1 已知限制 & 规避方案

| # | 问题 | 影响 | 规避 / 说明 |
|---|------|------|------------|
| 1 | **无认证** | 当前所有接口无需登录即可访问 | Phase 2 接入认证后，前端需增加登录页和 Token 管理 |
| 2 | **`/web-search/stats` 返回 0** | Web Search 统计不可用 | 返回占位值（total=0, success_rate=0.0）。前端可显示"统计数据收集中" |
| 3 | **`/web-search/calls` 返回空列表** | Web Search 明细不可用 | 同上 |
| 4 | **`/tools/{name}/stats` 返回 0** | 工具统计不可用 | 同上 |
| 5 | **`/skills/{id}/test` 返回 501** | 技能测试不可用 | 返回 `{"detail": "Skill test execution not yet implemented..."}`，前端可显示"即将上线" |
| 6 | **`/kg/sync` 返回 501** | KG 同步不可用 | 同上 |
| 7 | **审计 changes 字段为请求元信息** | 审计日志的 `changes` 目前记录 method/path/status_code，而非字段级变更 | 端点内显式调用 `audit_log()` 可记录精确字段变更（Phase 2 逐步接入）。当前审计日志至少能告诉你"谁在什么时候调了什么接口" |

### 17.2 关键对接细节

1. **`session_id` vs `id`**:
   - 会话对外的唯一标识是 `session_id`（UUID 字符串），不是数据库自增 `id`
   - 用户、药品、库存等使用自增整数 `id`

2. **`turn_id` URL 编码**:
   - `turn_id` 格式为 `"{session_id}:{count}:{hex8}"`（如 `"abc:3:a1b2c3d4"`）
   - 作为 URL 路径参数时，冒号 `:` 必须编码为 `%3A`
   - 示例: `/api/v1/admin/traces/abc%3A3%3Aa1b2c3d4`

3. **Neo4j 不可用处理**:
   - `/admin/kg/stats` 返回 `available: false` 而非 HTTP 错误
   - 其他 KG 接口返回 `503` + `"Neo4j knowledge graph is not available"`
   - 前端应根据 `/admin/kg/stats` 的 `available` 字段决定是否展示 KG 相关菜单

4. **分页 total_pages 计算**:
   - `PaginatedResponse` 的 `total_pages` 是 Python `@property`，JSON 序列化后**不包含**此字段
   - 前端自行计算: `Math.ceil(total / page_size)`

5. **DELETE 接口响应体**:
   - **软删除资源**（Drug/Inventory/PromptTemplate/HighRiskKeyword）：返回 **200** + JSON body（如 `{"success": true, "message": "...", "id": 1}`）
   - **物理删除资源**（KG 节点/关系）：返回 **204 No Content**，响应体为空
   - 前端判断: 检查响应状态码。200 → 解析 JSON body；204 → 直接判定成功

6. **PUT 更新接口语义**:
   - 药品/库存/工具/配置的 PUT 接口所有字段可选，只传需要更新的字段
   - 未传的字段保持原值不变（PATCH 语义）

7. **配置项值类型**:
   - `SystemConfig.value` 统一为 `string`，前端需按 key 类型转换（如 `"8"` → `8`, `"true"` → `true`）

8. **`state_snapshot` 为 null**:
   - 会话进行中（未完成首次推荐）时，`state_snapshot` 为 `null`
   - 前端注意 null-safe 访问

9. **错误响应格式**:
   - 大多数接口的 HTTP 异常由 FastAPI 自动生成，格式为 `{"detail": "error message"}`
   - 部分接口返回结构化错误 `{"success": false, "message": "...", "errors": [...]}`

10. **stub 占位接口**（返回 501）:
    - `POST /admin/skills/{id}/test`
    - `POST /admin/kg/sync`
    - 前端可显示 "即将上线" 的 disabled 状态

11. **软删除资源**:
    - Drug / Inventory / PromptTemplate / HighRiskKeyword 四个资源使用软删除
    - 列表接口只返回未删除记录（`deleted_at IS NULL`）
    - 详情接口也只能访问未删除记录
    - 已删除的数据在管理员界面不可见，但保留在数据库中用于审计和恢复

12. **审计日志自动采集**:
    - 所有 admin 写操作（POST/PUT/DELETE → 2xx）自动记录到审计日志
    - 无需前端额外操作
    - 审计页面可直接使用 `/admin/audit` 接口展示操作历史

### 17.3 推荐的前端路由结构

```
/admin                         # 后台首页 → 仪表盘
/admin/conversations           # 对话管理
/admin/conversations/:sid      # 对话详情
/admin/users                   # 用户管理
/admin/users/:uid              # 用户详情
/admin/llm                     # LLM 用量仪表盘
/admin/llm/models              # 模型配置
/admin/database                # 药品数据库
/admin/database/drugs/:id      # 药品详情
/admin/kg                      # 知识图谱
/admin/skills                  # 技能管理
/admin/skills/:id              # 技能详情（含版本历史）
/admin/skills/:id/versions/:vid/sop  # SOP 编排器
/admin/tools                   # 工具管理
/admin/prompts                 # Prompt 版本管理
/admin/web-search              # Web Search 配置
/admin/feedback                # 用户反馈
/admin/risk                    # 高风险监控（关键字+告警）
/admin/traces                  # 链路追踪
/admin/traces/:turnId          # Turn 详情
/admin/config                  # 系统配置
/admin/audit                   # 审计日志
```

### 17.4 健康检查

```
GET /api/v1/admin/health
```

响应: `{"status": "ok", "service": "admin"}` — 可用于前端检查后端连通性。

### 17.5 前端改造指南（v1.0 → v1.1 差异）

> 本文档经过一轮对抗性审查后，多个已知 bug 已修复。以下是从旧版本文档（v1.0）迁移到当前版本（v1.1）时，前端需要关注的**接口行为变化**和**新增能力**。

#### 🟢 已修复的接口行为变化（前端需要改代码）

**1. DELETE 接口返回码变更（核心资源）**

| 资源 | 旧行为 | 新行为 | 前端改造 |
|------|--------|--------|----------|
| Drug | `204 No Content`（物理删除） | `200` + `{"success":true,"message":"...","id":N}`（软删除） | 旧代码判断 `response.status === 204` → 改为 `response.ok`（200/204 都算成功）。成功后解析 JSON body 展示删除提示 |
| Inventory | `204 No Content` | 同上 `200` + JSON body | 同上 |
| PromptTemplate | `204 No Content` | 同上 `200` + JSON body（含 `deleted_at` 时间戳） | 同上 |
| HighRiskKeyword | `204 No Content` | 同上 `200` + JSON body | 同上 |
| KG 节点/关系 | `204 No Content` | **不变**（仍为物理删除，`204`） | 无需改动 |

> **改造建议**: 封装一个 `handleDelete(response)` 工具函数：
> ```js
> async function handleDelete(response) {
>   if (response.status === 200) {
>     const body = await response.json();
>     showToast(`已删除: ${body.message}`);
>   } else if (response.status === 204) {
>     showToast("已删除");
>   }
>   refreshList();
> }
> ```

**2. LLM 调用日志 `session_id` 不再为 NULL**

| 旧行为 | 新行为 | 前端改造 |
|--------|--------|----------|
| `LLMCallLog.session_id` 可能为 `null`，无法按会话筛选 LLM 调用 | 始终填充（通过 ContextVar 自动采集） | 之前为规避 NULL 做的防御代码可以移除。`/llm/calls` 筛选 `session_id` 现在有效 |

**3. 审计日志不再是空表**

| 旧行为 | 新行为 | 前端改造 |
|--------|--------|----------|
| `/audit` 永远返回空列表 | 所有 admin 写操作自动记录 | **审计页面可以上线了**。之前如果隐藏了审计菜单，现在可以放出来 |

**4. Trace 时间戳不再重复**

| 旧行为 | 新行为 | 前端改造 |
|--------|--------|----------|
| `started_at` 和 `completed_at` 可能相同（bug） | `started_at` 是节点真实开始时间，`completed_at` 是结束时间 | 之前如果有 "只展示 duration_ms，隐藏时间点" 的 workaround 可以移除。时间线展示现在准确了 |

**5. `turn_id` 格式变化**

| 旧格式 | 新格式 | 前端改造 |
|--------|--------|----------|
| `"{session_id}:{seq}"` 如 `"abc:1"` | `"{session_id}:{count}:{hex8}"` 如 `"abc:3:a1b2c3d4"` | URL 编码方式不变（冒号→`%3A`）。但如果前端有解析 `turn_id` 的逻辑（如用 `split(":")` 取 session_id），注意现在是 **3 段** 而非 2 段 |

#### 🟡 新增接口能力（前端可以利用的新功能）

**6. 库存紧张筛选**

```
GET /api/v1/admin/database/inventory?stock_low=true
```

新增 `stock_low` 参数。设为 `true` 时只返回 `0 < stock_quantity < 10` 的 SKU。前端可以：
- 库存管理页加一个 "仅看库存紧张" 的开关/Tab
- Dashboard 加一个 "低库存预警" 卡片，直接调此接口

**7. 高风险关键字现在支持 `negative_patterns`（白名单）**

| 接口 | 字段 | 类型 | 说明 |
|------|------|------|------|
| `GET /risk-keywords` 响应 | `negative_patterns` | `string\|null` | 白名单正则（逗号分隔） |
| `POST /risk-keywords` 请求体 | `negative_patterns` | `string\|null` | 可选。如 `"药品,解毒,消毒"` |
| `PUT /risk-keywords/{id}` 请求体 | `negative_patterns` | `string\|null` | 编辑时也可修改 |

前端改造：
- 关键字列表/详情页增加 `negative_patterns` 展示列
- 新增/编辑关键字的表单增加白名单输入框（label: "白名单模式（逗号分隔）"，helpText: "命中关键字但匹配白名单时不告警"）

**8. 审计日志新增 `ip_address` 字段**

`/audit` 响应中每项增加 `ip_address: string|null`。前端可以在审计表格中增加 "操作 IP" 列（Phase 1 下通常为 `192.168.x.x` 等内网地址）。

#### 🔴 前端改造优先级

| 优先级 | 事项 | 原因 |
|--------|------|------|
| **P0** | DELETE 接口 204→200 | **不改会导致删除操作明明成功了前端却报错**（因为前端期望 204 无 body，实际收到了 200+JSON） |
| **P1** | 审计页面可以上线 | 之前因为空表被隐藏，现在有数据了 |
| **P2** | 库存紧张筛选 | 运营刚需功能，改动量小（加一个开关） |
| **P2** | 关键字白名单字段 | 安全运营需要，避免误告警 |
| **P3** | turn_id 解析适配（如有） | 只有前端手动解析 turn_id 时才需要改 |
| **P3** | 审计日志 IP 列 | 锦上添花 |
| **P3** | 移除 session_id NULL 防御代码 | 代码清理，不改也不影响功能 |

#### 📋 API 行为速查表（v1.0 vs v1.1）

| 接口 | v1.0 | v1.1 |
|------|------|------|
| `DELETE /drugs/{id}` | 物理删除, 204 | 软删除, 200+JSON |
| `DELETE /inventory/{id}` | 物理删除, 204 | 软删除, 200+JSON |
| `DELETE /prompts/{id}` | 物理删除, 204 | 软删除, 200+JSON |
| `DELETE /risk-keywords/{id}` | 物理删除, 204 | 软删除, 200+JSON |
| `DELETE /kg/nodes/{id}` | 物理删除, 204 | **不变**, 204 |
| `DELETE /kg/relations/{id}` | 物理删除, 204 | **不变**, 204 |
| `GET /llm/calls` | session_id 可 NULL | session_id 始终填充 |
| `GET /audit` | 永远空列表 | 有数据 |
| `GET /traces/{id}` | started_at 不准 | started_at 准确 |
| `GET /traces/{id}` | turn_id 2段 | turn_id 3段 |
| `GET /inventory` | 无 stock_low | 新增 stock_low 参数 |
| `GET /risk-keywords` | 无 negative_patterns | 新增 negative_patterns 字段 |
| `POST /risk-keywords` | 无 negative_patterns | 新增 negative_patterns 字段 |
| `GET /audit` | 无 ip_address | 新增 ip_address 字段 |

---

## 附录A: 完整端点清单（80 个）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/admin/health` | 健康检查 |
| GET | `/admin/analytics/overview` | 分析概览 |
| GET | `/admin/analytics/trends` | 时间趋势 |
| GET | `/admin/analytics/intents` | Intent 分布 |
| GET | `/admin/analytics/conversion` | 转化漏斗 |
| GET | `/admin/analytics/top-drugs` | Top 推荐药品 |
| GET | `/admin/conversations` | 对话列表 |
| GET | `/admin/conversations/{sid}` | 对话详情 |
| GET | `/admin/conversations/{sid}/export` | 导出对话 |
| GET | `/admin/users` | 用户列表 |
| GET | `/admin/users/{uid}` | 用户详情 |
| GET | `/admin/users/{uid}/sessions` | 用户会话 |
| GET | `/admin/llm/overview` | LLM 概览 |
| GET | `/admin/llm/trends` | LLM 趋势 |
| GET | `/admin/llm/by-node` | LLM 节点分解 |
| GET | `/admin/llm/calls` | LLM 调用明细 |
| GET | `/admin/llm/models` | 模型配置列表 |
| PUT | `/admin/llm/models/{role}` | 更新模型配置 |
| GET | `/admin/database/drugs` | 药品列表（软删除过滤） |
| GET | `/admin/database/drugs/{id}` | 药品详情+库存（软删除过滤） |
| POST | `/admin/database/drugs` | 创建药品 |
| PUT | `/admin/database/drugs/{id}` | 更新药品 |
| DELETE | `/admin/database/drugs/{id}` | 删除药品（软删除, 200） |
| GET | `/admin/database/inventory` | 库存列表（软删除过滤） |
| POST | `/admin/database/inventory` | 创建库存 |
| PUT | `/admin/database/inventory/{id}` | 更新库存 |
| DELETE | `/admin/database/inventory/{id}` | 删除库存（软删除, 200） |
| GET | `/admin/database/weights` | 权重列表 |
| POST | `/admin/database/weights` | 创建权重 |
| PUT | `/admin/database/weights/{id}/activate` | 激活权重 |
| GET | `/admin/kg/stats` | KG 统计 |
| GET | `/admin/kg/nodes` | KG 节点列表 |
| GET | `/admin/kg/nodes/{id}` | KG 节点详情 |
| POST | `/admin/kg/nodes` | 创建节点 |
| DELETE | `/admin/kg/nodes/{id}` | 删除节点（物理删除, 204） |
| POST | `/admin/kg/relations` | 创建关系 |
| DELETE | `/admin/kg/relations/{id}` | 删除关系（物理删除, 204） |
| POST | `/admin/kg/sync` | 触发同步 (501) |
| GET | `/admin/skills` | 技能列表 |
| GET | `/admin/skills/{id}` | 技能详情 |
| POST | `/admin/skills` | 创建技能 |
| POST | `/admin/skills/{id}/versions` | 发布版本 |
| PUT | `/admin/skills/{id}/versions/{vid}/activate` | 激活版本 |
| POST | `/admin/skills/{id}/test` | 模拟执行 (501) |
| GET | `/admin/skills/{id}/versions/{vid}/sop` | 查看 SOP |
| PUT | `/admin/skills/{id}/versions/{vid}/sop` | 替换 SOP |
| POST | `/admin/skills/{id}/versions/{vid}/steps` | 添加 SOP 步骤 |
| PUT | `/admin/skills/{id}/versions/{vid}/steps/{order}` | 编辑步骤 |
| DELETE | `/admin/skills/{id}/versions/{vid}/steps/{order}` | 删除步骤 |
| POST | `/admin/skills/{id}/versions/{vid}/validate` | 校验 SOP |
| GET | `/admin/tools` | 工具列表 |
| GET | `/admin/tools/{name}` | 工具详情 |
| PUT | `/admin/tools/{name}` | 更新工具 |
| PUT | `/admin/tools/{name}/status` | 启停工具 |
| GET | `/admin/tools/{name}/stats` | 工具统计 (占位) |
| GET | `/admin/prompts` | Prompt 列表（软删除过滤） |
| GET | `/admin/prompts/{id}` | Prompt 详情（软删除过滤） |
| POST | `/admin/prompts` | 创建 Prompt |
| PUT | `/admin/prompts/{id}/activate` | 激活 Prompt（软删除过滤） |
| DELETE | `/admin/prompts/{id}` | 删除 Prompt（软删除, 200） |
| GET | `/admin/web-search/config` | Web Search 配置 |
| PUT | `/admin/web-search/config` | 更新 Web Search |
| GET | `/admin/web-search/stats` | Web Search 统计 (占位) |
| GET | `/admin/web-search/calls` | Web Search 明细 (占位) |
| POST | `/admin/web-search/test` | 测试搜索 |
| GET | `/admin/feedback` | 反馈列表 |
| GET | `/admin/feedback/stats` | 反馈统计 |
| GET | `/admin/audit` | 审计日志列表（中间件自动采集） |
| GET | `/admin/risk-keywords` | 关键字列表 |
| POST | `/admin/risk-keywords` | 创建关键字 |
| PUT | `/admin/risk-keywords/{id}` | 编辑关键字 |
| DELETE | `/admin/risk-keywords/{id}` | 删除关键字（软删除, 200） |
| GET | `/admin/risk-alerts` | 告警列表 |
| PUT | `/admin/risk-alerts/{id}/review` | 标记告警已处理 |
| GET | `/admin/risk-alerts/stats` | 告警统计 |
| GET | `/admin/traces` | Trace 列表 |
| GET | `/admin/traces/{turn_id}` | Turn 详情（向后兼容新旧 turn_id 格式） |
| GET | `/admin/traces/stats` | Trace 统计 |
| GET | `/admin/config` | 系统配置 |
| PUT | `/admin/config` | 更新配置 |

---

> 有任何疑问或需要字段级别的详细说明，请参考对应源码文件 `app/api/routes/admin/<module>.py`。
