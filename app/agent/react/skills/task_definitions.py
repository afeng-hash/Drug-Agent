"""
任务类型 SOP（标准作业程序）定义 —— 纯数据。

每种 task_type 对应一个 SOP 实例，包含：执行步骤、响应结构、强制性安全约束以及备用（兜底）模板。
本模块不包含任何执行逻辑。
"""

from app.agent.react.skills.types import SOP, SOPStep, TaskType

# 大多数任务类型所共有的强制性提醒
_COMMON_REMINDERS = [
    "请仔细阅读药品说明书并按说明使用，或在药师指导下购买和使用",
    "如症状持续不缓解或加重，请及时就医",
]


# =====================================================================
# 1. 副作用查询
# =====================================================================

SIDE_EFFECTS_SOP = SOP(
    task_type=TaskType.SIDE_EFFECTS,
    steps=[
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_name}", "question": "副作用 不良反应", "top_k": "5"}),
        SOPStep(order=2, tool_name="get_drug_detail",
                args_template={"drug_name": "{drug_name}"}),
        SOPStep(order=3, tool_name="search_web",
                args_template={"query": "{drug_name} 副作用", "num_results": "5"}),
    ],
    response_structure=(
        "1. 先说明常见副作用（发生率高、症状较轻）\n"
        "2. 再说明偶见/罕见副作用（发生率低）\n"
        "3. 列出需要立即就医的严重不良反应信号\n"
        "4. 给出观察和应对建议（如饭后服用可减轻胃肠不适）"
    ),
    mandatory_reminders=[
        *_COMMON_REMINDERS,
        "如出现严重不良反应，请立即停药并就医",
        "说明书中列出的副作用并非都会发生，大多数属于偶见或罕见",
    ],
    fallback_response=(
        "抱歉，未能找到关于{drug_name}副作用的信息。"
        "建议您查看药品说明书中「不良反应」章节，或咨询医生/药师。"
    ),
)

# =====================================================================
# 2. 禁忌查询
# =====================================================================

CONTRAINDICATIONS_SOP = SOP(
    task_type=TaskType.CONTRAINDICATIONS,
    steps=[
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_name}", "question": "禁忌 注意事项 警告", "top_k": "5"}),
        SOPStep(order=2, tool_name="get_drug_detail",
                args_template={"drug_name": "{drug_name}"}),
        SOPStep(order=3, tool_name="search_web",
                args_template={"query": "{drug_name} 禁忌 注意事项", "num_results": "5"}),
    ],
    response_structure=(
        "1. 先列出绝对禁忌症（什么情况下绝对不能用）\n"
        "2. 再列出慎用情况（需要医生评估后才能使用的场景）\n"
        "3. 如果用户提到了自己的具体情况，针对性地回答\n"
        "4. 强调以上信息仅供参考，具体情况需由医生判断"
    ),
    mandatory_reminders=[
        *_COMMON_REMINDERS,
        "如果您有慢性病史或正在服用其他药物，使用前请咨询医生或药师",
        "禁忌信息来源于药品说明书，是否可以使用需结合个人情况由医生判断",
    ],
    fallback_response=(
        "抱歉，未能找到关于{drug_name}禁忌的详细信息。"
        "建议您查看药品说明书中「禁忌」和「注意事项」章节，或咨询医生/药师。"
    ),
)

# =====================================================================
# 3. 用法用量查询
# =====================================================================

DOSAGE_SOP = SOP(
    task_type=TaskType.DOSAGE,
    steps=[
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_name}", "question": "用法用量 剂量 服用方法", "top_k": "5"}),
        SOPStep(order=2, tool_name="get_drug_detail",
                args_template={"drug_name": "{drug_name}"}),
        SOPStep(order=3, tool_name="search_web",
                args_template={"query": "{drug_name} 用法用量", "num_results": "5"}),
    ],
    response_structure=(
        "1. 先说明成人标准用量\n"
        "2. 如适用，分别说明儿童/老人/孕妇等特殊人群的用量\n"
        "3. 说明服用时间（饭前/饭后/空腹）\n"
        "4. 强调不要超过每日最大剂量"
    ),
    mandatory_reminders=[
        *_COMMON_REMINDERS,
        "请严格按说明书用法用量服用，不要自行调整剂量",
        "如服药后症状未见好转或加重，请及时就医",
        "儿童用量通常按体重计算，请参照说明书或遵医嘱",
    ],
    fallback_response=(
        "抱歉，未能找到关于{drug_name}用法用量的信息。"
        "建议您查看药品说明书中「用法用量」章节，或咨询医生/药师。"
    ),
)

