# 感冒退烧 OTC AI 导购系统 Checklist

> 每一项通过运行代码或观察行为来验证。

## 实现完整性

- [ ] C1: 所有 56 个文件已创建，无空占位文件（验证：`find app -name "*.py" | wc -l` ≥ 40）
- [ ] C2: `docker-compose up -d` 启动 PostgreSQL 和 Milvus（验证：`docker ps` 显示两个容器 healthy）
- [ ] C3: 种子数据导入成功（验证：`python data/seed.py` 无报错，DB 有药品数据，Milvus 有向量）
- [ ] C4: 规则引擎 7 条规则全部注册（验证：单元测试覆盖每条规则的触发/不触发/边界）
- [ ] C5: LLM 客户端 5 个方法均可调用（验证：pytest 中 mock 调用无异常）
- [ ] C6: 8 个 Graph 节点全部实现（验证：`build_graph()` 不抛异常，mermaid 图显示完整拓扑）

## Spec 功能验收

对应 spec.md F1-F7：

- [ ] C7: **F1 匿名会话** — `POST /api/v1/sessions` 返回 session_id，30 分钟过期自动关闭（验证：创建会话→GET 返回 active→等待/模拟过期→GET 返回 expired）
- [ ] C8: **F2 意图识别** — 「我头疼流鼻涕」路由到 consult（验证：单元测试覆盖 6 种意图各至少 1 个 case）
- [ ] C9: **F2 话题跳转** — 先在 consult 中，用户说「布洛芬有用吗」→ dispatcher 路由到 explain，previous_phase 记录为 consulting（验证：集成测试模拟 3 轮对话，检查 state 路由正确）
- [ ] C10: **F2 放弃就医** — 用户说「算了去医院」→ route="end"，输出就医引导（验证：集成测试）
- [ ] C11: **F3 动态追问** — ReAct Agent 追问至少覆盖 4 个不同维度后才判定 info sufficient（验证：集成测试 + LangSmith Trace）
- [ ] C12: **F3 不充分不推荐** — 症状只说「不舒服」→ Consult 不输出 done（验证：单元测试 mock LLM 返回 next_action="ask"）
- [ ] C13: **F4 R1 高热阻断** — 体温 ≥ 39°C + 持续 ≥ 3 天 → BLOCK，输出就医引导（验证：规则引擎单元测试，参数化覆盖临界值）
- [ ] C14: **F4 R3 孕妇阻断** — 孕妇 + 体温 ≥ 38.5°C → BLOCK（验证：规则引擎单元测试）
- [ ] C15: **F4 R6 过敏排除** — 用户对布洛芬过敏 + 候选列表含布洛芬 → FILTER 排除（验证：规则引擎单元测试）
- [ ] C16: **F4 R7 儿童禁阿司匹林** — 年龄 < 12 → 排除含阿司匹林药物（验证：规则引擎单元测试）
- [ ] C17: **F5 对症推荐** — 完整症状（头痛发热 2 天、成人、无特殊）→ 输出 1-3 个推荐药 + 每个有推荐理由（验证：集成测试）
- [ ] C18: **F5 免责声明** — 每次药品推荐回复包含免责声明文本（验证：检查 Recommend 节点的 response 文本）
- [ ] C19: **F6 药品解释** — 查询「布洛芬副作用」→ RAG 检索 + 结构化输出含：药品名称、适应症、用法用量、不良反应、禁忌、注意事项（验证：RAG 集成测试）
- [ ] C20: **F7 库存查询** — 推荐后查库存，输出有货/缺货 + 价格 + 货架位置（验证：集成测试，检查 response 包含价格和位置字段）

## 非功能验收

- [ ] C21: **N1 响应延迟** — 单次 API 调用（不含 LLM 推理）< 200ms；安全规则检查 < 100ms（验证：单元测试中打点计时）
- [ ] C22: **N2 安全日志** — 每次 SafetyCheck 判定写入 safety_logs 表（验证：集成测试后查 DB 有对应记录）
- [ ] C23: **N3 规则插件化** — 新增一条规则只需新增一个文件 + 在 `__init__.py` 注册一行（验证：手动新增一条 mock 规则，不修改 engine.py，规则生效）
- [ ] C24: **N4 LangSmith Trace** — 一次完整对话的 Graph 节点流转在 LangSmith 可见（验证：运行对话→打开 LangSmith Dashboard 查看 Trace）
- [ ] C25: **N5 Health Check** — `GET /health` 返回 PostgreSQL、Milvus、LLM 三者的连接状态（验证：`curl /health` 返回 JSON 各字段非 error）

## 编译与测试

- [ ] C26: 项目目录下 `python -c "from app.main import app"` 无 import 错误
- [ ] C27: `pytest tests/ -v` 全部通过（≥ 40 条测试）
- [ ] C28: 无 import 了但未使用的依赖（验证：`pip check` 无冲突）

## 端到端场景

### E2E-1: 标准问诊推荐闭环

```
1. POST /api/v1/sessions → 拿到 session_id
2. POST /api/v1/chat/{session_id} {"message": "我头疼，有点发烧"}
   → SSE 流式返回追问（如"体温多少度？持续多久了？"）
3. POST /api/v1/chat/{session_id} {"message": "38度，两天了"}
   → SSE 流式返回追问（如"有没有药物过敏？"）
4. POST /api/v1/chat/{session_id} {"message": "没有过敏"}
   → SSE 流式返回追问（如"有没有其他不舒服？"）
5. POST /api/v1/chat/{session_id} {"message": "没有"}
   → SSE 流式返回追问结束
   → node: safety_check → verdict: PASS
   → node: recommend → 1-3 个推荐药品 + 理由 + 免责声明
   → node: inventory → 每药库存/价格/位置
   → event: done
```

- [ ] E2E-1: 完整走通上述流程，最后一条 SSE 为 `event: done`（验证：手动 curl 或集成测试脚本）

### E2E-2: 安全阻断场景

```
1. 创建会话
2. 用户消息 1: "我发烧39.5度"
   → 追问体温持续时间
3. 用户消息 2: "烧了四天了"
   → Dispatcher route=consult
   → Consult 收集完整 slots(体温39.5, 持续4天)
   → SafetyCheck → R1 触发 → BLOCK
   → event: safety → verdict=BLOCK, reason=持续高热
   → 输出就医引导（非药品推荐）
   → event: done
```

- [ ] E2E-2: 安全阻断场景走通，输出就医引导而非药品推荐

### E2E-3: 话题跳转 + 回归

```
1. 创建会话
2. 用户: "我咳嗽流鼻涕" → Consult 追问
3. 用户: "布洛芬有什么副作用？" → 跳转到 Explain → RAG 输出布洛芬副作用
4. 用户: "没有" → Dispatcher 根据 previous_phase 回归 Consult，继续追问
5. 后续正常流转至推荐
```

- [ ] E2E-3: 话题跳转后自动回归原流程，对话不丢失上下文
