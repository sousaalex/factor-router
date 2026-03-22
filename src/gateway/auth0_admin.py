"""
src/gateway/auth0_admin.py

Verificação de JWT Auth0 para proteger a Admin API (ou fluxos equivalentes).

O que validamos:
    - Assinatura RS256 contra o JWKS do tenant (`/.well-known/jwks.json`)
    - `iss` (issuer) — por defeito `https://<AUTH0_DOMAIN>/`
    - `aud` (audience) — identificador da API Auth0 (APIs → Identifier)
    - `exp` / `nbf` quando presentes
    - Permissões: o token tem de incluir **todas** as entradas em
      `ADMIN_GATEWAY_REQUIRED_PERMISSIONS` (claim `permissions` ou `scope`).

Nota sobre "session":
    O Auth0 não envia um "session_id" no JWT típico de acesso. Este módulo
    valida o **access token** (Bearer JWT) emitido para a tua API. Esse token
    contém `sub`, `permissions`, etc. O SPA guarda-o em memória/sessão no
    browser; no gateway recebes-no no header `Authorization: Bearer <jwt>`.

Uso futuro em FastAPI:
    verifier = auth0_verifier_from_settings(get_settings())
    user = verifier.verify(bearer_token)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import jwt
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

# Permissões RBAC da API Auth0 — todas obrigatórias para contar como admin no gateway.
ADMIN_GATEWAY_REQUIRED_PERMISSIONS: tuple[str, ...] = (
    "create:admin-factorai",
    "delete:admin-factorai",
    "read:admin-factorai",
    "update:admin-factorai",
)


class Auth0AdminError(Exception):
    """Erro base da verificação Auth0 admin."""


class Auth0AdminConfigError(Auth0AdminError):
    """Configuração em falta ou inválida (domínio, audience, etc.)."""


class Auth0AdminTokenError(Auth0AdminError):
    """Token inválido, expirado, audience errada ou sem permissões exigidas."""


@dataclass(frozen=True)
class Auth0AdminUser:
    """Claims úteis após verificação bem-sucedida."""

    sub: str
    permissions: tuple[str, ...]
    email: Optional[str] = None
    email_verified: Optional[bool] = None
    issuer: Optional[str] = None
    audience: Any = None
    expires_at: Optional[int] = None
    raw_claims: Optional[dict[str, Any]] = None

    def has_all_permissions(self, required: tuple[str, ...]) -> bool:
        if not required:
            return True
        perms = set(self.permissions)
        return all(p in perms for p in required)

    def to_public_dict(self) -> dict[str, Any]:
        """Dados seguros para logs / respostas de debug (sem token)."""
        return {
            "sub": self.sub,
            "email": self.email,
            "email_verified": self.email_verified,
            "permissions": list(self.permissions),
            "issuer": self.issuer,
            "audience": self.audience,
            "expires_at": self.expires_at,
        }


class Auth0AdminVerifier:
    """
    Cliente de verificação: JWKS em cache no PyJWKClient (por URL).
    """

    def __init__(
        self,
        domain: str,
        audience: str,
        *,
        issuer: Optional[str] = None,
        required_permissions: Optional[list[str]] = None,
        leeway_seconds: int = 0,
    ) -> None:
        domain = domain.strip().rstrip("/")
        if not domain:
            raise Auth0AdminConfigError("AUTH0_DOMAIN is empty.")
        if not audience or not str(audience).strip():
            raise Auth0AdminConfigError("AUTH0_AUDIENCE is empty.")

        self._domain = domain
        self._audience = audience.strip()
        self._issuer = (issuer or f"https://{domain}/").strip()
        if not self._issuer.endswith("/"):
            self._issuer += "/"
        self._required = tuple(
            p.strip()
            for p in (required_permissions or [])
            if p and p.strip()
        )
        self._leeway = leeway_seconds
        jwks_url = f"https://{domain}/.well-known/jwks.json"
        self._jwks = PyJWKClient(jwks_url)

    @property
    def required_permissions(self) -> tuple[str, ...]:
        return self._required

    def verify(self, token: str, *, check_permissions: bool = True) -> Auth0AdminUser:
        """
        Valida o JWT e opcionalmente exige todas as permissions configuradas.
        """
        raw = (token or "").strip()
        if raw.lower().startswith("bearer "):
            raw = raw[7:].strip()
        if not raw:
            raise Auth0AdminTokenError("Missing JWT.")

        segments = raw.split(".")
        if len(segments) == 5:
            raise Auth0AdminTokenError(
                "This token is an encrypted JWT (JWE — five dot-separated parts). "
                "This verifier only accepts a signed access token (JWS — three parts: "
                "header.payload.signature). In Auth0: enable your API, use its "
                "Identifier as audience, and request an access token for that API "
                "(not an encrypted session blob or a token format with five segments)."
            )
        if len(segments) != 3:
            raise Auth0AdminTokenError(
                f"Expected a signed JWT with exactly 3 segments; got {len(segments)}."
            )

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(raw)
            claims = jwt.decode(
                raw,
                signing_key.key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=self._leeway,
                options={
                    "verify_aud": True,
                    "verify_iss": True,
                    "require": ["exp"],
                },
            )
        except jwt.ExpiredSignatureError as e:
            raise Auth0AdminTokenError("Token expired.") from e
        except jwt.InvalidAudienceError as e:
            raise Auth0AdminTokenError("Invalid audience.") from e
        except jwt.InvalidIssuerError as e:
            raise Auth0AdminTokenError("Invalid issuer.") from e
        except jwt.PyJWTError as e:
            raise Auth0AdminTokenError(f"Invalid token: {e}") from e

        # APIs com RBAC → claim "permissions" (lista).
        # Auth0 Management API e outros fluxos OAuth → claim "scope" (string com espaços).
        perms_raw = claims.get("permissions")
        if perms_raw is None:
            permissions = ()
        elif isinstance(perms_raw, list):
            permissions = tuple(str(p) for p in perms_raw)
        elif isinstance(perms_raw, str):
            permissions = tuple(perms_raw.split())
        else:
            permissions = ()
        if not permissions:
            scope_raw = claims.get("scope")
            if isinstance(scope_raw, str) and scope_raw.strip():
                permissions = tuple(scope_raw.split())

        user = Auth0AdminUser(
            sub=str(claims.get("sub", "")),
            permissions=permissions,
            email=claims.get("email"),
            email_verified=claims.get("email_verified"),
            issuer=claims.get("iss"),
            audience=claims.get("aud"),
            expires_at=claims.get("exp"),
            raw_claims=dict(claims),
        )

        if not user.sub:
            raise Auth0AdminTokenError("Token missing 'sub'.")

        if check_permissions and self._required and not user.has_all_permissions(self._required):
            missing = [p for p in self._required if p not in set(user.permissions)]
            raise Auth0AdminTokenError(
                f"Missing required permissions: {missing}. "
                f"Token has: {list(user.permissions)}"
            )

        return user


def auth0_verifier_from_settings(settings: Any) -> Optional[Auth0AdminVerifier]:
    """
    Constrói um verifier a partir de Settings (Pydantic).
    Devolve None se domain ou audience estiverem vazios.
    """
    domain = getattr(settings, "auth0_domain", None)
    audience = getattr(settings, "auth0_audience", None)
    if not domain or not audience:
        return None
    issuer = getattr(settings, "auth0_issuer", None) or None
    leeway = int(getattr(settings, "auth0_jwt_leeway_seconds", 0) or 0)
    return Auth0AdminVerifier(
        domain=str(domain),
        audience=str(audience),
        issuer=str(issuer) if issuer else None,
        required_permissions=list(ADMIN_GATEWAY_REQUIRED_PERMISSIONS),
        leeway_seconds=leeway,
    )
