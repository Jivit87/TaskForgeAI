from __future__ import annotations
"""
tools/mcp_manager.py
MCPConnectionManager — manages connections to all 4 MCP servers.

Responsibilities:
  - Connect / reconnect MCP servers with health tracking
  - Idempotency key registry — prevents duplicate writes on retry
  - Async tool call dispatch with result caching for idempotent ops
  - Health check polling for the frontend dashboard
"""

import hashlib
import json
import logging
import os
from typing import Any

import httpx

log = logging.getLogger("frame_mo.mcp_manager")


# ── MCP server configurations ─────────────────────────────────────────────────

def _load_mcp_configs() -> dict[str, dict]:
    """Build MCP server configs from environment variables."""
    return {
        "gmail-mcp": {
            "type": "url",
            "url": "https://gmailmcp.googleapis.com/mcp/v1",
            "name": "gmail-mcp",
        },
        "github-mcp": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {
                "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get(
                    "GITHUB_PERSONAL_ACCESS_TOKEN", ""
                )
            },
        },
        "notion-mcp": {
            "type": "url",
            "url": "https://mcp.notion.com/mcp",
            "name": "notion-mcp",
            "headers": {
                "Authorization": f"Bearer {os.environ.get('NOTION_TOKEN', '')}",
            },
        },
        "tavily-mcp": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "@tavily/mcp-server"],
            "env": {"TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", "")},
        },
    }


# ── Agent → MCP server mapping ────────────────────────────────────────────────

AGENT_MCP_MAP: dict[str, str] = {
    "research_agent":  "tavily-mcp",
    "code_agent":      "github-mcp",
    "knowledge_agent": "notion-mcp",
    "comms_agent":     "gmail-mcp",
}


# ── MCPConnectionManager ──────────────────────────────────────────────────────

