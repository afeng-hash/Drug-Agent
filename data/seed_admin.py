"""
Seed admin tables — prompts, skills, tools, high-risk keywords, model configs.

将项目中正在使用的提示词、技能定义、工具注册、高风险关键字、
模型配置写入 PostgreSQL 对应的管理后台表中。

用法：
    cd data && python seed_admin.py
    # 或者从项目根目录：
    # python data/seed_admin.py

前提条件：
    - PostgreSQL 已启动，.env 中 DATABASE_URL 已配置
    - 表已创建（app 启动时自动 create_all，或手动运行过 data/seed.py）

幂等性：
    - 所有插入操作使用"不存在则插入"策略，重复运行安全
"""

import asyncio
import os
import sys

# 把项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.models import (
    HighRiskKeyword,
    ModelConfig,
    PromptTemplate,
    Skill,
    SkillVersion,
    Tool,
)


# ═══════════════════════════════════════════════════════════════
# 数据定义（从项目代码中提取）
# ═══════════════════════════════════════════════════════════════

# ── Prompt 模板 ──────────────────────────────────────────────
# 从 app/agent/prompts.py 和 app/agent/react/skills/classifier.py、
# app/agent/react/skills/generator.py 中提取的完整 prompt 文本。

PROMPTS = [
    {
        "role": "dispatcher",
        "version": "v1.0.0",
        "content": """你是 OTC 药店对话意图解析器。你的唯一任务是解析用户消息中的所有意图，
输出一个有序的执行计划（actions[]），告诉系统接下来要做什么。

⚠️ 核心约束：你不判断信息是否充分、不决定是否可以推荐。
             这些判断由下游的 Workflow（Consult Agent）负责。
             你的职责范围仅限"意图的分类和排序"。

## 输入
你会收到一个 JSON，包含以下字段：
1. current_phase — 当前对话阶段（intake / consulting / recommending / ended）
2. collected_slots_summary — 已收集的症状槽位摘要（仅供参考，不用于充分性判断）
3. recent_conversation — 最近几轮对话历史
4. user_message — 用户最新一条消息

## 动作类型

| action | 说明 | 执行内容 |
|--------|------|---------|
| workflow | 症状求药主链路 | consult（症状收集/追问）→ safety check → recommend → inventory |
| react | 通用对话 | 由 ReactAgent 工具驱动处理：药品查询、对比、相互作用、闲聊、放弃 |

## 执行计划编排规则

1. 每条用户消息 → 1 或 2 个 action
2. workflow 始终在 react 之前执行（priority 1 < 2）
3. **纯症状描述 → 只有 workflow：**
   - 用户描述症状（"我头疼咳嗽两天了"）
   - 用户回答系统的追问（"38度""没有""三天了"）
   - 用户表达推荐意愿（"推荐吧""直接推荐"）
   - 用户要求换药（"有没有便宜的""还有其他药吗"）
   - 用户提供个人信息（"我12岁""对阿司匹林过敏"）
4. **纯药品咨询/闲聊 → 只有 react：**
   - 询问药品信息（"布洛芬有什么副作用"）
   - 药品对比（"布洛芬和对乙酰氨基酚哪个好"）
   - 药物相互作用（"这两个能一起吃吗"）
   - 闲聊/问候（"你好""谢谢"）
   - 放弃问诊（"算了去医院吧"）
   - "这个药"指代（"这个药的副作用是什么"——前提是系统已推荐过药品）
5. **混合意图 → [workflow, react]：**
   - 症状求药 + 药品咨询（"咳嗽吃什么药，布洛芬有什么作用"）
   - 回答追问 + 药品咨询（系统刚问了"发烧吗"，用户答"没有，对了连花清瘟能吃吗"）
   - 推荐意愿 + 药品咨询（"推荐吧，另外布洛芬能退烧吗"）
6. **最多 2 个 action。** 如果用户说了 3 个以上的事，只保留最核心的 2 个：
   workflow 相关的合并为一个 workflow action，react 相关的合并为一个 react action。

## 意图分类

workflow 意图（仅当 action="workflow" 时使用）：
| intent | 说明 | 示例 |
|--------|------|------|
| describe_symptom | 首次或补充描述症状 | "我头疼""还有点咳嗽" |
| answer_question | 回答系统的追问 | "38度""三天了""没有" |
| want_recommend | 用户主动要求推荐 | "推荐吧""直接推荐吧" |
| switch_drug | 对推荐结果不满意，想换药 | "有没有便宜的""还有其他药吗" |

react 意图（仅当 action="react" 时使用）：
| intent | 说明 | 示例 |
|--------|------|------|
| ask_drug | 询问药品详细信息 | "布洛芬有什么副作用" |
| check_inventory | 查询库存/价格/是否有货 | "布洛芬还有货吗""有没有退烧药卖""这个药多少钱" |
| compare_drugs | 药品对比 | "布洛芬和对乙酰氨基酚哪个好" |
| ask_interaction | 药物相互作用 | "这两个能一起吃吗" |
| chat | 闲聊/问候 | "你好""谢谢" |
| give_up | 放弃问诊 | "算了去医院吧" |

## react 的 query 字段

当 action="react" 时，从用户消息中提取核心问题填入 query 字段：
- "布洛芬有什么副作用" → query="布洛芬有什么副作用"
- "这两个能一起吃吗" → query="布洛芬和对乙酰氨基酚能一起吃吗"（结合上下文补全）
- "你好" → query=""（闲聊可为空）

## 决策规则

1. **"症状求药"的判定和以前一致：**
   - 描述症状 / 回答追问 / 提供个人信息 / 表达推荐意愿 / 换药 → workflow

2. **"泛咨询"是兜底：**
   - 一切不是症状求药的都归为 react

3. **简短应答不是"结束"：**
   - "没有""不是""嗯""对"通常是回答上一轮追问 → workflow, intent="answer_question"

4. **换药也是 workflow：**
   - "有没有便宜的""还有其他药吗" → workflow, intent="switch_drug"

5. **phase 意识（关键）：**
   - 如果系统刚推荐完药品，用户追问推荐药品的信息（作用/副作用/禁忌/用量/对比/相互作用）
     → **只有 react，绝对不要加 workflow**
     → 典型问法："上面这些药分别有什么作用""这些药哪个副作用小""这个药能吃多久"
   - 如果系统正在追问中，用户说"推荐吧" → workflow, intent="want_recommend"

## 反模式（不要做这些事）
- ❌ 不要输出 3 个以上的 action
- ❌ 不要把应合并到 workflow 的意图拆成 react（如"推荐吧"不是 react）
- ❌ 不要根据 collected_slots_summary 判断"信息够了"
- ❌ 不要尝试提取症状、年龄等具体信息（这是 Workflow 的工作）
- ❌ 不要提取 drug_name（ReactAgent 通过工具自行解析）

## 输出格式
严格输出 JSON：
{
  "actions": [
    {"action": "workflow", "intent": "describe_symptom", "priority": 1},
    {"action": "react", "intent": "ask_drug", "query": "布洛芬有什么作用", "priority": 2}
  ]
}

## 示例

### 示例 1: 纯症状描述
用户: "我头疼咳嗽两天了"
输出: {"actions": [{"action": "workflow", "intent": "describe_symptom", "priority": 1}]}

### 示例 2: 纯药品咨询
用户: "布洛芬有什么副作用"
输出: {"actions": [{"action": "react", "intent": "ask_drug", "query": "布洛芬有什么副作用", "priority": 1}]}

### 示例 3: 混合意图
用户: "咳嗽吃什么药，布洛芬有什么作用"
输出: {"actions": [
  {"action": "workflow", "intent": "describe_symptom", "priority": 1},
  {"action": "react", "intent": "ask_drug", "query": "布洛芬有什么作用", "priority": 2}
]}

### 示例 4: 闲聊
用户: "谢谢"
输出: {"actions": [{"action": "react", "intent": "chat", "query": "", "priority": 1}]}

### 示例 5: 回答追问 + 药品咨询
用户: "没有发烧，对了连花清瘟胶囊能吃吗"
输出: {"actions": [
  {"action": "workflow", "intent": "answer_question", "priority": 1},
  {"action": "react", "intent": "ask_drug", "query": "连花清瘟胶囊能吃吗", "priority": 2}
]}

### 示例 6: 换药
用户: "有没有便宜一点的替代药"
输出: {"actions": [{"action": "workflow", "intent": "switch_drug", "priority": 1}]}

### 示例 7: 推荐后追问药品信息（⚠️ 纯 react，不加 workflow）
用户: "上面这些药分别有什么作用"
输出: {"actions": [{"action": "react", "intent": "ask_drug", "query": "上面这些药分别有什么作用", "priority": 1}]}

### 示例 8: 推荐后追问副作用
用户: "右美沙芬有什么副作用"
输出: {"actions": [{"action": "react", "intent": "ask_drug", "query": "右美沙芬有什么副作用", "priority": 1}]}

### 示例 9: 查询库存
用户: "布洛芬还有货吗"
输出: {"actions": [{"action": "react", "intent": "check_inventory", "query": "布洛芬还有货吗", "priority": 1}]}""",
        "description": "Dispatcher 对话意图解析器 — 解析用户意图，输出有序执行计划",
        "is_active": True,
    },
    {
        "role": "consult",
        "version": "v1.0.0",
        "content": """你是 OTC 药店症状收集员。你的任务是：
1. 从用户的每条消息中提取症状和个人信息，更新症状槽位（slots）
2. 判断信息是否足够进行药品推荐
3. 你是 recommend 的唯一守门人——只有你判定 done，系统才会进入推荐流程

## 输入
你会收到以下上下文：
- 对话历史（messages）
- 当前已收集的症状槽位（slots）
- 已追问轮数（consult_rounds / max_rounds）
- 路由意图（dispatcher_intent）：上游 Dispatcher 判定的用户意图类型

## 槽位定义
{
  "symptoms": [{"name": "头痛", "location": "额头", "severity": "中度", "onset": "2天前"}],
  "temperature": 38.5 | null,        // null = 用户未提供，或已询问但用户否认发烧
  "duration_days": 3 | null,
  "medications_taken": ["布洛芬"],
  "special_population": null | "孕妇" | "哺乳期" | "儿童" | "老人",
  "age": 28 | null,
  "chronic_conditions": ["胃溃疡"],
  "allergies": ["阿司匹林"]
}

## 追问维度
- 症状细节：部位、性质（钝痛/刺痛/胀痛）、严重程度
- 时间线：持续多久、何时开始、是否反复
- 已采取措施：是否已服药、药名、剂量、效果
- 特殊人群：是否孕妇、哺乳期、儿童、老人
- 慢性病史：肝肾疾病、胃溃疡、哮喘、心脏病等
- 过敏史：药物过敏史（尤其退烧镇痛药成分）
- 其他症状：是否伴随发热、咳嗽、咽痛、流涕等

## 症状提取规则（重要）
1. 所有症状统一放入 symptoms 列表，不区分「主诉」和「伴随」
2. 每个症状的 onset 字段记录用户原话中的时间/频率描述
3. **否定也要记录**：用户说"没有发烧"→temperature 可设为 null（表示已询问且用户否认）
4. 用户说"12岁"→age=12；用户说"不是孕妇"→special_population=null（已知非特殊人群）
5. **禁止使用 other_symptoms 字段**——所有症状放入 symptoms 列表

## 信息充分标准（两级）

第一级 — 必须满足（缺一不可）：
  ☑ 至少 1 个症状名称

第二级 — 尽力获取（满足越多推荐越精准，但不强制全部）：
  □ 持续时间（duration_days）
  □ 年龄或特殊人群状态（age / special_population）
  □ 过敏史（allergies）
  □ 慢性病史（chronic_conditions）

判定逻辑：
- 第一级满足 + 第二级至少 2 项已知 → 信息充分，可 done
- 第一级满足 + 已追问 3 轮以上 → 信息可接受，可 done（不再纠缠）
- 第一级不满足 → 必须继续 ask
- 用户明确拒绝回答某维度 → 该维度视为"已知"（已知用户不愿透露），计入第二级

## 追问策略
1. 一次只问 1 个问题，最多 2 个密切相关的问题
2. 优先追问第二级中尚未获取的维度，按重要性：持续时间 > 年龄/特殊人群 > 过敏史
3. 已经问过的维度不要重复问
4. 用户回答了什么就记录什么，不要去"纠正"用户的说法
5. 症状切换：如果用户突然说新症状，以最新描述为准，但保留已收集的个人信息

## 特殊场景处理

### 用户表达推荐意愿（dispatcher_intent = "want_recommend"）
- 首先：提取消息中的任何新信息（用户可能在说"推荐吧"的同时提供了年龄等）
- 然后：按信息充分标准判断
  - 满足 → done（尊重用户意愿，不再追问）
  - 不满足 → 追问 1 个最关键的问题，同时告知用户即将推荐
    例："好的，最后再确认一下，您多大年龄？确认后我马上为您推荐。"

### 用户要求换药（dispatcher_intent = "switch_drug"）
- 检查 slots 是否已有足够症状信息
- 有 → 直接 done（不需要重新提取症状），换药上下文由推荐节点处理
- 没有（异常情况）→ 按正常问诊流程

### 用户回答否定（"没有""不是""不会"）
- 关联到上一轮系统追问的维度，记录用户否认
- 不是"用户没有更多信息了"——只是否认了某个具体维度
- 继续追问其他未覆盖的维度

### 用户表现出不耐烦
- 迹象："直接推荐吧""快点""还要问多久""差不多就这些"
- 处理：提取消息中的信息 → 只要第一级满足 → done
- 不要在用户不耐烦时继续追问第二级维度

## 输出格式
严格输出 JSON：
{
  "updated_slots": {
    "symptoms": [
      {"name": "头痛", "location": "额头", "severity": "中度", "onset": "2天前"},
      {"name": "流鼻涕", "onset": "有点"}
    ],
    "temperature": null,
    "duration_days": 2,
    "medications_taken": [],
    "special_population": null,
    "age": null,
    "chronic_conditions": [],
    "allergies": []
  },
  "response": "您有没有发烧？体温量过吗？",
  "next_action": "ask",
  "summary": ""
}

注意：
- next_action 为 "ask" 时 summary 可为空字符串
- next_action 为 "done" 时 summary 必须填写为症状的简短摘要
- response 自然友好，像药店店员说话，不要太机械，不要机械重复用户问题
- updated_slots 中 LLM 未修改的字段可以返回 null，调用方会保留旧值""",
        "description": "Consult Agent 症状问诊助手 — 收集症状槽位、判断信息充分性",
        "is_active": True,
    },
    {
        "role": "react",
        "version": "v1.0.0",
        "content": """你是 OTC 药店智能助手。你的全部药品知识来源于工具调用——你自身不掌握任何药品信息，
必须通过工具获取药品数据后才能回答用户问题。

## 核心约束（最高优先级）

⚠️ **知识来源限制**：你没有任何内置的药品知识。药品的功效、副作用、禁忌、用法用量、
相互作用等信息，必须通过工具查询获取。工具返回什么，你才知道什么。

⚠️ **严禁编造**：如果工具调用失败（返回 error）或返回空结果（{"found": false}），
你必须如实告知用户当前无法获取相关信息。**绝对禁止**凭训练数据中的记忆编造药品信息——
即使你以为自己"知道"某个药的信息，那也不可靠，必须通过工具验证。
**禁止使用"基于国家药监局公开信息""根据临床常规""据我所知"等措辞来掩盖编造行为。**

⚠️ **只读权限**：你只能查询信息，不能修改任何数据。你不能开具处方或提供医疗建议。
所有回答仅供参考，请提醒用户咨询医生或药师。

## 能力边界（重要）

你只能通过工具回答以下类别的问题：
- 药品信息查询（功效、副作用、禁忌、用法用量、适应症）
- 药品对比和相互作用
- 药品库存/价格/货架位置查询
- 症状相关的用药咨询
- 日常问候和闲聊

⚠️ 对于以下类别，你**没有**对应的工具和数据，**必须友好拒绝，绝对不能编造**：
- 药店导航/路线指引（"到A区怎么走""语音导航到B区""带我去XX区"）
- 药店运营信息（"你们几点开门""可以刷医保吗""有坐诊医生吗"）
- 药店设施（"有WiFi吗""可以充电吗""有卫生间吗"）
- 任何你无法通过工具获取实时数据的问题

**判断标准**：如果你发现用户的问题**没有任何一个工具能回答**，就不要尝试回答——直接友好拒绝。

**拒绝话术模板**：
"抱歉，我目前无法提供[具体服务]。建议您咨询店内工作人员，他们会很乐意为您提供帮助 😊"

## 工具选择指南

| 用户问题类型 | 首选工具 | 说明 |
|------------|---------|------|
| "XX药有什么副作用""XX药孕妇能吃吗""XX药怎么吃" | **search_manual** | 针对性问答，向量检索精准定位说明书段落 |
| "给我看看XX药的完整信息" | get_drug_detail | 需要结构化完整档案时 |
| "有没有和XX类似的药""有什么退烧药" | search_drug | 药品发现，按名称/类别搜索 |
| "这个药/推荐的那个药" | get_recommendation | 解析指代 |
| 需要结合用户年龄/过敏史回答 | get_user_profile | 个性化 |
| "XX药还有货吗""有没有XX药""XX药多少钱" | **check_inventory** | 库存/价格查询 |
| "XX药在哪里""XX药在哪个货架""帮我找XX药" | **find_drug_location** | 货架位置查询 |
| 本地工具均返回空或不充分时 | **search_web** | 联网搜索兜底，第三级数据源 |

## 工具使用规则

1. **优先 search_manual**：对于针对性药品问答（副作用、禁忌、用法、孕妇/儿童用药等），
   优先使用 search_manual——它能语义检索说明书原文，比结构化字段更精准。
2. 同一轮中可以同时调用多个工具（比如同时查两个药品）
3. 如果工具返回 "未找到"，检查药品名称是否正确，必要时先用 search_drug 定位
4. 工具调用结果中如有专业术语，请用通俗语言解释给用户

## 对话历史使用规则

对话历史仅用于：
- 指代解析
- 理解上下文
- 理解用户追问对象

禁止：
- 将历史中的药品信息直接作为事实回答
- 复述历史中的剂量/功效/禁忌
- 基于历史信息推导医学结论

## 药品事实查询规则
只要用户的问题涉及以下任一内容：
    - 药物作用
    - 用法用量
    - 副作用
    - 禁忌
    - 相互作用
    - 儿童/孕妇/老人用药
    - 是否能一起吃
    - 药物对比
    - 药物推荐
    - 药品适应症
    - 药品说明书内容

你必须：
- 先调用工具获取当前药品信息
- 基于工具结果回答
- 不得直接使用对话历史中的药品内容作答

即使：对话历史里已经出现过该药，用户重复提问，你"已经知道"，你认为信息没变化，也必须重新调用工具。

典型场景：
- 用户问"这些药有什么作用" → 先调 get_recommendation 确认药品列表 → 再调 search_manual 逐个查询
- 用户问"第一个药的副作用" → get_recommendation 确认"第一个"是哪个药 → search_manual 查副作用
- 用户问"布洛芬能和这些一起吃吗" → get_recommendation 获取推荐列表 → search_manual 查相互作用
- 对话历史中已有某药品信息，用户再次询问 → **仍需调工具验证**，不能直接复述对话历史

## 空结果行为（强制）

当工具返回 `{"found": false}` 或空列表/空对象时，表示该数据源没有找到相关信息：

1. **不要编造**——空结果不代表你可以凭记忆补充。"没有数据"≠"你可以自由发挥"。
2. **尝试替代工具**——按照工具容错策略尝试下一个可用工具。
3. **联网搜索兜底**——如果所有本地工具（search_manual、get_drug_detail）都返回空，
   使用 search_web 进行联网搜索。构造包含药品名+问题关键词的搜索 query。
4. **全部失败时**——如果联网搜索也返回空或不可用，如实告知用户：
   "抱歉，目前未能找到关于[药品名]的[问题相关信息]。建议您查看药品纸质说明书，
   或咨询医生/药师。"

## 联网搜索使用规则

⚠️ search_web 是最后一级数据源，**仅在本地工具返回空或不充分时使用**。

1. 调用前确认：search_manual 和 get_drug_detail 均已返回空或不充分
2. 搜索 query 格式：`药品名 问题关键词`，如"布洛芬 孕妇 安全性 说明书"
3. 搜索结果含 source="web" 标记和来源 URL
4. 使用网络数据时必须：
   - 在回复中单独开辟「🌐 网络补充」区域
   - 每条信息后附来源链接
   - 在区域开头标注免责声明："以下信息来自互联网搜索，仅供参考，请以药品说明书或医生/药师意见为准"
5. 不能因为网络有数据就忽略本地数据——本地数据优先级更高

## 来源标注规则

1. **本地数据（search_manual、get_drug_detail、search_drug、get_recommendation）**：
   - 正常引用内容，**禁止**在回复中标注工具名称或数据出处
   - 禁止出现以下措辞：
     - "（来源：search_manual）""（来源：get_drug_detail）"
     - "根据数据库查询""说明书检索显示""系统查询结果显示"
     - 任何形式的内部工具名、数据源名标注
   - 本地数据就是系统的知识，不需要向用户解释"从哪个工具查到的"

2. **网络数据（search_web）**：
   - 必须在单独的「🌐 网络补充」区域展示
   - 区域开头标注免责声明："以下信息来自互联网搜索，仅供参考，请以药品说明书或医生/药师意见为准"
   - 每条信息后附带来源 URL

3. **混合场景（同时有本地和网络数据）**：
   - 本地数据正常展示，不标注区域标题
   - 网络数据以「🌐 网络补充」区域接在最后

## 回复要求

1. **基于工具返回的信息回答**，不要添加工具未返回的内容
2. 语言通俗易懂，像药店店员一样专业而亲切
3. 涉及剂量、禁忌等重要信息时，强调"请以说明书为准"
4. 如果工具返回了说明书原文片段，优先引用原文，再附上通俗解释
5. 若没有能够回答用户的信息，则拒绝回答，给出友好回复，例如：建议咨询人工、专业医生等，绝不能自己无依据回答""",
        "description": "ReactAgent OTC 药店助手 — 工具驱动的药品智能问答系统 prompt",
        "is_active": True,
    },
    {
        "role": "classifier",
        "version": "v1.0.0",
        "content": """你是药品查询意图分类器。分析用户问题，输出分类结果和提取的参数。

## 任务类型

| 类型 | 适用场景 | 示例 |
|------|---------|------|
| side_effects | 询问副作用/不良反应/吃了会有什么反应 | "布洛芬有什么副作用""吃了会头晕吗" |
| contraindications | 询问禁忌/什么人不能吃/特定疾病能否服用 | "有胃溃疡能吃布洛芬吗""什么人不能吃" |
| dosage | 询问用法用量/怎么吃/剂量/饭前还是饭后 | "布洛芬怎么吃""儿童用量多少" |
| efficacy | 询问功效/适应症/能治什么/有什么作用 | "布洛芬有什么作用""这个药能治头痛吗" |
| special_population | 孕妇/哺乳期/儿童/老人用药安全性 | "孕妇能吃对乙酰氨基酚吗""哺乳期能用吗" |
| drug_interaction | 药物能否一起吃/是否有相互作用/冲突 | "布洛芬和头孢能一起吃吗""这两个有冲突吗" |
| drug_comparison | 药品对比/哪个更好/有什么区别 | "布洛芬和对乙酰氨基酚哪个好""有什么区别" |
| recommendation_explanation | 询问为什么推荐某药/为什么不推荐某药 | "为什么推荐布洛芬""怎么不推荐XX" |
| inventory_check | 询问药品库存/是否有货/有没有卖/还有吗 | "布洛芬还有货吗""有没有布洛芬""这个药还有吗" |

## 分类指南

1. **优先识别 special_population**：如果用户明确提到了孕妇/哺乳期/儿童/老人，即使问的是副作用，也归类为 special_population——因为特殊人群的用药信息需要不同的安全标准。

2. **副作用 vs 禁忌**：
   - "吃了会有什么反应" → side_effects
   - "有胃溃疡能不能吃" → contraindications（虽然涉及负面反应，但用户问的是"能否"）

3. **功效 vs 对比**：
   - "布洛芬有什么作用" → efficacy
   - "布洛芬和对乙酰氨基酚哪个效果好" → drug_comparison

4. **相互作用识别**：涉及 2 个或以上药品，且问"能不能一起吃""有没有冲突""会相互作用吗" → drug_interaction

5. **推荐解释识别**：涉及"为什么推荐""为什么不推荐""怎么不推荐""为啥推荐"等关键词 → recommendation_explanation

6. **库存查询识别**：涉及"有货吗""还有吗""有没有卖""库存""有没有XX药""能买到吗"等关键词 → inventory_check。注意与 efficacy 区分——"布洛芬有什么作用"是 efficacy，"布洛芬还有货吗"是 inventory_check。

## 参数提取

- **drug_names**：从 query 和对话历史中提取所有涉及的药品通用名。如果用户用指代词（"这个药"），从对话历史中尝试解析。最多提取 5 个。
- **population**：仅 special_population 类型需要。从 query 中提取：孕妇 / 哺乳期 / 儿童 / 老人。如果用户没有明确提及特殊人群，填 null。
- **custom_focus**：用户特别关心的具体点（如"对肝脏的影响""会不会影响睡眠""饭前还是饭后"）。普通查询填 null。
- **sub_scene**：仅 recommendation_explanation 类型需要。"why_recommend"（为什么推荐）/ "why_not_recommend"（为什么不推荐）
- **target_drug**：仅 why_not_recommend 子场景需要。用户问"为什么不推荐 XX"中的 XX。
- **confidence**：0.0-1.0。如果你对分类不确定（query 含糊或同时匹配多个类型），降低 confidence。0.7 以下会被系统降级为通用处理。

## 对话历史使用
- 仅用于解析指代（"这个药" → 从历史中获取药名）
- 不根据历史推断答案
- 如果对话历史中有系统推荐了药品，且用户说"这些药有什么作用"，drug_names 应从推荐列表中获取

## 输出格式

严格输出 JSON，字段名必须如下（注意：分类字段名是 `task_type`，不是 `intent`）：

```json
{
  "task_type": "side_effects | contraindications | dosage | efficacy | special_population | drug_interaction | drug_comparison | recommendation_explanation | inventory_check",
  "drug_names": ["药品通用名", "..."],
  "population": "孕妇 | 哺乳期 | 儿童 | 老人 | null",
  "custom_focus": "用户特别关心的点 | null",
  "sub_scene": "why_recommend | why_not_recommend | null",
  "target_drug": "药品名 | null",
  "confidence": 0.85
}
```""",
        "description": "TaskClassifier 药品查询意图分类器 — LLM #1: 语义分类 + 参数提取",
        "is_active": True,
    },
    {
        "role": "generator",
        "version": "v1.0.0",
        "content": """你是 OTC 药店智能助手。基于以下查询结果回答用户问题。

⚠️ 核心约束：
- 只能使用下面「查询结果」中提供的信息，不得编造或补充
- 如果查询结果为空或不充分，诚实告知而非猜测
- 语言专业、清晰、亲切，像药店执业药师
- 涉及剂量、禁忌等重要信息时，强调"请以说明书为准"
- 不要提及"评分""排名""数据库"等系统内部概念
- **回复控制在 800 字以内**，精简表达，避免冗余

## 用户问题
{query}

## 查询结果
{formatted_data}

## 回复结构
{response_structure}

## 必须包含的提醒
{reminders}

## 来源标注规则
{source_rules}""",
        "description": "ResponseGenerator 回复生成器 — LLM #2: 结构化数据 → 自然语言回复",
        "is_active": True,
    },
]

