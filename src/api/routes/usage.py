"""
src/api/routes/usage.py

Endpoints de leitura do centro de custos.
GET /usage/logs   — lista de registos por turno
GET /usage/stats  — agregados por modelo, empresa e app

Controlo de acesso:
    - Bearer <app_key>              → só vê os logs da sua própria app (app_id forçado)
    - Bearer <Auth0 access_token>   → admin, vê tudo (JWS com 3 segmentos; validado com Auth0)
"""
from __future__ import annotations

from typing import Annotated, Optional
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials

from src.api.deps_auth0_admin import get_auth0_verifier, token_looks_like_jws
from src.gateway.bearer_schemes import usage_access_bearer
from src.gateway.auth0_admin import Auth0AdminTokenError
from src.gateway.config import Settings, get_settings
from src.gateway.key_store import get_key_store
from src.usage.service import get_usage_logs, get_usage_stats

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# UsageCaller — quem está a chamar e que app_id lhe pertence
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class UsageCaller:
    is_admin: bool
    app_id: str | None   # None = admin (sem filtro de app)


async def get_usage_caller(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(usage_access_bearer),
    ] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,
) -> UsageCaller:
    """
    Dependency que aceita duas formas de autenticação no mesmo header Bearer:

    1. Access token Auth0 (JWS, 3 segmentos) → admin, vê tudo sem filtros
    2. API key da app → só vê os seus próprios logs

    Se nenhuma for fornecida → 401.
    """
    if not credentials or not credentials.credentials or not credentials.credentials.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_authentication",
                "message": (
                    "Authenticate with 'Authorization: Bearer <api_key>' (app) "
                    "or 'Authorization: Bearer <auth0_access_token>' (admin)."
                ),
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    raw = credentials.credentials.strip()

    if token_looks_like_jws(raw):
        verifier = get_auth0_verifier(settings)
        try:
            verifier.verify(raw)
            return UsageCaller(is_admin=True, app_id=None)
        except Auth0AdminTokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={"error": "invalid_token", "message": str(e)},
                headers={"WWW-Authenticate": "Bearer"},
            ) from e

    store = get_key_store()
    entry = await store.validate(raw)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_api_key", "message": "Invalid or revoked API key."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return UsageCaller(is_admin=False, app_id=entry.app_id)


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get(
    "/logs",
    summary="Per-turn usage logs",
    description="""
Lists token consumption records.

**App (Bearer API key):** only sees its own rows — `app_id` is enforced automatically.  
**Admin (Bearer Auth0 access token):** sees everything; can filter by any `app_id`.
    """,
)
async def handle_get_usage_logs(
    caller: Annotated[UsageCaller, Depends(get_usage_caller)],
    company_id: str | None = Query(default=None, description="Filter by company"),
    app_id:     str | None = Query(default=None, description="Filter by app (admin only)"),
    session_id: str | None = Query(default=None, description="Filter by session"),
    date_from:  str | None = Query(default=None, description="ISO 8601, e.g. 2025-03-01"),
    date_to:    str | None = Query(default=None, description="ISO 8601, e.g. 2025-03-31"),
    limit:      int        = Query(default=50, ge=1, le=500),
    offset:     int        = Query(default=0, ge=0),
):
    # App → ignora o app_id do query param e força o seu próprio
    # Admin → usa o app_id do query param (ou None = tudo)
    effective_app_id = caller.app_id if not caller.is_admin else app_id

    return await get_usage_logs(
        company_id=company_id,
        app_id=effective_app_id,
        session_id=session_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/stats",
    summary="Aggregated usage statistics",
    description="""
Total tokens and USD cost with breakdown by model and app.

**App (Bearer API key):** only sees its own statistics.  
**Admin (Bearer Auth0 access token):** sees everything; can filter by any `app_id`.
    """,
)
async def handle_get_usage_stats(
    caller: Annotated[UsageCaller, Depends(get_usage_caller)],
    company_id: str | None = Query(default=None, description="Filter by company"),
    app_id:     str | None = Query(default=None, description="Filter by app (admin only)"),
    date_from:  str | None = Query(default=None, description="ISO 8601"),
    date_to:    str | None = Query(default=None, description="ISO 8601"),
):
    effective_app_id = caller.app_id if not caller.is_admin else app_id

    return await get_usage_stats(
        company_id=company_id,
        app_id=effective_app_id,
        date_from=date_from,
        date_to=date_to,
    )