# =====================================================================
# 4. 功效/适应症查询
# =====================================================================

EFFICACY_SOP = SOP(
    task_type=TaskType.EFFICACY,
    steps=[
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_name}", "question": "功效 适应症 作用 用途", "top_k": "5"}),
        SOPStep(order=2, tool_name="get_drug_detail",
                args_template={"drug_name": "{drug_name}"}),
        SOPStep(order=3, tool_name="search_web",
                args_template={"query": "{drug_name} 适应症 作用", "num_results": "5"}),
    ],
    response_structure=(
        "1. 先说明药品类别（如非甾体抗炎药/解热镇痛药）\n"
        "2. 列出主要适应症/治疗用途\n"
        "3. 用通俗语言简要解释药理作用（如有相关信息）\n"
        "4. 如果检索结果中有与用户症状相关的内容，针对性回应"
    ),
    mandatory_reminders=[
        *_COMMON_REMINDERS,
        "不同药品的适应症可能不同，请确保您使用的是对症的药品",
    ],
    fallback_response=(
        "抱歉，未能找到关于{drug_name}功效和适应症的信息。"
        "建议您查看药品说明书中「适应症」或「作用类别」章节，或咨询医生/药师。"
    ),
)

# =====================================================================
# 5. 特殊人群用药查询（孕妇/哺乳期/儿童/老人）
# =====================================================================

SPECIAL_POPULATION_SOP = SOP(
    task_type=TaskType.SPECIAL_POPULATION,
    steps=[
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_name}", "question": "{population} 安全性 禁忌", "top_k": "5"}),
        SOPStep(order=2, tool_name="get_drug_detail",
                args_template={"drug_name": "{drug_name}"}),
        SOPStep(order=3, tool_name="search_web",
                args_template={"query": "{drug_name} {population} 用药安全", "num_results": "5"}),
    ],
    response_structure=(
        "1. 先给出明确的总体结论（安全/慎用/禁用/数据不足）\n"
        "2. 解释原因（如 FDA 妊娠分级、说明书禁忌、临床研究数据）\n"
        "3. 如适用，说明在什么条件下可以使用\n"
        "4. 提供替代建议（如更安全的替代药物）\n"
        "5. 强调咨询医生的重要性（尤其是孕妇和哺乳期女性）"
    ),
    mandatory_reminders=[
        "⚠️ 警告：特殊人群用药安全信息可能不完整，请务必在医生或药师指导下使用",
        "孕妇及哺乳期女性必须格外谨慎，切勿仅凭网络信息自行用药",
        "药物安全性可能因孕期阶段（孕早期/中期/晚期）而异",
        "请仔细阅读说明书并按说明使用",
        "如症状持续不缓解或加重，请及时就医",
    ],
    fallback_response=(
        "抱歉，未能找到关于{drug_name}在{population}人群中使用的安全信息。"
        "建议您咨询妇产科/儿科医生或药师，获取针对性的用药指导。"
        "在没有专业指导的情况下，请勿自行用药。"
    ),
)

# =====================================================================
# 6. 药物相互作用查询
# =====================================================================

DRUG_INTERACTION_SOP = SOP(
    task_type=TaskType.DRUG_INTERACTION,
    steps=[
        # 步骤 1: 并行查询每种药的相互作用信息
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_a}", "question": "药物相互作用", "top_k": "5"},
                parallel_group=1),
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_b}", "question": "药物相互作用", "top_k": "5"},
                parallel_group=1),
        # 步骤 2: 交叉检索
        SOPStep(order=2, tool_name="search_manual",
                args_template={"drug_name": "{drug_a}", "question": "{drug_b} 相互作用", "top_k": "5"}),
        # 步骤 3: 联网兜底
        SOPStep(order=3, tool_name="search_web",
                args_template={"query": "{drug_a} {drug_b} 相互作用 能否同服", "num_results": "5"}),
    ],
    response_structure=(
        "1. 先给出明确的总体结论（已知有/无相互作用，能否同服）\n"
        "2. 如存在相互作用，说明具体机制和可能带来的后果\n"
        "3. 提供安全建议（用药间隔、需观察的症状等）\n"
        "4. 即使未发现已知相互作用，也提供一般性安全提醒"
    ),
    mandatory_reminders=[
        "⚠️ 警告：药物相互作用数据库可能不完整，未发现已知相互作用不保证绝对安全",
        "即使没有已知相互作用，也建议两种药物间隔至少 2 小时服用",
        "用药期间注意观察身体反应，如出现异常请立即停药并就医",
        "如果您正在长期服用慢性病药物（降压药、降糖药等），请咨询医生",
        "请仔细阅读说明书并按说明使用",
    ],
    fallback_response=(
        "抱歉，未能找到关于{drug_a}和{drug_b}相互作用的信息。"
        "建议两种药物间隔至少 2 小时服用，用药期间注意观察身体反应。"
        "如果您正在服用其他长期药物（降压药、降糖药等），请咨询医生或药师。"
    ),
)