class MCPConnectionManager:
    """
    Manages the lifecycle of MCP server connections.

    Provides:
      - connect_all() / connect(name) / reconnect(name)
      - call_tool(server, tool, args)
      - get_health() / is_healthy(server)
      - Idempotency caching for write operations
    """

    def __init__(self):
        from contextlib import AsyncExitStack
        self.configs = _load_mcp_configs()
        self.connections: dict[str, Any] = {}
        self.exit_stacks: dict[str, AsyncExitStack] = {}
        self.health_status: dict[str, str] = {}
        self._idempotency_cache: dict[str, dict] = {}

    async def connect_all(self) -> None:
        """Connect to all configured MCP servers."""
        for name in self.configs:
            await self.connect(name)

    async def connect(self, name: str) -> None:
        """Attempt to connect to a single MCP server."""
        config = self.configs.get(name)
        if not config:
            log.error(f"[mcp] Unknown server: {name}")
            return

        try:
            # Real MCP connection (requires `mcp` package installed)
            import mcp
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
            try:
                from mcp.client.sse import sse_client
            except ImportError:
                sse_client = None

            from contextlib import AsyncExitStack
            stack = AsyncExitStack()

            if config["type"] == "stdio":
                server_params = StdioServerParameters(
                    command=config["command"],
                    args=config["args"],
                    env={**os.environ, **config.get("env", {})}
                )
                transport = await stack.enter_async_context(stdio_client(server_params))
            elif config["type"] == "url":
                if sse_client is None:
                    raise Exception("sse_client not available in this mcp version")
                transport = await stack.enter_async_context(sse_client(config["url"], headers=config.get("headers")))
            else:
                raise ValueError(f"Unknown mcp type {config['type']}")

            read_stream, write_stream = transport
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()

            self.exit_stacks[name] = stack
            self.connections[name] = session
            self.health_status[name] = "healthy"
            log.info(f"[mcp] Connected → {name}")

        except ImportError:
            # mcp package not yet installed — use stub mode for local dev
            log.warning(f"[mcp] `mcp` package not found — using STUB for {name}")
            self.connections[name] = _StubMCPConnection(name)
            self.health_status[name] = "stub"

        except Exception as exc:
            if 'stack' in locals():
                try:
                    await stack.aclose()
                except Exception:
                    pass
            log.error(f"[mcp] Connection failed for {name}: {exc}")
            log.warning(f"[mcp] Falling back to STUB for {name} due to error")
            self.connections[name] = _StubMCPConnection(name)
            self.health_status[name] = "stub"

    async def reconnect(self, name: str) -> None:
        """Reconnect a failed or disconnected server."""
        log.info(f"[mcp] Reconnecting → {name}")
        self.health_status[name] = "reconnecting"
        await self.connect(name)

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    async def call_tool(
        self,
        server: str,
        tool: str,
        args: dict,
        idempotent: bool = True,
    ) -> Any:
        """
        Call a tool on the specified MCP server.

        If the server is unhealthy, attempts reconnect first.
        If idempotent=True, caches the result and skips duplicate calls.
        """
        # ── Unwrap nested "args" key that LLMs frequently hallucinate ──
        if "args" in args and isinstance(args["args"], dict):
            inner = args.pop("args")
            args.update(inner)

        # Health check + reconnect
        if self.health_status.get(server) not in ("healthy", "stub"):
            await self.reconnect(server)

        # Idempotency check
        if idempotent:
            idem_key = self._make_idem_key(server, tool, args)
            if idem_key in self._idempotency_cache:
                log.info(f"[mcp] Idempotency hit — skipping duplicate call: {tool}")
                cached = self._idempotency_cache[idem_key]
                return {**cached, "idempotency_hit": True}

        conn = self.connections.get(server)
        if not conn:
            log.error(f"[mcp] No connection for {server}")
            return {"error": f"No MCP connection for {server}"}

        log.info(f"[mcp] → {server}.{tool}({list(args.keys())})")
        try:
            if isinstance(conn, _StubMCPConnection):
                result = await conn.call_tool(tool, args)
            else:
                raw_result = await conn.call_tool(tool, arguments=args)
                try:
                    result = raw_result.model_dump()
                except AttributeError:
                    result = str(raw_result)
        except Exception as exc:
            log.error(f"[mcp] Tool call failed — {server}.{tool}: {exc}")
            return {"error": str(exc)}

        # Cache result for idempotency
        if idempotent:
            self._idempotency_cache[idem_key] = result

        return result

    async def get_tools(self, server: str) -> list[dict]:
        """
        Dynamically fetch JSON schemas for all tools hosted by this MCP server.
        Converts MCP SDK schemas into OpenAI/Groq compatible schemas.
        """
        conn = self.connections.get(server)
        if not conn or isinstance(conn, _StubMCPConnection):
            return []

        tools_list = []
        try:
            if hasattr(conn, "list_tools"):
                res = await conn.list_tools()
                for t in res.tools:
                    tools_list.append({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": getattr(t, "description", f"MCP Tool: {t.name}"),
                            "parameters": getattr(t, "inputSchema", {"type": "object", "properties": {}}),
                        }
                    })
        except Exception as exc:
            log.error(f"[mcp] Error fetching tools for {server}: {exc}")

        return tools_list

    # ── Health ────────────────────────────────────────────────────────────────
    def get_health(self) -> dict[str, str]:
        return dict(self.health_status)

    def is_healthy(self, server: str) -> bool:
        return self.health_status.get(server) in ("healthy", "stub")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _make_idem_key(server: str, tool: str, args: dict) -> str:
        payload = f"{server}:{tool}:{json.dumps(args, sort_keys=True)}"
        return hashlib.sha256(payload.encode()).hexdigest()

    async def close_all(self) -> None:
        for name, stack in self.exit_stacks.items():
            try:
                await stack.aclose()
                log.info(f"[mcp] Closed stack → {name}")
            except Exception as exc:
                log.warning(f"[mcp] Error closing {name}: {exc}")
        for name, conn in self.connections.items():
            if isinstance(conn, _StubMCPConnection):
                try:
                    await conn.close()
                except Exception as exc:
                    log.warning(f"[mcp] Error closing {name}: {exc}")


# ── Stub connection (local dev / no mcp package) ──────────────────────────────

# Tavily search_depth mapping: agent says "standard"/"shallow"/"deep" but
# the Tavily REST API only accepts "basic" or "advanced".
_DEPTH_MAP = {
    "shallow": "basic",
    "standard": "basic",
    "deep": "advanced",
    "basic": "basic",
    "advanced": "advanced",
}


