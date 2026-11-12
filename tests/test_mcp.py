"""
Unit and integration tests for PayPal MCP integration:
- Tool discovery endpoint (/api/v1/mcp/tools)
- Safe tool execution endpoint (/api/v1/mcp/tools/call)
- Separation of concerns: MCP restricted against direct disbursements
- Fallback resilience and caching
- Demo agent AGENT_TOOL=mcp alternative path
"""

import asyncio
from unittest.mock import AsyncMock, patch
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from app.config import settings
from app.db import engine
from app.main import app
from app.mcp_tools import PayPalMCPClient, PayPalMCPError, get_mcp_client
from app.models import AuditEntry


client = TestClient(app)


def test_list_paypal_mcp_tools_endpoint():
    """Verify GET /api/v1/mcp/tools returns a valid list of tools."""
    response = client.get("/api/v1/mcp/tools")
    assert response.status_code == 200
    data = response.json()
    assert "tools" in data
    assert "tools_count" in data
    assert isinstance(data["tools"], list)
    assert data["tools_count"] == len(data["tools"])


def test_call_tool_unauthorized_without_key():
    """Verify tool execution requires X-Agent-Key."""
    response = client.post(
        "/api/v1/mcp/tools/call",
        json={"name": "list_products", "arguments": {}},
    )
    assert response.status_code == 401


def test_call_tool_restricted_disbursement_blocked():
    """Verify financial disbursement tools cannot be called directly via MCP."""
    restricted_tools = ["payout", "execute_payout", "send_money", "pay_order"]
    for tool_name in restricted_tools:
        response = client.post(
            "/api/v1/mcp/tools/call",
            headers={"X-Agent-Key": settings.AGENT_API_KEY},
            json={"name": tool_name, "arguments": {"amount": "100.00"}},
        )
        assert response.status_code == 403
        assert "restricted" in response.json()["detail"].lower()


def test_call_tool_allowed_mock_mode():
    """Verify non-restricted tool execution succeeds in mock mode."""
    with patch.object(settings, "PAYPAL_MOCK", True):
        response = client.post(
            "/api/v1/mcp/tools/call",
            headers={"X-Agent-Key": settings.AGENT_API_KEY},
            json={"name": "create_order", "arguments": {"amount": "25.00"}},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["tool"] == "create_order"
        assert "result" in data
        assert data["result"]["status"] == "DRAFT_READY"


def test_mcp_client_caching():
    """Verify MCP tools list is cached and doesn't re-fetch within TTL."""
    async def _test():
        test_client = PayPalMCPClient()
        mock_tools = [{"name": "tool_a", "description": "Tool A"}]

        with patch.object(test_client, "_remote_list_tools", new_callable=AsyncMock) as mock_fetch:
            mock_fetch.return_value = mock_tools
            with patch("app.mcp_tools.settings.PAYPAL_MOCK", False):
                # First fetch
                res1 = await test_client.list_tools()
                assert res1 == mock_tools
                assert mock_fetch.call_count == 1

                # Second fetch within cache TTL
                res2 = await test_client.list_tools()
                assert res2 == mock_tools
                assert mock_fetch.call_count == 1

                # Force refresh
                res3 = await test_client.list_tools(force_refresh=True)
                assert res3 == mock_tools
                assert mock_fetch.call_count == 2

    asyncio.run(_test())


def test_demo_agent_mcp_path_fallback(monkeypatch):
    """Verify demo agent executes cleanly with AGENT_TOOL=mcp and falls back to REST on error."""
    async def _test():
        from app.demo_agent import run_demo_agent
        from app.llm import MockLLMClient, set_llm_client

        monkeypatch.setattr(settings, "DEMO_MODE", True)
        monkeypatch.setattr(settings, "AGENT_TOOL", "mcp")
        monkeypatch.setattr(settings, "PAYPAL_MOCK", True)
        monkeypatch.setattr(settings, "AGENT_MODE", "scripted")
        set_llm_client(MockLLMClient())

        with patch("app.mcp_tools.PayPalMCPClient.call_tool", side_effect=Exception("SSE Timeout")):
            result = await run_demo_agent(scenario="normal")
            assert "id" in result
            assert result["decision"] in ["APPROVE", "ESCALATE", "BLOCK"]
            assert result["status"] in ["PAID", "APPROVED", "completed", "ESCALATED"]

        # Verify audit event logged for mcp tool invocation
        with Session(engine) as session:
            events = session.exec(
                select(AuditEntry).where(AuditEntry.event.like("mcp_tool%"))
            ).all()
            assert len(events) >= 1

        set_llm_client(None)

    asyncio.run(_test())
