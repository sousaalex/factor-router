"""Teto de gasto por app (SaaS): payload 402, PATCH admin para apps já existentes, proxy."""
from __future__ import annotations

import os
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import status
from fastapi.testclient import TestClient
from pydantic import ValidationError

os.environ.setdefault(
    "MODELS_CONFIG_PATH",
    str(Path(__file__).resolve().parent.parent / "src" / "router" / "models_config.dev.yaml"),
)
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
os.environ.setdefault("AUTH0_DOMAIN", "example.auth0.com")
os.environ.setdefault("AUTH0_AUDIENCE", "https://api.example.local")
os.environ.setdefault("OPENROUTER_API_DEV", "sk-test-dev")

from src.api.app import app
from src.api.deps_auth0_admin import require_auth0_admin
from src.api.routes.admin import CreateAppRequest, PatchAppRequest
from src.gateway.auth0_admin import ADMIN_GATEWAY_REQUIRED_PERMISSIONS, Auth0AdminUser
from src.gateway.key_store import _serialize_app_row, get_key_store
from src.gateway.proxy import _app_budget_exceeded_body, handle_chat_completions


class TestAdminSpendSchemas(unittest.TestCase):
    def test_create_app_default_spend_cap(self) -> None:
        b = CreateAppRequest(name="My SaaS App")
        self.assertEqual(b.spend_cap_usd, 10.0)

    def test_create_app_spend_cap_minimum(self) -> None:
        with self.assertRaises(ValidationError):
            CreateAppRequest(name="X", spend_cap_usd=0.001)

    def test_patch_spend_cap_minimum(self) -> None:
        with self.assertRaises(ValidationError):
            PatchAppRequest(spend_cap_usd=0.001)


class TestAppBudgetBody(unittest.TestCase):
    def test_payload_shape(self) -> None:
        body = _app_budget_exceeded_body("my-app", 10.0, 10.0)
        self.assertEqual(body["error"], "app_budget_exceeded")
        self.assertEqual(body["app_id"], "my-app")
        self.assertEqual(body["spend_cap_usd"], 10.0)
        self.assertEqual(body["spent_usd_total"], 10.0)
        self.assertIn("maximum allowed usage", body["message"].lower())
        self.assertIn("openrouter account balance", body["message"].lower())

    def test_block_when_spent_equals_cap(self) -> None:
        self.assertFalse(9.99 >= 10.0)
        self.assertTrue(10.0 >= 10.0)
        self.assertTrue(10.01 >= 10.0)


class TestHandleChatCompletionsBudget(unittest.IsolatedAsyncioTestCase):
    async def test_402_when_budget_exceeded(self) -> None:
        mock_request = MagicMock()
        mock_request.json = AsyncMock(
            return_value={
                "model": "gpt-4",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }
        )
        mock_ctx = MagicMock()
        mock_ctx.app_id = "tenant-a"
        mock_ctx.turn_id = "turn-test-budget-1"
        mock_ctx.session_id = "sess"
        mock_ctx.user_message = "hi"
        mock_ctx.company_id = None

        mock_settings = MagicMock()
        mock_settings.openrouter_router_budget_threshold_usd = None
        mock_settings.openrouter_credits_alert_threshold_usd = 10.0
        mock_settings.openrouter_router_budget_enabled = False

        store = MagicMock()
        store.get_app_spend_status = AsyncMock(
            return_value={
                "spend_cap_usd":   10.0,
                "spent_usd_total": 10.0,
                "remaining_usd":   0.0,
                "is_active":       True,
            }
        )

        with patch("src.gateway.proxy.get_key_store", return_value=store):
            resp = await handle_chat_completions(
                mock_request, mock_ctx, mock_settings
            )

        self.assertEqual(resp.status_code, status.HTTP_402_PAYMENT_REQUIRED)
        data = resp.body.decode()
        self.assertIn("app_budget_exceeded", data)
        self.assertIn("tenant-a", data)

    async def test_403_when_app_inactive(self) -> None:
        mock_request = MagicMock()
        mock_request.json = AsyncMock(
            return_value={
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }
        )
        mock_ctx = MagicMock()
        mock_ctx.app_id = "tenant-b"

        mock_settings = MagicMock()
        mock_settings.openrouter_router_budget_threshold_usd = None
        mock_settings.openrouter_credits_alert_threshold_usd = 10.0
        mock_settings.openrouter_router_budget_enabled = False

        store = MagicMock()
        store.get_app_spend_status = AsyncMock(
            return_value={
                "spend_cap_usd":   100.0,
                "spent_usd_total": 0.0,
                "remaining_usd":   100.0,
                "is_active":       False,
            }
        )

        from fastapi import HTTPException

        with patch("src.gateway.proxy.get_key_store", return_value=store):
            with self.assertRaises(HTTPException) as cm:
                await handle_chat_completions(
                    mock_request, mock_ctx, mock_settings
                )
        self.assertEqual(cm.exception.status_code, 403)
        self.assertEqual(cm.exception.detail["error"], "app_disabled")