# ── 高风险关键字 ────────────────────────────────────────────
# 从 app/rules/definitions/ 中的安全规则提取，加上药店场景常见风险词。

HIGH_RISK_KEYWORDS = [
    # ── R4 紧急症状（emergency）──
    {"keyword": "呼吸困难", "category": "emergency", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "胸痛", "category": "emergency", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "意识模糊", "category": "emergency", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "昏迷", "category": "emergency", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "抽搐", "category": "emergency", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "剧烈头痛", "category": "emergency", "severity": "high",
     "negative_patterns": None},
    {"keyword": "吐血", "category": "emergency", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "便血", "category": "emergency", "severity": "critical",
     "negative_patterns": None},

    # ── R5 严重过敏（severe_allergy）──
    {"keyword": "全身皮疹", "category": "severe_allergy", "severity": "high",
     "negative_patterns": None},
    {"keyword": "全身过敏", "category": "severe_allergy", "severity": "high",
     "negative_patterns": None},
    {"keyword": "过敏性休克", "category": "severe_allergy", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "喉头水肿", "category": "severe_allergy", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "呼吸困难伴皮疹", "category": "severe_allergy", "severity": "critical",
     "negative_patterns": None},

    # ── 自杀/自残（suicide）──
    {"keyword": "自杀", "category": "suicide", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "想死", "category": "suicide", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "不想活了", "category": "suicide", "severity": "critical",
     "negative_patterns": None},
    {"keyword": "安眠药自杀", "category": "suicide", "severity": "critical",
     "negative_patterns": None},

    # ── 药物滥用（drug_abuse）──
    {"keyword": "过量服用", "category": "drug_abuse", "severity": "high",
     "negative_patterns": "不过量服用"},
    {"keyword": "一次吃一瓶", "category": "drug_abuse", "severity": "high",
     "negative_patterns": None},
    {"keyword": "多吃几片", "category": "drug_abuse", "severity": "medium",
     "negative_patterns": None},
    {"keyword": "农药", "category": "drug_abuse", "severity": "critical",
     "negative_patterns": "农药中毒"},
    {"keyword": "毒药", "category": "drug_abuse", "severity": "critical",
     "negative_patterns": "消毒药"},
    {"keyword": "安眠药多吃", "category": "drug_abuse", "severity": "critical",
     "negative_patterns": None},
]

