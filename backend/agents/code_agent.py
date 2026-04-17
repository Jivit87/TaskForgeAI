from __future__ import annotations
"""
agents/code_agent.py
Code Sub-Agent — GitHub interactions.

Model : llama-3.3-70b-versatile (Groq API)
MCP   : GitHub MCP (github-mcp)
Tools : get_pr_diff, create_github_issue, post_review_comment, list_issues
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
    mcp_server = "github-mcp"
    tool_names  = [
        # MCP tools
        "get_pr_diff",
        "create_github_issue",
        "post_review_comment",
        "list_issues",
        # Native
        "summarize_content",
        "calculate_confidence",
    ]

    async def execute(
        self,
        task: dict,
        state: AgentState,
        mcp_manager=None,
    ) -> dict:
        """
        Execute a GitHub action.

        task dict expected keys:
          - repo   (str): "owner/repo"
          - action (str): "read_pr" | "create_issue" | "post_comment"
          - target_id (int): PR or issue number (optional)
          - title  (str): Issue title (for create_issue)
          - body   (str): Issue/comment body
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

        result = await self.run(enriched_task, state)

        if result.get("status") != "success":
            log.warning(
                f"[code_agent] Non-success status: {result.get('status')} — "
                f"details: {result.get('details', '')[:100]}"
            )

        return result
