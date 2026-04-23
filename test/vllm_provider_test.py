"""
Testes para o provider FactorAI vLLM local.

Valida:
1. Resolução de provider factorai/ para FACTORAI_VLLM_BASE_URL
2. Modelos FactorAI registados no YAML com pricing $0
3. Fallback chain inclui modelos FactorAI
"""
import pytest
from unittest.mock import Mock, patch

from src.gateway.provider_upstream import resolve_upstream, UpstreamTarget
from src.gateway.config import Settings


class TestFactorAIProviderResolution:
    """Testa resolução de modelos com prefixo factorai/"""

    def test_resolve_factorai_model_with_config(self):
        """Modelo factorai/ resolve para FACTORAI_VLLM_BASE_URL"""
        settings = Settings(
            database_url="postgresql://test@test/test",
            auth0_domain="test.auth0.com",
            auth0_audience="https://test-api",
            openrouter_api_dev="sk-test",
            factorai_vllm_base_url="http://192.168.1.223:8000/v1",
            factorai_vllm_api_key="EMPTY",
            factorai_vllm_timeout=120,
        )

        target = resolve_upstream("factorai/qwen3.6-35b-a3b", settings)

        assert target.chat_completions_url == "http://192.168.1.223:8000/v1/chat/completions"
        assert target.api_model == "qwen3.6-35b-a3b"
        assert target.headers == {}  # EMPTY key → no auth header
        assert target.selected_env == "factorai"
        assert target.api_key_source == "FACTORAI_VLLM_API_KEY"

    def test_resolve_factorai_model_with_api_key(self):
        """Modelo factorai/ com API key inclui Authorization header"""
        settings = Settings(
            database_url="postgresql://test@test/test",
            auth0_domain="test.auth0.com",
            auth0_audience="https://test-api",
            openrouter_api_dev="sk-test",
            factorai_vllm_base_url="http://192.168.1.223:8000/v1",
            factorai_vllm_api_key="sk-factorai-test-key",
            factorai_vllm_timeout=120,
        )

        target = resolve_upstream("factorai/qwen3.6-35b-a3b", settings)

        assert target.headers == {"Authorization": "Bearer sk-factorai-test-key"}

    def test_resolve_factorai_model_strips_trailing_slash(self):
        """Base URL com / no final é normalizado"""
        settings = Settings(
            database_url="postgresql://test@test/test",
            auth0_domain="test.auth0.com",
            auth0_audience="https://test-api",
            openrouter_api_dev="sk-test",
            factorai_vllm_base_url="http://192.168.1.223:8000/v1/",
            factorai_vllm_api_key="EMPTY",
        )

        target = resolve_upstream("factorai/qwen3.6-35b-a3b", settings)

        assert target.chat_completions_url == "http://192.168.1.223:8000/v1/chat/completions"

    def test_resolve_factorai_model_without_config_raises(self):
        """Modelo factorai/ sem configuração lança erro 503"""
        settings = Settings(
            database_url="postgresql://test@test/test",
            auth0_domain="test.auth0.com",
            auth0_audience="https://test-api",
            openrouter_api_dev="sk-test",
            factorai_vllm_base_url=None,
        )

        with pytest.raises(Exception) as exc_info:
            resolve_upstream("factorai/qwen3.6-35b-a3b", settings)

        assert exc_info.value.status_code == 503
        assert "FACTORAI_VLLM_BASE_URL" in str(exc_info.value.detail)

    def test_resolve_factorai_model_empty_name_raises(self):
        """Modelo factorai/ sem nome lança erro 400"""
        settings = Settings(
            database_url="postgresql://test@test/test",
            auth0_domain="test.auth0.com",
            auth0_audience="https://test-api",
            openrouter_api_dev="sk-test",
            factorai_vllm_base_url="http://192.168.1.223:8000/v1",
        )

        with pytest.raises(Exception) as exc_info:
            resolve_upstream("factorai/", settings)

        assert exc_info.value.status_code == 400
        assert "factorai/<nome>" in str(exc_info.value.detail)


class TestFactorAIModelConfig:
    """Testa configuração dos modelos FactorAI no YAML"""

    def test_factorai_models_in_yaml(self):
        """Modelos FactorAI estão registados no YAML"""
        import yaml
        from pathlib import Path

        yaml_path = Path(__file__).parent.parent / "src" / "router" / "models_config.yaml"
        with open(yaml_path) as f:
            config = yaml.safe_load(f)

        models = config.get("models", [])
        factorai_models = [m for m in models if m.get("id", "").startswith("factorai/")]

        assert len(factorai_models) >= 2, "Esperados pelo menos 2 modelos FactorAI"

        # Verifica qwen3.6-35b-a3b
        qwen35 = next((m for m in factorai_models if "qwen3.6-35b-a3b" in m.get("id", "")), None)
        assert qwen35 is not None, "Modelo factorai/qwen3.6-35b-a3b não encontrado"
        assert qwen35["tier"] == "reasoning"
        assert qwen35["pricing"]["input_per_1m_tokens"] == "$0.00"
        assert qwen35["pricing"]["output_per_1m_tokens"] == "$0.00"
        assert qwen35["provider"] == "factorai"
        assert qwen35["is_local"] is True

        # Verifica qwen2.5:0.5b
        qwen25 = next((m for m in factorai_models if "qwen2.5:0.5b" in m.get("id", "")), None)
        assert qwen25 is not None, "Modelo factorai/qwen2.5:0.5b não encontrado"
        assert qwen25["tier"] == "simple"
        assert qwen25["pricing"]["input_per_1m_tokens"] == "$0.00"
        assert qwen25["pricing"]["output_per_1m_tokens"] == "$0.00"
        assert qwen25["provider"] == "factorai"
        assert qwen25["is_local"] is True


class TestFactorAIFallback:
    """Testa fallback chain com modelos FactorAI"""

    def test_factorai_in_fallback_chain(self):
        """Modelo FactorAI está no início da fallback chain"""
        from src.gateway.resilience import _FALLBACK_CHAIN

        assert len(_FALLBACK_CHAIN) > 0, "Fallback chain vazia"
        assert _FALLBACK_CHAIN[0].startswith("factorai/"), \
            "Modelo FactorAI deve ser o primeiro fallback (custo $0)"
        assert "qwen3.6-35b-a3b" in _FALLBACK_CHAIN[0], \
            "Fallback principal deve ser factorai/qwen3.6-35b-a3b"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