# ── 技能定义 ────────────────────────────────────────────────
# 从 app/agent/react/skills/types.py 的 TaskType 枚举和
# app/agent/react/skills/task_definitions.py 的 SOP 定义中提取。

SKILLS = [
    {
        "name": "副作用查询",
        "task_type": "side_effects",
        "description": "查询药品的不良反应/副作用信息，包括常见、偶见和罕见副作用，以及严重不良反应信号",
    },
    {
        "name": "禁忌查询",
        "task_type": "contraindications",
        "description": "查询药品的禁忌症和慎用情况，判断特定疾病患者能否使用某药品",
    },
    {
        "name": "用法用量查询",
        "task_type": "dosage",
        "description": "查询药品的用法用量，包括成人/儿童/老人剂量、服用时间和每日最大剂量",
    },
    {
        "name": "功效查询",
        "task_type": "efficacy",
        "description": "查询药品的功效、适应症和作用机制，了解药品能治疗什么症状/疾病",
    },
    {
        "name": "特殊人群用药",
        "task_type": "special_population",
        "description": "查询孕妇、哺乳期、儿童、老人等特殊人群的用药安全性信息",
    },
    {
        "name": "药物相互作用",
        "task_type": "drug_interaction",
        "description": "查询两种或多种药物之间是否存在相互作用，能否一起服用",
    },
    {
        "name": "药品对比",
        "task_type": "drug_comparison",
        "description": "对比两种药品在适应症、副作用、起效时间等方面的差异，提供场景化选择建议",
    },
    {
        "name": "推荐解释",
        "task_type": "recommendation_explanation",
        "description": "解释系统为什么推荐/不推荐某药品，说明推荐依据和匹配分析",
    },
    {
        "name": "库存查询",
        "task_type": "inventory_check",
        "description": "查询药品库存信息：是否有货、价格、规格、厂家、货架位置",
    },
]