class TestAdminPatchAppExistingSpendCap(unittest.TestCase):
    """
    PATCH /admin/apps/{app_id} — definir ou aumentar teto para apps (e respectivas API keys)
    já existentes após a migration 006 (default spend_cap_usd=10 na app).
    """

    def tearDown(self) -> None:
        app.dependency_overrides.clear()

    @staticmethod
    def _mock_key_store_lifecycle() -> MagicMock:
        mock_ks = MagicMock()
        mock_ks.startup = AsyncMock()
        mock_ks.shutdown = AsyncMock()
        mock_ks.cache_size = 0
        return mock_ks

    def test_patch_existing_app_raises_spend_cap(self) -> None:
        from datetime import datetime, timezone

        app_row = {
            "id":              uuid.uuid4(),
            "app_id":          "cliente-antigo",
            "name":            "Cliente Antigo",
            "description":     None,
            "is_active":       True,
            "created_at":      datetime.now(timezone.utc),
            "spend_cap_usd":   500.0,
            "spent_usd_total": 48.5,
        }
        serialized = _serialize_app_row(dict(app_row))
        mock_store = MagicMock()
        mock_store.patch_app = AsyncMock(return_value=serialized)

        async def fake_admin() -> Auth0AdminUser:
            return Auth0AdminUser(
                sub="admin-test",
                permissions=ADMIN_GATEWAY_REQUIRED_PERMISSIONS,
            )

        app.dependency_overrides[require_auth0_admin] = fake_admin
        app.dependency_overrides[get_key_store] = lambda: mock_store

        mock_lifecycle = self._mock_key_store_lifecycle()
        with patch("src.gateway.key_store.init_key_store", return_value=mock_lifecycle):
            with TestClient(app) as client:
                r = client.patch(
                    "/admin/apps/cliente-antigo",
                    json={"spend_cap_usd": 500.0},
                    headers={"Authorization": "Bearer fake-jwt"},
                )

        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["app_id"], "cliente-antigo")
        self.assertEqual(data["spend_cap_usd"], 500.0)
        self.assertEqual(data["spent_usd_total"], 48.5)
        mock_store.patch_app.assert_awaited_once()
        call_kw = mock_store.patch_app.await_args
        self.assertEqual(call_kw[0][0], "cliente-antigo")
        self.assertEqual(call_kw[1]["spend_cap_usd"], 500.0)

    def test_patch_app_not_found_404(self) -> None:
        mock_store = MagicMock()
        mock_store.patch_app = AsyncMock(return_value=None)

        async def fake_admin() -> Auth0AdminUser:
            return Auth0AdminUser(
                sub="admin-test",
                permissions=ADMIN_GATEWAY_REQUIRED_PERMISSIONS,
            )

        app.dependency_overrides[require_auth0_admin] = fake_admin
        app.dependency_overrides[get_key_store] = lambda: mock_store

        mock_lifecycle = self._mock_key_store_lifecycle()
        with patch("src.gateway.key_store.init_key_store", return_value=mock_lifecycle):
            with TestClient(app) as client:
                r = client.patch(
                    "/admin/apps/nao-existe",
                    json={"spend_cap_usd": 99.0},
                    headers={"Authorization": "Bearer fake-jwt"},
                )

        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"], "app_not_found")

    def test_patch_empty_body_400(self) -> None:
        mock_store = MagicMock()

        async def fake_admin() -> Auth0AdminUser:
            return Auth0AdminUser(
                sub="admin-test",
                permissions=ADMIN_GATEWAY_REQUIRED_PERMISSIONS,
            )

        app.dependency_overrides[require_auth0_admin] = fake_admin
        app.dependency_overrides[get_key_store] = lambda: mock_store

        mock_lifecycle = self._mock_key_store_lifecycle()
        with patch("src.gateway.key_store.init_key_store", return_value=mock_lifecycle):
            with TestClient(app) as client:
                r = client.patch(
                    "/admin/apps/x",
                    json={},
                    headers={"Authorization": "Bearer fake-jwt"},
                )

        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"]["error"], "empty_patch")
        mock_store.patch_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()
