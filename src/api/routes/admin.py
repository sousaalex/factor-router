"""
src/api/routes/admin.py

Admin API — gestão de apps e API Keys.

Todos os endpoints requerem:
    Authorization: Bearer <access_token Auth0>

Endpoints:
    POST   /admin/apps                     — cria nova app
    GET    /admin/apps                     — lista todas as apps
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
    description="Returns all registered apps with active key counts.",
)
async def list_apps(
    _admin: Annotated[Auth0AdminUser, Depends(require_auth0_admin)],
    store: Annotated[KeyStore, Depends(get_key_store)],
):
    return {"apps": await store.list_apps()}


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
