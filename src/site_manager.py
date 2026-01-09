"""
站点管理器 v2.1 - 企业级高可用版

特性:
1. 站点切换：主站优先，失败自动切换备用站
2. 并发控制：信号量限制并发请求数
3. 定期 GC：防止内存泄漏
4. 邮件告警：全站不可用时发送通知
5. 模型降级：当模型频繁不可用时自动切换到可用模型
6. 快速失败：所有资源不可用时立即返回错误
7. 熔断器半开状态：冷却期后渐进式恢复
8. 站点优先降级：先换站点再换模型
9. 冷却时间抖动：防止惊群效应

v2.1 新增:
- 快速失败机制 (has_any_available_site, has_any_available_model)
- 熔断器半开状态 (HALF_OPEN)
- 冷却时间随机抖动
- 站点优先降级策略支持
"""

import asyncio
import gc
import os
import random
import time
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from loguru import logger


# ==================== 配置 ====================
SITE_MANAGER_ENABLED = os.getenv("SITE_MANAGER_ENABLED", "true").lower() == "true"
SITE_MAX_CONSECUTIVE_FAILS = int(os.getenv("SITE_MAX_CONSECUTIVE_FAILS", "3"))  # 从5改为3
SITE_COOLDOWN_SECONDS = int(os.getenv("SITE_COOLDOWN_SECONDS", "30"))  # 从60改为30
MAX_CONCURRENT_REQUESTS = int(os.getenv("MAX_CONCURRENT_REQUESTS", "50"))
GC_INTERVAL_SECONDS = int(os.getenv("GC_INTERVAL_SECONDS", "300"))

# 告警配置
ALERT_ENABLED = os.getenv("SITE_ALERT_ENABLED", "true").lower() == "true"
ALERT_COOLDOWN_SECONDS = int(os.getenv("SITE_ALERT_COOLDOWN", "300"))

# 熔断器半开状态配置
HALF_OPEN_ENABLED = os.getenv("HALF_OPEN_ENABLED", "true").lower() == "true"
SITE_HALF_OPEN_DURATION = int(os.getenv("SITE_HALF_OPEN_DURATION", "30"))
SITE_HALF_OPEN_PASS_RATE = float(os.getenv("SITE_HALF_OPEN_PASS_RATE", "0.1"))
SITE_HALF_OPEN_REQUIRED_SUCCESSES = int(os.getenv("SITE_HALF_OPEN_REQUIRED_SUCCESSES", "3"))

# 快速失败配置
FAST_FAIL_ENABLED = os.getenv("FAST_FAIL_ENABLED", "true").lower() == "true"

# 冷却时间抖动配置
COOLDOWN_JITTER_ENABLED = os.getenv("COOLDOWN_JITTER_ENABLED", "true").lower() == "true"
COOLDOWN_JITTER_MAX = int(os.getenv("COOLDOWN_JITTER_MAX", "10"))

# 模型降级配置
MODEL_DEGRADATION_ENABLED = os.getenv("MODEL_DEGRADATION_ENABLED", "true").lower() == "true"
MODEL_DEGRADATION_THRESHOLD = int(os.getenv("MODEL_DEGRADATION_THRESHOLD", "3"))
MODEL_DEGRADATION_COOLDOWN = int(os.getenv("MODEL_DEGRADATION_COOLDOWN", "180"))  # 3分钟上限

# 模型替换开关
MODEL_AUTO_REPLACE_ENABLED = os.getenv("MODEL_AUTO_REPLACE_ENABLED", "true").lower() == "true"

# 立即黑名单冷却时间（指数退避基础值）
MODEL_IMMEDIATE_COOLDOWN = int(os.getenv("MODEL_IMMEDIATE_COOLDOWN", "60"))  # 1分钟基础

# 降级策略配置
DEGRADATION_STRATEGY = os.getenv("DEGRADATION_STRATEGY", "site_first")
MAX_MODEL_FALLBACKS = int(os.getenv("MAX_MODEL_FALLBACKS", "3"))

