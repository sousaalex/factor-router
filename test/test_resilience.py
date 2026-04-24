"""
Testes unitários para o módulo src/gateway/resilience.py

Circuit Breaker, Retry com Backoff, e Model Fallback.
"""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.gateway.resilience import (
    CircuitBreaker,
    _FALLBACK_THRESHOLD,
    get_circuit_breaker,
    get_fallback_model,
    record_model_failure,
    record_model_success,
    reset_model_failures,
    retry_upstream_call,
    _is_retryable_status,
)
from src.router.router import get_default_model


class TestCircuitBreaker:
    """Testes para o Circuit Breaker."""

    def test_initial_state_closed(self):
        """Circuit breaker começa fechado."""
        cb = CircuitBreaker()
        assert cb.is_open("test-model") is False

    def test_opens_after_max_failures(self):
        """Circuit abre após 5 falhas em 60s."""
        cb = CircuitBreaker(max_failures=5, window_seconds=60.0, cooldown_seconds=120.0)
        
        for i in range(5):
            cb.record_failure("test-model")
        
        assert cb.is_open("test-model") is True

    def test_resets_on_success(self):
        """Sucesso reseta o contador de falhas."""
        cb = CircuitBreaker(max_failures=5)
        
        for i in range(3):
            cb.record_failure("test-model")
        
        cb.record_success("test-model")
        
        # Após sucesso, falhas anteriores são ignoradas
        for i in range(4):
            cb.record_failure("test-model")
        
        # Ainda não abriu (só 4 falhas após o reset)
        assert cb.is_open("test-model") is False

    def test_closes_after_cooldown(self):
        """Circuit fecha após cooldown expirar."""
        cb = CircuitBreaker(max_failures=3, window_seconds=1.0, cooldown_seconds=0.5)
        
        for i in range(3):
            cb.record_failure("test-model")
        
        assert cb.is_open("test-model") is True
        
        time.sleep(0.6)  # Aguarda cooldown
        
        # Após cooldown, circuit está half-open (is_open=False)
        assert cb.is_open("test-model") is False

    def test_window_sliding(self):
        """Falhas fora da janela não contam."""
        cb = CircuitBreaker(max_failures=3, window_seconds=0.5)
        
        cb.record_failure("test-model")
        cb.record_failure("test-model")
        
        time.sleep(0.6)  # Aguarda janela expirar
        
        cb.record_failure("test-model")  # Esta é a 1ª na nova janela
        
        # Não abriu (só 1 falha na janela atual)
        assert cb.is_open("test-model") is False


class TestRetryUpstreamCall:
    """Testes para retry_upstream_call."""

    @pytest.mark.asyncio
    async def test_no_retry_on_success(self):
        """Não faz retry se primeira tentativa succeed."""
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        call_count = 0
        
        async def mock_func():
            nonlocal call_count
            call_count += 1
            return mock_response
        
        result = await retry_upstream_call(mock_func, max_retries=2)
        
        assert call_count == 1
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_retry_on_5xx(self):
        """Faz retry em erros 5xx."""
        mock_response_500 = AsyncMock()
        mock_response_500.status_code = 500
        
        mock_response_200 = AsyncMock()
        mock_response_200.status_code = 200
        
        call_count = 0
        
        async def mock_func():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_response_500
            return mock_response_200
        
        result = await retry_upstream_call(mock_func, max_retries=2, base_delay=0.01)
        
        assert call_count == 2
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_no_retry_on_4xx(self):
        """Não faz retry em erros 4xx (exceto 429)."""
        mock_response_400 = AsyncMock()
        mock_response_400.status_code = 400
        
        call_count = 0
        
        async def mock_func():
            nonlocal call_count
            call_count += 1
            return mock_response_400
        
        result = await retry_upstream_call(mock_func, max_retries=2)
        
        assert call_count == 1
        assert result.status_code == 400

    @pytest.mark.asyncio
    async def test_retry_on_429(self):
        """Faz retry em 429 (rate limit)."""
        mock_response_429 = AsyncMock()
        mock_response_429.status_code = 429
        
        mock_response_200 = AsyncMock()
        mock_response_200.status_code = 200
        
        call_count = 0
        
        async def mock_func():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return mock_response_429
            return mock_response_200
        
        result = await retry_upstream_call(mock_func, max_retries=2, base_delay=0.01)
        
        assert call_count == 2
        assert result.status_code == 200

    @pytest.mark.asyncio
    async def test_exhausts_retries(self):
        """Retorna último erro após esgotar retries."""
        mock_response_500 = AsyncMock()
        mock_response_500.status_code = 500
        
        call_count = 0
        
        async def mock_func():
            nonlocal call_count
            call_count += 1
            return mock_response_500
        
        result = await retry_upstream_call(mock_func, max_retries=2, base_delay=0.01)
        
        assert call_count == 3  # 1 inicial + 2 retries
        assert result.status_code == 500


class TestIsRetryableStatus:
    """Testes para _is_retryable_status."""

    def test_5xx_retryable(self):
        assert _is_retryable_status(500) is True
        assert _is_retryable_status(502) is True
        assert _is_retryable_status(503) is True
        assert _is_retryable_status(504) is True

    def test_429_retryable(self):
        assert _is_retryable_status(429) is True

    def test_4xx_not_retryable(self):
        assert _is_retryable_status(400) is False
        assert _is_retryable_status(401) is False
        assert _is_retryable_status(403) is False
        assert _is_retryable_status(404) is False

    def test_2xx_not_retryable(self):
        assert _is_retryable_status(200) is False
        assert _is_retryable_status(201) is False


class TestModelFallback:
    """Testes para model fallback chain."""

    def setup_method(self):
        """Reset failure tracking before each test."""
        reset_model_failures()

    def teardown_method(self):
        """Cleanup after each test."""
        reset_model_failures()

    def test_fallback_after_threshold(self):
        """Retorna fallback após 2 falhas consecutivas."""
        # Primeira falha
        fb = record_model_failure("moonshotai/kimi-k2.5")
        assert fb is None

        # Segunda falha → default_model do YAML (mesmo que o router sem roteamento)
        fb = record_model_failure("moonshotai/kimi-k2.5")
        assert fb == get_default_model()

    def test_success_resets_counter(self):
        """Sucesso reseta contador de falhas."""
        record_model_failure("moonshotai/kimi-k2.5")
        record_model_success("moonshotai/kimi-k2.5")

        # Após sucesso, precisa de 2 falhas novamente
        fb = record_model_failure("moonshotai/kimi-k2.5")
        assert fb is None

    def test_get_fallback_model(self):
        """get_fallback_model devolve default_model se o actual é outro."""
        d = get_default_model()
        fb = get_fallback_model("moonshotai/kimi-k2.5")
        assert fb == d

        # Já estamos no default — não há para onde cair
        assert get_fallback_model(d) is None


class TestGlobalCircuitBreaker:
    """Testes para o circuit breaker global."""

    def test_get_circuit_breaker_returns_singleton(self):
        """get_circuit_breaker retorna mesma instância."""
        cb1 = get_circuit_breaker()
        cb2 = get_circuit_breaker()
        assert cb1 is cb2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
