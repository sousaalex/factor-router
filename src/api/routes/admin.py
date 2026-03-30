"""
src/api/routes/admin.py

Admin API — gestão de apps e API Keys.

Quota de consumo (USD) é sempre por APP, nunca por API key:
  - Uma app pode ter várias keys (prod, staging, rotação).
  - Todas debitam o mesmo `spent_usd_total` e obedecem ao mesmo `spend_cap_usd`.
  - O router identifica a app pela key, mas o limite e o acumulado são da app.
  - Aumentar o teto: PATCH /admin/apps/{app_id} com spend_cap_usd.

Todos os endpoints requerem:
    Authorization: Bearer <access_token Auth0>

Endpoints:
    POST   /admin/apps                     — cria nova app
    GET    /admin/apps                     — lista todas as apps
    PATCH  /admin/apps/{app_id}            — teto USD / is_active
    POST   /admin/apps/{app_id}/keys       — gera nova API Key
    GET    /admin/apps/{app_id}/keys       — lista keys da app
    DELETE /admin/apps/{app_id}/keys/{id}  — revoga uma key
"""
from __future__ import annotations

import logging
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field

from src.api.deps_auth0_admin import require_auth0_admin
from src.gateway.auth0_admin import Auth0AdminUser
from src.gateway.key_store import KeyStore, get_key_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["admin"])


# ─────────────────────────────────────────────────────────────────────────────
# Schemas Pydantic
# ─────────────────────────────────────────────────────────────────────────────

class CreateAppRequest(BaseModel):
    name:        str           = Field(..., min_length=2, max_length=100,
                                       description="Nome legível: 'Severino WhatsApp'")
    description: Optional[str] = Field(default=None, max_length=500)
    spend_cap_usd: float = Field(
        10.0,
        ge=0.01,
        description=(
            "Teto em USD para esta app inteira — todas as API keys desta app partilham o mesmo consumo acumulado. "
            "Não há quota separada por key. Estimado por tokens × preço do modelo no router."
        ),
    )


class PatchAppRequest(BaseModel):
    spend_cap_usd: Optional[float] = Field(
        default=None,
        ge=0.01,
        description="Novo teto em USD para a app (todas as keys somam no mesmo spent_usd_total).",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Se definido, activa ou desactiva a app.",
    )


class CreateKeyRequest(BaseModel):
    label: Optional[str] = Field(
        default=None,
        max_length=100,
        description="E.g. 'production', 'staging', 'v2'",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/apps",
    status_code=status.HTTP_201_CREATED,
    summary="Create a new app",
    description="""
Register a new app on the gateway. After creating the app, use
`POST /admin/apps/{app_id}/keys` to generate the first API key.

`app_id` is derived from `name` (lowercase, hyphenated) and must be unique (e.g. `my-app-v2`).
    """,
)
async def create_app(
    body: CreateAppRequest,
    _admin: Annotated[Auth0AdminUser, Depends(require_auth0_admin)],
    store: Annotated[KeyStore, Depends(get_key_store)],
):
    try:
        return await store.create_app(
            name=body.name,
            description=body.description,
            spend_cap_usd=body.spend_cap_usd,
        )
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "app already exists",
                    "message": (
                        "App already exists with the same name"
                        "Use a different name"
                    ),
                },
            )
        logger.exception("Failed to create app: %s", e)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Internal server error.",
            },
        )


@router.get(
    "/apps",
    summary="List all apps",
    description="""
Lista apps com `spend_cap_usd`, `spent_usd_total` e `remaining_usd`.
O consumo é por app: várias keys da mesma app partilham o mesmo acumulado.
""",
)
async def list_apps(
    _admin: Annotated[Auth0AdminUser, Depends(require_auth0_admin)],
    store: Annotated[KeyStore, Depends(get_key_store)],
):
    return {"apps": await store.list_apps()}


@router.patch(
    "/apps/{app_id}",
    summary="Update app (spend cap / active)",
    description="""
Actualiza a **quota de consumo em USD** (`spend_cap_usd`) e/ou o estado `is_active` da app.
Envia pelo menos um campo no body.

Isto define quanto **esta integração** (ex.: Severino AgiWeb) pode gastar no vosso router — por **app**, não por key:
todas as API keys da mesma app partilham o mesmo teto e o mesmo `spent_usd_total`.
**Não confundir** com créditos/saldo OpenRouter da organização (isso é outro fluxo: `/usage/openrouter/credits`).

Serve para apps e keys **já existentes** após a migration 006 (default 10 USD).
    """,
)
async def patch_app(
    app_id: Annotated[str, Path(description="App identifier")],
    body: PatchAppRequest,
    _admin: Annotated[Auth0AdminUser, Depends(require_auth0_admin)],
    store: Annotated[KeyStore, Depends(get_key_store)],
):
    if body.spend_cap_usd is None and body.is_active is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "empty_patch",
                "message": "Provide at least one of: spend_cap_usd, is_active.",
            },
        )
    try:
        row = await store.patch_app(
            app_id,
            spend_cap_usd=body.spend_cap_usd,
            is_active=body.is_active,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_patch", "message": str(e)},
        )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "app_not_found", "message": f"App '{app_id}' not found."},
        )
    return row


@router.post(
    "/apps/{app_id}/keys",
    status_code=status.HTTP_201_CREATED,
    summary="Generate a new API key",
    description="""
Generates a new API key for the app.

**IMPORTANT:** The raw key is returned **ONLY ONCE** in this response.
Store it securely immediately (e.g. in the app's environment variables).
The key cannot be retrieved later — only revoked and replaced.

Postgres stores only the SHA-256 hash of the key.
    """,
)
async def create_key(
    app_id: Annotated[str, Path(description="App identifier")],
    body: CreateKeyRequest,
    _admin: Annotated[Auth0AdminUser, Depends(require_auth0_admin)],
    store: Annotated[KeyStore, Depends(get_key_store)],
):
    try:
        return await store.create_key(
            app_id=app_id,
            label=body.label,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "app_not_found", "message": str(e)},
        )
    except Exception as e:
        logger.exception("Failed to create key: %s", e)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Internal server error.",
            },
        )


@router.get(
    "/apps/{app_id}/keys",
    summary="List an app's API keys",
    description="""
Lists all keys for the app (active and revoked).
**Never exposes the raw key** — only the prefix (e.g. `sk-gw-bluma-a3f9`)
and metadata (label, last_used_at, created_at).
    """,
)
async def list_keys(
    app_id: Annotated[str, Path()],
    _admin: Annotated[Auth0AdminUser, Depends(require_auth0_admin)],
    store: Annotated[KeyStore, Depends(get_key_store)],
):
    return {"keys": await store.list_keys(app_id)}


@router.delete(
    "/apps/{app_id}/keys/{key_id}",
    summary="Revoke an API key",
    description="""
Revokes an API key with **immediate** effect — the cache is invalidated
and the key stops being accepted within seconds.

The row remains in Postgres for audit trail (`revoked_at` set).
The key is **not deleted** — only marked inactive.
    """,
)
async def revoke_key(
    app_id: Annotated[str, Path()],
    key_id: Annotated[str, Path()],
    _admin: Annotated[Auth0AdminUser, Depends(require_auth0_admin)],
    store: Annotated[KeyStore, Depends(get_key_store)],
):
    try:
        return await store.revoke_key(key_id=key_id, app_id=app_id)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "key_not_found", "message": str(e)},
        )
    except Exception as e:
        logger.exception("Failed to revoke key: %s", e)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "internal_error",
                "message": "Internal server error.",
            },
        )
