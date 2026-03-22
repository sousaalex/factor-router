"""
Esquemas HTTP Bearer para OpenAPI / Swagger UI.

Cada `HTTPBearer` com `scheme_name` distinto aparece no diálogo Authorize com a descrição adequada.
"""
from __future__ import annotations

from fastapi.security import HTTPBearer

# Proxy /v1/* e /v1/turns/* — só API Key da app
gateway_api_key_bearer = HTTPBearer(
    scheme_name="GatewayApiKey",
    auto_error=False,
    description=(
        "API Key da aplicação (ex.: `sk-fai-...`). "
        "Criada com `POST /admin/apps/{app_id}/keys`."
    ),
)

# /admin/*
auth0_admin_bearer = HTTPBearer(
    scheme_name="Auth0AdminJWT",
    bearerFormat="JWT",
    auto_error=False,
    description=(
        "Access token **JWT** do Auth0 para a audience da API (`AUTH0_AUDIENCE`). "
        "Obrigatórias no token as permissões: `create:admin-factorai`, "
        "`delete:admin-factorai`, `read:admin-factorai`, `update:admin-factorai`."
    ),
)

# /usage/* — mesmo header, dois tipos de credencial
usage_access_bearer = HTTPBearer(
    scheme_name="UsageAccess",
    auto_error=False,
    description=(
        "**App:** cola a API Key (`sk-fai-...`). "
        "**Admin:** cola o access token Auth0 (JWT, três segmentos `xx.yy.zz`)."
    ),
)
