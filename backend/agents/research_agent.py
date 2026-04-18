from __future__ import annotations
"""
agents/research_agent.py
Research Sub-Agent — web search and fact-finding.

Model : Local Ollama (via OpenAI SDK)
MCP   : Tavily Search HTTP API (direct REST, no MCP SDK needed)
Output: ResearchResult
"""

import json
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
        pei_context=None,
    ) -> dict:
        """
        Run web research for the given query.

        Directly calls Tavily via MCP manager and then uses Ollama to
        synthesize the raw results into a clean structured summary.
        This bypasses the unreliable tool-call loop pattern.
        """
        if mcp_manager:
            self.mcp = mcp_manager

        query = task.get("query", "")
        depth = task.get("depth", "shallow")
        max_sources = 5 if depth == "deep" else 3

        log.info(f"[research_agent] query='{query}'  depth={depth}")

        state.mark_agent_started(self.agent_name)

        # ── Step 1: Search Tavily directly ───────────────────────────────────
        search_result = await self.mcp.call_tool("tavily-mcp", "search", {
            "query": query,
            "depth": depth,
            "num_results": max_sources,
        })

        results = search_result.get("results", [])
        log.info(f"[research_agent] Tavily returned {len(results)} results")

        # ── Step 2: Fetch content from top URLs ───────────────────────────────
        fetched_content = []
        for r in results[:max_sources]:
            url = r.get("url", "")
            if not url:
                continue
            try:
                page = await self.mcp.call_tool("tavily-mcp", "fetch_url", {"url": url})
                content = page.get("content", "")[:2000]
                fetched_content.append({
                    "url": url,
                    "title": r.get("title", url),
                    "snippet": r.get("snippet", ""),
                    "content": content,
                })
            except Exception as exc:
                log.warning(f"[research_agent] fetch_url failed for {url}: {exc}")

        # ── Step 3: Synthesize with Ollama ────────────────────────────────────
        if not fetched_content and not results:
            # Nothing retrieved at all
            result_dict = {
                "query": query,
                "summary": "No results were found for this query.",
                "sources": [],
                "key_facts": [],
                "confidence": 0.0,
                "status": "partial",
            }
        else:
            # Build a data payload for Ollama to summarize
            data_str = json.dumps([
                {
                    "title": fc.get("title", ""),
                    "url": fc.get("url", ""),
                    "snippet": fc.get("snippet", ""),
                    "content": fc.get("content", "")[:800],
                }
                for fc in (fetched_content or [{"url": r.get("url",""), "title": r.get("title",""), "snippet": r.get("snippet","")} for r in results[:max_sources]])
            ], indent=2)

            synthesis_prompt = (
                f"You are a research journalist. Based on the following web data fetched right now, "
                f"answer this query: \"{query}\"\n\n"
                f"## Fetched Web Data\n{data_str}\n\n"
                f"Write a factual, detailed summary of the KEY NEWS AND FACTS from this data. "
                f"Include specific headlines, events, numbers, and names from the content above. "
                f"Do NOT mention rate limits, APIs, or technical issues.\n\n"
                f"Respond ONLY with this exact JSON (no markdown, no code fences):\n"
                f'{{"query":"{query}","summary":"<2-4 paragraphs of actual news content>","sources":["<url1>","<url2>"],"key_facts":["<specific fact 1>","<specific fact 2>","<specific fact 3>"],"confidence":0.85,"status":"complete"}}'
            )

            try:
                response = await self.llm.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": synthesis_prompt}],
                    temperature=0.1,
                    max_tokens=2048,
                )
                raw = response.choices[0].message.content or "{}"

                # Strip markdown fences if present
                if "```" in raw:
                    parts = raw.split("```")
                    raw = parts[1] if len(parts) > 1 else parts[0]
                    if raw.startswith("json"):
                        raw = raw[4:]

                result_dict = json.loads(raw.strip())
                log.info(f"[research_agent] Ollama synthesis successful")

            except (json.JSONDecodeError, Exception) as exc:
                log.warning(f"[research_agent] Ollama synthesis failed: {exc} — using direct fallback")
                # Fallback: build the result from raw Tavily data directly
                snippets = [fc.get("snippet", fc.get("content", ""))[:200] for fc in fetched_content]
                sources = [fc.get("url", "") for fc in fetched_content]
                result_dict = {
                    "query": query,
                    "summary": " ".join(snippets[:5]) or "Retrieved content from web search.",
                    "sources": sources,
                    "key_facts": snippets[:5],
                    "confidence": 0.7,
                    "status": "complete",
                }

        # Ensure required fields always present
        result_dict.setdefault("query", query)
        result_dict.setdefault("summary", "Research completed.")
        result_dict.setdefault("sources", [r.get("url","") for r in results[:3]])
        result_dict.setdefault("key_facts", [])
        result_dict.setdefault("confidence", 0.5)
        result_dict.setdefault("status", "complete")

        state.mark_agent_complete(self.agent_name, result_dict)
        log.info(f"[research_agent] ✅ Complete — status={result_dict.get('status')}  confidence={result_dict.get('confidence')}")
        return result_dict