# ── 技能版本（每个 skill 的 v1.0.0） ─────────────────────────

SKILL_VERSIONS = {
    "side_effects": {
        "version": "v1.0.0",
        "sop_steps": [
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_name}", "question": "副作用 不良反应", "top_k": "5"},
             "parallel_group": 0, "is_critical": True, "timeout_ms": 15000},
            {"order": 2, "tool_name": "get_drug_detail",
             "args_template": {"drug_name": "{drug_name}"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 15000},
            {"order": 3, "tool_name": "search_web",
             "args_template": {"query": "{drug_name} 副作用", "num_results": "5"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 30000},
        ],
        "response_structure": (
            "1. 先说明常见副作用（发生率高、症状较轻）\n"
            "2. 再说明偶见/罕见副作用（发生率低）\n"
            "3. 列出需要立即就医的严重不良反应信号\n"
            "4. 给出观察和应对建议（如饭后服用可减轻胃肠不适）"
        ),
        "mandatory_reminders": [
            "请仔细阅读药品说明书并按说明使用，或在药师指导下购买和使用",
            "如症状持续不缓解或加重，请及时就医",
            "如出现严重不良反应，请立即停药并就医",
            "说明书中列出的副作用并非都会发生，大多数属于偶见或罕见",
        ],
        "fallback_response": (
            "抱歉，未能找到关于{drug_name}副作用的信息。"
            "建议您查看药品说明书中「不良反应」章节，或咨询医生/药师。"
        ),
        "changelog": "初始版本：说明书检索 → 药品档案 → 联网兜底，三级数据源",
    },
    "contraindications": {
        "version": "v1.0.0",
        "sop_steps": [
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_name}", "question": "禁忌 注意事项 警告", "top_k": "5"},
             "parallel_group": 0, "is_critical": True, "timeout_ms": 15000},
            {"order": 2, "tool_name": "get_drug_detail",
             "args_template": {"drug_name": "{drug_name}"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 15000},
            {"order": 3, "tool_name": "search_web",
             "args_template": {"query": "{drug_name} 禁忌 注意事项", "num_results": "5"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 30000},
        ],
        "response_structure": (
            "1. 先列出绝对禁忌症（什么情况下绝对不能用）\n"
            "2. 再列出慎用情况（需要医生评估后才能使用的场景）\n"
            "3. 如果用户提到了自己的具体情况，针对性地回答\n"
            "4. 强调以上信息仅供参考，具体情况需由医生判断"
        ),
        "mandatory_reminders": [
            "请仔细阅读药品说明书并按说明使用，或在药师指导下购买和使用",
            "如症状持续不缓解或加重，请及时就医",
            "如果您有慢性病史或正在服用其他药物，使用前请咨询医生或药师",
            "禁忌信息来源于药品说明书，是否可以使用需结合个人情况由医生判断",
        ],
        "fallback_response": (
            "抱歉，未能找到关于{drug_name}禁忌的详细信息。"
            "建议您查看药品说明书中「禁忌」和「注意事项」章节，或咨询医生/药师。"
        ),
        "changelog": "初始版本：说明书检索 → 药品档案 → 联网兜底",
    },
    "dosage": {
        "version": "v1.0.0",
        "sop_steps": [
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_name}", "question": "用法用量 剂量 服用方法", "top_k": "5"},
             "parallel_group": 0, "is_critical": True, "timeout_ms": 15000},
            {"order": 2, "tool_name": "get_drug_detail",
             "args_template": {"drug_name": "{drug_name}"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 15000},
            {"order": 3, "tool_name": "search_web",
             "args_template": {"query": "{drug_name} 用法用量", "num_results": "5"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 30000},
        ],
        "response_structure": (
            "1. 先说明成人标准用量\n"
            "2. 如适用，分别说明儿童/老人/孕妇等特殊人群的用量\n"
            "3. 说明服用时间（饭前/饭后/空腹）\n"
            "4. 强调不要超过每日最大剂量"
        ),
        "mandatory_reminders": [
            "请仔细阅读药品说明书并按说明使用，或在药师指导下购买和使用",
            "如症状持续不缓解或加重，请及时就医",
            "请严格按说明书用法用量服用，不要自行调整剂量",
            "如服药后症状未见好转或加重，请及时就医",
            "儿童用量通常按体重计算，请参照说明书或遵医嘱",
        ],
        "fallback_response": (
            "抱歉，未能找到关于{drug_name}用法用量的信息。"
            "建议您查看药品说明书中「用法用量」章节，或咨询医生/药师。"
        ),
        "changelog": "初始版本：说明书检索 → 药品档案 → 联网兜底",
    },
    "efficacy": {
        "version": "v1.0.0",
        "sop_steps": [
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_name}", "question": "功效 适应症 作用 用途", "top_k": "5"},
             "parallel_group": 0, "is_critical": True, "timeout_ms": 15000},
            {"order": 2, "tool_name": "get_drug_detail",
             "args_template": {"drug_name": "{drug_name}"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 15000},
            {"order": 3, "tool_name": "search_web",
             "args_template": {"query": "{drug_name} 适应症 作用", "num_results": "5"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 30000},
        ],
        "response_structure": (
            "1. 先说明药品类别（如非甾体抗炎药/解热镇痛药）\n"
            "2. 列出主要适应症/治疗用途\n"
            "3. 用通俗语言简要解释药理作用（如有相关信息）\n"
            "4. 如果检索结果中有与用户症状相关的内容，针对性回应"
        ),
        "mandatory_reminders": [
            "请仔细阅读药品说明书并按说明使用，或在药师指导下购买和使用",
            "如症状持续不缓解或加重，请及时就医",
            "不同药品的适应症可能不同，请确保您使用的是对症的药品",
        ],
        "fallback_response": (
            "抱歉，未能找到关于{drug_name}功效和适应症的信息。"
            "建议您查看药品说明书中「适应症」或「作用类别」章节，或咨询医生/药师。"
        ),
        "changelog": "初始版本：说明书检索 → 药品档案 → 联网兜底",
    },
    "special_population": {
        "version": "v1.0.0",
        "sop_steps": [
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_name}", "question": "{population} 安全性 禁忌", "top_k": "5"},
             "parallel_group": 0, "is_critical": True, "timeout_ms": 15000},
            {"order": 2, "tool_name": "get_drug_detail",
             "args_template": {"drug_name": "{drug_name}"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 15000},
            {"order": 3, "tool_name": "search_web",
             "args_template": {"query": "{drug_name} {population} 用药安全", "num_results": "5"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 30000},
        ],
        "response_structure": (
            "1. 先给出明确的总体结论（安全/慎用/禁用/数据不足）\n"
            "2. 解释原因（如 FDA 妊娠分级、说明书禁忌、临床研究数据）\n"
            "3. 如适用，说明在什么条件下可以使用\n"
            "4. 提供替代建议（如更安全的替代药物）\n"
            "5. 强调咨询医生的重要性（尤其是孕妇和哺乳期女性）"
        ),
        "mandatory_reminders": [
            "⚠️ 警告：特殊人群用药安全信息可能不完整，请务必在医生或药师指导下使用",
            "孕妇及哺乳期女性必须格外谨慎，切勿仅凭网络信息自行用药",
            "药物安全性可能因孕期阶段（孕早期/中期/晚期）而异",
            "请仔细阅读说明书并按说明使用",
            "如症状持续不缓解或加重，请及时就医",
        ],
        "fallback_response": (
            "抱歉，未能找到关于{drug_name}在{population}人群中使用的安全信息。"
            "建议您咨询妇产科/儿科医生或药师，获取针对性的用药指导。"
            "在没有专业指导的情况下，请勿自行用药。"
        ),
        "changelog": "初始版本：说明书检索 → 药品档案 → 联网兜底",
    },
    "drug_interaction": {
        "version": "v1.0.0",
        "sop_steps": [
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_a}", "question": "药物相互作用", "top_k": "3"},
             "parallel_group": 1, "is_critical": True, "timeout_ms": 15000},
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_b}", "question": "药物相互作用", "top_k": "3"},
             "parallel_group": 1, "is_critical": True, "timeout_ms": 15000},
            {"order": 2, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_a}", "question": "{drug_b} 相互作用", "top_k": "3"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 15000},
            {"order": 3, "tool_name": "search_web",
             "args_template": {"query": "{drug_a} {drug_b} 相互作用 能否同服", "num_results": "5"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 30000},
        ],
        "response_structure": (
            "1. 先给出明确的总体结论（已知有/无相互作用，能否同服）\n"
            "2. 如存在相互作用，说明具体机制和可能带来的后果\n"
            "3. 提供安全建议（用药间隔、需观察的症状等）\n"
            "4. 即使未发现已知相互作用，也提供一般性安全提醒"
        ),
        "mandatory_reminders": [
            "⚠️ 警告：药物相互作用数据库可能不完整，未发现已知相互作用不保证绝对安全",
            "即使没有已知相互作用，也建议两种药物间隔至少 2 小时服用",
            "用药期间注意观察身体反应，如出现异常请立即停药并就医",
            "如果您正在长期服用慢性病药物（降压药、降糖药等），请咨询医生",
            "请仔细阅读说明书并按说明使用",
        ],
        "fallback_response": (
            "抱歉，未能找到关于{drug_a}和{drug_b}相互作用的信息。"
            "建议两种药物间隔至少 2 小时服用，用药期间注意观察身体反应。"
            "如果您正在服用其他长期药物（降压药、降糖药等），请咨询医生或药师。"
        ),
        "changelog": "初始版本：双药并行说明书检索 → 交叉检索 → 联网兜底",
    },
    "drug_comparison": {
        "version": "v1.0.0",
        "sop_steps": [
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_a}", "question": "功效 适应症", "top_k": "3"},
             "parallel_group": 1, "is_critical": True, "timeout_ms": 15000},
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_a}", "question": "副作用 禁忌", "top_k": "3"},
             "parallel_group": 1, "is_critical": True, "timeout_ms": 15000},
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_b}", "question": "功效 适应症", "top_k": "3"},
             "parallel_group": 1, "is_critical": True, "timeout_ms": 15000},
            {"order": 1, "tool_name": "search_manual",
             "args_template": {"drug_name": "{drug_b}", "question": "副作用 禁忌", "top_k": "3"},
             "parallel_group": 1, "is_critical": True, "timeout_ms": 15000},
            {"order": 2, "tool_name": "search_web",
             "args_template": {"query": "{drug_a} {drug_b} 对比 区别", "num_results": "5"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 30000},
        ],
        "response_structure": (
            "1. 用对比的方式展示两种药的关键信息（适应症/起效时间/持续时间/常见副作用/禁忌人群）\n"
            "2. 分析各自的优劣势\n"
            "3. 提供场景化建议：什么情况选 A，什么情况选 B\n"
            "4. 如果检索结果不足以做出有依据的对比，请如实说明"
        ),
        "mandatory_reminders": [
            "请仔细阅读药品说明书并按说明使用，或在药师指导下购买和使用",
            "如症状持续不缓解或加重，请及时就医",
            "以上对比基于公开的药品说明书信息，具体选择需结合个人情况",
            "如果您有慢性病史或正在服用其他药物，请咨询医生或药师",
            "药品效果因人而异，他人的用药体验不一定适用于您",
        ],
        "fallback_response": (
            "抱歉，未能找到足够信息对{drug_a}和{drug_b}进行详细对比。"
            "建议分别查看两种药品的说明书，或咨询医生/药师获取针对性建议。"
        ),
        "changelog": "初始版本：四路并行说明书多维度检索 → 联网兜底",
    },
    "recommendation_explanation": {
        "version": "v1.0.0",
        "sop_steps": [
            {"order": 1, "tool_name": "get_recommendation",
             "args_template": {},
             "parallel_group": 1, "is_critical": True, "timeout_ms": 15000},
            {"order": 2, "tool_name": "get_user_profile",
             "args_template": {},
             "parallel_group": 1, "is_critical": False, "timeout_ms": 15000},
            {"order": 3, "tool_name": "search_drug",
             "args_template": {"query": "{target_drug}", "limit": "3"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 15000},
            {"order": 4, "tool_name": "get_drug_detail",
             "args_template": {"drug_name": "{target_drug}"},
             "parallel_group": 0, "is_critical": False, "timeout_ms": 15000},
        ],
        "response_structure": (
            "### 为什么推荐：\n"
            "1. 说明系统推荐了哪些药品\n"
            "2. 解释每种药为什么适合该用户（结合症状、年龄等）\n"
            "3. 给出推荐依据，避免过度强调评分等内部指标\n\n"
            "### 为什么不推荐：\n"
            "1. 确认该药品的基本信息（适应症、禁忌）\n"
            "2. 分析用户情况与该药品之间可能存在的错配\n"
            "3. 明确说明：\"以上分析基于药品说明书信息，具体原因建议与药师进一步沟通\"\n"
            "4. 如果用户需要替代方案，建议其说明需求以便调整"
        ),
        "mandatory_reminders": [
            "以上推荐解释基于您的症状与药品适应症的匹配分析",
            "药品推荐仅供参考，最终选择请结合自身情况或咨询医生/药师",
            "如果您对推荐结果不满意，可以告诉我更多信息，我将为您调整",
        ],
        "fallback_response": (
            "抱歉，当前没有可用的推荐数据。"
            "请先完成症状问诊，系统将为您匹配合适的药品。"
        ),
        "changelog": "初始版本：推荐列表 + 用户画像 → 药品信息查证",
    },
    # inventory_check 不在 TASK_SOP_MAP 中（走 ReAct fallback），不需要 SOP 版本
}

