"""访问控制：API_TOKEN 鉴权、按 IP 限流、CORS 与安全响应头。"""

from __future__ import annotations

import hmac
import threading
import time
from collections.abc import Sequence

from flask import Request, Response


def supplied_token(request: Request) -> str:
    """依次读取 Authorization: Bearer、X-API-Token 头和 ?token= 参数（浏览器直接打开下载链接时用）。"""
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-API-Token") or request.args.get("token") or "").strip()


def is_authorized(request: Request, token: str) -> bool:
    if not token:
        return True
    return hmac.compare_digest(supplied_token(request).encode(), token.encode())


class RateLimiter:
    """令牌桶：每个 IP 每分钟最多 per_minute 次，允许短时突发到同样的数量。"""

    def __init__(self, per_minute: int):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        if self.capacity <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            allowed = tokens >= 1
            self._buckets[key] = (tokens - 1 if allowed else tokens, now)
            if len(self._buckets) > 10000:  # 清理长时间不活跃的 IP
                cutoff = now - 3600
                self._buckets = {k: v for k, v in self._buckets.items() if v[1] > cutoff}
            return allowed


def apply_cors(request: Request, response: Response, origins: Sequence[str]) -> None:
    """默认不返回跨域头（只允许同源）；CORS_ORIGINS 显式列出的来源才放行。"""
    origin = request.headers.get("Origin")
    if not origin or not origins:
        return
    if "*" in origins:
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers.add("Vary", "Origin")
    else:
        return
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization, X-API-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Expose-Headers"] = "Content-Disposition, X-Download-Filename"
    response.headers["Access-Control-Max-Age"] = "3600"


def apply_security_headers(response: Response) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    # 不把带 ?token= 的地址作为 Referer 发给第三方
    response.headers.setdefault("Referrer-Policy", "no-referrer")
