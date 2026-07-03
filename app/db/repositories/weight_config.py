"""
WeightConfigRepository — 评分权重配置的加载、缓存与 A/B 路由。

这是评分子系统的"数据源"——从 PostgreSQL weights_config 表加载
当前生效的权重配置，并提供给 ScoringPipeline 用于加权评分。

关键设计：
  1. TTL 缓存（60 秒）— 避免每次推荐都查数据库
  2. A/B 路由 — 按 session_id 哈希分桶，支持多版本权重并行对比
  3. 版本管理 — 支持多版本配置、激活/切换、历史追溯

权重配置示例（JSON 存储在 weights 字段）：
  {
    "symptom_match": 30,     ← 症状匹配权重（相对值，内部归一化）
    "safety": 25,            ← 安全性权重
    "age_suitability": 20,   ← 年龄适用性权重
    "otc_safety_level": 10,  ← OTC 等级权重
    "ingredient_coverage": 10,← 成分覆盖度权重
    "evidence_quality": 5    ← 证据质量权重（预留）
  }

数据表结构：
  weights_config:
    id (PK), version (unique), policy, weights (JSON),
    feature_defaults (JSON), safety_block_threshold,
    is_active, ab_group, ab_ratio, created_at
"""

import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import WeightConfig


# ═══════════════════════════════════════════════════════════════
# 模块级 TTL 缓存
# ═══════════════════════════════════════════════════════════════
# 为什么需要缓存？
#   每次推荐都会调用 get_active()，如果用户频繁发消息，每次查 DB 是浪费。
#   用模块级变量存最近一次查询结果 + 时间戳，60 秒内命中直接返回。
#
# 为什么不用 Redis？
#   权重配置是低频变更的数据（几天甚至几周才改一次），60 秒的
#   内存缓存足够。加 Redis 反而增加运维复杂度。
#
# 缓存失效：
#   - 60 秒 TTL 自动过期
#   - set_active() 切换版本时强制刷新

_cache_active: tuple[float, WeightConfig | None] = (0.0, None)
"""缓存：(最近查询时间戳, 缓存的主配置)"""

_CACHE_TTL_SECONDS = 60
"""缓存有效期（秒）。60 秒内对 get_active() 的重复调用直接返回缓存"""


# ═══════════════════════════════════════════════════════════════
# A/B 分桶工具
# ═══════════════════════════════════════════════════════════════

def _hash_bucket(session_id: str, num_buckets: int = 100) -> int:
    """将 session_id 哈希到 0~(num_buckets-1) 的确定性桶号。

    为什么需要分桶？
      运营人员想测试"症状权重 30% vs 50%"哪个推荐效果更好，
      在 DB 中插入两条 is_active=True 的配置（ab_group="A" 和 "B"），
      系统按 session_id 的哈希值把 50% 用户分到 A、50% 分到 B。

    确定性保证：
      同一个 session_id 永远落在同一个桶 → 用户不会在中途切换权重版本。

    Args:
        session_id:  会话 UUID（如 "f45c6a16-9754-4306-99c7-a537595160fb"）
        num_buckets: 桶总数，默认 100（粒度 1%）

    Returns:
        0~99 之间的整数桶号
    """
    h = hash(session_id) % num_buckets
    return abs(h)


# ═══════════════════════════════════════════════════════════════
# 仓库类
# ═══════════════════════════════════════════════════════════════