class _StubMCPConnection:
    """
    Stand-in MCP connection for local development without live API keys.

    For tavily-mcp: transparently routes search/fetch_url to the real
    Tavily REST API via httpx (no mcp SDK required).

    For all other servers: returns realistic placeholder data so the full
    agent pipeline can be exercised end-to-end.
    """

    STUB_RESPONSES: dict[str, dict] = {
        "search":              {"results": [{"title": "Stub result", "url": "https://example.com", "snippet": "Stub content"}]},
        "fetch_url":           {"content": "Stub page content from example.com"},
        "get_pr_diff":         {"diff": "--- stub diff ---"},
        "create_github_issue": {"issue_number": 42, "url": "https://github.com/stub/issue/42"},
        "post_review_comment": {"comment_id": "stub_comment_001"},
        "read_notion_page":    {"page_id": "stub-page-id", "content": "Stub Notion content"},
        "create_notion_page":  {"page_id": "stub-page-id", "url": "https://notion.so/stub"},
        "append_notion_block": {"block_id": "stub-block-id"},
        "read_email_thread":   {"thread_id": "stub-thread", "messages": []},
        "draft_email":         {"message_id": "stub-draft-001"},
        "send_email":          {"message_id": "stub-sent-001", "status": "sent"},
    }

    def __init__(self, server_name: str):
        self.server_name = server_name

    async def call_tool(self, tool: str, args: dict) -> dict:
        # ── Unwrap nested "args" key that LLMs frequently hallucinate ──
        if "args" in args and isinstance(args["args"], dict):
            inner = args.pop("args")
            args.update(inner)

        # ── Tavily: transparent HTTP fallback ──────────────────────────
        if self.server_name == "tavily-mcp":
            api_key = os.environ.get("TAVILY_API_KEY")
            if api_key:
                return await self._tavily_http(tool, args, api_key)
            else:
                log.warning("[stub_mcp] TAVILY_API_KEY not set — using stub data")

        # ── All other servers: return static stub data ─────────────────
        log.warning(f"[stub_mcp] {self.server_name}.{tool}({args}) → returning stub data")
        return self.STUB_RESPONSES.get(tool, {"status": "stub_ok", "tool": tool})

    async def _tavily_http(self, tool: str, args: dict, api_key: str) -> dict:
        """Direct HTTP calls to Tavily REST API — no MCP SDK needed."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                if tool == "search":
                    query = args.get("query", "")
                    raw_depth = args.get("depth", "basic")
                    depth = _DEPTH_MAP.get(raw_depth, "basic")

                    log.info(f"[tavily-http] /search query={query!r} depth={depth}")
                    res = await client.post(
                        "https://api.tavily.com/search",
                        json={
                            "api_key": api_key,
                            "query": query,
                            "search_depth": depth,
                            "max_results": int(args.get("num_results", 5)),
                            "include_raw_content": False,
                        },
                    )
                    if res.status_code == 200:
                        data = res.json()
                        results = data.get("results", [])
                        return {
                            "results": [
                                {
                                    "title": r.get("title", ""),
                                    "url": r.get("url", ""),
                                    "snippet": r.get("content", ""),
                                }
                                for r in results
                            ]
                        }
                    log.error(f"[tavily-http] search failed {res.status_code}: {res.text[:200]}")
                    return {"error": f"Tavily search error: {res.status_code}"}

                elif tool == "fetch_url":
                    url = args.get("url", "")
                    log.info(f"[tavily-http] /extract url={url}")
                    res = await client.post(
                        "https://api.tavily.com/extract",
                        json={
                            "api_key": api_key,
                            "urls": [url],
                        },
                    )
                    if res.status_code == 200:
                        data = res.json()
                        results = data.get("results", [])
                        if results:
                            return {"content": results[0].get("raw_content", "")[:3000]}
                        return {"content": f"No content extracted from {url}"}
                    log.error(f"[tavily-http] extract failed {res.status_code}: {res.text[:200]}")
                    return {"error": f"Tavily extract error: {res.status_code}"}

                else:
                    log.warning(f"[tavily-http] Unknown tool {tool} — returning stub")
                    return self.STUB_RESPONSES.get(tool, {"status": "stub_ok", "tool": tool})

        except Exception as exc:
            log.error(f"[tavily-http] Exception: {exc}")
            return {"error": f"Tavily HTTP error: {exc}"}

    async def close(self):
        pass
