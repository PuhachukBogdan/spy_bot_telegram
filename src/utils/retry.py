"""Retry wrappers for transient LLM/API failures. Phase 8.

OpenRouter (and the upstream model providers) occasionally return transient
errors — connection drops, timeouts, 429 rate limits, 5xx. Those are worth a
bounded exponential-backoff retry; everything else (bad request, auth, schema
validation) is a hard failure that retrying cannot fix, so it propagates
immediately.

Usage::

    from src.utils.retry import with_llm_retry

    @with_llm_retry()
    async def _call() -> ...:
        return await client.chat.completions.create(...)

Backoff is bounded and ``reraise=True`` so the final attempt's real exception
surfaces (not tenacity's ``RetryError``), which keeps caller error handling and
the LLM audit trail honest.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.utils.logging import get_logger

log = get_logger(__name__)

# Transient API errors worth retrying. Auth / bad-request / validation errors are
# deliberately excluded — they are deterministic and retrying only wastes budget.
TRANSIENT_LLM_ERRORS: tuple[type[Exception], ...] = (
    APIConnectionError,
    APITimeoutError,
    RateLimitError,
    InternalServerError,
)

_T = TypeVar("_T")

# Defaults: 3 attempts, exponential backoff 1s -> 2s -> 4s (capped at 10s).
DEFAULT_ATTEMPTS = 3
_WAIT_MULTIPLIER = 1.0
_WAIT_MAX = 10.0


def with_llm_retry(
    attempts: int = DEFAULT_ATTEMPTS,
) -> Callable[[Callable[..., Awaitable[_T]]], Callable[..., Awaitable[_T]]]:
    """Decorate an async call with bounded exponential-backoff retry.

    Retries only on :data:`TRANSIENT_LLM_ERRORS`; re-raises the underlying
    exception after the last attempt.
    """

    def decorator(fn: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
        wrapped: Callable[..., Awaitable[_T]] = retry(
            retry=retry_if_exception_type(TRANSIENT_LLM_ERRORS),
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=_WAIT_MULTIPLIER, max=_WAIT_MAX),
            before_sleep=_log_before_sleep,
            reraise=True,
        )(fn)
        return wrapped

    return decorator


def _log_before_sleep(retry_state: Any) -> None:
    """Structured log line emitted before each backoff sleep."""
    exc = retry_state.outcome.exception() if retry_state.outcome is not None else None
    log.warning(
        "llm.retry",
        attempt=retry_state.attempt_number,
        error=type(exc).__name__ if exc is not None else None,
    )
