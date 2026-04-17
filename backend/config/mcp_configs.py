from __future__ import annotations
"""
config/mcp_configs.py
MCP server configuration objects for all 4 MCP servers.

Each config dict is passed directly to MCPConnectionManager.connect().
Auth credentials are read from environment variables at call time —
never hardcoded here.
"""

import os
from typing import Literal


# ── Config types ──────────────────────────────────────────────────────────────

MCPTransport = Literal["url", "stdio"]


def _require_env(key: str) -> str:
    """Read a required env var, raise a clear error if missing."""
    val = os.environ.get(key, "")
    if not val:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Copy backend/.env.example → backend/.env and fill in your keys."
        )
    return val


# ── Gmail MCP ─────────────────────────────────────────────────────────────────

def gmail_mcp_config() -> dict:
    """
    Gmail MCP — Google-hosted remote server.
    Requires a valid Google OAuth token passed as Authorization header.
    Used by: Communication Sub-Agent
    """
    return {
        "type": "url",
        "url": "https://gmailmcp.googleapis.com/mcp/v1",
        "name": "gmail-mcp",
        "headers": {
            "Authorization": f"Bearer {os.environ.get('GMAIL_OAUTH_TOKEN', '')}",
            "Content-Type": "application/json",
        },
        "capabilities": [
            "read_email_thread",
            "list_emails",
            "draft_email",
            "send_email",
            "search_inbox",
        ],
    }


# ── GitHub MCP ────────────────────────────────────────────────────────────────

def github_mcp_config() -> dict:
    """
    GitHub MCP — runs locally via npx.
    Auth via GITHUB_PERSONAL_ACCESS_TOKEN env var passed to the child process.
    Used by: Code Sub-Agent
    """
    return {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-github"],
        "env": {
            "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get(
                "GITHUB_PERSONAL_ACCESS_TOKEN", ""
            ),
        },
        "capabilities": [
            "get_pr_diff",
            "list_issues",
            "create_github_issue",
            "post_review_comment",
            "read_file",
            "list_repos",
        ],
    }


# ── Notion MCP ────────────────────────────────────────────────────────────────

def notion_mcp_config() -> dict:
    """
    Notion MCP — Notion-hosted remote server.
    Auth via NOTION_TOKEN (Notion Integration Token).
    Used by: Knowledge Sub-Agent
    """
    return {
        "type": "url",
        "url": "https://mcp.notion.com/mcp",
        "name": "notion-mcp",
        "headers": {
            "Authorization": f"Bearer {os.environ.get('NOTION_TOKEN', '')}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        },
        "capabilities": [
            "read_notion_page",
            "create_notion_page",
            "append_notion_block",
            "search_notion",
            "list_databases",
        ],
    }


# ── Tavily Web Search MCP ────────────────────────────────────────────────────────────

def tavily_mcp_config() -> dict:
    """
    Tavily Search MCP — runs locally via npx.
    Auth via TAVILY_API_KEY env var passed to the child process.
    Used by: Research Sub-Agent
    """
    return {
        "type": "stdio",
        "command": "npx",
        "args": ["-y", "@tavily/mcp-server"],
        "env": {
            "TAVILY_API_KEY": os.environ.get("TAVILY_API_KEY", ""),
        },
        "capabilities": [
            "search",
            "fetch_url",
            "extract_content",
        ],
    }


# ── Registry ──────────────────────────────────────────────────────────────────

def get_all_mcp_configs() -> dict[str, dict]:
    """
    Return all MCP server configs as a dict keyed by server name.
    Credentials are read from environment at call time.
    """
    return {
        "gmail-mcp":      gmail_mcp_config(),
        "github-mcp":     github_mcp_config(),
        "notion-mcp":     notion_mcp_config(),
        "tavily-mcp":     tavily_mcp_config(),
    }


# ── Agent → Server mapping ────────────────────────────────────────────────────

AGENT_TO_MCP: dict[str, str] = {
    "research_agent":  "tavily-mcp",
    "code_agent":      "github-mcp",
    "knowledge_agent": "notion-mcp",
    "comms_agent":     "gmail-mcp",
}


def get_mcp_config_for_agent(agent_name: str) -> dict:
    """Return the MCP server config for a given agent name."""
    server_name = AGENT_TO_MCP.get(agent_name)
    if not server_name:
        raise ValueError(
            f"No MCP server mapped for agent '{agent_name}'. "
            f"Known agents: {list(AGENT_TO_MCP.keys())}"
        )
    return get_all_mcp_configs()[server_name]


# ── Validation helper ─────────────────────────────────────────────────────────

def validate_env_keys() -> list[str]:
    """
    Check which required API keys are missing from the environment.
    Returns a list of missing key names (empty = all good).
    """
    required = {
        "OLLAMA_BASE_URL":             "Master Orchestrator (Ollama)",
        "GROQ_API_KEY":                "Sub-Agents (LLaMA 3.3)",
        "GITHUB_PERSONAL_ACCESS_TOKEN": "GitHub MCP (Code Agent)",
        "TAVILY_API_KEY":               "Tavily Search MCP (Research Agent)",
        "NOTION_TOKEN":                "Notion MCP (Knowledge Agent)",
    }
    missing = []
    for key, label in required.items():
        if not os.environ.get(key):
            missing.append(f"{key}  ({label})")
    return missing
