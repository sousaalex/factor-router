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