class WeightConfigRepository:
    """权重配置仓库 — 加载、缓存、A/B 路由。

    使用方式：
        repo = WeightConfigRepository(db)
        config = await repo.get_active(session_id)
        # config.weights = {"symptom_match": 30, "safety": 25, ...}
        # config.safety_block_threshold = 0.2
    """

    def __init__(self, db: AsyncSession):
        """初始化。

        Args:
            db: 已绑定的异步数据库会话
        """
        self.db = db

    async def get_active(self, session_id: str = "") -> WeightConfig:
        """获取当前激活的权重配置，含 A/B 路由。

        这是评分子系统最常调用的方法。流程：
          1. 检查模块级 TTL 缓存（命中则直接返回，避免 DB 查询）
          2. 缓存未命中 → 查询 DB 中所有 is_active=True 的配置
          3. 如果只有一条 → 直接返回
          4. 如果多条（不同 ab_group）→ 按 session_id 哈希分桶路由
          5. 更新缓存

        A/B 路由逻辑：
          假设有两条活跃配置：
            - config_A: ab_group="A", ab_ratio=0.3  → 30% 流量
            - config_B: ab_group="B", ab_ratio=0.3  → 30% 流量
          剩余的 40% 走不带 ab_group 的默认配置。

          路由算法：
            bucket = hash(session_id) % 100  （0~99）
            if bucket < 30    → 走 A 配置
            if bucket >= 70   → 走 B 配置（即 bucket >= (1-ratio)*100）
            else              → 走默认配置

        Args:
            session_id: 会话 UUID。空字符串时返回主配置（不做 A/B 路由）

        Returns:
            当前生效的 WeightConfig ORM 实例

        Raises:
            ValueError: 数据库中没有 is_active=True 的配置（部署问题）
        """
        global _cache_active
        cache_time, cached_config = _cache_active

        # ── 缓存命中 ──
        if cached_config is not None and (time.monotonic() - cache_time) < _CACHE_TTL_SECONDS:
            return self._apply_ab_routing(cached_config, session_id)

        # ── 缓存未命中 → 查询数据库 ──
        # 查询所有 is_active=True 的配置，按创建时间降序（最新的在前）
        stmt = (
            select(WeightConfig)
            .where(WeightConfig.is_active == True)
            .order_by(WeightConfig.created_at.desc())
        )
        result = await self.db.execute(stmt)
        active_configs = list(result.scalars().all())

        if not active_configs:
            raise ValueError("No active weight config found in weights_config table.")

        # 取最新的一条作为主配置，更新缓存
        primary = active_configs[0]
        _cache_active = (time.monotonic(), primary)

        return self._apply_ab_routing(primary, session_id, active_configs)

    def _apply_ab_routing(
        self,
        primary: WeightConfig,
        session_id: str,
        all_configs: list[WeightConfig] | None = None,
    ) -> WeightConfig:
        """根据 session hash 做 A/B 路由，决定使用哪个权重配置。

        路由优先级：
          1. 如果只有一条活跃配置 → 直接返回（无 A/B 测试）
          2. 如果多条且 session 落入 A 桶 → 返回 A 配置
          3. 如果多条且 session 落入 B 桶 → 返回 B 配置
          4. 如果都不命中 → 返回不带 ab_group 的默认配置

        Args:
            primary:      主配置（缓存中最新的那条）
            session_id:   会话 UUID
            all_configs:  所有活跃配置列表（缓存未命中时从 DB 查的）

        Returns:
            路由到的 WeightConfig
        """
        # 只有一条配置 → 无需 A/B 路由，直接返回
        if all_configs and len(all_configs) > 1:
            ab_configs = [c for c in all_configs if c.ab_group]
            if ab_configs:
                # 按 session_id 计算桶号
                bucket = _hash_bucket(session_id, 100)  # 0~99

                for config in ab_configs:
                    ratio = config.ab_ratio or 0.5  # 默认 50%
                    if config.ab_group == "A" and bucket < int(ratio * 100):
                        # A 组：桶号在 [0, ratio*100) 区间 → 走 A 配置
                        return config
                    elif config.ab_group == "B" and bucket >= int((1 - ratio) * 100):
                        # B 组：桶号在 [(1-ratio)*100, 100) 区间 → 走 B 配置
                        return config

                # 桶号不在 A/B 范围内 → 走默认（非 AB）配置
                non_ab = [c for c in all_configs if not c.ab_group]
                if non_ab:
                    return non_ab[0]

        # 默认返回主配置
        return primary

    async def get_version(self, version: str) -> WeightConfig | None:
        """按版本号精确查找配置。

        Args:
            version: 版本号字符串，如 "v3.2.1"

        Returns:
            WeightConfig 或 None（版本不存在）
        """
        stmt = select(WeightConfig).where(WeightConfig.version == version)
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def list_versions(self) -> list[WeightConfig]:
        """列出所有权重配置版本，最新的在前。"""
        stmt = select(WeightConfig).order_by(WeightConfig.created_at.desc())
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def insert(self, config: WeightConfig) -> WeightConfig:
        """新增一个权重配置。

        注意：新配置默认 is_active=False，需调用 set_active() 激活。

        Args:
            config: WeightConfig ORM 实例

        Returns:
            已 flush 的配置（含自增 ID）
        """
        self.db.add(config)
        await self.db.flush()
        return config

    async def set_active(self, version: str) -> None:
        """激活指定版本，同时停用所有其他版本。

        这是一个"开关"操作：
          1. 遍历所有配置，全部设为 is_active=False
          2. 找到目标版本，设为 is_active=True
          3. 强制刷新模块级缓存（使下次 get_active() 立即生效）

        Args:
            version: 要激活的版本号

        Raises:
            ValueError: 版本号不存在
        """
        # 全部停用
        all_configs = await self.list_versions()
        for c in all_configs:
            c.is_active = False

        # 激活目标
        target = await self.get_version(version)
        if target is None:
            raise ValueError(f"Version not found: {version}")
        target.is_active = True
        await self.db.flush()

        # 强制刷新缓存 — 让切换立即生效
        global _cache_active
        _cache_active = (0.0, target)
