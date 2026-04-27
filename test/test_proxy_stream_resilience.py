"""Regression tests for streaming proxy resilience."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault(
    "MODELS_CONFIG_PATH",
    str(Path(__file__).resolve().parent.parent / "src" / "router" / "models_config.dev.yaml"),
)

from src.gateway.context import GatewayContext
from src.gateway.provider_upstream import UpstreamTarget
from src.gateway.proxy import _proxy_stream


def _context() -> GatewayContext:
    ctx = GatewayContext(
        turn_id=str(uuid.uuid4()),
        session_id="sess-1",
        conversation_id=None,
        user_message="stream test",
        user_id="user-1",
        user_name=None,
        user_email=None,
        company_id=None,
        company_name=None,
    )
    ctx.app_id = "test-app"
    ctx.upstream_env = "dev"
    return ctx


@pytest.mark.asyncio
async def test_stream_upstream_json_is_converted_to_sse_error():
    ctx = _context()
    settings = MagicMock()
    settings.upstream_timeout = 30

    target = UpstreamTarget(
        chat_completions_url="http://upstream.example/v1/chat/completions",
        api_model="gpt-4o-mini",
        headers={},
        selected_env="dev",
        api_key_source="OPENROUTER_API_DEV",
    )

    upstream = MagicMock()
    upstream.status_code = 200
    upstream.headers = {"content-type": "application/json"}
    upstream.aread = AsyncMock(
        return_value=b'{"error":{"message":"upstream sent json"}}'
    )

    stream_cm = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=upstream)
    stream_cm.__aexit__ = AsyncMock(return_value=False)

    inner_client = MagicMock()
    inner_client.stream = MagicMock(return_value=stream_cm)

    outer_cm = MagicMock()
    outer_cm.__aenter__ = AsyncMock(return_value=inner_client)
    outer_cm.__aexit__ = AsyncMock(return_value=False)

    accumulator = MagicMock()
    accumulator.touch_activity = AsyncMock()
    accumulator.record = AsyncMock()

    cb = MagicMock()
    cb.is_open.return_value = False
    cb.record_success = MagicMock()
    cb.record_failure = MagicMock()

    with patch("src.gateway.proxy.get_accumulator", return_value=accumulator):
        with patch("src.gateway.proxy.get_circuit_breaker", return_value=cb):
            with patch("src.gateway.proxy.record_model_success") as record_success:
                with patch("src.gateway.proxy.httpx.AsyncClient", return_value=outer_cm):
                    response = await _proxy_stream(
                        body={
                            "model": "gpt-4o-mini",
                            "messages": [{"role": "user", "content": "hi"}],
                            "stream": True,
                        },
                        ctx=ctx,
                        settings=settings,
                        upstream_target=target,
                        upstream_env="dev",
                    )

                    chunks = [chunk async for chunk in response.body_iterator]
                    body = b"".join(chunks).decode("utf-8")

                    assert response.media_type == "text/event-stream"
                    assert "data: " in body
                    assert '"error": "upstream_non_sse_response"' in body
                    assert '"upstream_error": {"message": "upstream sent json"}' in body
                    assert "upstream sent json" in body
                    record_success.assert_called_once_with("gpt-4o-mini")
                    cb.record_success.assert_called_once_with("gpt-4o-mini")
                    accumulator.record.assert_awaited_once()
