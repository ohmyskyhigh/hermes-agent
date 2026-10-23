"""Tests for OAuth server metadata persistence across process restarts.

Covers:
- :class:`HermesTokenStorage` ``.meta.json`` roundtrip (save / load / remove)
- The production manager provider
  (:class:`tools.mcp_oauth_manager.HermesMCPOAuthProvider`) restoring metadata
  on cold-load init and persisting metadata at the end of ``async_auth_flow``.

Context
=======
The MCP SDK discovers OAuth server metadata (``token_endpoint``, etc.)
on-demand and keeps it in memory only. Without disk persistence a restart
forces the SDK to fall back to guessing ``{server_url}/token``, which returns
404 on most real providers and triggers a full browser re-auth even when the
refresh token is still valid. These tests lock in the disk persistence
layer so refresh across restarts stays quiet.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytest.importorskip("mcp.shared.auth", reason="MCP SDK not installed")

from mcp.shared.auth import OAuthMetadata

from tools.mcp_oauth import HermesTokenStorage
from tools.mcp_oauth_manager import _HERMES_PROVIDER_CLS


def _make_metadata(token_endpoint: str = "https://auth.example.com/oauth/token") -> OAuthMetadata:
    return OAuthMetadata.model_validate(
        {
            "issuer": "https://auth.example.com",
            "authorization_endpoint": "https://auth.example.com/oauth/authorize",
            "token_endpoint": token_endpoint,
            "response_types_supported": ["code"],
        }
    )


# ---------------------------------------------------------------------------
# HermesTokenStorage metadata roundtrip
# ---------------------------------------------------------------------------


class TestMetadataStorage:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        storage = HermesTokenStorage("example-server")

        meta = _make_metadata()
        storage.save_oauth_metadata(meta)

        meta_path = tmp_path / "mcp-tokens" / "example-server.meta.json"
        assert meta_path.exists()

        loaded = storage.load_oauth_metadata()
        assert loaded is not None
        assert str(loaded.token_endpoint) == "https://auth.example.com/oauth/token"
        assert str(loaded.issuer).rstrip("/") == "https://auth.example.com"


    def test_remove_deletes_meta_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        storage = HermesTokenStorage("cleanup-server")

        storage.save_oauth_metadata(_make_metadata())
        assert storage._meta_path().exists()

        storage.remove()
        assert not storage._meta_path().exists()


# ---------------------------------------------------------------------------
# Manager-path provider (HermesMCPOAuthProvider) — production code path
# ---------------------------------------------------------------------------


def _manager_provider_with_context(storage: HermesTokenStorage, **context_attrs):
    """Build an uninitialized manager provider with a mocked context.

    Bypasses the full OAuthClientProvider init so we can exercise the
    override logic in isolation.
    """
    if _HERMES_PROVIDER_CLS is None:
        pytest.skip("MCP SDK auth not available")
    provider = _HERMES_PROVIDER_CLS.__new__(_HERMES_PROVIDER_CLS)
    provider._hermes_server_name = context_attrs.get("server_name", "srv")
    provider._hermes_home = ""
    provider._hermes_authorization_server_url = None
    provider._hermes_authorization_server_selected = False
    provider._hermes_authorization_server_bound = False
    provider._hermes_prm_discovery_urls = set()
    provider._hermes_asm_discovery_urls = set()
    provider._validate_resource_url_callback = None
    context = MagicMock()
    context.storage = storage
    context.oauth_metadata = context_attrs.get("oauth_metadata")
    context.current_tokens = context_attrs.get("current_tokens")
    context.server_url = context_attrs.get("server_url", "https://example.com")
    context.auth_server_url = context_attrs.get("auth_server_url", context.server_url)
    import anyio
    context.lock = anyio.Semaphore(1, max_value=1)
    context.update_token_expiry = MagicMock()
    provider.context = context
    return provider


class TestManagerOAuthProviderMetadata:
    def test_manager_rebuilds_when_configured_authorization_server_changes(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr("tools.mcp_oauth._is_interactive", lambda: True)
        from tools.mcp_oauth_manager import MCPOAuthManager

        manager = MCPOAuthManager()
        first = manager.get_or_build_provider(
            "srv", "https://mcp.example.com/mcp",
            {"authorization_server_url": "https://idp-one.example.com"},
        )
        second = manager.get_or_build_provider(
            "srv", "https://mcp.example.com/mcp",
            {"authorization_server_url": "https://idp-two.example.com"},
        )

        assert second is not first
        assert second._hermes_authorization_server_url == "https://idp-two.example.com"
        with pytest.raises(ValueError, match="authorization_server_url"):
            manager.get_or_build_provider(
                "invalid", "https://mcp.example.com/mcp",
                {"authorization_server_url": "http://idp.example.com?token=secret"},
            )

    @pytest.mark.asyncio
    async def test_auth_flow_selects_configured_server_only_after_prm_advertises_it(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        provider = _manager_provider_with_context(
            HermesTokenStorage("srv"), server_url="https://mcp.example.com/mcp"
        )
        provider._hermes_authorization_server_url = "https://chosen.example.com"
        from tools.mcp_tool import sdk_httpx

        httpx = sdk_httpx()
        resource_request = httpx.Request("POST", "https://mcp.example.com/mcp")
        prm_request = httpx.Request(
            "GET", "https://mcp.example.com/.well-known/oauth-protected-resource"
        )

        async def fake_parent_flow(self, request):
            async with self.context.lock:
                yield request
                prm_response = yield prm_request
                prm = json.loads(await prm_response.aread())
                self.context.auth_server_url = prm["authorization_servers"][0]
                yield httpx.Request(
                    "GET", f"{self.context.auth_server_url}/.well-known/oauth-authorization-server"
                )

        manager = MagicMock()
        manager.invalidate_if_disk_changed = AsyncMock(return_value=False)
        with patch.object(
            _HERMES_PROVIDER_CLS.__bases__[-1], "async_auth_flow", new=fake_parent_flow
        ), patch("tools.mcp_oauth_manager.get_manager", return_value=manager):
            flow = provider.async_auth_flow(resource_request)
            assert await flow.__anext__() is resource_request
            unauthorized = httpx.Response(
                401,
                headers={
                    "WWW-Authenticate": 'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
                },
                request=resource_request,
            )
            assert await flow.asend(unauthorized) is prm_request
            prm_response = httpx.Response(
                200,
                json={
                    "resource": "https://mcp.example.com/mcp",
                    "authorization_servers": [
                        "https://other.example.com",
                        "https://chosen.example.com/",
                    ],
                },
                request=prm_request,
            )
            metadata_request = await flow.asend(prm_response)
            assert str(metadata_request.url) == \
                "https://chosen.example.com/.well-known/oauth-authorization-server"
            assert provider.context.auth_server_url == "https://chosen.example.com"
            await flow.aclose()

        from mcp.client.auth import OAuthFlowError
        with pytest.raises(OAuthFlowError, match="not a trailing-slash-equivalent"):
            provider._select_authorization_server(["https://other.example.com"])

    def test_initialize_restores_metadata_from_disk(self, tmp_path, monkeypatch):
        """Cold-load: if we have no in-memory metadata but disk has some, restore it."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        storage = HermesTokenStorage("mgr-srv")
        storage.save_oauth_metadata(_make_metadata("https://mgr.example.com/token"))
        provider = _manager_provider_with_context(storage, oauth_metadata=None)

        with patch.object(
            _HERMES_PROVIDER_CLS.__bases__[-1], "_initialize", new=AsyncMock()
        ):
            asyncio.run(provider._initialize())

        assert provider.context.oauth_metadata is not None
        assert str(provider.context.oauth_metadata.token_endpoint) == \
            "https://mgr.example.com/token"


    def test_async_auth_flow_persists_on_completion(self, tmp_path, monkeypatch):
        """End-to-end: running the wrapped auth_flow persists discovered metadata."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        storage = HermesTokenStorage("flow-srv")
        provider = _manager_provider_with_context(
            storage,
            oauth_metadata=_make_metadata("https://flow.example.com/token"),
            server_name="flow-srv",
        )

        async def fake_parent_flow(self, request):
            if False:
                yield  # pragma: no cover -- make this an async generator

        manager = MagicMock()
        manager.invalidate_if_disk_changed = AsyncMock(return_value=False)

        with patch.object(
            _HERMES_PROVIDER_CLS.__bases__[-1],
            "async_auth_flow",
            new=fake_parent_flow,
        ), patch("tools.mcp_oauth_manager.get_manager", return_value=manager):
            async def drive():
                gen = provider.async_auth_flow(MagicMock())
                async for _ in gen:
                    pass

            asyncio.run(drive())

        loaded = storage.load_oauth_metadata()
        assert loaded is not None
        assert str(loaded.token_endpoint) == "https://flow.example.com/token"
