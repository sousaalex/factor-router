"""Unit tests for TTS proxy handle_audio_speech (mocked httpx)."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Request

os.environ.setdefault(
    "MODELS_CONFIG_PATH",
    str(Path(__file__).resolve().parent.parent / "src" / "router" / "models_config.dev.yaml"),
)

from src.gateway.context import GatewayContext
from src.gateway.proxy import handle_audio_speech


def _context() -> GatewayContext:
    tid = str(uuid.uuid4())
    ctx = GatewayContext(
        turn_id=tid,
        session_id="sess-1",
        conversation_id=None,
        user_message="tts test",
        user_id="user-1",
        user_name=None,
        user_email=None,
        company_id=None,
        company_name=None,
    )
    ctx.app_id = "test-app"
    return ctx


@pytest.mark.asyncio
async def test_speech_proxies_audio_bytes_and_records_usage():
    ctx = _context()
    settings = MagicMock()
    settings.speech_upstream_url = "http://upstream.example/v1/audio/speech"
    settings.speech_upstream_timeout = 120

    req = MagicMock(spec=Request)
    req.headers = {"content-type": "application/json"}
    req.json = AsyncMock(
        return_value={"model": "tts-1", "input": "Olá mundo", "response_format": "opus"}
    )

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.content = b"\x4f\x67\x67"  # fake audio
    fake_resp.headers = {"content-type": "audio/ogg"}
    fake_resp.text = ""

    inner_client = MagicMock()
    inner_client.post = AsyncMock(return_value=fake_resp)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner_client)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("src.gateway.proxy._enforce_app_budget_or_raise", new_callable=AsyncMock):
        with patch("src.gateway.proxy.httpx.AsyncClient", return_value=cm):
            with patch("src.usage.service.record_turn_usage", new_callable=AsyncMock) as rec:
                out = await handle_audio_speech(req, ctx, settings)

    assert out.status_code == 200
    assert out.body == b"\x4f\x67\x67"
    inner_client.post.assert_called_once()
    call_kw = inner_client.post.call_args
    assert call_kw[0][0] == "http://upstream.example/v1/audio/speech"
    assert call_kw[1]["json"]["model"] == "tts-1"
    rec.assert_awaited_once()


@pytest.mark.asyncio
async def test_speech_rejects_non_json_content_type():
    ctx = _context()
    settings = MagicMock()

    req = MagicMock(spec=Request)
    req.headers = {"content-type": "text/plain"}

    with patch("src.gateway.proxy._enforce_app_budget_or_raise", new_callable=AsyncMock):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as ei:
            await handle_audio_speech(req, ctx, settings)
        assert ei.value.status_code == 400


@pytest.mark.asyncio
async def test_speech_forwards_upstream_error_json():
    ctx = _context()
    settings = MagicMock()
    settings.speech_upstream_url = "http://upstream.example/v1/audio/speech"
    settings.speech_upstream_timeout = 60

    req = MagicMock(spec=Request)
    req.headers = {"content-type": "application/json"}
    req.json = AsyncMock(return_value={"model": "tts-1", "input": "x"})

    err_resp = MagicMock()
    err_resp.status_code = 503
    err_resp.headers = {"content-type": "application/json"}
    err_resp.json = MagicMock(return_value={"error": {"message": "busy"}})
    err_resp.text = ""

    inner_client = MagicMock()
    inner_client.post = AsyncMock(return_value=err_resp)

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=inner_client)
    cm.__aexit__ = AsyncMock(return_value=False)

    with patch("src.gateway.proxy._enforce_app_budget_or_raise", new_callable=AsyncMock):
        with patch("src.gateway.proxy.httpx.AsyncClient", return_value=cm):
            out = await handle_audio_speech(req, ctx, settings)

    assert out.status_code == 503
    body = out.body.decode() if isinstance(out.body, bytes) else str(out.body)
    assert "busy" in body or "error" in body.lower()
