"""
src/gateway/auth.py

Validação da API Key via KeyStore (Postgres + cache em memória).
A key real nunca é guardada — apenas o SHA-256 hash.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials

from src.gateway.bearer_schemes import gateway_api_key_bearer
from src.gateway.key_store import CachedKey, KeyStore, get_key_store

bearer_scheme = gateway_api_key_bearer


class AuthenticatedApp:
    """Resultado de uma autenticação bem-sucedida."""

    def __init__(
        self,
        app_id: str,
        app_name: str,
        key_id: str,
        key_label: str | None = None,
    ) -> None:
        self.app_id   = app_id
        self.app_name = app_name
        self.key_id   = key_id
        self.key_label = key_label

    @property
    def upstream_env(self) -> str | None:
        """
        Ambiente lógico do upstream inferido a partir do label da API key.
        Convenção estrita:
          - dev  -> "dev"
          - prod -> "prod"
        """
        label = (self.key_label or "").strip().lower()
        if ":" in label:
            label = label.split(":", 1)[0].strip()
        if label == "dev":
            return "dev"
        if label == "prod":
            return "prod"
        return None

    def __repr__(self) -> str:
        return f"AuthenticatedApp(app_id={self.app_id!r}, name={self.app_name!r})"


async def authenticate(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ] = None,
    store: Annotated[KeyStore, Depends(get_key_store)] = None,
) -> AuthenticatedApp:
    """
    FastAPI dependency. Valida Authorization: Bearer <key>.

    Fluxo:
        1. Extrai a key do header
        2. Calcula SHA-256(key)
        3. Lookup no cache em memória (O(1), zero DB)
        4. Verifica is_active

    Erros:
        401 missing_authorization — header ausente
        401 invalid_api_key       — key não reconhecida ou revogada
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "missing_authorization",
                "message": "Header 'Authorization: Bearer <key>' required.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    entry: CachedKey | None = await store.validate(credentials.credentials)

    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error": "invalid_api_key",
                "message": "Invalid API Key or revoked key.",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    return AuthenticatedApp(
        app_id=entry.app_id,
        app_name=entry.app_name,
        key_id=entry.key_id,
        key_label=entry.label,
    )