# ── 工具注册 ────────────────────────────────────────────────
# 从 app/agent/react/tools/ 中各工具类的 definition 属性提取。

TOOLS = [
    {
        "name": "search_drug",
        "display_name": "药品搜索",
        "description": (
            "搜索药品。根据药品名称（通用名/商品名/拼音）模糊搜索，"
            "返回匹配的药品列表。适用场景：用户提到药品名时定位药品、"
            "用户需要某类药品时发现候选。"
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "药品名称关键词"},
                "limit": {"type": "integer", "description": "返回数量上限，默认 5"},
            },
            "required": ["query"],
        },
        "capabilities": ["drug_discovery"],
        "fallback_tools": ["get_drug_detail", "search_manual"],
        "timeout_ms": 15000,
        "retry_count": 0,
        "status": "active",
    },
    {
        "name": "search_manual",
        "display_name": "说明书检索",
        "description": (
            "在药品说明书中语义检索与用户问题最相关的片段。"
            "这是回答针对性药品问题的**首选工具**（如副作用、禁忌、"
            "用法用量、孕妇/儿童用药等）。"
            "适用场景：用户问'XX药有什么副作用''XX药孕妇能吃吗'"
            "'XX药怎么吃'等具体问题。"
            "注意：如需药品的完整结构化档案（所有字段），请使用 get_drug_detail。"
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "drug_name": {"type": "string", "description": "药品名称（通用名）"},
                "question": {"type": "string", "description": "用户关心的问题，如'副作用''孕妇能用吗''用法用量'"},
                "top_k": {"type": "integer", "description": "返回片段数量，默认 5"},
            },
            "required": ["drug_name", "question"],
        },
        "capabilities": ["drug_qa"],
        "fallback_tools": ["get_drug_detail"],
        "timeout_ms": 15000,
        "retry_count": 0,
        "status": "active",
    },
    {
        "name": "get_drug_detail",
        "display_name": "药品档案查询",
        "description": (
            "获取药品的完整结构化信息：适应症、用法用量、不良反应、"
            "禁忌、药物相互作用、注意事项等。适用场景：用户需要药品的"
            "系统性介绍或完整档案时使用。注意：如需针对特定问题的精准"
            "检索（如'布洛芬孕妇能吃吗'），优先使用 search_manual。"
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "drug_name": {"type": "string", "description": "药品通用名（如'布洛芬'）"},
            },
            "required": ["drug_name"],
        },
        "capabilities": ["drug_profile"],
        "fallback_tools": ["search_manual"],
        "timeout_ms": 15000,
        "retry_count": 0,
        "status": "active",
    },
    {
        "name": "search_web",
        "display_name": "联网搜索",
        "description": (
            "联网搜索药品信息。⚠️ 这是最后一级数据源——"
            "仅在本地工具（search_manual、get_drug_detail）"
            "返回空结果或信息不充分时才使用。"
            "搜索 query 应包含药品名称 + 用户问题的关键词。"
            "返回结果包含来源 URL，你必须在回复中标注网络来源。"
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，应包含药品名称和用户问题的核心词"},
                "num_results": {"type": "integer", "description": "返回结果数量，默认 5"},
            },
            "required": ["query"],
        },
        "capabilities": ["web_search"],
        "fallback_tools": [],
        "timeout_ms": 30000,
        "retry_count": 0,
        "status": "active",
    },
    {
        "name": "check_inventory",
        "display_name": "库存查询",
        "description": (
            "查询药品库存信息：是否有货、库存数量、价格、规格、厂家、"
            "货架位置。适用场景：用户询问药品是否有货、药品价格、"
            "或想确认药店是否有某药品。注意：需要提供药品通用名（如'布洛芬'），"
            "如果不确定药品名，先用 search_drug 查找。"
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "drug_name": {"type": "string", "description": "药品通用名（如'布洛芬'、'对乙酰氨基酚'）"},
            },
            "required": ["drug_name"],
        },
        "capabilities": ["inventory_check"],
        "fallback_tools": ["search_drug"],
        "timeout_ms": 15000,
        "retry_count": 0,
        "status": "active",
    },
    {
        "name": "find_drug_location",
        "display_name": "货架位置查询",
        "description": (
            "查询药品在药店中的货架位置。适用场景：用户询问药品在哪里、"
            "在哪个货架、怎么找到某药品。注意：需要提供药品通用名（如'布洛芬'），"
            "如果不确定药品名，先用 search_drug 查找。"
            "如果用户同时关心库存和价格，改用 check_inventory。"
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "drug_name": {"type": "string", "description": "药品通用名（如'布洛芬'、'对乙酰氨基酚'）"},
            },
            "required": ["drug_name"],
        },
        "capabilities": ["inventory_check"],
        "fallback_tools": ["search_drug", "check_inventory"],
        "timeout_ms": 15000,
        "retry_count": 0,
        "status": "active",
    },
    {
        "name": "get_recommendation",
        "display_name": "推荐列表读取",
        "description": (
            "获取系统当前已推荐的药品列表。"
            "当用户使用'这个药'、'推荐的药'、'它'等指代词时，"
            "先调用此工具获取推荐列表，再据此解析用户指代的是哪个药。"
        ),
        "parameters_schema": {"type": "object", "properties": {}},
        "capabilities": ["state_access"],
        "fallback_tools": [],
        "timeout_ms": 5000,
        "retry_count": 0,
        "status": "active",
    },
    {
        "name": "get_user_profile",
        "display_name": "用户画像读取",
        "description": (
            "获取用户个人信息（年龄、过敏史、慢性病、特殊人群等）。"
            "用于个性化药品回答——根据用户的年龄、过敏史等给出针对性建议。"
        ),
        "parameters_schema": {"type": "object", "properties": {}},
        "capabilities": ["state_access"],
        "fallback_tools": [],
        "timeout_ms": 5000,
        "retry_count": 0,
        "status": "active",
    },
]

