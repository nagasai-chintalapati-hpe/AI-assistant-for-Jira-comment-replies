"""Webhook HMAC signature verification and per-IP rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time as _time
from collections import defaultdict
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)


# HMAC webhook signature verification 


def _verify_signature(body: bytes, signature_header: Optional[str]) -> bool:
    """Verify the Jira webhook HMAC-SHA256 signature."""
    if not settings.webhook.validate_signature or not settings.webhook.secret:
        return True
    if not signature_header:
        logger.warning("Webhook received without X-Hub-Signature header — rejecting")
        return False
    try:
        scheme, provided = signature_header.split("=", 1)
        if scheme != "sha256":
            return False
    except ValueError:
        return False
    expected = hmac.new(
        settings.webhook.secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, provided)


# Per-IP rate limiter


class _RateLimiter:
    """Per-IP rate limiter — Redis-backed or in-memory fallback."""

    def __init__(self, rpm: int = 60) -> None:
        self._rpm = rpm
        self._counts: dict[str, list[float]] = defaultdict(list)
        self._redis = self._init_redis()

    def _init_redis(self):
        """Try to connect to Redis; return client or ``None`` on failure."""
        if not settings.redis.enabled:
            return None
        try:
            import redis  # type: ignore[import]

            url = settings.redis.url or (
                f"redis://:{settings.redis.password}@{settings.redis.host}"
                f":{settings.redis.port}/{settings.redis.db}"
                if settings.redis.password
                else f"redis://{settings.redis.host}:{settings.redis.port}/{settings.redis.db}"
            )
            client = redis.from_url(url, socket_connect_timeout=2)
            client.ping()
            logger.info(
                "Redis rate limiter connected (%s:%s)",
                settings.redis.host,
                settings.redis.port,
            )
            return client
        except ImportError:
            logger.warning(
                "redis-py not installed — using in-memory rate limiter "
                "(install redis for multi-process HA)"
            )
        except Exception as exc:
            logger.warning(
                "Redis unavailable (%s) — falling back to in-memory rate limiter", exc
            )
        return None

    def is_allowed(self, key: str) -> bool:
        if not settings.rate_limit.enabled:
            return True
        if self._redis is not None:
            return self._is_allowed_redis(key)
        return self._is_allowed_memory(key)

    def _is_allowed_redis(self, key: str) -> bool:
        """Sliding-window rate limit using a Redis sorted set."""
        try:
            import time as _time_module

            now = _time_module.time()
            window_key = f"rl:{key}"
            pipe = self._redis.pipeline()
            pipe.zremrangebyscore(window_key, 0, now - 60)
            pipe.zcard(window_key)
            pipe.zadd(window_key, {str(now): now})
            pipe.expire(window_key, 120)
            _, count, *_ = pipe.execute()
            return int(count) < self._rpm
        except Exception as exc:
            logger.warning(
                "Redis rate-limit check failed (%s) — allowing request", exc
            )
            return True

    def _is_allowed_memory(self, key: str) -> bool:
        now = _time.monotonic()
        window_start = now - 60.0
        self._counts[key] = [t for t in self._counts[key] if t > window_start]
        if len(self._counts[key]) >= self._rpm:
            return False
        self._counts[key].append(now)
        return True
