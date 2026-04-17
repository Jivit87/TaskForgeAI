from __future__ import annotations
"""
core/orchestrator.py
Master Orchestrator — powered by Ollama (local) via OpenAI SDK.

Responsibilities:
  1. Parse & decompose the user goal into an ExecutionPlan
  2. Route each SubTask to the correct sub-agent
  3. Validate sub-agent outputs via Pydantic gate
  4. Trigger HITL gate for irreversible or low-confidence actions
  5. Checkpoint AgentState after every successful agent hop
  6. Aggregate all results into a final structured summary

Flow:
  run() → plan() → for each subtask:
              → [hitl check] → dispatch_agent() → validate → checkpoint
          → aggregate() → return final summary
"""

import json
import logging
import os
import uuid
from datetime import datetime

import openai

from core.checkpoint import CheckpointStore
from core.hitl import HITLGate
from schemas.agent_state import AgentState
from schemas.execution_plan import ExecutionPlan, SubTask
from schemas.agent_outputs import validate_agent_output
from config.prompts import get_prompt
from tools.mcp_manager import MCPConnectionManager

log = logging.getLogger("frame_mo.orchestrator")


# ── Routing tools exposed to Ollama (OpenAI format) ──────────────────────────

ROUTING_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "route_to_research_agent",
            "description": "Route a web research subtask to the Research sub-agent",
            "parameters": {
                "type": "object",
                "properties": {
                    "query":       {"type": "string",  "description": "Research query"},
                    "depth":       {"type": "string",  "enum": ["shallow", "deep"],
                                    "description": "Search depth"},
                    "description": {"type": "string",  "description": "Task description"},
                },
                "required": ["query"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "route_to_code_agent",
            "description": "Route a GitHub-related subtask to the Code sub-agent",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo":        {"type": "string",  "description": "owner/repo"},
                    "action":      {"type": "string",
                                    "enum": ["read_pr", "create_issue", "post_comment", "list_issues"]},
                    "target_id":   {"type": "integer", "description": "PR or issue number"},
                    "title":       {"type": "string",  "description": "Issue title"},
                    "body":        {"type": "string",  "description": "Issue or comment body"},
                    "description": {"type": "string",  "description": "Task description"},
                },
                "required": ["repo", "action"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "route_to_knowledge_agent",
            "description": "Route a Notion read/write task to the Knowledge sub-agent",
            "parameters": {
                "type": "object",
                "properties": {
                    "action":      {"type": "string",  "enum": ["read", "create", "append"]},
                    "page_id":     {"type": "string",  "description": "Notion page ID"},
                    "title":       {"type": "string",  "description": "Page title"},
                    "content":     {"type": "string",  "description": "Markdown content"},
                    "description": {"type": "string",  "description": "Task description"},
                },
                "required": ["action"],
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "route_to_comms_agent",
            "description": "Route a Gmail email task to the Communication sub-agent",
            "parameters": {
                "type": "object",
                "properties": {
                    "action":      {"type": "string",  "enum": ["read", "draft", "send"]},
                    "recipient":   {"type": "string",  "description": "Email address"},
                    "subject":     {"type": "string",  "description": "Email subject"},
                    "body":        {"type": "string",  "description": "Email body"},
                    "thread_id":   {"type": "string",  "description": "Thread ID to read"},
                    "description": {"type": "string",  "description": "Task description"},
                },
                "required": ["action"],
            }
        }
    },
]

# Maps routing tool name → agent name
TOOL_TO_AGENT: dict[str, str] = {
    "route_to_research_agent":  "research_agent",
    "route_to_code_agent":      "code_agent",
    "route_to_knowledge_agent": "knowledge_agent",
    "route_to_comms_agent":     "comms_agent",
}


