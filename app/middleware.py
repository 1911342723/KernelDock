"""
HTTP 中间件：CORS、API Key 认证、令牌桶限流

由 main.install_middleware(app) 一次性装配。
"""

import logging
import os
import time
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings

logger = logging.getLogger(__name__)

# /health 与 /metrics 豁免认证，便于探活与采集。
_AUTH_EXEMPT_PATHS = {"/health", "/metrics", "/docs", "/openapi.json", "/redoc"}


def _load_api_keys() -> set:
    raw = getattr(settings, "api_keys", "") or ""
    return {k.strip() for k in raw.split(",") if k.strip()}


def _is_valid_api_key(provided: str, api_keys: set) -> bool:
    """常量时间比对（production-hardening #13）：逐个 compare_digest，避免
    `in` 的短路特性泄露 key 长度/前缀信息（时序侧信道）。key 数量少，开销可忽略。"""
    import secrets

    if not provided:
        return False
    valid = False
    for key in api_keys:
        # 不短路：每个都比完，命中则置位
        if secrets.compare_digest(provided, key):
            valid = True
    return valid


def _extract_client_credential(request) -> str:
    """提取请求方标识：优先 API Key，否则客户端 IP。"""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        key = auth_header[7:].strip()
        if key:
            return key
    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key
    return request.client.host if request.client else "unknown"


class _TokenBucketLimiter:
    """纯内存令牌桶（按 API Key / IP），适合单实例部署。"""

    def __init__(self, per_minute: int, burst: int):
        self._rate = per_minute / 60.0  # tokens per second
        self._capacity = float(burst or per_minute)
        self._buckets: dict = {}  # key -> [tokens, last_ts]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = [self._capacity - 1.0, now]
            return True
        tokens, last = bucket
        tokens = min(self._capacity, tokens + (now - last) * self._rate)
        if tokens < 1.0:
            bucket[0], bucket[1] = tokens, now
            return False
        bucket[0], bucket[1] = tokens - 1.0, now
        return True

    def prune(self, max_entries: int = 10000) -> None:
        """桶数量过多时清掉最久未活动的一半（防内存膨胀）。"""
        if len(self._buckets) <= max_entries:
            return
        items = sorted(self._buckets.items(), key=lambda kv: kv[1][1])
        for key, _ in items[: len(items) // 2]:
            self._buckets.pop(key, None)


def install_middleware(app: FastAPI) -> None:
    """装配 CORS + API Key 认证 + 限流中间件。"""

    # CORS: 显式配置 origin 时才允许携带凭证；未配置时使用 "*" 但禁用凭证
    # （"*" + allow_credentials=True 是无效且危险的组合，浏览器也会拒绝）。
    cors_origins_raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    cors_origins = [o.strip() for o in cors_origins_raw.split(",") if o.strip()]
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    else:
        logger.warning(
            "CORS_ALLOWED_ORIGINS 未配置，使用 '*'（不带凭证）。生产环境请显式配置前端域名。"
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # API Key 认证：配置 SANDBOX_API_KEYS（逗号分隔）后启用；
    # 未配置时放行并告警（开发模式）。
    api_keys = _load_api_keys()
    if not api_keys:
        logger.warning(
            "SANDBOX_API_KEYS 未配置，API 认证已禁用（仅限开发环境！生产必须配置）。"
        )

    # 限流：SANDBOX_RATE_LIMIT_PER_MINUTE > 0 时启用。
    rate_limit_per_minute = int(getattr(settings, "rate_limit_per_minute", 0) or 0)
    rate_limiter: Optional[_TokenBucketLimiter] = None
    if rate_limit_per_minute > 0:
        rate_limiter = _TokenBucketLimiter(
            rate_limit_per_minute,
            int(getattr(settings, "rate_limit_burst", 0) or 0),
        )
        logger.info(f"限流已启用: {rate_limit_per_minute} 次/分钟/客户端")

    @app.middleware("http")
    async def api_key_auth_middleware(request, call_next):
        if request.url.path not in _AUTH_EXEMPT_PATHS:
            # --- 认证 ---
            if api_keys:
                provided = _extract_client_credential(request)
                if not _is_valid_api_key(provided, api_keys):
                    return JSONResponse(
                        status_code=401,
                        content={"detail": "无效或缺失的 API Key"},
                    )
            # --- 限流 ---
            if rate_limiter is not None:
                client_key = _extract_client_credential(request)
                if not rate_limiter.allow(client_key):
                    return JSONResponse(
                        status_code=429,
                        content={"detail": "请求过于频繁，请稍后重试"},
                        headers={"Retry-After": "10"},
                    )
                rate_limiter.prune()
        return await call_next(request)

    # 注意：最后注册 = 最外层（最先执行）。request_id 必须最外层，
    # 这样连认证/限流被拒的响应也带上 X-Request-Id。
    @app.middleware("http")
    async def request_id_middleware(request, call_next):
        # 分布式链路标识（production-hardening #11）：沿用上游（router）传入的
        # X-Request-Id，没有则生成；回写响应头，便于跨 router→node 串联排障。
        import uuid as _uuid

        req_id = request.headers.get("x-request-id") or f"req-{_uuid.uuid4().hex[:12]}"
        request.state.request_id = req_id
        response = await call_next(request)
        response.headers["X-Request-Id"] = req_id
        return response