# 模型优先级列表（按能力从高到低排序）
# 查找策略：先向下降级（cheaper/faster），再向上升级（better/more capable）
# 注意：claude-opus-4-20250514 已从列表移除（上游不支持，返回"请使用 claude-opus-4-1-20250805 模型"）
MODEL_PRIORITY = [
    "claude-opus-4-5-20251101",      # Opus 4.5 - 最强
    "claude-opus-4-1-20250805",      # Opus 4.1
    "claude-sonnet-4-5-20250929",    # Sonnet 4.5
    "claude-sonnet-4-20250514",      # Sonnet 4
    "claude-haiku-4-5-20251001",     # Haiku 4.5 - 最弱但最便宜
]

# 构建模型索引映射（用于快速查找）
MODEL_INDEX_MAP = {model: idx for idx, model in enumerate(MODEL_PRIORITY)}


@dataclass
class SiteStatus:
    """站点状态 - 支持半开状态"""
    name: str
    url: str
    consecutive_fails: int = 0
    total_fails: int = 0
    total_successes: int = 0
    last_fail_time: float = 0
    last_success_time: float = 0
    last_error: str = ""
    is_cooling: bool = False
    cooldown_until: float = 0
    # 半开状态字段
    is_half_open: bool = False
    half_open_until: float = 0
    half_open_success_count: int = 0


@dataclass
class ModelStatus:
    """模型状态"""
    name: str
    consecutive_fails: int = 0      # 连续失败次数（用于阈值触发）
    total_fails: int = 0            # 总失败次数
    blacklist_count: int = 0        # 被加入黑名单的次数（用于指数退避）
    last_fail_time: float = 0
    unavailable_until: float = 0    # 不可用到什么时候（黑名单冷却时间）