class MasterOrchestrator:
    """
    Ollama-powered Master Orchestrator.

    Usage:
        orchestrator = MasterOrchestrator()
        result = await orchestrator.run(
            goal="Research AI trends, save to Notion, post GitHub issue, email team",
            task_id="abc-123"   # optional — for resuming checkpoints
        )
    """

    def __init__(
        self,
        checkpoint_store: CheckpointStore | None = None,
        mcp_manager: MCPConnectionManager | None = None,
        inject_failures: dict | None = None,
    ):
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self.client = openai.OpenAI(
            base_url=base_url,
            api_key="ollama"  # Ollama requires a synthetic api_key
        )
        self.model = os.environ.get("OLLAMA_ORCHESTRATOR_MODEL", "llama3.3")
        
        self.checkpoint = checkpoint_store or CheckpointStore()
        self.mcp = mcp_manager or MCPConnectionManager()
        self.hitl = HITLGate(self.checkpoint)
        self.inject_failures = inject_failures or {}   # for demo failure injection
        self._agents: dict = {}   # lazily instantiated

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(
        self,
        goal: str,
        task_id: str | None = None,
    ) -> dict:
        """
        Execute a full multi-agent pipeline for the given goal.
        If task_id matches an existing checkpoint, resumes from there.
        """
        task_id = task_id or str(uuid.uuid4())[:8]
        state = self.checkpoint.resume_or_create(task_id, goal)

        log.info(f"[orchestrator] ▶ Starting task_id={task_id}  goal='{goal[:60]}...'")

        try:
            # Step 1 — Planning
            if not state.execution_plan:
                state = await self._plan(goal, state)
                self.checkpoint.save(state)

            # Step 2 — Execute each sub-agent in order
            state = await self._execute_plan(state)

            # Step 3 — Aggregate
            final = await self._aggregate(state)

            state.status = "complete"
            self.checkpoint.save(state)
            log.info(f"[orchestrator] ✅ Task complete  task_id={task_id}")
            return final

        except Exception as exc:
            state.status = "failed"
            state.log_error("orchestrator", str(exc))
            self.checkpoint.save(state)
            log.error(f"[orchestrator] ❌ Task failed  task_id={task_id}: {exc}")
            raise

    # ── Step 1: Planning ──────────────────────────────────────────────────────

    async def _plan(self, goal: str, state: AgentState) -> AgentState:
        """
        Call Ollama to decompose the goal into an ordered ExecutionPlan.
        Ollama uses the routing tools to signal which agents are needed.
        """
        log.info("[orchestrator] Planning phase — calling Ollama...")
        messages = [
            {"role": "system", "content": get_prompt("orchestrator")},
            {"role": "user", "content": goal}
        ]

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=ROUTING_TOOLS,
            temperature=0.1,
            max_tokens=2048,
        )

        message = response.choices[0].message
        plan_steps: list[SubTask] = []
        order = 0

        if message.tool_calls:
            for tool_call in message.tool_calls:
                agent_name = TOOL_TO_AGENT.get(tool_call.function.name)
                if not agent_name:
                    continue

                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    continue

                requires_hitl = self._needs_hitl(agent_name, args)
                step = SubTask(
                    agent=agent_name,
                    description=args.get("description", tool_call.function.name),
                    input={k: v for k, v in args.items() if k != "description"},
                    requires_hitl=requires_hitl,
                    order=order,
                )
                plan_steps.append(step)
                order += 1
                log.info(f"[orchestrator] → Planned: {agent_name}  "
                         f"hitl={requires_hitl}  order={order - 1}")

        if not plan_steps and message.content:
            # Fallback: extract from text response
            plan_steps = self._parse_plan_from_text(message.content, goal)

        plan = ExecutionPlan(goal=goal, steps=plan_steps)
        state.execution_plan = [s.model_dump() for s in plan.steps]
        state.status = "running"
        log.info(f"[orchestrator] Plan ready — {len(plan_steps)} steps: "
                 f"{[s.agent for s in plan_steps]}")
        return state

    def _parse_plan_from_text(self, text: str, goal: str) -> list[SubTask]:
        """Fallback: roughly try to map text mentions of agents if tool calls fail."""
        steps = []
        text_lower = text.lower()
        order = 0
        
        # Simple heuristic fallback
        if "research" in text_lower or "search" in text_lower:
            steps.append(SubTask(agent="research_agent", description=goal[:50], input={"query": goal}, order=order))
            order += 1
        if "code" in text_lower or "github" in text_lower:
            steps.append(SubTask(agent="code_agent", description="Check code", input={"repo": "", "action": "list_issues"}, order=order))
            order += 1
        if "knowledge" in text_lower or "notion" in text_lower:
            steps.append(SubTask(agent="knowledge_agent", description="Read Notion", input={"action": "read"}, order=order))
            order += 1
        if "comms" in text_lower or "email" in text_lower or "message" in text_lower:
            steps.append(SubTask(agent="comms_agent", description="Send comms", input={"action": "read"}, order=order))
            order += 1
            
        return steps

    # ── Step 2: Execute plan ──────────────────────────────────────────────────

    async def _execute_plan(self, state: AgentState) -> AgentState:
        """Execute subtasks in order, skipping already-completed agents."""
        steps = [SubTask(**s) for s in state.execution_plan]

        for step in sorted(steps, key=lambda s: s.order):
            agent_name = step.agent

            # Skip already done
            if state.is_agent_done(agent_name):
                log.info(f"[orchestrator] ⏭ Skipping {agent_name} (already complete)")
                continue

            # HITL gate
            if step.requires_hitl or self._flag_for_hitl(agent_name, step.input, state):
                approved = self.hitl.request_approval(
                    task_id=state.task_id,
                    action={"tool": agent_name, "input": step.input,
                            "confidence": 1.0},
                    state=state,
                    checkpoint_store=self.checkpoint,
                )
                if not approved:
                    log.warning(f"[orchestrator] HITL rejected {agent_name} — skipping")
                    state.log_error(agent_name, "HITL rejected by human")
                    state.mark_agent_complete(
                        agent_name,
                        {"status": "skipped", "reason": "HITL rejected"}
                    )
                    self.checkpoint.save(state)
                    continue

            # Inject failure for demo mode
            if agent_name in self.inject_failures:
                await self._inject_demo_failure(agent_name, self.inject_failures[agent_name])

            # Dispatch to sub-agent
            result = await self._dispatch_agent(agent_name, step.input, state)

            # Validate output
            try:
                validated = validate_agent_output(agent_name, result)
                result = validated.model_dump()
                log.info(f"[orchestrator] ✅ Validated {agent_name} output")
            except Exception as exc:
                log.warning(f"[orchestrator] ⚠ Validation failed for {agent_name}: {exc}")
                state.increment_retry(agent_name)
                state.log_error(agent_name, f"Validation error: {exc}")
                # Retry once with corrected input
                result = await self._dispatch_agent(agent_name, step.input, state)
                validated = validate_agent_output(agent_name, result)
                result = validated.model_dump()

            # Checkpoint after successful validation
            state.mark_agent_complete(agent_name, result)
            self.checkpoint.save(state)
            log.info(f"[orchestrator] 💾 Checkpointed after {agent_name}")

        return state

    # ── Step 3: Aggregate ─────────────────────────────────────────────────────

    async def _aggregate(self, state: AgentState) -> dict:
        """
        Call Ollama to synthesize all sub-agent results into a final summary.
        """
        log.info("[orchestrator] Aggregating results with Ollama...")

        summary_prompt = (
            f"You have completed a multi-agent task.\n\n"
            f"Original goal: {state.goal}\n\n"
            f"Agent results:\n{json.dumps(state.agent_results, indent=2)}\n\n"
            f"Write a concise 3-5 sentence summary of what was accomplished, "
            f"what each agent did, and any notable outcomes. "
            f"Respond ONLY with valid JSON:\n"
            f'{{"status":"complete","goal":"...","summary":"...","highlights":[...]}}'
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": get_prompt("orchestrator")},
                {"role": "user", "content": summary_prompt}
            ],
            temperature=0.1,
            max_tokens=1024,
        )

        raw_text = response.choices[0].message.content or "{}"
        try:
            if "```" in raw_text:
                raw_text = raw_text.split("```")[1]
                if raw_text.startswith("json"):
                    raw_text = raw_text[4:]
            final = json.loads(raw_text.strip())
        except json.JSONDecodeError:
            final = {
                "status": "complete",
                "goal": state.goal,
                "summary": raw_text[:500],
                "highlights": [],
            }

        final["task_id"] = state.task_id
        final["completed_agents"] = state.completed_agents
        final["agent_results"] = state.agent_results
        final["error_log"] = state.error_log
        final["retry_counts"] = state.retry_counts
        final["completed_at"] = datetime.utcnow().isoformat()
        return final

    # ── Agent dispatch ────────────────────────────────────────────────────────

    async def _dispatch_agent(
        self,
        agent_name: str,
        task_input: dict,
        state: AgentState,
    ) -> dict:
        """Lazily instantiate and run the sub-agent for the given name."""
        from agents import AGENT_REGISTRY

        if agent_name not in self._agents:
            agent_cls = AGENT_REGISTRY.get(agent_name)
            if not agent_cls:
                raise ValueError(f"Unknown agent: '{agent_name}'")
            self._agents[agent_name] = agent_cls(mcp_manager=self.mcp)

        agent = self._agents[agent_name]
        log.info(f"[orchestrator] → Dispatching {agent_name}")
        return await agent.execute(task_input, state, mcp_manager=self.mcp)

    # ── HITL helpers ──────────────────────────────────────────────────────────

    @staticmethod
    def _needs_hitl(agent_name: str, task_input: dict) -> bool:
        """Determine if a planned subtask needs HITL before execution."""
        write_agents = {"knowledge_agent", "comms_agent", "code_agent"}
        write_actions = {"create", "append", "send", "create_issue", "post_comment"}
        action = task_input.get("action", "")
        return agent_name in write_agents and action in write_actions

    def _flag_for_hitl(
        self,
        agent_name: str,
        task_input: dict,
        state: AgentState,
    ) -> bool:
        """Runtime HITL check based on retry count and agent type."""
        retry = state.retry_counts.get(agent_name, 0)
        return retry >= 2 or self._needs_hitl(agent_name, task_input)

    # ── Demo failure injection ────────────────────────────────────────────────

    async def _inject_demo_failure(self, agent_name: str, failure_type: str) -> None:
        """Simulate failures for hackathon demo (--inject-failure flag)."""
        import asyncio
        if "rate_limit" in failure_type:
            log.warning(f"[demo] Injecting rate_limit for {agent_name}")
            raise ConnectionError("429 Too Many Requests (injected for demo)")
        elif "malformed" in failure_type:
            log.warning(f"[demo] Injecting malformed output for {agent_name}")
            # Patch agent to return invalid schema on next call
            pass
        await asyncio.sleep(0)   # yield

    # ── Status accessors (for frontend/API) ──────────────────────────────────

    def get_live_status(self, task_id: str) -> dict | None:
        """Return live task status for dashboard polling."""
        state = self.checkpoint.load(task_id)
        if not state:
            return None
        return {
            "task_id":           state.task_id,
            "status":            state.status,
            "goal":              state.goal,
            "current_agent":     state.current_agent,
            "completed_agents":  state.completed_agents,
            "retry_counts":      state.retry_counts,
            "error_count":       len(state.error_log),
            "hitl_pending":      self.hitl.get_pending(),
            "mcp_health":        self.mcp.get_health(),
        }
