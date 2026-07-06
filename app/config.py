"""
Application configuration — pydantic-settings.

所有配置项从 .env 文件和环境变量中读取。pydantic-settings 自动处理类型转换。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.llm.profile import LLMProfile


class Settings(BaseSettings):
    """应用全局配置。

    配置来源优先级（从高到低）：
      1. 环境变量（如 export LLM_API_KEY=xxx）
      2. .env 文件（项目根目录）
      3. 代码中的默认值

    使用方式：
        settings = Settings()
        print(settings.llm_model)  # "qwen-plus"
    """

    model_config = SettingsConfigDict(
        env_file=".env",           # 从项目根目录的 .env 文件加载
        env_file_encoding="utf-8",
        extra="ignore",            # 忽略 .env 中未定义的字段（不会报错）
    )

    # ── LLM 配置 ────────────────────────────────────────
    # 默认使用阿里云 DashScope（兼容 OpenAI 协议）

    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    """LLM API 地址（OpenAI-compatible 端点）"""

    llm_api_key: str = ""
    """LLM API Key。在 .env 中设置，形如 LLM_API_KEY=sk-xxx"""

    llm_model: str = "qwen-plus"
    """对话模型名称。可选：qwen-plus, qwen-max, qwen-turbo"""

    embedding_model: str = "text-embedding-v3"
    """嵌入模型名称。用于生成 RAG 检索用的文本向量"""

    # ── 多 Profile 配置 ──────────────────────────────────
    # 每个场景独立配置 LLMProfile。字段为 dict 类型，通过 get_profile() 转为 LLMProfile 对象。
    # 环境变量覆盖示例：LLM_DISPATCHER='{"model":"qwen-turbo","temperature":0.1}'

    llm_dispatcher: dict = {"model": "qwen-turbo", "temperature": 0.1, "max_tokens": 256}
    """Dispatcher 的 LLMProfile。快速模型 + 低温度保证路由稳定性"""

    llm_consult: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 1024}
    """Consult Agent 的 LLMProfile。准确模型 + 中等温度"""

    llm_react: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 1024}
    """ReactAgent 的 LLMProfile。推理模型 + 平衡温度"""

    llm_recommend: dict = {"model": "qwen-plus", "temperature": 0.3, "max_tokens": 512}
    """Recommend 的 LLMProfile。文案生成，需要自然流畅"""

    # ── 数据库配置 ───────────────────────────────────────

    database_url: str = "postgresql+asyncpg://drug_agent:drug_agent@localhost:5432/drug_agent"
    """PostgreSQL 连接字符串。
    格式：postgresql+asyncpg://用户名:密码@主机:端口/数据库名
    使用 asyncpg 驱动（异步）"""

    # ── Neo4j 配置 ──────────────────────────────────────

    neo4j_uri: str = "bolt://localhost:7687"
    """Neo4j Bolt 协议地址"""

    neo4j_user: str = "neo4j"
    """Neo4j 用户名"""

    neo4j_password: str = ""
    """Neo4j 密码。在 .env 中设置，形如 NEO4J_PASSWORD=xxx"""

    neo4j_database: str = "neo4j"
    """Neo4j 数据库名"""

    # ── Milvus 配置 ──────────────────────────────────────

    milvus_host: str = "localhost"
    """Milvus 向量数据库地址"""

    milvus_port: int = 19530
    """Milvus gRPC 端口"""

    # ── LangSmith 配置（可选，用于调试和追踪） ────────────

    langsmith_api_key: str = ""
    """LangSmith API Key。不设置则不启用追踪"""

    langsmith_project: str = "drug-agent"
    """LangSmith 项目名"""

    # ── 应用配置 ────────────────────────────────────────

    app_host: str = "0.0.0.0"
    """uvicorn 绑定地址。0.0.0.0 表示接受所有网络接口的连接"""

    app_port: int = 8000
    """uvicorn 监听端口"""

    debug: bool = True
    """调试模式。True 时：
      - SQLAlchemy echo=True（打印所有 SQL）
      - uvicorn reload=True（代码修改自动重载）"""

    # ── 会话配置 ────────────────────────────────────────

    session_expire_minutes: int = 30
    """会话过期时间（分钟）。超过此时间未活动，session 自动标记为 expired"""

    max_consult_rounds: int = 6
    """问诊最大追问轮数。超过此轮数强制进入推荐，防止无休止追问"""

    # ── 联网搜索配置 ─────────────────────────────────────

    web_search_enabled: bool = True
    """是否启用联网搜索。False 时 search_web 工具直接返回不可用"""

    tavily_api_key: str = ""
    """Tavily Search API Key。在 .env 中设置，形如 TAVILY_API_KEY=tvly-xxx"""

    web_search_timeout: float = 10.0
    """联网搜索单次请求超时（秒）"""

    web_search_max_results: int = 5
    """联网搜索最大返回结果数"""

    def get_profile(self, field_name: str) -> LLMProfile:
        """从 Settings 的 dict 字段构建 LLMProfile 对象。

        Args:
            field_name: 字段名，如 "llm_dispatcher" / "llm_consult" / "llm_react" / "llm_recommend"

        Returns:
            对应的 LLMProfile 实例

        Raises:
            AttributeError: 字段名不存在
            ValidationError: dict 内容不符合 LLMProfile schema
        """
        raw = getattr(self, field_name)
        return LLMProfile(**raw)
