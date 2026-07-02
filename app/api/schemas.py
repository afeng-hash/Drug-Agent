"""
Request/Response Pydantic models — API 层的数据契约。

定义了所有对外接口的请求体和响应体格式。
FastAPI 用这些模型自动生成 OpenAPI 文档和参数校验。

命名规范：
  - *Request  ← 前端发来的请求体
  - *Response ← 后端返回的响应体
"""

from datetime import datetime

from pydantic import BaseModel, Field


# ──────────────────────────────────────────────
# 对话请求
# ──────────────────────────────────────────────

class ChatRequest(BaseModel):
    """POST /api/v1/chat/{session_id} 的请求体。

    前端发来的一条对话消息。
    """
    message: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="用户输入的文本消息。最少 1 个字符，最多 2000 个字符。"
    )


# ──────────────────────────────────────────────
# 会话响应
# ──────────────────────────────────────────────

class SessionResponse(BaseModel):
    """会话创建 / 查询的响应体（精简版，不含消息列表）。

    用于 POST /api/v1/sessions 的返回值。
    """

    session_id: str
    """会话唯一标识，UUID v4，如 "f45c6a16-9754-4306-99c7-a537595160fb" """

    status: str
    """会话状态：'active' / 'expired' / 'closed' """

    created_at: str
    """创建时间，ISO 8601 格式，如 "2026-07-02T18:00:00+00:00" """

    expires_at: str | None = None
    """过期时间，ISO 8601 格式。默认创建后 30 分钟"""


class SessionDetailResponse(BaseModel):
    """会话详情响应体（含消息历史）。

    用于 GET /api/v1/sessions/{session_id} 的返回值。
    """

    session_id: str
    """会话 ID"""

    status: str
    """会话状态"""

    created_at: str
    """创建时间"""

    expires_at: str | None = None
    """过期时间"""

    messages: list[dict] = Field(default_factory=list)
    """消息列表（按时间先后排序）。
    每项格式：{"role": "user"|"assistant", "content": "文本", "timestamp": "ISO 8601"} """


# ──────────────────────────────────────────────
# 健康检查
# ──────────────────────────────────────────────

class HealthResponse(BaseModel):
    """GET /health 的响应体。

    用于健康检查，报告所有后端服务的连通性。
    """

    status: str
    """整体状态：
      - 'ok'       ← PostgreSQL + Milvus + LLM 全部可用
      - 'degraded' ← 至少一个服务不可用"""

    postgres: str
    """PostgreSQL 连接状态：'ok' / 'error' """

    milvus: str
    """Milvus 向量数据库连接状态：'ok' / 'error' """

    llm: str
    """LLM 服务状态：
      - 'ok'              ← API key 已配置，可用
      - 'no_api_key'      ← 未配置 API key（设置了 llm_api_key=""）
      - 'not_initialized' ← LLMClient 未初始化（应用启动异常）
      - 'error'           ← 其他错误"""


# ──────────────────────────────────────────────
# 错误响应
# ──────────────────────────────────────────────

class ErrorResponse(BaseModel):
    """标准错误响应体。当 HTTP 状态码非 2xx 时返回。"""

    code: str
    """机器可读的错误码，如 'INTERNAL_ERROR' / 'SESSION_NOT_FOUND'。
       前端可根据 code 做分支处理"""

    message: str
    """人类可读的错误描述。可直接展示给用户"""

    detail: str | None = None
    """可选的详细错误信息（调试用，生产环境可置 null 避免信息泄露）"""
