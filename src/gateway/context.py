"""
src/gateway/context.py

Contexto de cada request ao gateway extraído dos headers X-*.

O app_id NÃO vem daqui — vem da API Key via auth.py.
O GatewayContext só guarda o contexto de negócio (sessão, utilizador, empresa).

Headers obrigatórios (ausente = 400):
    X-Turn-Id         — UUID v4 gerado pelo agente no início de cada turno
    X-Session-Id      — ID da sessão de chat
    X-User-Message    — texto da mensagem do utilizador (completo; URL-encoded se necessário)

Headers obrigatórios que aceitam "null" (ausente = 400, valor desconhecido = "null"):
    X-Conversation-Id
    X-User-Id
    X-User-Name
    X-User-Email
    X-Company-Id
    X-Company-Name
"""
from __future__ import annotations

import uuid
import urllib.parse
from typing import Annotated

from fastapi import Header, HTTPException, status


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _decode(value: str) -> str:
    """
    Faz URL-decode do valor do header para preservar UTF-8.
    Os agentes enviam os headers com percent-encoding:
        "Ol%C3%A1%20como%20est%C3%A1s%3F" -> "Ola como estas?"
    Valores nao encoded passam sem alteracao (retrocompativel).
    """
    try:
        return urllib.parse.unquote(value, encoding="utf-8")
    except Exception:
        return value


def _require(value: str | None, header_name: str) -> str:
    """Garante que o header existe. Ausente = 400 explícito."""
    if value is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "missing_required_header",
                "header": header_name,
                "message": (
                    f"The '{header_name}' header is required. "
                    f"If the value is unknown, send the literal string \"null\"."
                ),
            },
        )
    return value


def _nullable(value: str | None, header_name: str) -> str | None:
    """
    Header obrigatório mas cujo valor pode ser desconhecido.
    - Ausente      → 400
    - Valor "null" → None (gravado como NULL no DB)
    - Outro valor  → devolve o valor
    """
    raw = _require(value, header_name)
    return None if raw.lower() == "null" else raw


def _validate_uuid(value: str, header_name: str) -> str:
    """Valida que o valor é um UUID v4 válido."""
    try:
        uuid.UUID(value, version=4)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "invalid_header_format",
                "header": header_name,
                "message": (
                    f"The '{header_name}' header must be a valid UUID v4. "
                    f"Received: '{value}'"
                ),
            },
        )
    return value


# ─────────────────────────────────────────────────────────────────────────────
# Geração de título (AGIWeb / Factor Agent): mesmo POST /v1/chat/completions, sem router.
# ─────────────────────────────────────────────────────────────────────────────

# Valor exacto de X-Conversation-Id que activa modelo fixo (sem classificador).
GENERATE_TITLE_CONVERSATION_ID = "generate-title"


# ─────────────────────────────────────────────────────────────────────────────
# GatewayContext
# ─────────────────────────────────────────────────────────────────────────────

class GatewayContext:
    """
    Contexto de negócio de um request.
    O app_id não está aqui — vem de auth.py (API Key).
    """

    def __init__(
        self,
        turn_id: str,
        session_id: str,
        conversation_id: str | None,
        user_message: str,
        user_id: str | None,
        user_name: str | None,
        user_email: str | None,
        company_id: str | None,
        company_name: str | None,
    ) -> None:
        self.app_id          = None   # preenchido pelo proxy.py a partir do auth
        self.upstream_env    = None   # preenchido pelo proxy.py com base no label da API key
        self.turn_id         = turn_id
        self.session_id      = session_id
        self.conversation_id = conversation_id
        self.user_message    = user_message
        self.user_id         = user_id
        self.user_name       = user_name
        self.user_email      = user_email
        self.company_id      = company_id
        self.company_name    = company_name

    @property
    def is_title_generation_request(self) -> bool:
        """True quando X-Conversation-Id é o valor reservado para título (sem router)."""
        return (self.conversation_id or "").strip() == GENERATE_TITLE_CONVERSATION_ID

    @property
    def accumulator_bucket_id(self) -> str:
        """
        Chave do balde de custos. O título partilha X-Turn-Id com o chat mas precisa de
        balde/registo separados (turn_id único em llm_usage_log).
        """
        if self.is_title_generation_request:
            return f"{self.turn_id}::generate-title"
        return self.turn_id

    @classmethod
    async def from_headers(
        cls,
        # obrigatórios — nunca null
        x_turn_id:      Annotated[str | None, Header()] = None,
        x_session_id:   Annotated[str | None, Header()] = None,
        x_user_message: Annotated[str | None, Header()] = None,
        # obrigatórios — podem ser "null"
        x_conversation_id: Annotated[str | None, Header()] = None,
        x_user_id:         Annotated[str | None, Header()] = None,
        x_user_name:       Annotated[str | None, Header()] = None,
        x_user_email:      Annotated[str | None, Header()] = None,
        x_company_id:      Annotated[str | None, Header()] = None,
        x_company_name:    Annotated[str | None, Header()] = None,
    ) -> "GatewayContext":

        raw_turn_id = _require(x_turn_id,      "X-Turn-Id")
        session_id  = _require(x_session_id,   "X-Session-Id")
        user_msg    = _require(x_user_message, "X-User-Message")

        turn_id = _validate_uuid(raw_turn_id, "X-Turn-Id")

        conversation_id = _nullable(x_conversation_id, "X-Conversation-Id")
        user_id         = _nullable(x_user_id,         "X-User-Id")
        user_name       = _decode(_nullable(x_user_name,    "X-User-Name") or "") or None
        user_email      = _nullable(x_user_email,      "X-User-Email")
        company_id      = _nullable(x_company_id,      "X-Company-Id")
        company_name    = _decode(_nullable(x_company_name, "X-Company-Name") or "") or None

        return cls(
            turn_id=turn_id,
            session_id=session_id,
            conversation_id=conversation_id,
            user_message=_decode(user_msg),
            user_id=user_id,
            user_name=user_name,
            user_email=user_email,
            company_id=company_id,
            company_name=company_name,
        )

    def __repr__(self) -> str:
        return (
            f"GatewayContext("
            f"app_id={self.app_id!r}, "
            f"turn_id={self.turn_id[:8]}..., "
            f"session_id={self.session_id!r}, "
            f"company_id={self.company_id!r}"
            f")"
        )