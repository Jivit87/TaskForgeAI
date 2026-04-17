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
      - async connect() with error tracking
      - async call_tool() with idempotency key dedup
      - health_status dict for dashboard polling
      - reconnect() on detected failure
    """

    def __init__(self):
        self.configs: dict[str, dict] = _load_mcp_configs()
        self.connections: dict[str, Any] = {}
        self.health_status: dict[str, str] = {
            name: "disconnected" for name in self.configs
        }
        # Idempotency — key → cached result
        self._call_log: set[str] = set()
        self._call_cache: dict[str, Any] = {}

    # ── Connection lifecycle ──────────────────────────────────────────────────

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
            conn = await mcp.connect(config)
            self.connections[name] = conn
            self.health_status[name] = "healthy"
            log.info(f"[mcp] Connected → {name}")

        except ImportError:
            # mcp package not yet installed — use stub mode for local dev
            log.warning(f"[mcp] `mcp` package not found — using STUB for {name}")
            self.connections[name] = _StubMCPConnection(name)
            self.health_status[name] = "stub"

        except Exception as exc:
            self.health_status[name] = "failed"
            log.error(f"[mcp] Connection failed for {name}: {exc}")

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
        # Health check + reconnect
        if self.health_status.get(server) not in ("healthy", "stub"):
            await self.reconnect(server)

        # Idempotency check
        if idempotent:
            idem_key = self._make_idem_key(server, tool, args)
            if idem_key in self._call_log:
                log.info(f"[mcp] Idempotency hit — skipping duplicate call: {tool}")
                cached = self._call_cache.get(idem_key, {})
                # Return with idempotency_hit flag so the agent tool loop can break early
                if isinstance(cached, dict):
                    return {**cached, "idempotency_hit": True}
                return {"result": cached, "idempotency_hit": True}

        # Execute
        conn = self.connections.get(server)
        if conn is None:
            raise RuntimeError(f"No active connection for MCP server: {server}")

        log.info(f"[mcp] → {server}.{tool}({list(args.keys())})")
        result = await conn.call_tool(tool, args)

        # Cache result for idempotency
        if idempotent:
            idem_key = self._make_idem_key(server, tool, args)
            self._call_log.add(idem_key)
            self._call_cache[idem_key] = result

        return result

    def call_tool_for_agent(self, agent_name: str, tool: str, args: dict):
        """Convenience: resolve agent → MCP server automatically."""
        server = AGENT_MCP_MAP.get(agent_name)
        if not server:
            raise ValueError(f"No MCP server mapped for agent: {agent_name}")
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self.call_tool(server, tool, args)
        )

    # ── Health ────────────────────────────────────────────────────────────────

    def get_health(self) -> dict[str, str]:
        """Return health status for all servers (used by dashboard API)."""
        return dict(self.health_status)

    def is_healthy(self, server: str) -> bool:
        return self.health_status.get(server) in ("healthy", "stub")

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _make_idem_key(server: str, tool: str, args: dict) -> str:
        payload = f"{server}:{tool}:{json.dumps(args, sort_keys=True)}"
        return hashlib.sha256(payload.encode()).hexdigest()

    async def close_all(self) -> None:
        for name, conn in self.connections.items():
            try:
                if hasattr(conn, "close"):
                    await conn.close()
                log.info(f"[mcp] Closed → {name}")
            except Exception as exc:
                log.warning(f"[mcp] Error closing {name}: {exc}")


# ── Stub connection (local dev / no mcp package) ──────────────────────────────

class _StubMCPConnection:
    """
    Stand-in MCP connection for local development without live API keys.
    Returns realistic placeholder data so the full agent pipeline can be
    exercised end-to-end without real external services.
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
        log.warning(f"[stub_mcp] {self.server_name}.{tool}({args}) → returning stub data")
        return self.STUB_RESPONSES.get(tool, {"status": "stub_ok", "tool": tool})

    async def close(self):
        pass
