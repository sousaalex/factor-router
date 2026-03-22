"""
Dependências FastAPI — autenticação admin via Auth0 (JWT Bearer).

Rotas /admin/* e o modo admin de /usage/* usam `Authorization: Bearer <access_token>`.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from src.gateway.bearer_schemes import auth0_admin_bearer
from src.gateway.auth0_admin import (
    Auth0AdminTokenError,
    Auth0AdminUser,
    Auth0AdminVerifier,
    auth0_verifier_from_settings,
)
from src.gateway.config import Settings, get_settings


def get_auth0_verifier(settings: Settings) -> Auth0AdminVerifier:
    v = auth0_verifier_from_settings(settings)
    if v is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "auth0_not_configured",
                "message": "AUTH0_DOMAIN and AUTH0_AUDIENCE must be set.",
            },
        )
    return v


def token_looks_like_jws(raw: str) -> bool:
    """Access token Auth0 é um JWS com 3 segmentos; API keys do gateway não têm este formato."""
    parts = raw.split(".")
    return len(parts) == 3 and all(p.strip() for p in parts)


async def require_auth0_admin(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(auth0_admin_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Auth0AdminUser:
    verifier = get_auth0_verifier(settings)
    if not creds or not creds.credentials or not creds.credentials.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_bearer_token",
                "message": "Authorization: Bearer <access_token> is required.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return verifier.verify(creds.credentials.strip())
    except Auth0AdminTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_token", "message": str(e)},
            headers={"WWW-Authenticate": "Bearer"},
        ) from e
