"""按 API Key 维度的并发限制（进程内内存，不落库）。

需求（2026-11-26）：
* 以单个 API Key 为维度，统计「正在等待上游响应」的请求数；
* 通过配置项设置每个 Key 的最大并发数；
* 达到上限后新请求不再调用上游，直接返回友好限流提示；
* 请求成功、失败、超时（含客户端断开导致的流式中断）后及时释放；
* 不同 Key 之间互不影响。

实现要点
--------
* 用 ``threading.Lock`` 保护计数（与 :mod:`app.core.live` 风格一致），
  当前部署为单 worker 单进程；多 worker 时需换 Redis 等共享存储。
* 计数以 ``token``（通常是 request_id）为身份的 set，``release`` 用
  ``discard`` 保证幂等——失败出口可能多处调用 release（``_fail``、
  handle_chat finally、流式 wrapped finally），重复调用不会误减其他请求。
* ``limit <= 0`` 表示不限制：不登记、不计数，``release`` 为 no-op。
* 上限取值优先级：Key 自身的 ``max_concurrency`` > 全局
  ``settings.api_key_max_concurrency``；两者均为 0 表示不限。
"""
from __future__ import annotations

import threading
from collections import defaultdict

from app.core.config import settings
from app.core.models import ApiKey


class KeyConcurrencyLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # key_id -> 进行中的 token 集合
        self._active: dict[str, set[str]] = defaultdict(set)

    def try_acquire(self, key_id: str, token: str, limit: int) -> bool:
        """尝试占用一个并发槽位；成功 True，已达上限 False。

        ``limit <= 0`` 时永远成功且不登记（不限流）。同一 token 重复 acquire
        视为同一个请求，不重复计数。
        """
        if limit <= 0:
            return True
        with self._lock:
            active = self._active[key_id]
            if token in active:
                return True
            if len(active) >= limit:
                return False
            active.add(token)
            return True

    def release(self, key_id: str, token: str) -> None:
        """释放槽位，幂等。未占用或已释放时无副作用。"""
        with self._lock:
            active = self._active.get(key_id)
            if not active:
                return
            active.discard(token)
            if not active:
                self._active.pop(key_id, None)

    def in_use(self, key_id: str) -> int:
        """该 Key 当前进行中的请求数。"""
        with self._lock:
            return len(self._active.get(key_id, ()))

    def snapshot(self) -> dict[str, int]:
        """按 Key 聚合的当前占用（管理/排障用）。"""
        with self._lock:
            return {kid: len(tokens) for kid, tokens in self._active.items() if tokens}


def effective_max_concurrency(key: ApiKey | None) -> int:
    """计算某个 Key 的生效并发上限；0 表示不限。"""
    if key is None:
        return int(getattr(settings, "api_key_max_concurrency", 0) or 0)
    own = int(getattr(key, "max_concurrency", 0) or 0)
    if own > 0:
        return own
    return int(getattr(settings, "api_key_max_concurrency", 0) or 0)


# 进程内全局单例：LLM 与 OCR 共用，同一 Key 跨服务共享计数
key_concurrency = KeyConcurrencyLimiter()
