from __future__ import annotations
"""config — System prompts and MCP server configurations for FRAME-MO."""

from .prompts import AGENT_PROMPTS, get_prompt
from .mcp_configs import (
    get_all_mcp_configs,
    get_mcp_config_for_agent,
    AGENT_TO_MCP,
    validate_env_keys,
)

__all__ = [
    "AGENT_PROMPTS",
    "get_prompt",
    "get_all_mcp_configs",
    "get_mcp_config_for_agent",
    "AGENT_TO_MCP",
    "validate_env_keys",
]
