"""
LLMProfile — 单场景 LLM 配置。

每个场景（dispatcher / consult / react / recommend）可以有独立的模型、温度、token 上限。
LLMClient 的 generate() / generate_structured() / generate_with_tools() 接受可选的 profile 参数，
不传则使用全局默认值（向后兼容）。
"""

from pydantic import BaseModel


class LLMProfile(BaseModel):
    """单个 LLM 调用场景的配置。

    Examples:
        # dispatcher 用快速模型
        dispatcher_profile = LLMProfile(model="qwen-turbo", temperature=0.1, max_tokens=256)

        # react agent 用推理模型
        react_profile = LLMProfile(model="qwen-plus", temperature=0.3, max_tokens=1024)
    """

    model: str = "qwen-plus"
    """模型名称（如 qwen-turbo / qwen-plus / qwen-max）"""

    temperature: float = 0.3
    """采样温度 0-2。越低越确定性，越高越随机"""

    max_tokens: int = 1024
    """最大输出 token 数"""

    timeout: float = 30.0
    """超时秒数"""
