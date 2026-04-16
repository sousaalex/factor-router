"""
src/gateway/resilience.py

Resilience mechanisms for upstream calls:
- Retry with exponential backoff
- Model fallback (switch to backup model on repeated failures)
- Circuit breaker (temporarily disable failing models)

These mechanisms prevent infinite loops when upstream providers fail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

import httpx

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Circuit Breaker
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CircuitState:
    """Tracks failure state for a single model."""
    failures: int = 0
    opened_at: float | None = None
    last_failure_at: float | None = None


class CircuitBreaker:
    """
    Per-model circuit breaker.
    
    After `max_failures` within `window_seconds`, the circuit opens
    and the model is marked unavailable for `cooldown_seconds`.
    
    After cooldown, the circuit goes to half-open (one probe allowed).
    If the probe succeeds, circuit closes. If it fails, circuit reopens.
    """
    
    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 60.0,
        cooldown_seconds: float = 120.0,
    ):
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._circuits: dict[str, CircuitState] = {}
    
    def is_open(self, model_id: str) -> bool:
        """Return True if the circuit is open (model should be skipped)."""
        state = self._circuits.get(model_id)
        if state is None or state.opened_at is None:
            return False
        
        elapsed = time.monotonic() - state.opened_at
        if elapsed >= self.cooldown_seconds:
            # Cooldown expired — half-open, allow one probe
            return False
        return True
    
    def record_success(self, model_id: str) -> None:
        """Reset failure count on success."""
        if model_id in self._circuits:
            self._circuits[model_id] = CircuitState()
    
    def record_failure(self, model_id: str) -> None:
        """Record a failure. Opens circuit if threshold exceeded."""
        now = time.monotonic()
        state = self._circuits.get(model_id)
        
        if state is None:
            state = CircuitState()
            self._circuits[model_id] = state
        
        # Reset count if last failure was outside the window
        if state.last_failure_at and (now - state.last_failure_at) > self.window_seconds:
            state.failures = 0
        
        state.failures += 1
        state.last_failure_at = now
        
        if state.failures >= self.max_failures and state.opened_at is None:
            state.opened_at = now
            logger.warning(
                "[CircuitBreaker] OPENED for model %s after %d failures in %.0fs — "
                "cooldown %.0fs",
                model_id,
                state.failures,
                self.window_seconds,
                self.cooldown_seconds,
            )
    
    def get_open_models(self) -> list[str]:
        """Return list of currently open (unavailable) model IDs."""
        return [mid for mid in self._circuits if self.is_open(mid)]


# Global circuit breaker instance
_circuit_breaker = CircuitBreaker()


def get_circuit_breaker() -> CircuitBreaker:
    return _circuit_breaker


# ─────────────────────────────────────────────────────────────────────────────
# Retry with Exponential Backoff
# ─────────────────────────────────────────────────────────────────────────────

def _is_retryable_status(status_code: int) -> bool:
    """5xx errors are retryable. 4xx (except 429) are not."""
    return status_code >= 500 or status_code == 429


async def retry_upstream_call(
    func: Callable[..., Coroutine[Any, Any, httpx.Response]],
    *args: Any,
    max_retries: int = 2,
    base_delay: float = 1.0,
    **kwargs: Any,
) -> httpx.Response:
    """
    Call an async function that returns httpx.Response, with retry on 5xx.
    
    Uses exponential backoff: 1s, 2s, 4s...
    Does NOT retry on 4xx (client errors — those are permanent).
    """
    last_exception: Exception | None = None
    
    for attempt in range(max_retries + 1):
        try:
            response = await func(*args, **kwargs)
            
            if response.status_code < 400:
                # Success — circuit breaker should record this
                return response
            
            if not _is_retryable_status(response.status_code):
                # 4xx (not 429) — permanent error, don't retry
                return response
            
            # Retryable status (5xx or 429)
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.info(
                    "[Retry] Attempt %d/%d failed with status %d — retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    response.status_code,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            
            return response  # Last attempt, return whatever we got
            
        except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_exception = e
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.info(
                    "[Retry] Attempt %d/%d raised %s — retrying in %.1fs",
                    attempt + 1,
                    max_retries + 1,
                    type(e).__name__,
                    delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
    
    # Should not reach here, but just in case
    if last_exception:
        raise last_exception
    raise RuntimeError("retry_upstream_call: exhausted all attempts without response")


# ─────────────────────────────────────────────────────────────────────────────
# Model Fallback
# ─────────────────────────────────────────────────────────────────────────────

# Fallback chain: when a model fails, try these in order
_FALLBACK_CHAIN: list[str] = [
    "moonshotai/kimi-k2.5",
    "qwen/qwen3.6-plus",
    "x-ai/grok-4.1-fast",
    "google/gemini-2.5-flash-lite",
]

# Track consecutive failures per model (for fallback triggering)
_model_failure_counts: dict[str, int] = {}
_FALLBACK_THRESHOLD = 2  # After 2 consecutive failures, switch to fallback


def record_model_failure(model_id: str) -> str | None:
    """
    Record a failure for a model. Returns fallback model ID if threshold exceeded.
    Returns None if model should still be tried.
    """
    count = _model_failure_counts.get(model_id, 0) + 1
    _model_failure_counts[model_id] = count
    
    if count >= _FALLBACK_THRESHOLD:
        # Find a fallback that isn't also failing
        for fb in _FALLBACK_CHAIN:
            if fb == model_id:
                continue
            fb_count = _model_failure_counts.get(fb, 0)
            if fb_count < _FALLBACK_THRESHOLD:
                logger.info(
                    "[ModelFallback] Switching from %s (failed %d times) → %s",
                    model_id,
                    count,
                    fb,
                )
                return fb
        
        # All fallbacks are also failing — use the first one anyway
        fallback = _FALLBACK_CHAIN[0] if _FALLBACK_CHAIN else None
        if fallback and fallback != model_id:
            logger.warning(
                "[ModelFallback] All fallbacks failing — using %s anyway",
                fallback,
            )
            return fallback
    
    return None


def record_model_success(model_id: str) -> None:
    """Reset failure count for a model on success."""
    _model_failure_counts.pop(model_id, None)


def get_fallback_model(current_model: str) -> str | None:
    """Get the next fallback model in the chain."""
    try:
        idx = _FALLBACK_CHAIN.index(current_model)
        if idx + 1 < len(_FALLBACK_CHAIN):
            return _FALLBACK_CHAIN[idx + 1]
    except ValueError:
        # Current model not in chain — return first fallback
        pass
    
    return _FALLBACK_CHAIN[0] if _FALLBACK_CHAIN else None


def reset_model_failures(model_id: str | None = None) -> None:
    """Reset failure tracking. If model_id is None, reset all."""
    if model_id:
        _model_failure_counts.pop(model_id, None)
    else:
        _model_failure_counts.clear()
