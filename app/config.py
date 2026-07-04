"""
Application configuration — pydantic-settings.

所有配置项从 .env 文件和环境变量中读取。pydantic-settings 自动处理类型转换。
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


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
