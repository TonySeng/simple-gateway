"""进行中请求的实时登记表（进程内内存，不落库）。

作用：后台「实时监控」页要能看到「已经到站、还没返回」的请求 —— 这类请求
在 ``request_logs`` 里根本不存在（行是结束后才写的），只能靠内存登记。

约定：
* 请求进入转发逻辑时 ``start()``，结束（成功/失败/客户端断开）时 ``finish()``；
* ``finish`` 幂等，重复调用无副作用；
* 只保留最近 ``MAX_AGE_S`` 秒内**有过更新**的条目，防止异常路径漏掉 ``finish`` 时无限增长；
  判定基准是「最后一次 update 时间」而非入口时间 —— 否则一个正常跑了半小时的流式请求
  会被误判为僵死而消失；
* 单进程内存表：多 worker 部署时各自只看得到自己进程的请求（当前部署为单 worker）。
"""
from __future__ import annotations

import threading
import time
from typing import Any

# 超过这个时长仍未结束，视为僵死条目（客户端断开且生成器未走到 finally 的极端情况）
MAX_AGE_S = 1800
# UA 只留这么长，够识别客户端即可
UA_LIMIT = 200

STAGE_LABELS = {
    "routing": "路由中",
    "reviewing": "安全审查中",
    "connecting": "等待上游",
    "streaming": "流式接收中",
    "writing": "回传中",
}


class LiveRegistry:
    def __init__(self) -> None:
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ---------- 写入 ----------
    def start(
        self,
        request_id: str,
        *,
        service: str,
        endpoint: str,
        key_id: str = "",
        key_name: str = "",
        model: str = "",
        provider_name: str = "",
        provider_type: str = "",
        client_ip: str = "",
        user_agent: str = "",
        stream: bool = False,
    ) -> None:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            self._items[request_id] = {
                "request_id": request_id,
                "service": service,
                "endpoint": endpoint,
                "key_id": key_id,
                "key_name": key_name,
                "model": model,
                "provider_name": provider_name,
                "provider_type": provider_type,
                "client_ip": client_ip,
                "user_agent": (user_agent or "")[:UA_LIMIT],
                "stream": bool(stream),
                "stage": "routing",
                "upstream_status": None,
                "started_at": now,
                "updated_at": now,
            }

    def update(self, request_id: str, **fields: Any) -> None:
        with self._lock:
            item = self._items.get(request_id)
            if item is None:
                return
            for k, v in fields.items():
                if k in item:
                    item[k] = v
            # 刷新存活时间：僵尸判定只看「多久没动静」，不看「跑了多久」
            item["updated_at"] = time.time()

    def finish(self, request_id: str) -> None:
        with self._lock:
            self._items.pop(request_id, None)

    # ---------- 读取 ----------
    def snapshot(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            rows = [dict(v) for v in self._items.values()]
        for r in rows:
            r["elapsed_ms"] = int((now - r["started_at"]) * 1000)
            r["stage_label"] = STAGE_LABELS.get(r["stage"], r["stage"])
            # 给前端一个时间戳，由浏览器按本地时区格式化（避免服务端/客户端时区错位）
            r["started_at_ms"] = int(r["started_at"] * 1000)
        rows.sort(key=lambda r: r["started_at"])
        return rows

    def count(self) -> int:
        with self._lock:
            self._prune_locked(time.time())
            return len(self._items)

    def _prune_locked(self, now: float) -> None:
        if not self._items:
            return
        dead = [
            rid
            for rid, it in self._items.items()
            if now - it.get("updated_at", it["started_at"]) > MAX_AGE_S
        ]
        for rid in dead:
            self._items.pop(rid, None)


live = LiveRegistry()
