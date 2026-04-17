from __future__ import annotations
"""
core/retry.py
Exponential backoff retry decorator with jitter.

Wraps both sync and async agent/tool functions.
Only retries on transient errors (timeout, rate-limit, connection).
Fails fast on fatal errors (auth failure, not-found, etc.).

Usage:
    @with_retry(max_attempts=3, base_delay=1.0)
    async def call_groq_api(...):
        ...
"""

import asyncio
import random
import logging
from functools import wraps
from typing import Callable, Type

log = logging.getLogger("frame_mo.retry")

# ── Retriable error types ────────────────────────────────────────────────────
# Add SDK-specific exceptions here as needed (e.g., groq.RateLimitError)
RETRIABLE_EXCEPTIONS: tuple[Type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    ConnectionResetError,
    OSError,
)

# Strings in exception messages that signal a rate-limit (429)
RATE_LIMIT_SIGNALS = ("rate limit", "429", "too many requests", "quota")


class AgentStepError(Exception):
    """Raised when an agent step exhausts all retries."""
    pass


def _is_rate_limit(exc: Exception) -> bool:
    return any(s in str(exc).lower() for s in RATE_LIMIT_SIGNALS)


def _is_retriable(exc: Exception) -> bool:
    return isinstance(exc, RETRIABLE_EXCEPTIONS) or _is_rate_limit(exc)


# ── Async retry decorator ────────────────────────────────────────────────────

def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    jitter_factor: float = 0.2,
) -> Callable:
    """
    Async retry decorator with exponential backoff + jitter.

    Args:
        max_attempts:  Total attempts before raising AgentStepError.
        base_delay:    Initial wait in seconds (doubles each attempt).
        max_delay:     Cap on wait time (seconds).
        jitter_factor: Adds ±(jitter_factor * delay) randomness to prevent
                       thundering herd when multiple agents retry simultaneously.
    """
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_error: Exception | None = None

            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)

                except Exception as exc:
                    # Non-retriable — fail immediately
                    if not _is_retriable(exc):
                        log.error(
                            f"[retry] Fatal error in {func.__name__} "
                            f"(attempt {attempt + 1}) — not retrying: {exc}"
                        )
                        raise

                    last_error = exc

                    if attempt == max_attempts - 1:
                        break   # Last attempt — fall through to raise

                    # Compute backoff: base * 2^attempt ± jitter
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    jitter = random.uniform(-jitter_factor * delay,
                                           jitter_factor * delay)
                    wait = max(0.1, delay + jitter)

                    log.warning(
                        f"[retry] {func.__name__} failed "
                        f"(attempt {attempt + 1}/{max_attempts}) — "
                        f"waiting {wait:.2f}s. Error: {exc}"
                    )
                    await asyncio.sleep(wait)

            raise AgentStepError(
                f"[{func.__name__}] Max retries ({max_attempts}) exceeded. "
                f"Last error: {last_error}"
            )

        return wrapper
    return decorator


# ── Sync retry (for non-async tool calls) ────────────────────────────────────

def with_retry_sync(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
) -> Callable:
    """Synchronous version of with_retry for non-async functions."""
    import time

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_error: Exception | None = None

            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if not _is_retriable(exc):
                        raise
                    last_error = exc
                    if attempt < max_attempts - 1:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        jitter = random.uniform(0, delay * 0.2)
                        wait = delay + jitter
                        log.warning(
                            f"[retry_sync] {func.__name__} failed "
                            f"(attempt {attempt + 1}) — waiting {wait:.2f}s"
                        )
                        time.sleep(wait)

            raise AgentStepError(
                f"[{func.__name__}] Max retries exceeded. Last: {last_error}"
            )

        return wrapper
    return decorator
