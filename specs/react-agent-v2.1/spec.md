# ReactAgent v2.1 Spec

## 背景

ReactAgent v2.0 已实现基础的工具驱动药品问答能力（5 个工具：search_drug、get_drug_detail、search_manual、get_recommendation、get_user_profile）。在真实对话测试中暴露出以下问题：

1. **消息顺序错乱**：`react_node` 中 `normalize_messages(messages)` 后取到的最后一条用户消息不是当前轮次的
2. **状态不一致**：workflow 已完成推荐并返回了 `recommendations`，但 `consult_next_action` 仍为 "ask"、`response` 为空，导致 react 节点的 `workflow_context` 判错
3. **空结果后编造**：工具（尤其是 search_manual）返回空结果时，AI 仍然凭训练数据编造药品信息，并用"基于国家药监局公开信息"等措辞使其显得权威——这是最危险的行为
4. **缺少兜底数据源**：本地数据库和向量库都没有数据时，AI 只能说"未找到"，无法给用户任何有价值的信息

## 目标

- **鲁棒性**：修复消息顺序和状态一致性问题，消除因状态错误导致的回复质量下降
- **安全性**：彻底杜绝 AI 在工具无结果时编造药品信息的行为（代码级 + prompt 级双重防御）
- **完整性**：增加联网搜索作为第三级数据源（DB → Milvus → Web），本地数据不足时自动兜底
- **可追溯**：联网数据必须明确标注来源，与本地数据分块展示

## 功能需求

### F1: 消息顺序修复
react_node 在取最后一条用户消息时，`normalize_messages` 后消息顺序应与 `state.messages` 保持一致。取到的 query 必须对应当前轮次的用户输入，不能取到历史消息。

### F2: 状态一致性修复
workflow 完成后的状态应自洽。如果 `recommendations` 有数据且当前轮次不是 consult 追问，`workflow_context.workflow_action` 应正确判定为 "done" 而非 "ask"。

### F3: 空结果反编造（双重防御）
- **Prompt 层**：明确空结果 ≠ 错误，空结果 ≠ "可以凭记忆补充"，空结果时只能如实告知并建议查说明书/问药师
- **代码层**：工具返回空列表 `[]` 时，在 tool result 中注入明确的上下文标记（如 `{"empty": true, "message": "本地知识库未找到相关信息"}`），让 LLM 明确知道「没有数据」
- **兜底行为**：所有本地工具都返回空结果时，如果联网搜索可用则自动触发；如果不可用，给出降级回复模板

### F4: 联网搜索工具
- 新增 `search_web` 工具，基于现有 WebSearch 能力（Bing）
- 行为约束：**仅在本地工具（search_manual、get_drug_detail）返回空结果或不充分时**才调用
- 搜索 query 由 LLM 根据用户问题 + 已尝试的本地查询自动构造
- 返回结果包含：标题、摘要、来源 URL

### F5: 来源标注与分块展示
- 回复分为两个区域：
  - **"📋 本地知识库"**：来自 DB/Milvus 的信息
  - **"🌐 网络补充"**：来自 WebSearch 的信息（附带来源链接）
- 仅本地数据时不需要标注区域标题
- 网络数据必须标注：「以下信息来自互联网搜索，仅供参考，请以药品说明书或医生/药师意见为准」

### F6: 工具扩展架构
- 新增工具通过 `BaseTool` 子类实现，在 builder.py 的工具列表加一行即可
- 联网搜索工具的注册方式与现有 5 个工具一致

## 非功能需求

### N1: 延迟控制
- 联网搜索是同步阻塞的 HTTP 调用，可能导致 react 循环超时
- 单次联网搜索超时上限 10 秒
- 整个 react 循环（含联网搜索）不超过 max_iterations=5 的限制

### N2: 可观测性
- 每次联网搜索调用记录在 AgentStep 中
- 搜索结果是否被使用记录在 node_events 中

### N3: 向后兼容
- 现有 188 个测试保持通过
- 现有 API 接口不变
- state 结构不变

## 不做的事

- 不修复 Milvus 查不到数据的问题（用户已说明后续单独处理）
- 不新增 KG 查询工具或其他 skills（后续单独规划）
- 不改变 graph 编排结构
- 不改变 dispatcher/consult/safety/recommend/inventory 节点

## 验收标准

- AC1: react_node 取到的 query 始终对应当前轮次用户输入，不受历史消息顺序影响
- AC2: workflow done + 有 recommendations 时，workflow_context.workflow_action = "done"
- AC3: 所有本地工具返回空结果时，AI 回复不得包含任何药品的具体功效、副作用、禁忌、用法用量等信息
- AC4: 联网搜索触发后，回复中明确区分本地数据和网络数据，网络数据附带来源 URL
- AC5: 新增测试覆盖空结果行为、联网搜索触发条件、来源标注格式
- AC6: 现有 188 个测试全部通过
