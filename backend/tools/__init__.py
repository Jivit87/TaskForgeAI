from __future__ import annotations
"""tools — Tool registry, native function tools, and MCP connection manager."""

from .registry import (
    TOOL_REGISTRY,
    ToolDefinition,
    tool,
    dispatch_tool,
    get_anthropic_schemas,
    get_groq_schemas,
)
from .mcp_manager import MCPConnectionManager, AGENT_MCP_MAP

# Import native_tools to trigger @tool decorator registration
import tools.native_tools  # noqa: F401

__all__ = [
    "TOOL_REGISTRY",
    "ToolDefinition",
    "tool",
    "dispatch_tool",
    "get_anthropic_schemas",
    "get_groq_schemas",
    "MCPConnectionManager",
    "AGENT_MCP_MAP",
]
