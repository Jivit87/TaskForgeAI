from __future__ import annotations
"""
agents/research_agent.py
Research Sub-Agent — web search and fact-finding.

Model : llama-3.3-70b-versatile (Groq API)
MCP   : Tavily Search MCP (tavily-mcp)
Tools : search, fetch_url
Output: ResearchResult
"""

import logging

from agents.base_agent import BaseAgent
from schemas.agent_state import AgentState

log = logging.getLogger("frame_mo.research_agent")


class ResearchAgent(BaseAgent):
    agent_name = "research_agent"
    agent_description = "Web search and fact-finding via Tavily Search"
    mcp_server = "tavily-mcp"
    tool_names  = [
        "search",
        "fetch_url",
        "summarize_content",
        "calculate_confidence",
    ]
    routing_parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Research query"},
            "depth": {"type": "string", "enum": ["shallow", "deep"], "description": "Search depth"},
            "description": {"type": "string", "description": "Task description"},
        },
        "required": ["query"],
    }
    compensating_actions = {}  # research is read-only, no rollback needed

    async def execute(
        self,
        task: dict,
        state: AgentState,
        mcp_manager=None,
    ) -> dict:
        """
        Run web research for the given query.

        task dict expected keys:
          - query (str): the research question
          - depth (str): "shallow" | "deep"  (optional, default "shallow")
        """
        if mcp_manager:
            self.mcp = mcp_manager

        query = task.get("query", "")
        depth = task.get("depth", "shallow")
        max_sources = 5 if depth == "deep" else 3

        log.info(f"[research_agent] query='{query}'  depth={depth}")

        enriched_task = {
            **task,
            "instructions": (
                f"Search for: {query}\n"
                f"Fetch the top {max_sources} most relevant results.\n"
                "Synthesize findings into a structured summary with confidence score."
            ),
        }

        result = await self.run(enriched_task, state)

        # Inject agent-side confidence check
        from tools.native_tools import calculate_confidence
        confidence = calculate_confidence(
            output=result,
            expected_fields=["query", "summary", "sources", "confidence"],
        )
        if confidence < 0.7:
            log.warning(
                f"[research_agent] Low confidence ({confidence}) — "
                f"consider deeper search."
            )
            state.log_error(
                self.agent_name,
                f"Low confidence output: {confidence}"
            )

        return result