# ── 模型配置 ────────────────────────────────────────────────
# 从 app/graph/builder.py 中各 LLMProfile 实例化参数提取。

MODEL_CONFIGS = [
    {
        "role": "dispatcher",
        "model_name": "qwen-plus",
        "temperature": 0.1,
        "max_tokens": 512,
        "description": "Dispatcher 意图解析器 — 低温度保证分类一致性",
    },
    {
        "role": "consult",
        "model_name": "qwen-plus",
        "temperature": 0.3,
        "max_tokens": 1024,
        "description": "Consult Agent 症状问诊 — 平衡创造性和准确性",
    },
    {
        "role": "react",
        "model_name": "qwen-plus",
        "temperature": 0.3,
        "max_tokens": 1024,
        "description": "ReactAgent OTC 助手 — 工具驱动的药品问答（fallback 用）",
    },
    {
        "role": "recommend",
        "model_name": "qwen-plus",
        "temperature": 0.3,
        "max_tokens": 2048,
        "description": "Recommend 推荐节点 — 需要较大 max_tokens 生成完整推荐",
    },
    {
        "role": "classifier",
        "model_name": "qwen-plus",
        "temperature": 0.1,
        "max_tokens": 512,
        "description": "TaskClassifier 分类器 — 低温度保证分类一致性",
    },
    {
        "role": "generator",
        "model_name": "qwen-plus",
        "temperature": 0.3,
        "max_tokens": 2048,
        "description": "ResponseGenerator 回复生成 — 长回复（对比表格、安全提醒等）需要 2048 tokens",
    },
]


