from __future__ import annotations
"""
agents/code_agent.py
Code Sub-Agent — GitHub interactions.

Model : llama-3.3-70b-versatile (Groq API)
MCP   : GitHub MCP (github-mcp)
Tools : Dynamic (All GitHub MCP supported functions)
Output: CodeResult
"""

import logging

from agents.base_agent import BaseAgent
from schemas.agent_state import AgentState

log = logging.getLogger("frame_mo.code_agent")

# Actions that require HITL approval (irreversible GitHub writes)
WRITE_ACTIONS = {"create_issue", "post_comment", "merge_pr"}


class CodeAgent(BaseAgent):
    agent_name = "code_agent"
    agent_description = "GitHub interactions: full repository access including read/write files, branches, PRs, issues, and search"
    mcp_server = "github-mcp"
    tool_names  = [
        "summarize_content",
        "calculate_confidence",
    ]
    routing_parameters = {
        "type": "object",
        "properties": {
            "repo":        {"type": "string", "description": "owner/repo"},
            "action":      {"type": "string", "description": "High-level goal (e.g., 'read_file', 'create_branch', 'create_pr', 'fix_bug')"},
            "target_id":   {"type": "integer", "description": "PR or issue number if applicable"},
            "title":       {"type": "string", "description": "Title if creating PR or Issue"},
            "body":        {"type": "string", "description": "Body content for issue/PR/comment"},
            "description": {"type": "string", "description": "Detailed task description"},
        },
        "required": ["repo", "action"],
    }
    compensating_actions = {
        "create_issue": "Close the GitHub issue that was created",
        "post_comment": "Delete the GitHub comment that was posted",
    }

    async def execute(
        self,
        task: dict,
        state: AgentState,
        mcp_manager=None,
        pei_context=None,
    ) -> dict:
        """
        Execute a GitHub action.

        task dict expected keys:
          - repo   (str): "owner/repo"
          - action (str): High-level goal intended for the GitHub repo
          - target_id (int): PR or issue number (optional)
          - title  (str): Issue/PR title (optional)
          - body   (str): Issue/comment body (optional)
        """
        if mcp_manager:
            self.mcp = mcp_manager

        repo   = task.get("repo", "")
        action = task.get("action", "")

        log.info(f"[code_agent] repo={repo}  action={action}")

        # Flag write actions for HITL gate (orchestrator will handle)
        if action in WRITE_ACTIONS:
            task["_requires_hitl"] = True
            log.info(f"[code_agent] Write action '{action}' flagged for HITL")

        enriched_task = {
            **task,
            "instructions": (
                f"Perform GitHub action '{action}' on repository '{repo}'.\n"
                "Return a CodeResult JSON with repo, action_taken, status, and "
                "any relevant IDs or URLs."
            ),
        }

        result = await self.run(enriched_task, state, pei_context=pei_context)

        if result.get("status") != "success":
            log.warning(
                f"[code_agent] Non-success status: {result.get('status')} — "
                f"details: {result.get('details', '')[:100]}"
            )

        return result
