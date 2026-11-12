"""
PayPal Model Context Protocol (MCP) Client Wrapper for Agent Wallet Guard.

Provides connection to the PayPal MCP Server (remote SSE endpoint at https://mcp.sandbox.paypal.com/sse
or local @paypal/mcp package). Used for agentic commerce operations (read-only tools, product discovery,
and order/invoice drafting) while financial settlement and payouts remain strictly guarded by Guard's
REST spending firewall.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional
import httpx
from app.config import settings
from app.paypal import get_access_token

logger = logging.getLogger(__name__)

# Standard fallback / mock MCP tool catalog (matches remote schema)
MOCK_MCP_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "create_order",
        "description": "Create an order in PayPal sandbox. Used to initialize purchase intent before authorization.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "intent": {"type": "string", "enum": ["CAPTURE", "AUTHORIZE"], "default": "CAPTURE"},
                "amount": {"type": "string", "description": "Total payment amount (e.g. '20.00')"},
                "currency": {"type": "string", "default": "USD"},
                "payee_email": {"type": "string", "description": "Payee settlement email"}
            },
            "required": ["amount"]
        }
    },
    {
        "name": "get_order",
        "description": "Retrieves the order details from PayPal for a given order ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "The PayPal Order ID"}
            },
            "required": ["order_id"]
        }
    },
    {
        "name": "create_invoice",
        "description": "Create Invoices on PayPal. Creates a draft invoice specifying currency, recipient, and line items.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "currency_code": {"type": "string", "default": "USD"},
                "invoicer_email_address": {"type": "string"},
                "primary_recipients": {"type": "array"},
                "items": {"type": "array"}
            }
        }
    },
    {
        "name": "list_invoices",
        "description": "List invoices from PayPal. Retrieves recent merchant billing invoices.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 10}
            }
        }
    },
    {
        "name": "get_invoice",
        "description": "Get an invoice from PayPal. Retrieves details of a specific invoice by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string"}
            },
            "required": ["invoice_id"]
        }
    },
    {
        "name": "create_product",
        "description": "Create a product in PayPal product catalog - create product item and pricing.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {"type": "string", "default": "PHYSICAL"},
                "category": {"type": "string"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "list_products",
        "description": "List products from PayPal product catalog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 10}
            }
        }
    },
    {
        "name": "list_transactions",
        "description": "List transactions from PayPal for the authenticated merchant account.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "start_date": {"type": "string", "description": "ISO 8601 start date"},
                "end_date": {"type": "string", "description": "ISO 8601 end date"}
            }
        }
    },
    {
        "name": "get_merchant_insights",
        "description": "Retrieve business intelligence metrics and analytics for a merchant.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "insight_type": {"type": "string", "enum": ["ORDERS", "SALES"], "default": "ORDERS"}
            }
        }
    }
]


class PayPalMCPError(RuntimeError):
    """Raised when an MCP protocol or server error occurs."""
    pass


class PayPalMCPClient:
    """
    Model Context Protocol (MCP) Client for PayPal Sandbox.
    Connects to the SSE transport endpoint, establishes a session,
    and supports JSON-RPC 2.0 tools/list and tools/call methods.
    """

    def __init__(self, base_url: Optional[str] = None):
        self.base_url = (base_url or settings.PAYPAL_MCP_URL).rstrip("/")
        self._cached_tools: Optional[List[Dict[str, Any]]] = None
        self._cache_timestamp: float = 0.0
        self._cache_ttl_seconds: float = 300.0  # 5 minutes

    async def list_tools(self, force_refresh: bool = False) -> List[Dict[str, Any]]:
        """
        Retrieves the list of tools available on the PayPal MCP server via tools/list.
        Falls back to cached or mock tools if MCP server is offline, mocked, or unreachable.
        """
        now = time.time()
        if not force_refresh and self._cached_tools and (now - self._cache_timestamp < self._cache_ttl_seconds):
            return self._cached_tools

        # In mock mode, immediately return the mock tool catalog
        if settings.PAYPAL_MOCK:
            logger.info("PAYPAL_MOCK=True: Returning mocked PayPal MCP tool catalog (%d tools)", len(MOCK_MCP_TOOLS))
            self._cached_tools = MOCK_MCP_TOOLS
            self._cache_timestamp = now
            return self._cached_tools

        # Attempt remote live connection via SSE
        try:
            tools = await self._remote_list_tools()
            if tools:
                self._cached_tools = tools
                self._cache_timestamp = now
                logger.info("Successfully discovered %d live PayPal MCP tools via SSE", len(tools))
                return tools
        except Exception as exc:
            logger.warning("Remote PayPal MCP tools/list failed (%s). Falling back to mock catalog.", exc)

        # Fallback
        self._cached_tools = MOCK_MCP_TOOLS
        self._cache_timestamp = now
        return self._cached_tools

    async def _remote_list_tools(self) -> List[Dict[str, Any]]:
        """Connects to the remote SSE endpoint and requests tools/list over JSON-RPC."""
        token = await get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "text/event-stream"
        }
        sse_url = f"{self.base_url}/sse"

        async with httpx.AsyncClient() as client:
            async with client.stream("GET", sse_url, headers=headers, timeout=12.0) as sse_resp:
                if sse_resp.status_code != 200:
                    raise PayPalMCPError(f"SSE handshake failed with HTTP {sse_resp.status_code}")

                # 1. Read endpoint event to get message POST path
                endpoint_path: Optional[str] = None
                lines = sse_resp.aiter_lines()
                async for line in lines:
                    line_str = line.strip()
                    if line_str.startswith("data:"):
                        endpoint_path = line_str[5:].strip()
                        break

                if not endpoint_path:
                    raise PayPalMCPError("MCP SSE server did not return a valid endpoint path.")

                post_url = f"{self.base_url}{endpoint_path}"

                # 2. POST tools/list request
                rpc_req = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {}
                }
                post_resp = await client.post(
                    post_url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json=rpc_req,
                    timeout=8.0,
                )
                if post_resp.status_code not in (200, 202):
                    raise PayPalMCPError(f"tools/list POST returned HTTP {post_resp.status_code}")

                # 3. Read JSON-RPC response from SSE stream
                async for line in lines:
                    line_str = line.strip()
                    if line_str.startswith("data:"):
                        payload = json.loads(line_str[5:])
                        if "result" in payload and "tools" in payload["result"]:
                            return payload["result"]["tools"]
                        elif "error" in payload:
                            raise PayPalMCPError(f"MCP RPC Error: {payload['error']}")

        raise PayPalMCPError("Did not receive tools/list response from SSE stream.")

    async def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Executes an MCP tool call (tools/call) against the PayPal MCP server.
        Only used for read/draft operations. Never bypasses Guard's financial engine.
        """
        if settings.PAYPAL_MOCK:
            logger.info("PAYPAL_MOCK=True: Simulating MCP tool call '%s' with args %s", name, arguments)
            return {
                "ok": True,
                "tool": name,
                "mock": True,
                "result": {
                    "id": f"MOCK-MCP-{int(time.time())}",
                    "status": "DRAFT_READY",
                    "tool": name,
                    "message": f"Simulated MCP response for {name}",
                }
            }

        try:
            token = await get_access_token()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream"
            }
            sse_url = f"{self.base_url}/sse"

            async with httpx.AsyncClient() as client:
                async with client.stream("GET", sse_url, headers=headers, timeout=12.0) as sse_resp:
                    if sse_resp.status_code != 200:
                        raise PayPalMCPError(f"SSE handshake failed with HTTP {sse_resp.status_code}")

                    endpoint_path = None
                    lines = sse_resp.aiter_lines()
                    async for line in lines:
                        line_str = line.strip()
                        if line_str.startswith("data:"):
                            endpoint_path = line_str[5:].strip()
                            break

                    if not endpoint_path:
                        raise PayPalMCPError("MCP SSE server did not return a valid endpoint path.")

                    post_url = f"{self.base_url}{endpoint_path}"
                    rpc_req = {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {
                            "name": name,
                            "arguments": arguments
                        }
                    }
                    post_resp = await client.post(
                        post_url,
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        json=rpc_req,
                        timeout=8.0,
                    )
                    if post_resp.status_code not in (200, 202):
                        raise PayPalMCPError(f"tools/call POST returned HTTP {post_resp.status_code}")

                    async for line in lines:
                        line_str = line.strip()
                        if line_str.startswith("data:"):
                            payload = json.loads(line_str[5:])
                            return payload

        except Exception as exc:
            logger.warning("Remote MCP tool call '%s' failed: %s", name, exc)
            raise PayPalMCPError(f"Tool call '{name}' failed: {exc}") from exc


_mcp_client_instance: Optional[PayPalMCPClient] = None


def get_mcp_client() -> PayPalMCPClient:
    """Returns singleton PayPalMCPClient instance."""
    global _mcp_client_instance
    if _mcp_client_instance is None:
        _mcp_client_instance = PayPalMCPClient()
    return _mcp_client_instance