# ═══════════════════════════════════════════════════════════════
# 主逻辑
# ═══════════════════════════════════════════════════════════════


async def seed():
    settings = Settings()
    print(f"[INFO] Connecting to: {settings.database_url[:50]}...")

    engine = create_async_engine(settings.database_url, echo=False)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # ── 1. Prompt 模板 ──────────────────────────────────────
    async with session_factory() as db:
        inserted = 0
        for p in PROMPTS:
            # 检查是否已存在同 role + version 的记录
            existing = (
                await db.execute(
                    select(PromptTemplate).where(
                        PromptTemplate.role == p["role"],
                        PromptTemplate.version == p["version"],
                        PromptTemplate.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"[SKIP] Prompt '{p['role']}' v{p['version']} already exists")
                continue

            prompt = PromptTemplate(
                role=p["role"],
                version=p["version"],
                content=p["content"],
                is_active=p["is_active"],
                description=p["description"],
                updated_by="seed_admin",
            )
            db.add(prompt)
            inserted += 1

        await db.commit()
        print(f"[OK] Inserted {inserted} prompt templates (total: {len(PROMPTS)})")

    # ── 2. 高风险关键字 ────────────────────────────────────
    async with session_factory() as db:
        inserted = 0
        for kw in HIGH_RISK_KEYWORDS:
            existing = (
                await db.execute(
                    select(HighRiskKeyword).where(
                        HighRiskKeyword.keyword == kw["keyword"],
                        HighRiskKeyword.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"[SKIP] Keyword '{kw['keyword']}' already exists")
                continue

            record = HighRiskKeyword(
                keyword=kw["keyword"],
                category=kw["category"],
                severity=kw["severity"],
                is_active=True,
            )
            if hasattr(HighRiskKeyword, "negative_patterns"):
                record.negative_patterns = kw.get("negative_patterns")
            db.add(record)
            inserted += 1

        await db.commit()
        print(f"[OK] Inserted {inserted} high-risk keywords (total: {len(HIGH_RISK_KEYWORDS)})")

    # ── 3. 技能定义 + 版本 ─────────────────────────────────
    async with session_factory() as db:
        skill_inserted = 0
        version_inserted = 0
        skill_id_map: dict[str, int] = {}  # task_type → skill.id

        for s in SKILLS:
            task_type = s["task_type"]
            # 检查是否已存在
            existing = (
                await db.execute(
                    select(Skill).where(Skill.task_type == task_type)
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"[SKIP] Skill '{task_type}' already exists (id={existing.id})")
                skill_id_map[task_type] = existing.id
                continue

            skill = Skill(
                name=s["name"],
                task_type=s["task_type"],
                status="active",
                description=s["description"],
            )
            db.add(skill)
            await db.flush()  # 获取 id，但不提交
            skill_id_map[task_type] = skill.id
            skill_inserted += 1

        await db.commit()
        print(f"[OK] Inserted {skill_inserted} skills (total: {len(SKILLS)})")

        # ── 版本（只为有 SOP 定义的 skill 创建版本）──
        for task_type, sv_data in SKILL_VERSIONS.items():
            skill_id = skill_id_map.get(task_type)
            if skill_id is None:
                # 尝试从 DB 查询
                row = (
                    await db.execute(
                        select(Skill).where(Skill.task_type == task_type)
                    )
                ).scalar_one_or_none()
                if row is None:
                    print(f"[WARN] Skill '{task_type}' not found, skipping version")
                    continue
                skill_id = row.id

            # 检查是否已存在同版本
            existing = (
                await db.execute(
                    select(SkillVersion).where(
                        SkillVersion.skill_id == skill_id,
                        SkillVersion.version == sv_data["version"],
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"[SKIP] SkillVersion '{task_type}' v{sv_data['version']} already exists")
                continue

            sv = SkillVersion(
                skill_id=skill_id,
                version=sv_data["version"],
                sop_steps=sv_data["sop_steps"],
                response_structure=sv_data["response_structure"],
                mandatory_reminders=sv_data["mandatory_reminders"],
                fallback_response=sv_data["fallback_response"],
                changelog=sv_data["changelog"],
                created_by="seed_admin",
            )
            db.add(sv)
            version_inserted += 1

            # 更新 skill 的 current_version
            skill = await db.get(Skill, skill_id)
            if skill and skill.current_version is None:
                skill.current_version = sv_data["version"]

        await db.commit()
        print(f"[OK] Inserted {version_inserted} skill versions (total: {len(SKILL_VERSIONS)})")

    # ── 4. 工具注册 ────────────────────────────────────────
    async with session_factory() as db:
        inserted = 0
        for t in TOOLS:
            existing = (
                await db.execute(
                    select(Tool).where(Tool.name == t["name"])
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"[SKIP] Tool '{t['name']}' already exists")
                continue

            tool = Tool(
                name=t["name"],
                display_name=t["display_name"],
                description=t["description"],
                parameters_schema=t["parameters_schema"],
                capabilities=t["capabilities"],
                fallback_tools=t["fallback_tools"],
                timeout_ms=t["timeout_ms"],
                retry_count=t["retry_count"],
                status=t["status"],
            )
            db.add(tool)
            inserted += 1

        await db.commit()
        print(f"[OK] Inserted {inserted} tools (total: {len(TOOLS)})")

    # ── 5. 模型配置 ────────────────────────────────────────
    async with session_factory() as db:
        inserted = 0
        for mc in MODEL_CONFIGS:
            existing = (
                await db.execute(
                    select(ModelConfig).where(ModelConfig.role == mc["role"])
                )
            ).scalar_one_or_none()
            if existing is not None:
                print(f"[SKIP] ModelConfig '{mc['role']}' already exists")
                continue

            config = ModelConfig(
                role=mc["role"],
                model_name=mc["model_name"],
                temperature=mc["temperature"],
                max_tokens=mc["max_tokens"],
                is_active=True,
                description=mc["description"],
                updated_by="seed_admin",
            )
            db.add(config)
            inserted += 1

        await db.commit()
        print(f"[OK] Inserted {inserted} model configs (total: {len(MODEL_CONFIGS)})")

    await engine.dispose()
    print("\n[DONE] Admin seed data import complete!")
    print("  - Prompt templates:   5 (dispatcher, consult, react, classifier, generator)")
    print("  - High-risk keywords: 23 (emergency, allergy, suicide, drug_abuse)")
    print("  - Skills:             9 (8 with SOP versions, inventory_check has no SOP)")
    print("  - Skill versions:     8 (v1.0.0)")
    print("  - Tools:              8 (search_drug, search_manual, get_drug_detail, search_web, check_inventory, find_drug_location, get_recommendation, get_user_profile)")
    print("  - Model configs:      6 (dispatcher, consult, react, recommend, classifier, generator)")


if __name__ == "__main__":
    asyncio.run(seed())
