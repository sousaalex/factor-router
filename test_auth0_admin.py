#!/usr/bin/env python3
"""
Testa a verificação de JWT Auth0 (access token) sem subir o FastAPI.

Configuração (ficheiro .env ou variáveis de ambiente):
    AUTH0_DOMAIN              — tenant, ex: dev-xxx.eu.auth0.com
    AUTH0_AUDIENCE            — identifier da API Auth0
    AUTH0_ISSUER              — opcional; por defeito https://<DOMAIN>/
    Permissões admin — fixas no código (ADMIN_GATEWAY_REQUIRED_PERMISSIONS)
    AUTH0_JWT_LEEWAY_SECONDS  — opcional
    AUTH0_TEST_JWT            — token completo (ou usa --file / argv)

O token tem de ser um JWT **assinado** (JWS: 3 partes separadas por ponto).
Tokens **encriptados** (JWE: 5 partes) não são suportados — obtém um access token
para a API Auth0 (audience = Identifier da API).

Exemplos:
    uv run test_auth0_admin.py
    uv run test_auth0_admin.py --file /tmp/token.txt
    AUTH0_TEST_JWT="$(cat token.txt)" uv run test_auth0_admin.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv

load_dotenv()

from src.gateway.auth0_admin import (  # noqa: E402
    ADMIN_GATEWAY_REQUIRED_PERMISSIONS,
    Auth0AdminConfigError,
    Auth0AdminTokenError,
    Auth0AdminVerifier,
)


def _read_jwt(args: argparse.Namespace) -> str:
    if args.token:
        return args.token.strip()
    env_tok = os.environ.get("AUTH0_TEST_JWT", "").strip()
    if env_tok:
        return env_tok
    if args.file:
        path = os.path.abspath(os.path.expanduser(args.file))
        if not os.path.isfile(path):
            raise FileNotFoundError(path)
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Auth0 JWT for admin RBAC.")
    parser.add_argument("token", nargs="?", default=None, help="JWT (or use env / --file)")
    parser.add_argument("--file", "-f", help="Path to file containing the JWT")
    parser.add_argument(
        "--no-permission-check",
        action="store_true",
        help="Only validate signature, iss, aud, exp (ignore permissões admin)",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON only")
    args = parser.parse_args()

    domain = os.environ.get("AUTH0_DOMAIN", "").strip()
    audience = os.environ.get("AUTH0_AUDIENCE", "").strip()
    issuer = os.environ.get("AUTH0_ISSUER", "").strip() or None
    leeway = int(os.environ.get("AUTH0_JWT_LEEWAY_SECONDS", "0") or "0")

    try:
        jwt_str = _read_jwt(args)
    except FileNotFoundError as e:
        bad = getattr(e, "filename", None) or (e.args[0] if e.args else args.file)
        print(f"JWT file not found: {bad}", file=sys.stderr)
        print(f"Current working directory: {os.getcwd()}", file=sys.stderr)
        print(
            "Create the file with only the JWT on one line, or use an absolute path, "
            "or set AUTH0_TEST_JWT in .env",
            file=sys.stderr,
        )
        return 2

    if not jwt_str:
        print(
            "Missing JWT: pass as argument, set AUTH0_TEST_JWT in .env, or use --file path/to/token.txt",
            file=sys.stderr,
        )
        return 2

    try:
        verifier = Auth0AdminVerifier(
            domain=domain,
            audience=audience,
            issuer=issuer,
            required_permissions=(
                [] if args.no_permission_check else list(ADMIN_GATEWAY_REQUIRED_PERMISSIONS)
            ),
            leeway_seconds=leeway,
        )
    except Auth0AdminConfigError as e:
        print(f"Config error: {e}", file=sys.stderr)
        print("Set AUTH0_DOMAIN and AUTH0_AUDIENCE (see .env.example).", file=sys.stderr)
        return 2

    try:
        user = verifier.verify(jwt_str, check_permissions=not args.no_permission_check)
    except Auth0AdminTokenError as e:
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print(f"Token error: {e}", file=sys.stderr)
        return 1

    payload = {
        "ok": True,
        "user": user.to_public_dict(),
        "required_permissions": list(verifier.required_permissions),
        "has_all_required": user.has_all_permissions(verifier.required_permissions),
    }
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print("Verification OK\n")
        print(json.dumps(payload, indent=2, default=str))
        if user.raw_claims and not args.json:
            print("\n--- All decoded claims (debug) ---")
            print(json.dumps(user.raw_claims, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