# =====================================================================
# 7. 药品对比
# =====================================================================

DRUG_COMPARISON_SOP = SOP(
    task_type=TaskType.DRUG_COMPARISON,
    steps=[
        # 步骤 1: 并行从多个维度查询每种药
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_a}", "question": "功效 适应症", "top_k": "5"},
                parallel_group=1),
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_a}", "question": "副作用 禁忌", "top_k": "5"},
                parallel_group=1),
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_b}", "question": "功效 适应症", "top_k": "5"},
                parallel_group=1),
        SOPStep(order=1, tool_name="search_manual",
                args_template={"drug_name": "{drug_b}", "question": "副作用 禁忌", "top_k": "5"},
                parallel_group=1),
        # 步骤 2: 联网兜底
        SOPStep(order=2, tool_name="search_web",
                args_template={"query": "{drug_a} {drug_b} 对比 区别", "num_results": "5"}),
    ],
    response_structure=(
        "1. 用对比的方式展示两种药的关键信息（适应症/起效时间/持续时间/常见副作用/禁忌人群）\n"
        "2. 分析各自的优劣势\n"
        "3. 提供场景化建议：什么情况选 A，什么情况选 B\n"
        "4. 如果检索结果不足以做出有依据的对比，请如实说明"
    ),
    mandatory_reminders=[
        *_COMMON_REMINDERS,
        "以上对比基于公开的药品说明书信息，具体选择需结合个人情况",
        "如果您有慢性病史或正在服用其他药物，请咨询医生或药师",
        "药品效果因人而异，他人的用药体验不一定适用于您",
    ],
    fallback_response=(
        "抱歉，未能找到足够信息对{drug_a}和{drug_b}进行详细对比。"
        "建议分别查看两种药品的说明书，或咨询医生/药师获取针对性建议。"
    ),
)

# =====================================================================
# 8. 推荐解释
# =====================================================================

RECOMMENDATION_EXPLANATION_SOP = SOP(
    task_type=TaskType.RECOMMENDATION_EXPLANATION,
    steps=[
        # 场景 A（为什么推荐）: 获取推荐列表 + 用户画像
        SOPStep(order=1, tool_name="get_recommendation",
                args_template={}, parallel_group=1),
        SOPStep(order=2, tool_name="get_user_profile",
                args_template={}, parallel_group=1),
        # 场景 B（为什么不推荐）: 额外查证目标药品
        SOPStep(order=3, tool_name="search_drug",
                args_template={"query": "{target_drug}", "limit": "3"}),
        SOPStep(order=4, tool_name="get_drug_detail",
                args_template={"drug_name": "{target_drug}"}),
    ],
    response_structure=(
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
    mandatory_reminders=[
        "以上推荐解释基于您的症状与药品适应症的匹配分析",
        "药品推荐仅供参考，最终选择请结合自身情况或咨询医生/药师",
        "如果您对推荐结果不满意，可以告诉我更多信息，我将为您调整",
    ],
    fallback_response=(
        "抱歉，当前没有可用的推荐数据。"
        "请先完成症状问诊，系统将为您匹配合适的药品。"
    ),
)


# =====================================================================
# 汇总: task_type → SOP 查找表
# =====================================================================

TASK_SOP_MAP: dict[TaskType, SOP] = {
    TaskType.SIDE_EFFECTS: SIDE_EFFECTS_SOP,
    TaskType.CONTRAINDICATIONS: CONTRAINDICATIONS_SOP,
    TaskType.DOSAGE: DOSAGE_SOP,
    TaskType.EFFICACY: EFFICACY_SOP,
    TaskType.SPECIAL_POPULATION: SPECIAL_POPULATION_SOP,
    TaskType.DRUG_INTERACTION: DRUG_INTERACTION_SOP,
    TaskType.DRUG_COMPARISON: DRUG_COMPARISON_SOP,
    TaskType.RECOMMENDATION_EXPLANATION: RECOMMENDATION_EXPLANATION_SOP,
}

ALL_TASK_DEFINITIONS: list[SOP] = list(TASK_SOP_MAP.values())
