"""
PayPal Model Context Protocol (MCP) API Routes for Agent Wallet Guard.
Exposes read-only discovery of tools returned by the PayPal MCP server.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from app.config import settings
from app.mcp_tools import PayPalMCPError, get_mcp_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP Tools"])


def verify_agent_api_key(x_agent_key: Optional[str] = Header(None, alias="X-Agent-Key")):
    """Authenticate agent requests via header API key."""
    if not x_agent_key or x_agent_key.strip() != settings.AGENT_API_KEY.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: invalid or missing X-Agent-Key header",
        )
    return x_agent_key


class MCPToolCallRequest(BaseModel):
    name: str = Field(..., description="Tool name, e.g. 'create_order' or 'list_products'")
    arguments: Dict[str, Any] = Field(default_factory=dict, description="Tool input arguments")


@router.get("/tools")
async def list_paypal_mcp_tools():
    """
    Returns the list of tools discovered from the PayPal MCP server via tools/list.
    Used by the dashboard 'PayPal tools' panel and autonomous agents.
    """
    mcp_client = get_mcp_client()
    try:
        tools = await mcp_client.list_tools()
        return {
            "endpoint": f"{settings.PAYPAL_MCP_URL}/sse",
            "mock": settings.PAYPAL_MOCK,
            "tools_count": len(tools),
            "tools": tools,
        }
    except Exception as exc:
        logger.warning("Failed to list PayPal MCP tools: %s", exc)
        return {
            "endpoint": f"{settings.PAYPAL_MCP_URL}/sse",
            "mock": True,
            "tools_count": 0,
            "tools": [],
            "error": str(exc),
        }


@router.post("/tools/call")
async def call_paypal_mcp_tool(
    payload: MCPToolCallRequest,
    _key: str = Depends(verify_agent_api_key),
):
    """
    Executes a read-only or draft tool call through the PayPal MCP server.
    Note: Real payment payouts always go through Guard's spending firewall.
    """
    # Prohibit any direct payout / transfer attempts through MCP tool calling
    restricted_tools = ["payout", "pay_order", "transfer", "disburse", "send_money"]
    if any(r in payload.name.lower() for r in restricted_tools):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Tool '{payload.name}' is restricted: all financial disbursements must route through POST /api/v1/payment-requests.",
        )

    mcp_client = get_mcp_client()
    try:
        result = await mcp_client.call_tool(payload.name, payload.arguments)
        inner = result.get("result", result) if isinstance(result, dict) else result
        return {"ok": True, "tool": payload.name, "result": inner}
    except PayPalMCPError as exc:
        logger.warning("MCP tool execution error: %s", exc)
        return {"ok": False, "tool": payload.name, "error": str(exc), "fallback_to_rest": True}
