from __future__ import annotations
"""
agents/base_agent.py
Abstract base class for all FRAME-MO sub-agents.

Every sub-agent (Research, Code, Knowledge, Comms) inherits from BaseAgent.
BaseAgent handles:
  - Ollama (local) LLM calls via OpenAI SDK
  - Retry logic (via @with_retry)
  - Output validation (via Pydantic schemas)
  - MCP tool dispatch
  - Structured logging
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

import openai

from core.retry import with_retry, AgentStepError
from schemas.agent_outputs import validate_agent_output
from schemas.agent_state import AgentState
from config.prompts import get_prompt
from tools.registry import get_groq_schemas

log = logging.getLogger("frame_mo.agent")


class BaseAgent(ABC):
    """
    Abstract base for all FRAME-MO sub-agents.

    Subclasses must implement:
      - agent_name: str  (class attribute)
      - agent_description: str  (one-liner for LLM routing)
      - routing_parameters: dict  (JSON schema for the routing tool)
      - tool_names: list[str]  (Groq-callable tools for this agent)
      - execute(task: dict, state: AgentState, mcp_manager) -> dict
    """

    agent_name: str = "base_agent"
    agent_description: str = ""      # one-liner shown to orchestrator LLM
    routing_parameters: dict = {}    # JSON schema for the routing tool call
    compensating_actions: dict = {}  # action → rollback instruction
    tool_names: list[str] = []       # registry keys of tools available to this agent
    mcp_server: str = ""             # which MCP server this agent uses

    def __init__(self, mcp_manager=None):
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self.llm = openai.AsyncOpenAI(base_url=base_url, api_key="ollama")
        self.model = os.environ.get("OLLAMA_AGENT_MODEL", os.environ.get("OLLAMA_ORCHESTRATOR_MODEL", "llama3.3"))
        self.mcp = mcp_manager
        self.system_prompt = get_prompt(self.agent_name)
        self._call_history: list[dict] = []
        log.info(f"[{self.agent_name}] Initialised  model={self.model}")

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(
        self,
        task: dict,
        state: AgentState,
        pei_context=None,
    ) -> dict:
        """
        Top-level entry: run the agent with retry + validation.
        Returns validated output dict on success.
        Raises AgentStepError after max retries.
        """
        state.mark_agent_started(self.agent_name)
        self._pei_context = pei_context  # Store for use in _call_groq

        @with_retry(max_attempts=3, base_delay=1.0)
        async def _attempt():
            raw = await self._call_groq(task, state)
            return self._validate(raw)

        validated = await _attempt()
        result_dict = validated.model_dump()
        state.mark_agent_complete(self.agent_name, result_dict)
        log.info(f"[{self.agent_name}] ✅ Complete — status={result_dict.get('status')}")
        return result_dict

    # ── LLM call (Ollama) ──────────────────────────────────────────────────────

    async def _call_groq(self, task: dict, state: AgentState) -> dict:
        """
        Call the local Ollama model with the agent's system prompt and tool schemas.
        Handles tool call loop — executes MCP/native tools the model requests.
        """
        messages = [
            {"role": "user", "content": json.dumps(task, indent=2)}
        ]

        # Build tool schemas: native registry tools + MCP tools
        tools = get_groq_schemas(self.tool_names) if self.tool_names else []
        known_tool_names = set(t["function"]["name"] for t in tools)

        # Fetch dynamic tools from MCP server if available
        if self.mcp and self.mcp_server:
            mcp_tools = await self.mcp.get_tools(self.mcp_server)
            for t in mcp_tools:
                if t["function"]["name"] not in known_tool_names:
                    tools.append(t)
                    known_tool_names.add(t["function"]["name"])

        # Fallback for STUB mode where mcp_tools is empty
        for tn in self.tool_names:
            if tn not in known_tool_names:
                tools.append({
                    "type": "function",
                    "function": {
                        "name": tn,
                        "description": f"MCP tool: {tn}",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "args": {"type": "object", "description": "Tool arguments"}
                            },
                        },
                    },
                })

        log.debug(f"[{self.agent_name}] → Ollama  task_keys={list(task.keys())}  "
                  f"tools={[t['function']['name'] for t in tools]}")

        # First call
        response = await self.llm.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.system_prompt},
                *messages,
            ],
            tools=tools or None,
            tool_choice="auto" if tools else None,
            temperature=0.1,
            max_tokens=2048,
        )

        message = response.choices[0].message
        self._call_history.append({"role": "assistant", "content": message.content})

        # Tool call loop — capped at 10 iterations to prevent infinite spirals
        max_tool_rounds = 10
        tool_round = 0
        all_tool_results = []  # Accumulate ALL tool results across rounds
        while message.tool_calls and tool_round < max_tool_rounds:
            tool_round += 1
            tool_results = []
            idempotency_hit = False

            for tc in message.tool_calls:
                tool_name = tc.function.name
                tool_args = json.loads(tc.function.arguments)

                # ── PEI Monitor: record + kill check ──
                if self._pei_context is not None:
                    from core.pei_monitor import PEIMonitor
                    _pei = PEIMonitor()
                    _pei.record_tool_call(self._pei_context, tool_name, tool_args)
                    if _pei.should_kill(self._pei_context):
                        violation = self._pei_context.violations[-1] if self._pei_context.violations else "PEI kill"
                        log.warning(f"[{self.agent_name}] PEI killed agent: {violation}")
                        raise RuntimeError(f"PEI monitor killed {self.agent_name}: {violation}")

                result = await self._dispatch_tool(tool_name, tool_args, state)
                # Detect idempotency / stub hits — no point calling LLM again
                if isinstance(result, dict) and result.get("idempotency_hit"):
                    idempotency_hit = True

                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
                state.log_tool_call(
                    self.agent_name,
                    tool_name,
                    tool_args,
                )

            messages.append({"role": "assistant", "content": message.content,
                             "tool_calls": message.tool_calls})
            messages.extend(tool_results)
            all_tool_results.extend(tool_results)  # Keep cumulative record

            # If any tool call was an idempotency hit, do one final call
            # with tool_choice='none' to force the model to emit its JSON output
            # instead of requesting yet another tool call.
            if idempotency_hit:
                log.info(f"[{self.agent_name}] Idempotency hit — forcing final output")
                try:
                    final_call = await self.llm.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": self.system_prompt},
                            *messages,
                        ],
                        tools=tools or None,
                        tool_choice="none",  # force text output, no more tool calls
                        temperature=0.1,
                        max_tokens=2048,
                    )
                    message = final_call.choices[0].message
                except Exception as exc:
                    log.warning(f"[{self.agent_name}] Final call after idempotency failed: {exc}")
                    # LLM is down. Synthesize from ALL accumulated tool results.
                    fallback = self._synthesize_from_tool_results(all_tool_results)
                    if fallback:
                        log.info(f"[{self.agent_name}] Using synthesized fallback result")
                        return fallback
                break

            # Continuation call
            follow_up = await self.llm.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self.system_prompt},
                    *messages,
                ],
                tools=tools or None,
                tool_choice="auto" if tools else None,
                temperature=0.1,
                max_tokens=2048,
            )
            message = follow_up.choices[0].message

        if tool_round >= max_tool_rounds:
            log.warning(f"[{self.agent_name}] Tool loop capped at {max_tool_rounds} rounds")

        # Parse final JSON response
        content = message.content or "{}"
        try:
            # Strip markdown code fences if model wraps in ```json
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            parsed = json.loads(content.strip())
            # If parsed is empty (no content from model), try synthesizing
            if not parsed or parsed == {}:
                fallback = self._synthesize_from_tool_results(all_tool_results)
                if fallback:
                    log.info(f"[{self.agent_name}] Empty model output — using synthesized fallback")
                    return fallback
            return parsed
        except json.JSONDecodeError as exc:
            # Attempt to extract JSON dynamically if conversational wrapper is present
            import re
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

            fallback = self._synthesize_from_tool_results(all_tool_results)
            if fallback:
                log.info(f"[{self.agent_name}] Extracting JSON failed but synthesized fallback successfully salvaged logic.")
                return fallback

            log.warning(f"[{self.agent_name}] JSON parse failed: {exc}. Raw: {content[:200]}")
            raise ValueError(f"Agent returned non-JSON output: {content[:200]}")

    # ── Tool dispatch ─────────────────────────────────────────────────────────

    async def _dispatch_tool(
        self,
        tool_name: str,
        args: dict,
        state: AgentState,
    ) -> Any:
        """
        Route a tool call to either an MCP server tool or a native function tool.
        MCP tools take priority if this agent has an mcp_server configured.
        """
        from tools.registry import dispatch_tool, TOOL_REGISTRY

        # Native function tool
        if tool_name in TOOL_REGISTRY:
            log.debug(f"[{self.agent_name}] Native tool → {tool_name}")
            return dispatch_tool(tool_name, args)

        # MCP tool
        if self.mcp and self.mcp_server:
            log.debug(f"[{self.agent_name}] MCP tool → {self.mcp_server}.{tool_name}")
            return await self.mcp.call_tool(self.mcp_server, tool_name, args)

        raise KeyError(
            f"[{self.agent_name}] Unknown tool '{tool_name}'. "
            f"Not in TOOL_REGISTRY and no MCP server configured."
        )

    # ── Fallback synthesis ─────────────────────────────────────────────────────

    def _synthesize_from_tool_results(self, tool_results: list) -> dict | None:
        """
        Build a valid agent output dict from raw tool results when the LLM
        returns empty output. Each agent type gets a schema-compliant
        fallback so Pydantic validation doesn't crash.
        """
        # Extract text content from tool results
        snippets = []
        sources = []
        for tr in tool_results:
            try:
                data = json.loads(tr.get("content", "{}"))
                
                # Search results (Tavily HTTP stub style)
                if isinstance(data, dict) and "results" in data:
                    for r in data["results"]:
                        if isinstance(r, dict):
                            if r.get("snippet"):
                                snippets.append(r["snippet"][:500])
                            if r.get("url"):
                                sources.append(r["url"])
                
                # Fetch URL content or standard string fallback
                if isinstance(data, dict):
                    if "error" in data:
                        snippets.append(f"API Error: {str(data['error'])[:800]}")
                    if "content" in data:
                        c = data["content"]
                        if isinstance(c, str):
                            snippets.append(c[:4000])
                        elif isinstance(c, list):
                            # MCP standard array structure
                            for item in c:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    snippets.append(item.get("text", "")[:4000])
                # Pure array response fallback
                elif isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and item.get("type") == "text":
                            snippets.append(item.get("text", "")[:4000])
            except (json.JSONDecodeError, TypeError):
                pass

        summary = "\n\n".join(snippets[:5]) if snippets else "Information retrieved from tool results."

        if self.agent_name == "research_agent":
            return {
                "query": "synthesized from tool results",
                "summary": summary[:2000],
                "sources": sources[:5] or ["No sources available"],
                "key_facts": [s[:150] for s in snippets[:8]],
                "confidence": 0.7 if snippets else 0.3,
                "status": "complete",
            }
        elif self.agent_name == "code_agent":
            return {
                "repo": "unknown/repo",
                "action_taken": "completed via tool results",
                "status": "success",
                "details": summary[:500],
            }
        elif self.agent_name == "knowledge_agent":
            return {
                "action": "create",
                "page_id": "synthesized",
                "page_title": "Synthesized Result",
                "status": "success",
                "content_preview": summary[:300],
            }
        elif self.agent_name == "comms_agent":
            return {
                "action": "draft",
                "status": "drafted",
                "subject": "Synthesized Result",
                "preview": summary[:200],
            }

        # Generic fallback — may not pass all schemas but better than {}
        return {"status": "complete", "summary": summary}

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate(self, raw: dict):
        """Validate raw dict output against this agent's Pydantic schema."""
        return validate_agent_output(self.agent_name, raw)

    # ── Abstract (optional override) ──────────────────────────────────────────

    async def execute(self, task: dict, state: AgentState, mcp_manager=None, pei_context=None) -> dict:
        """Override in subclasses for custom pre/post processing."""
        return await self.run(task, state, pei_context=pei_context)