class SiteManager:
    """
    简化版站点管理器

    职责：
    1. 管理多站点切换
    2. 控制并发请求数
    3. 定期触发 GC
    4. 发送告警邮件
    5. 模型降级管理
    """

    def __init__(self, sites: List[dict]):
        self.sites = sites
        self.current_site_index = 0

        # 站点状态
        self.site_statuses: Dict[str, SiteStatus] = {
            site["url"]: SiteStatus(name=site["name"], url=site["url"])
            for site in sites
        }

        # 模型状态
        self.model_statuses: Dict[str, ModelStatus] = {}

        # 并发控制
        self._semaphore: Optional[asyncio.Semaphore] = None

        # GC 控制
        self._last_gc_time = time.time()

        # 告警控制
        self._last_alert_time = 0

        # 统计
        self.stats = {
            "total_requests": 0,
            "rejected_by_concurrency": 0,
            "site_switches": 0,
            "gc_count": 0,
            "alert_count": 0,
            "model_degradations": 0,
        }

        logger.info(
            f"[SiteManager] Initialized with {len(sites)} sites, "
            f"max_concurrent={MAX_CONCURRENT_REQUESTS}, "
            f"model_degradation={'enabled' if MODEL_DEGRADATION_ENABLED else 'disabled'}"
        )

    @property
    def semaphore(self) -> asyncio.Semaphore:
        """延迟初始化信号量（必须在事件循环中创建）"""
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        return self._semaphore

    # ==================== 站点管理 ====================

    def get_current_site(self) -> dict:
        """获取当前活跃站点"""
        return self.sites[self.current_site_index]

    def get_site_status(self, site_url: str) -> Optional[SiteStatus]:
        """获取站点状态"""
        return self.site_statuses.get(site_url)

    def is_site_available(self, site_url: str) -> bool:
        """检查站点是否可用（支持半开状态）"""
        status = self.site_statuses.get(site_url)
        if not status:
            return False

        now = time.time()

        # 冷却期内
        if status.is_cooling and now < status.cooldown_until:
            return False

        # 冷却期结束，进入半开状态
        if status.is_cooling and now >= status.cooldown_until:
            status.is_cooling = False
            if HALF_OPEN_ENABLED:
                status.is_half_open = True
                status.half_open_until = now + SITE_HALF_OPEN_DURATION
                status.half_open_success_count = 0
                logger.info(f"[SiteManager] {status.name} entering HALF_OPEN state for {SITE_HALF_OPEN_DURATION}s")
            else:
                # 不启用半开状态，直接恢复
                status.consecutive_fails = 0
                logger.info(f"[SiteManager] {status.name} cooldown ended, back to available")

        # 半开状态处理
        if status.is_half_open and HALF_OPEN_ENABLED:
            if now >= status.half_open_until:
                # 半开超时，检查成功率决定是否完全恢复
                if status.half_open_success_count >= SITE_HALF_OPEN_REQUIRED_SUCCESSES:
                    status.is_half_open = False
                    status.consecutive_fails = 0
                    logger.info(f"[SiteManager] {status.name} fully recovered from HALF_OPEN")
                else:
                    # 重新进入冷却
                    status.is_half_open = False
                    status.is_cooling = True
                    cooldown = self._add_jitter(SITE_COOLDOWN_SECONDS)
                    status.cooldown_until = now + cooldown
                    logger.warning(
                        f"[SiteManager] {status.name} failed HALF_OPEN test "
                        f"({status.half_open_success_count}/{SITE_HALF_OPEN_REQUIRED_SUCCESSES} successes), "
                        f"re-cooling for {cooldown:.1f}s"
                    )
                    return False
            else:
                # 在半开状态中，概率放行
                if random.random() > SITE_HALF_OPEN_PASS_RATE:
                    return False  # 拒绝大部分请求

        return True

    def _add_jitter(self, base_cooldown: int) -> float:
        """添加随机抖动到冷却时间"""
        if not COOLDOWN_JITTER_ENABLED:
            return float(base_cooldown)
        jitter = random.uniform(0, COOLDOWN_JITTER_MAX)
        return base_cooldown + jitter

    def record_site_success(self, site_url: str):
        """记录站点成功（支持半开状态）"""
        status = self.site_statuses.get(site_url)
        if status:
            status.consecutive_fails = 0
            status.total_successes += 1
            status.last_success_time = time.time()

            # 半开状态下的成功计数
            if status.is_half_open:
                status.half_open_success_count += 1
                if status.half_open_success_count >= SITE_HALF_OPEN_REQUIRED_SUCCESSES:
                    status.is_half_open = False
                    logger.info(
                        f"[SiteManager] {status.name} fully recovered "
                        f"({status.half_open_success_count} consecutive successes in HALF_OPEN)"
                    )
            else:
                status.is_cooling = False

        # 检查是否需要定期 GC
        self._check_periodic_gc()

    def record_site_failure(self, site_url: str, error: str = "") -> bool:
        """
        记录站点失败

        Returns:
            bool: True 表示需要切换站点
        """
        status = self.site_statuses.get(site_url)
        if not status:
            return False

        status.consecutive_fails += 1
        status.total_fails += 1
        status.last_fail_time = time.time()
        status.last_error = error[:100] if error else ""

        # 半开状态下的失败，重新进入冷却
        if status.is_half_open:
            status.is_half_open = False
            status.is_cooling = True
            cooldown = self._add_jitter(SITE_COOLDOWN_SECONDS)
            status.cooldown_until = time.time() + cooldown
            logger.warning(
                f"[SiteManager] {status.name} failed in HALF_OPEN state, "
                f"re-cooling for {cooldown:.1f}s"
            )
            self._switch_to_next_available_site()
            return True

        # 检查是否需要切换站点
        if status.consecutive_fails >= SITE_MAX_CONSECUTIVE_FAILS:
            status.is_cooling = True
            cooldown = self._add_jitter(SITE_COOLDOWN_SECONDS)
            status.cooldown_until = time.time() + cooldown
            logger.warning(
                f"[SiteManager] {status.name} reached {status.consecutive_fails} fails, "
                f"cooling down for {cooldown:.1f}s"
            )
            self._switch_to_next_available_site()
            return True

        return False

    def _switch_to_next_available_site(self):
        """切换到下一个可用站点"""
        original_index = self.current_site_index

        for i in range(len(self.sites)):
            next_index = (self.current_site_index + 1 + i) % len(self.sites)
            # 跳过当前站点，避免无效的"切换到自己"
            if next_index == self.current_site_index:
                continue
            site = self.sites[next_index]
            if self.is_site_available(site["url"]):
                old_site = self.sites[self.current_site_index]
                self.current_site_index = next_index
                self.stats["site_switches"] += 1
                logger.warning(
                    f"[SiteManager] Switched from {old_site['name']} to {site['name']}"
                )
                return

        # 所有其他站点都不可用，保持当前站点
        current_site = self.sites[self.current_site_index]
        logger.warning(f"[SiteManager] No other sites available, staying on {current_site['name']}")
        asyncio.create_task(self._send_all_sites_down_alert())

    def force_switch_to_primary(self) -> bool:
        """强制切换回主站"""
        if self.current_site_index == 0:
            return False

        old_site = self.sites[self.current_site_index]
        self.current_site_index = 0
        primary = self.sites[0]

        # 重置主站状态
        status = self.site_statuses.get(primary["url"])
        if status:
            status.is_cooling = False
            status.consecutive_fails = 0

        logger.info(f"[SiteManager] Force switched from {old_site['name']} to {primary['name']}")
        return True

    # ==================== 模型降级管理 ====================

    def _is_model_available(self, model: str) -> bool:
        """
        检查模型是否可用（不在黑名单中）

        Args:
            model: 模型名称

        Returns:
            bool: True 表示模型可用
        """
        status = self.model_statuses.get(model)
        if not status:
            return True  # 没有记录，默认可用

        now = time.time()
        if status.unavailable_until > now:
            return False  # 仍在黑名单冷却期内

        # 冷却期结束，重置状态
        if status.unavailable_until > 0:
            logger.info(
                f"[ModelDegradation] {model} cooldown ended, back to available "
                f"(was blacklisted {status.blacklist_count} times)"
            )
            status.unavailable_until = 0
            status.consecutive_fails = 0
            # 注意：不重置 blacklist_count，保留指数退避状态
            # 只有在模型成功响应后才重置（见 record_model_success）

        return True

    def _find_alternative_model(self, requested_model: str) -> Optional[str]:
        """
        使用双向搜索查找可用的替代模型

        搜索策略：
        1. 先向下搜索（降级到更便宜/更快的模型）
        2. 如果没找到，再向上搜索（升级到更强的模型）

        Args:
            requested_model: 请求的模型名称

        Returns:
            Optional[str]: 找到的替代模型，如果没有可用模型返回 None
        """
        # 检查请求的模型是否在优先级列表中
        if requested_model not in MODEL_INDEX_MAP:
            logger.warning(f"[ModelDegradation] {requested_model} not in MODEL_PRIORITY list")
            return None

        current_idx = MODEL_INDEX_MAP[requested_model]

        # 1. 先向下搜索（降级）
        for idx in range(current_idx + 1, len(MODEL_PRIORITY)):
            candidate = MODEL_PRIORITY[idx]
            if self._is_model_available(candidate):
                return candidate

        # 2. 再向上搜索（升级）
        for idx in range(current_idx - 1, -1, -1):
            candidate = MODEL_PRIORITY[idx]
            if self._is_model_available(candidate):
                return candidate

        # 没有可用的替代模型
        return None

    def get_effective_model(self, requested_model: str) -> str:
        """
        获取实际应使用的模型（考虑降级/替换）

        逻辑：
        1. 如果模型降级功能关闭，直接返回请求的模型
        2. 如果请求的模型可用，直接返回
        3. 如果模型替换功能关闭，直接返回请求的模型（即使不可用）
        4. 使用双向搜索找到可用的替代模型

        Args:
            requested_model: 请求的模型名称

        Returns:
            str: 实际应使用的模型（可能是替换后的模型）
        """
        if not MODEL_DEGRADATION_ENABLED:
            return requested_model

        # 检查请求的模型是否可用
        if self._is_model_available(requested_model):
            return requested_model

        # 请求的模型不可用，检查是否启用自动替换
        if not MODEL_AUTO_REPLACE_ENABLED:
            logger.debug(
                f"[ModelDegradation] {requested_model} unavailable, "
                f"auto-replace disabled, returning original"
            )
            return requested_model

        # 使用双向搜索找替代模型
        alternative = self._find_alternative_model(requested_model)
        if alternative:
            status = self.model_statuses.get(requested_model)
            remaining = int(status.unavailable_until - time.time()) if status else 0
            logger.info(
                f"[ModelDegradation] {requested_model} -> {alternative} "
                f"(unavailable for {remaining}s more)"
            )
            return alternative

        # 没有可用的替代模型，返回原模型让上游处理
        logger.warning(f"[ModelDegradation] No alternative found for {requested_model}")
        return requested_model

    def record_model_success(self, model: str):
        """
        记录模型请求成功

        成功后重置：
        - consecutive_fails: 连续失败计数
        - blacklist_count: 黑名单计数（重置指数退避）
        """
        if not MODEL_DEGRADATION_ENABLED:
            return

        status = self.model_statuses.get(model)
        if status:
            # 如果之前被黑名单过，记录恢复日志
            if status.blacklist_count > 0:
                logger.info(
                    f"[ModelDegradation] {model} recovered successfully, "
                    f"resetting blacklist count from {status.blacklist_count} to 0"
                )
            status.consecutive_fails = 0
            status.blacklist_count = 0  # 重置指数退避

    def get_immediate_fallback(self, model: str) -> Optional[str]:
        """
        立即获取备用模型并将当前模型加入黑名单

        当遇到"负载上限"等确定性错误时调用：
        1. 将模型加入黑名单（使用指数退避冷却时间）
        2. 查找并返回可用的替代模型

        冷却时间策略（指数退避）：
        - 第1次黑名单: 60秒
        - 第2次黑名单: 120秒
        - 第3次黑名单: 240秒
        - 第4次及以后: 300秒（上限）

        Args:
            model: 当前失败的模型

        Returns:
            Optional[str]: 替代模型名称，如果没有可用替代返回 None
        """
        if not MODEL_DEGRADATION_ENABLED or not MODEL_AUTO_REPLACE_ENABLED:
            return None

        # 初始化模型状态
        if model not in self.model_statuses:
            self.model_statuses[model] = ModelStatus(name=model)

        status = self.model_statuses[model]
        status.total_fails += 1
        status.last_fail_time = time.time()

        # 计算冷却时间（指数退避）
        # 第1次: 60s, 第2次: 120s, 第3次: 240s, 第4次+: 300s
        cooldown = min(
            MODEL_IMMEDIATE_COOLDOWN * (2 ** status.blacklist_count),
            MODEL_DEGRADATION_COOLDOWN  # 上限
        )

        # 加入黑名单
        status.unavailable_until = time.time() + cooldown
        status.blacklist_count += 1
        self.stats["model_degradations"] += 1

        logger.warning(
            f"[ModelDegradation] {model} added to blacklist for {cooldown}s "
            f"(attempt #{status.blacklist_count}, total fails: {status.total_fails})"
        )

        # 查找替代模型
        alternative = self._find_alternative_model(model)
        if alternative:
            logger.info(f"[ModelDegradation] Fallback: {model} -> {alternative}")
            return alternative
        else:
            logger.warning(f"[ModelDegradation] No alternative available for {model}")
            return None

    def record_model_load_limit(self, model: str) -> Optional[str]:
        """
        记录模型负载限制错误，触发降级

        当模型连续失败达到阈值时：
        1. 将该模型加入黑名单（设置冷却时间）
        2. 使用双向搜索找到替代模型

        Args:
            model: 遇到负载限制的模型

        Returns:
            Optional[str]: 替代模型名称，如果没有触发降级或没有可用替代返回 None
        """
        if not MODEL_DEGRADATION_ENABLED:
            return None

        # 初始化模型状态
        if model not in self.model_statuses:
            self.model_statuses[model] = ModelStatus(name=model)

        status = self.model_statuses[model]
        status.consecutive_fails += 1
        status.total_fails += 1
        status.last_fail_time = time.time()

        logger.debug(
            f"[ModelDegradation] {model} failed {status.consecutive_fails}/{MODEL_DEGRADATION_THRESHOLD}"
        )

        # 检查是否达到降级阈值
        if status.consecutive_fails >= MODEL_DEGRADATION_THRESHOLD:
            # 将模型加入黑名单
            status.unavailable_until = time.time() + MODEL_DEGRADATION_COOLDOWN
            status.consecutive_fails = 0  # 重置计数，等待冷却期结束
            self.stats["model_degradations"] += 1

            logger.warning(
                f"[ModelDegradation] {model} added to blacklist for {MODEL_DEGRADATION_COOLDOWN}s "
                f"(total fails: {status.total_fails})"
            )

            # 如果启用了自动替换，查找替代模型
            if MODEL_AUTO_REPLACE_ENABLED:
                alternative = self._find_alternative_model(model)
                if alternative:
                    logger.info(f"[ModelDegradation] Fallback: {model} -> {alternative}")
                    return alternative
                else:
                    logger.warning(f"[ModelDegradation] No alternative available for {model}")

        return None

    def get_model_status(self, model: str) -> Optional[dict]:
        """获取模型状态"""
        status = self.model_statuses.get(model)
        if not status:
            return None

        now = time.time()
        return {
            "name": status.name,
            "consecutive_fails": status.consecutive_fails,
            "total_fails": status.total_fails,
            "blacklist_count": status.blacklist_count,
            "is_available": self._is_model_available(model),
            "unavailable_remaining": max(0, int(status.unavailable_until - now)),
        }

    def get_all_model_statuses(self) -> dict:
        """获取所有模型状态（用于调试）"""
        now = time.time()
        return {
            "enabled": MODEL_DEGRADATION_ENABLED,
            "auto_replace": MODEL_AUTO_REPLACE_ENABLED,
            "threshold": MODEL_DEGRADATION_THRESHOLD,
            "cooldown_seconds": MODEL_DEGRADATION_COOLDOWN,
            "priority_list": MODEL_PRIORITY,
            "blacklist": {
                model: {
                    "unavailable_remaining": max(0, int(status.unavailable_until - now)),
                    "total_fails": status.total_fails,
                }
                for model, status in self.model_statuses.items()
                if status.unavailable_until > now
            },
            "all_models": {
                model: self.get_model_status(model)
                for model in self.model_statuses
            }
        }

    # ==================== 快速失败机制 ====================

    def has_any_available_site(self) -> Tuple[bool, int]:
        """
        检查是否有任何可用站点（快速失败检查）

        Returns:
            Tuple[bool, int]: (是否有可用站点, 最短剩余冷却时间秒数)
        """
        if not FAST_FAIL_ENABLED:
            return (True, 0)

        now = time.time()
        min_cooldown = float('inf')

        for site in self.sites:
            status = self.site_statuses.get(site["url"])
            if not status:
                continue

            # 检查是否可用（不在冷却期或已过冷却期）
            if not status.is_cooling or now >= status.cooldown_until:
                return (True, 0)

            # 记录最短冷却时间
            if status.cooldown_until > now:
                remaining = status.cooldown_until - now
                min_cooldown = min(min_cooldown, remaining)

        return (False, int(min_cooldown) if min_cooldown != float('inf') else 30)

    def has_any_available_model(self) -> Tuple[bool, int]:
        """
        检查是否有任何可用模型（快速失败检查）

        Returns:
            Tuple[bool, int]: (是否有可用模型, 最短剩余冷却时间秒数)
        """
        if not FAST_FAIL_ENABLED or not MODEL_DEGRADATION_ENABLED:
            return (True, 0)

        now = time.time()
        min_cooldown = float('inf')

        for model in MODEL_PRIORITY:
            if self._is_model_available(model):
                return (True, 0)

            status = self.model_statuses.get(model)
            if status and status.unavailable_until > now:
                remaining = status.unavailable_until - now
                min_cooldown = min(min_cooldown, remaining)

        return (False, int(min_cooldown) if min_cooldown != float('inf') else 30)

    def get_available_sites(self) -> List[dict]:
        """获取所有可用站点列表"""
        return [site for site in self.sites if self.is_site_available(site["url"])]

    def get_model_fallback_list(self, requested_model: str) -> List[str]:
        """
        获取模型降级列表（用于站点优先降级策略）

        Args:
            requested_model: 请求的模型

        Returns:
            List[str]: 模型列表，按优先级排序（原模型在前）
        """
        result = []

        # 首先加入原模型（如果可用）
        if self._is_model_available(requested_model):
            result.append(requested_model)

        # 然后添加降级模型
        if MODEL_AUTO_REPLACE_ENABLED:
            count = 0
            for model in MODEL_PRIORITY:
                if model == requested_model:
                    continue
                if self._is_model_available(model):
                    result.append(model)
                    count += 1
                    if count >= MAX_MODEL_FALLBACKS:
                        break

        # 如果原模型不可用但也没有其他选择，仍然返回原模型
        if not result:
            result.append(requested_model)

        return result

    # ==================== 并发控制 ====================

    async def acquire(self) -> bool:
        """
        尝试获取请求许可

        Returns:
            bool: True 表示可以处理请求
        """
        self.stats["total_requests"] += 1

        # 检查并发限制
        if self.semaphore.locked():
            self.stats["rejected_by_concurrency"] += 1
            logger.warning(f"[SiteManager] Concurrency limit ({MAX_CONCURRENT_REQUESTS}) reached")
            return False

        return True

    # ==================== GC 管理 ====================

    def _check_periodic_gc(self):
        """检查是否需要定期 GC"""
        if time.time() - self._last_gc_time > GC_INTERVAL_SECONDS:
            self._trigger_gc("periodic")

    def _trigger_gc(self, reason: str):
        """触发垃圾回收"""
        try:
            collected = gc.collect()
            self._last_gc_time = time.time()
            self.stats["gc_count"] += 1
            logger.info(f"[GC] Triggered ({reason}), collected {collected} objects")
        except Exception as e:
            logger.warning(f"[GC] Failed: {e}")

    def force_gc(self):
        """手动触发 GC"""
        self._trigger_gc("manual")

    # ==================== 告警 ====================

    async def _send_all_sites_down_alert(self):
        """发送全站不可用告警"""
        if not ALERT_ENABLED:
            return

        now = time.time()
        if now - self._last_alert_time < ALERT_COOLDOWN_SECONDS:
            logger.debug(f"[SiteManager] Alert cooldown, skipping")
            return

        self._last_alert_time = now
        self.stats["alert_count"] += 1

        try:
            from email_service import send_email, EMAIL_NOTIFY_ENABLED

            if not EMAIL_NOTIFY_ENABLED:
                return

            alert_time = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")

            # 构建站点状态
            site_rows = ""
            for site in self.sites:
                status = self.site_statuses.get(site["url"])
                if status:
                    state = "冷却中" if status.is_cooling else "可用"
                    color = "#e74c3c" if status.is_cooling else "#27ae60"
                    site_rows += f"""
                    <tr>
                        <td style="padding: 10px; border-bottom: 1px solid #eee;">{status.name}</td>
                        <td style="padding: 10px; border-bottom: 1px solid #eee; color: {color};">{state}</td>
                        <td style="padding: 10px; border-bottom: 1px solid #eee;">{status.consecutive_fails}</td>
                        <td style="padding: 10px; border-bottom: 1px solid #eee;">{status.last_error or '-'}</td>
                    </tr>
                    """

            html = f"""
            <html>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <div style="max-width: 600px; margin: 0 auto; background: #fff; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1);">
                    <div style="background: #e74c3c; color: white; padding: 20px; border-radius: 8px 8px 0 0;">
                        <h2 style="margin: 0;">🚨 站点告警</h2>
                        <p style="margin: 10px 0 0 0;">所有上游站点不可用</p>
                    </div>
                    <div style="padding: 20px;">
                        <p><strong>告警时间：</strong>{alert_time}</p>
                        <table style="width: 100%; border-collapse: collapse;">
                            <tr style="background: #f8f9fa;">
                                <th style="padding: 10px; text-align: left;">站点</th>
                                <th style="padding: 10px; text-align: left;">状态</th>
                                <th style="padding: 10px; text-align: left;">失败次数</th>
                                <th style="padding: 10px; text-align: left;">最后错误</th>
                            </tr>
                            {site_rows}
                        </table>
                        <p style="color: #666; margin-top: 20px;">
                            系统将在冷却期({SITE_COOLDOWN_SECONDS}秒)后自动重试。
                        </p>
                    </div>
                </div>
            </body>
            </html>
            """

            subject = f"【AnyRouter告警】所有站点不可用 - {datetime.now().strftime('%m月%d日 %H:%M')}"
            send_email(subject, html, html=True)
            logger.info("[SiteManager] Alert email sent")

        except Exception as e:
            logger.error(f"[SiteManager] Failed to send alert: {e}")

    # ==================== 状态查询 ====================

    def get_status(self) -> dict:
        """获取完整状态"""
        now = time.time()
        current_site = self.get_current_site()

        # 快速失败状态
        has_site, site_retry = self.has_any_available_site()
        has_model, model_retry = self.has_any_available_model()

        return {
            "enabled": SITE_MANAGER_ENABLED,
            "version": "v2.1",  # 版本标记
            "current_site": current_site["name"],
            "current_site_url": current_site["url"],
            "stats": self.stats,
            "last_gc_seconds_ago": int(now - self._last_gc_time),
            "gc_interval": GC_INTERVAL_SECONDS,
            "max_concurrent": MAX_CONCURRENT_REQUESTS,

            # v2.1 新增：快速失败状态
            "fast_fail": {
                "enabled": FAST_FAIL_ENABLED,
                "any_site_available": has_site,
                "any_model_available": has_model,
                "site_retry_after": site_retry if not has_site else 0,
                "model_retry_after": model_retry if not has_model else 0,
            },

            # v2.1 新增：熔断器半开状态配置
            "half_open": {
                "enabled": HALF_OPEN_ENABLED,
                "duration": SITE_HALF_OPEN_DURATION,
                "pass_rate": SITE_HALF_OPEN_PASS_RATE,
                "required_successes": SITE_HALF_OPEN_REQUIRED_SUCCESSES,
            },

            # v2.1 新增：降级策略
            "degradation_strategy": DEGRADATION_STRATEGY,

            "model_degradation": self.get_all_model_statuses(),
            "sites": {
                site["name"]: {
                    "url": site["url"],
                    "is_current": site["url"] == current_site["url"],
                    "is_available": self.is_site_available(site["url"]),
                    "consecutive_fails": self.site_statuses[site["url"]].consecutive_fails,
                    "total_fails": self.site_statuses[site["url"]].total_fails,
                    "total_successes": self.site_statuses[site["url"]].total_successes,
                    "is_cooling": self.site_statuses[site["url"]].is_cooling,
                    "is_half_open": self.site_statuses[site["url"]].is_half_open,
                    "half_open_successes": self.site_statuses[site["url"]].half_open_success_count,
                    "cooldown_remaining": max(0, int(self.site_statuses[site["url"]].cooldown_until - now))
                        if self.site_statuses[site["url"]].is_cooling else 0,
                    "half_open_remaining": max(0, int(self.site_statuses[site["url"]].half_open_until - now))
                        if self.site_statuses[site["url"]].is_half_open else 0,
                    "last_error": self.site_statuses[site["url"]].last_error,
                }
                for site in self.sites
            }
        }

    def reset_all(self):
        """重置所有状态"""
        for status in self.site_statuses.values():
            status.consecutive_fails = 0
            status.is_cooling = False
            status.cooldown_until = 0
            status.is_half_open = False
            status.half_open_until = 0
            status.half_open_success_count = 0

        for status in self.model_statuses.values():
            status.consecutive_fails = 0
            status.blacklist_count = 0
            status.unavailable_until = 0

        self.current_site_index = 0
        self._trigger_gc("manual_reset")
        logger.info("[SiteManager] All states reset")


# 全局实例（在应用启动时初始化）
_site_manager: Optional[SiteManager] = None


def init_site_manager(sites: List[dict]) -> SiteManager:
    """初始化站点管理器"""
    global _site_manager
    _site_manager = SiteManager(sites)
    return _site_manager


def get_site_manager() -> Optional[SiteManager]:
    """获取站点管理器实例"""
    return _site_manager
