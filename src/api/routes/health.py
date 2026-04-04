"""
src/api/routes/health.py

Health check e informação do sistema.
Não requer autenticação — usado por load balancers e monitorização.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from src.gateway.config import get_settings

router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: str
    version: str
    openrouter_upstream: str


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    description="Check if the gateway is operational. No authentication required.",
)
async def health_check() -> HealthResponse:
    settings = get_settings()
    return HealthResponse(
        status="ok",
        version="2.0.0",
        openrouter_upstream=settings.upstream_url,
    )
