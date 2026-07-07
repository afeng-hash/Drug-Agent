"""
Admin API — AI Agent 运营平台。

通过 /api/v1/admin/* 挂载，提供：
  - 对话管理、用户管理
  - Agent Trace（链路追踪）
  - LLM 用量仪表盘 + 模型配置
  - 数据库管理（药品/库存/权重）
  - 知识图谱管理
  - Skill 管理中心 + SOP 编排
  - 工具管理
  - Prompt 管理中心
  - Web Search 管理
  - 高风险关键字监控
  - 反馈管理、审计中心、系统配置
"""

from app.api.routes.admin.router import admin_router

__all__ = ["admin_router"]
