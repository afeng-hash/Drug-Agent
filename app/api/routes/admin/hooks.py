"""
Admin Hooks — 日志采集回调实现。

包含两个核心回调函数（fire-and-forget）：
  1. _write_llm_call_log(data)  → 写入 llm_call_logs 表
  2. _check_high_risk_keywords(session_id, content) → 高风险关键字检测

这些回调不阻塞主流程，写入失败静默忽略。

回调的注册和桥接由 hook_registry.py 的 register_admin_hooks() 统一管理。
新增钩子步骤：
  1. 在此文件中编写 async def 回调函数
  2. 在 hook_registry.register_admin_hooks() 中添加 registry.register("event", callback)

架构说明：
  - 关键字支持 negative_patterns（白名单正则），减少误匹配
  - 关键字缓存 TTL 60s，减少 DB 查询
  - 三层检测：substring → regex boundary → negative patterns
"""

import logging
import re
import time as time_module

from app.db.database import get_db
from app.db.models import HighRiskAlert, HighRiskKeyword, LLMCallLog
from app.llm.client import LLMCallLogData

logger = logging.getLogger(__name__)

# ── 关键字缓存 ──
_keyword_cache: list[HighRiskKeyword] = []
_keyword_cache_ts: float = 0.0
_CACHE_TTL_SECONDS = 60  # 每 60 秒自动刷新


async def _get_keywords_cached() -> list[HighRiskKeyword]:
    """获取缓存的关键字列表（TTL 过期自动刷新）。"""
    global _keyword_cache, _keyword_cache_ts
    now = time_module.monotonic()
    if now - _keyword_cache_ts > _CACHE_TTL_SECONDS:
        try:
            from sqlalchemy import select
            async with get_db() as db:
                result = await db.execute(
                    select(HighRiskKeyword).where(
                        HighRiskKeyword.is_active == True,
                        HighRiskKeyword.deleted_at.is_(None),
                    )
                )
                _keyword_cache = result.scalars().all()
                _keyword_cache_ts = now
        except Exception as e:
            logger.debug("Keyword cache refresh failed: %s", e)
    return _keyword_cache


def _build_keyword_pattern(keyword: str) -> re.Pattern:
    """用词边界构建正则，减少误匹配。

    Unicode 词边界 (\\b) 对中文效果有限，改用前瞻/后顾字符类：
    匹配 key 前后不能是字母/数字/下划线。
    """
    escaped = re.escape(keyword)
    return re.compile(rf"(?<![^\W_]){escaped}(?![^\W_])", re.IGNORECASE)


def _build_negative_pattern(pattern_str: str) -> re.Pattern | None:
    """编译 negative pattern（白名单正则）。

    当关键字命中后，再检查 negative pattern：
    如果 negative pattern 匹配，则说明是误匹配（白名单），不告警。

    例如：关键字 "毒品" 的 negative_pattern = "药品|解毒|消毒"
    当文本包含"消毒品"时会匹配关键字，但 negative pattern 中"消毒"会命中，
    说明这是合法医疗内容，不告警。

    Returns:
        编译后的 Pattern，或 None（若 pattern_str 为空或无效）。
    """
    if not pattern_str or not pattern_str.strip():
        return None
    try:
        return re.compile(pattern_str, re.IGNORECASE)
    except re.error:
        logger.warning("Invalid negative pattern: %s", pattern_str)
        return None


async def _write_llm_call_log(data: LLMCallLogData) -> None:
    """Fire-and-forget: 将 LLM 调用日志写入 llm_call_logs 表。"""
    try:
        async with get_db() as db:
            db.add(LLMCallLog(
                session_id=data.session_id,
                turn_id=data.turn_id,
                node=data.node,
                model=data.model,
                prompt_tokens=data.prompt_tokens,
                completion_tokens=data.completion_tokens,
                latency_ms=data.latency_ms,
                success=data.success,
                error_message=data.error_message,
            ))
            await db.commit()
    except Exception:
        pass  # 日志写入失败不影响主流程


async def _check_high_risk_keywords(session_id: str, content: str) -> None:
    """Fire-and-forget: 检测内容中的高风险关键字并写入告警。

    三层过滤：
      1. 缓存（TTL 60s）— 减少 DB 查询
      2. 正则词边界匹配 — 减少 substring 误匹配
      3. Negative patterns（白名单）— 过滤合法医疗内容

    支持 HighRiskKeyword.negative_patterns 字段（JSON 数组或逗号分隔字符串）。
    """
    if not content:
        return
    try:
        keywords = await _get_keywords_cached()
        if not keywords:
            return

        async with get_db() as db:
            matched_any = False
            for kw in keywords:
                try:
                    # 第一层：快速 substring 检查
                    if kw.keyword.lower() not in content.lower():
                        continue

                    # 第二层：正则词边界校验
                    pattern = _build_keyword_pattern(kw.keyword)
                    match = pattern.search(content)
                    if not match:
                        continue

                    # 第三层：Negative patterns（白名单过滤）
                    negative = _get_negative_patterns(kw)
                    if negative:
                        if negative.search(content):
                            # 白名单命中 → 合法医疗内容，跳过
                            logger.debug(
                                "Keyword '%s' matched but negative pattern '%s' "
                                "suppressed alert for session=%s",
                                kw.keyword, negative.pattern, session_id,
                            )
                            continue

                    db.add(HighRiskAlert(
                        session_id=session_id,
                        keyword_id=kw.id,
                        matched_content=content[:500],
                    ))
                    matched_any = True
                except Exception:
                    pass  # 单条匹配失败不影响其他关键字
            if matched_any:
                await db.commit()
    except Exception as e:
        logger.debug("High-risk keyword check failed: %s", e)


def _get_negative_patterns(kw: HighRiskKeyword) -> re.Pattern | None:
    """从 HighRiskKeyword 提取并编译 negative pattern。

    HighRiskKeyword 上可能没有 negative_patterns 字段（向后兼容）。
    如果后续在模型上增加了该字段，则自动生效。
    """
    neg = getattr(kw, 'negative_patterns', None)
    if neg is None:
        return None
    # 支持两种格式：逗号分隔字符串，或 list[str]
    if isinstance(neg, str):
        parts = [p.strip() for p in neg.split(",") if p.strip()]
    elif isinstance(neg, list):
        parts = [str(p).strip() for p in neg if str(p).strip()]
    else:
        return None
    if not parts:
        return None
    # 用 | 连接所有 negative pattern
    combined = "|".join(parts)
    return _build_negative_pattern(combined)


# ── 注意 ──
# register_log_callbacks() 已被移除。
# 所有注册逻辑现在统一在 hook_registry.py 的 register_admin_hooks() 中：
#   - 回调注册到 HookRegistry
#   - 桥接 registry → LLMClient._log_callback
#   - 桥接 registry → end_node._keyword_check_callback
# 在 main.py lifespan 中仅需调用 register_admin_hooks() 一次。
