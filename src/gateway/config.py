"""
src/gateway/config.py

Configuração central do gateway via Pydantic Settings.

As apps e as suas API Keys são geridas exclusivamente via base de dados
(tabelas gateway_apps e gateway_api_keys) através da Admin API.

NÃO existem GATEWAY_KEY_* neste ficheiro nem no .env.
A gestão de keys é feita via:
    POST /admin/apps                   — criar app
    POST /admin/apps/{id}/keys         — gerar key
    DELETE /admin/apps/{id}/keys/{kid} — revogar key

Variáveis obrigatórias no .env:
    OPENROUTER_API_KEY  — key do OpenRouter (nunca sai do gateway)
    DATABASE_URL        — postgresql+asyncpg://user:pass@host/db
    AUTH0_DOMAIN        — tenant Auth0 (JWT admin — ver src/gateway/auth0_admin.py)
    AUTH0_AUDIENCE      — identifier da API Auth0 (audience do access token)

Auth0 (admin via Bearer JWT):
    AUTH0_ISSUER (opcional), AUTH0_JWT_LEEWAY_SECONDS
    Permissões admin: fixas em src/gateway/auth0_admin.py (ADMIN_GATEWAY_REQUIRED_PERMISSIONS)

Variáveis opcionais (têm default):
    PORT             — default 8003
    HOST             — default 0.0.0.0
    LOG_LEVEL        — default info
    UPSTREAM_TIMEOUT — default 120 (segundos)
    UPSTREAM_URL     — default https://openrouter.ai/api/v1
    ACCUMULATOR_IDLE_TTL_SECONDS — inatividade máxima do balde (ver accumulator)
    OPENROUTER_MANAGEMENT_API_KEY (opcional), OPENROUTER_CREDITS_* , OPENROUTER_ROUTER_BUDGET_* — créditos / router económico
    GATEWAY_PREMIUM_MODEL + ALLOWLIST + FALLBACK — Claude só para alguns users; outros → Qwen3.5 Plus (default)
"""
from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── upstream provider ─────────────────────────────────────────────────
    openrouter_api_key: str = Field(
        ...,
        description="Key do OpenRouter — nunca sai do gateway",
    )
    upstream_url: str = Field(
        default="https://openrouter.ai/api/v1",
        description="Base URL do provider upstream",
    )
    upstream_timeout: int = Field(
        default=120,
        description="Timeout em segundos para calls ao upstream",
    )
    openrouter_management_api_key: Optional[str] = Field(
        default=None,
        description=(
            "Opcional: key com permissão para GET /api/v1/credits (management). "
            "Se vazio, usa OPENROUTER_API_KEY."
        ),
    )
    openrouter_credits_alert_threshold_usd: float = Field(
        default=10.0,
        ge=0,
        description=(
            "Em GET /usage/openrouter/credits: show_alert=true quando remaining_usd <= este valor. "
            "Créditos OpenRouter são em USD (≈ USD para efeitos de UI)."
        ),
    )
    openrouter_router_budget_enabled: bool = Field(
        default=True,
        description=(
            "Se true e o último snapshot em openrouter_credits_state tiver remaining_usd <= limiar "
            "(ver openrouter_router_budget_threshold_usd), o router favorece modelos baratos e "
            "aplica teto a tiers caros."
        ),
    )
    openrouter_router_budget_threshold_usd: Optional[float] = Field(
        default=None,
        ge=0,
        description=(
            "Limiar (USD) para modo económico no router. None = usa openrouter_credits_alert_threshold_usd."
        ),
    )

    # ── política de modelo (opcional) ─────────────────────────────────────
    gateway_premium_model: str = Field(
        default="",
        description=(
            "Model_id (ex. anthropic/claude-sonnet-4.6) reservado: só X-User-Id em "
            "GATEWAY_PREMIUM_MODEL_USER_ALLOWLIST pode usá-lo. Vazio = desligado."
        ),
    )
    gateway_premium_model_user_allowlist: str = Field(
        default="",
        description="Lista separada por vírgulas de valores de X-User-Id permitidos no modelo premium.",
    )
    gateway_premium_model_fallback: str = Field(
        default="qwen/qwen3.5-plus-02-15",
        description=(
            "Quando o router escolhe GATEWAY_PREMIUM_MODEL mas X-User-Id não está na allowlist, "
            "usa este model_id (tipicamente Qwen3.5 Plus / reasoning+)."
        ),
    )

    # ── base de dados ─────────────────────────────────────────────────────
    database_url: str = Field(
        ...,
        description="PostgreSQL connection string",
    )

    # ── Auth0 — admin: Authorization: Bearer <access_token> ────────────────
    auth0_domain: str = Field(
        ...,
        min_length=1,
        description="Tenant Auth0, ex: dev-xxx.eu.auth0.com (sem https://)",
    )
    auth0_audience: str = Field(
        ...,
        min_length=1,
        description="Identifier da API Auth0 (audience do access token)",
    )
    auth0_issuer: Optional[str] = Field(
        default=None,
        description="Issuer exato do JWT; por defeito https://<AUTH0_DOMAIN>/",
    )
    auth0_jwt_leeway_seconds: int = Field(
        default=0,
        ge=0,
        description="Margem em segundos para exp (clock skew)",
    )

    # ── acumulador de tokens (balde por turno) ────────────────────────────
    accumulator_idle_ttl_seconds: int = Field(
        default=30,
        ge=15,
        le=86400,
        description=(
            "Segundos sem actividade no balde antes do cleanup gravar (fallback). "
            "Deve ser maior que a maior pausa esperada entre calls ao LLM no mesmo turno "
            "(ex.: tools lentas). Turnos de 1h com iterações frequentes: default OK; "
            "com pausas de vários minutos: aumentar (ex. 600)."
        ),
    )

    # ── servidor ──────────────────────────────────────────────────────────
    host: str  = Field(default="0.0.0.0")
    port: int  = Field(default=8003)
    log_level: str = Field(default="info")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Singleton das settings — carregado uma vez, cacheado para sempre.
    FastAPI injeta via Depends(get_settings).
    """
    return Settings()