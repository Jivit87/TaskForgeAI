from __future__ import annotations
"""
agents/knowledge_agent.py
Knowledge Sub-Agent — Notion read/write.

Model : llama-3.3-70b-versatile (Groq API)
MCP   : Notion MCP (notion-mcp)
Tools : read_notion_page, create_notion_page, append_notion_block, search_notion
Output: KnowledgeResult
"""

import logging

from agents.base_agent import BaseAgent
from schemas.agent_state import AgentState

log = logging.getLogger("frame_mo.knowledge_agent")

# Write actions that need HITL approval
WRITE_ACTIONS = {"create", "append", "delete"}


class KnowledgeAgent(BaseAgent):
    agent_name = "knowledge_agent"
    agent_description = "Notion workspace: read pages, search databases, create and append pages"
    mcp_server = "notion-mcp"
    tool_names  = [
        "API-retrieve-a-page",
        "API-post-page",
        "API-patch-block-children",
        "API-post-search",
        "summarize_content",
        "extract_structured_data",
    ]
    routing_parameters = {
        "type": "object",
        "properties": {
            "action":      {"type": "string", "enum": ["read", "create", "append", "search_databases", "search_notion"]},
            "page_id":     {"type": "string", "description": "Notion page ID"},
            "title":       {"type": "string", "description": "Page title"},
            "content":     {"type": "string", "description": "Markdown content"},
            "description": {"type": "string", "description": "Task description"},
        },
        "required": ["action"],
    }
    compensating_actions = {
        "create": "Delete the Notion page that was created",
        "append": "Remove the appended blocks from the Notion page",
    }

    async def execute(
        self,
        task: dict,
        state: AgentState,
        mcp_manager=None,
        pei_context=None,
    ) -> dict:
        """
        Execute a Notion operation.

        task dict expected keys:
          - action   (str): "read" | "create" | "append"
          - page_id  (str): Notion page ID (required for read/append)
          - title    (str): Page title (for create)
          - content  (str): Markdown content to write
        """
        if mcp_manager:
            self.mcp = mcp_manager

        action  = task.get("action", "read")
        page_id = task.get("page_id", "")

        log.info(f"[knowledge_agent] action={action}  page_id={page_id[:8] if page_id else '—'}")

        # Flag write actions for HITL approval
        if action in WRITE_ACTIONS:
            task["_requires_hitl"] = True
            log.info(f"[knowledge_agent] Write action '{action}' flagged for HITL")

        # For create, summarise content if too long for context
        if action in ("create", "append") and len(task.get("content", "")) > 3000:
            from tools.native_tools import summarize_content
            task["content"] = summarize_content(
                content=task["content"],
                max_words=600,
            )
            log.debug("[knowledge_agent] Content summarised to fit context window")

        enriched_task = {
            **task,
            "instructions": (
                f"Perform Notion action '{action}'.\n"
                "Return a KnowledgeResult JSON with action, page_id, status, "
                "content_preview, and page_url."
            ),
        }

        result = await self.run(enriched_task, state, pei_context=pei_context)

        log.info(
            f"[knowledge_agent] ✅ {action} → "
            f"page_id={result.get('page_id', '?')[:8]}  "
            f"status={result.get('status')}"
        )
        return result
