from __future__ import annotations
"""
core/orchestrator.py
Master Orchestrator v2 — powered by Ollama (local) via OpenAI SDK.

v2 changes:
  - Dynamic agent discovery from AGENT_REGISTRY (no hardcoded routing tools)
  - Intent classification: conversation vs task
  - LTL verification gate before execution
  - PEI monitoring during each agent step
  - Saga rollback on pipeline failure

Flow:
  run() → classify_intent()
       → [conversation] → direct_reply()
       → [task] → plan() → ltl_verify() → execute_plan() → aggregate()
                                          ↳ on failure → saga_rollback()
"""

import json
import logging
import os
import uuid
from datetime import datetime

import openai

from core.checkpoint import CheckpointStore
from core.hitl import HITLGate
from core.ltl_verifier import verify_plan
from core.memory import ConversationMemory
from core.pei_monitor import PEIMonitor
from core.saga import SagaEngine
from schemas.agent_state import AgentState
from schemas.execution_plan import ExecutionPlan, SubTask
from schemas.agent_outputs import validate_agent_output
from config.prompts import get_prompt
from tools.mcp_manager import MCPConnectionManager

log = logging.getLogger("frame_mo.orchestrator")


def _build_routing_tools(agent_registry: dict) -> tuple[list[dict], dict[str, str]]:
    """
    Dynamically build OpenAI-format routing tools from the AGENT_REGISTRY.
    Returns (tools_list, tool_name_to_agent_name_map).
    """
    tools = []
    tool_to_agent = {}

    for agent_name, agent_cls in agent_registry.items():
        tool_name = f"route_to_{agent_name}"
        description = getattr(agent_cls, "agent_description", "") or f"Route task to {agent_name}"
        parameters = getattr(agent_cls, "routing_parameters", {}) or {
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "Task description"},
            },
            "required": [],
        }

        tools.append({
            "type": "function",
            "function": {
                "name": tool_name,
                "description": f"Route a subtask to the {agent_name.replace('_', ' ').title()}: {description}",
                "parameters": parameters,
            },
        })
        tool_to_agent[tool_name] = agent_name

    # Add direct_reply tool for conversational inputs
    tools.append({
        "type": "function",
        "function": {
            "name": "direct_reply",
            "description": (
                "Reply directly to the user without routing to any agent. "
                "Use this for greetings, small talk, questions about your capabilities, "
                "or any input that does NOT require web search, GitHub, Notion, or email actions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Your friendly, helpful response to the user",
                    },
                },
                "required": ["message"],
            },
        },
    })

    return tools, tool_to_agent


def _build_dynamic_agent_table(agent_registry: dict) -> str:
    """Build a markdown table of available agents for the system prompt."""
    rows = []
    for name, cls in agent_registry.items():
        desc = getattr(cls, "agent_description", "") or "No description"
        rows.append(f"| {name} | {desc} |")
    header = "| Agent Name | Capability |\n|---|---|"
    return header + "\n" + "\n".join(rows)


class MasterOrchestrator:
    """
    Ollama-powered Master Orchestrator v2.

    Key improvements over v1:
      - Agents are discovered dynamically from AGENT_REGISTRY
      - Intent classification separates conversations from tasks
      - LTL verification gate validates plans before execution
      - PEI monitor watches for hallucination loops during execution
      - Saga engine provides compensating rollback on failure
    """

    def __init__(
        self,
        checkpoint_store: CheckpointStore | None = None,
        mcp_manager: MCPConnectionManager | None = None,
        inject_failures: dict | None = None,
        memory: ConversationMemory | None = None,
    ):
        base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")
        self.client = openai.OpenAI(
            base_url=base_url,
            api_key="ollama"
        )
        self.model = os.environ.get("OLLAMA_ORCHESTRATOR_MODEL", "llama3.3")

        self.checkpoint = checkpoint_store or CheckpointStore()
        self.mcp = mcp_manager or MCPConnectionManager()
        self.memory = memory or ConversationMemory(
            db_path=self.checkpoint.db_path
        )
        self.hitl = HITLGate(self.checkpoint)
        self.pei = PEIMonitor()
        self.saga = SagaEngine(self.checkpoint)
        self.inject_failures = inject_failures or {}
        self._agents: dict = {}

        # Dynamic agent discovery
        from agents import AGENT_REGISTRY
        self._agent_registry = AGENT_REGISTRY
        self._routing_tools, self._tool_to_agent = _build_routing_tools(AGENT_REGISTRY)
        self._agent_table = _build_dynamic_agent_table(AGENT_REGISTRY)

        log.info(
            f"[orchestrator] Initialized with {len(AGENT_REGISTRY)} agents: "
            f"{list(AGENT_REGISTRY.keys())}"
        )

    # ── Main entry point ──────────────────────────────────────────────────────

    async def run(
        self,
        goal: str,
        task_id: str | None = None,
    ) -> dict:
        """
        Execute a full pipeline for the given goal.
        Classifies intent first: conversations get a direct reply,
        tasks go through the full agent pipeline.
        """
        task_id = task_id or str(uuid.uuid4())[:8]
        state = self.checkpoint.resume_or_create(task_id, goal)

        log.info(f"[orchestrator] ▶ Starting task_id={task_id}  goal='{goal[:60]}...'")

        try:
            # Step 1 — Planning (includes intent classification)
            # User turn is added to memory AFTER planning so that build_context_messages()
            # returns only *prior* history — the current goal is injected fresh in _plan().
            if not state.execution_plan and not state.direct_reply:
                state = await self._plan(goal, state)
                self.checkpoint.save(state)

            # Now record the user turn (after planning so it's not in history yet)
            self.memory.add_user_turn(
                content=goal,
                turn_type="task_request" if state.intent_type == "task" else "conversation",
                task_id=task_id,
            )

            # Fast-path: conversation mode
            if state.intent_type == "conversation":
                conv_result = {
                    "status": "complete",
                    "task_id": task_id,
                    "goal": goal,
                    "intent_type": "conversation",
                    "summary": state.direct_reply,
                    "highlights": [],
                    "completed_agents": [],
                    "agent_results": {},
                    "error_log": [],
                    "retry_counts": {},
                    "saga_log": [],
                    "completed_at": datetime.utcnow().isoformat(),
                }
                state.final_result = conv_result
                state.status = "complete"
                self.checkpoint.save(state)

                # Record reply in memory
                self.memory.add_assistant_turn(
                    content=state.direct_reply,
                    turn_type="conversation",
                    task_id=task_id,
                )
                # Auto-summarize if window overflowed
                self.memory.maybe_summarize(self.client, self.model)

                log.info(f"[orchestrator] ✅ Conversation reply  task_id={task_id}")
                return conv_result

            # Step 2 — LTL Verification
            if state.execution_plan:
                plan = ExecutionPlan(
                    goal=goal,
                    steps=[SubTask(**s) for s in state.execution_plan],
                )
                ltl_result = verify_plan(plan, set(self._agent_registry.keys()))
                plan.ltl_verified = ltl_result.valid
                plan.verification_notes = "; ".join(
                    ltl_result.violations + ltl_result.warnings
                )
                # Update state with verified plan
                state.execution_plan = [s.model_dump() for s in plan.steps]
                self.checkpoint.save(state)

                if not ltl_result.valid:
                    log.warning(
                        f"[orchestrator] LTL verification failed: "
                        f"{ltl_result.violations}. Proceeding with caution."
                    )

            # Step 3 — Execute each sub-agent
            state = await self._execute_plan(state)

            # Step 4 — Aggregate
            final = await self._aggregate(state)

            # Save result into state so late WS connections can read it
            state.final_result = final
            state.status = "complete"
            self.checkpoint.save(state)

            # Record task result in memory
            summary_text = final.get("summary", "Task completed.")
            self.memory.add_assistant_turn(
                content=summary_text,
                turn_type="task_result",
                task_id=task_id,
            )
            self.memory.add_task_episode(
                task_id=task_id,
                goal=goal,
                outcome=summary_text[:300],
                agents_used=state.completed_agents,
                status="complete",
            )
            # Auto-summarize if window overflowed
            self.memory.maybe_summarize(self.client, self.model)

            log.info(f"[orchestrator] ✅ Task complete  task_id={task_id}")
            return final

        except Exception as exc:
            # Saga rollback on failure
            log.error(f"[orchestrator] ❌ Task failed  task_id={task_id}: {exc}")

            if state.completed_agents:
                log.info(f"[orchestrator] Initiating saga rollback...")
                completed_steps = [
                    SubTask(**s) for s in state.execution_plan
                    if s.get("agent") in state.completed_agents
                ]
                state = await self.saga.rollback(
                    state=state,
                    failed_step_order=len(state.completed_agents),
                    completed_steps=completed_steps,
                )

            state.status = "failed"
            state.log_error("orchestrator", str(exc))
            self.checkpoint.save(state)

            # Record failure in memory
            self.memory.add_assistant_turn(
                content=f"Task failed: {str(exc)[:200]}",
                turn_type="task_result",
                task_id=task_id,
            )
            self.memory.add_task_episode(
                task_id=task_id,
                goal=goal,
                outcome=f"Failed: {str(exc)[:200]}",
                agents_used=state.completed_agents,
                status="failed",
            )
            raise

    # ── Step 1: Planning + Intent Classification ──────────────────────────────

    async def _plan(self, goal: str, state: AgentState) -> AgentState:
        """
        Call Ollama to classify intent and decompose tasks.
        Uses dynamically-built routing tools from AGENT_REGISTRY.
        If Ollama calls direct_reply, it's a conversation.
        If Ollama calls route_to_*, it's a task with agent steps.
        """
        log.info("[orchestrator] Planning phase — calling Ollama...")

        # Build dynamic system prompt
        system_prompt = get_prompt("orchestrator").replace(
            "{{AGENT_TABLE}}", self._agent_table
        )

        # Build messages: prior history (from memory) + the current user goal.
        # IMPORTANT: add_user_turn() is called AFTER _plan() returns, so
        # build_context_messages() only returns prior turns — no duplication.
        history = self.memory.build_context_messages()

        messages = [
            {"role": "system", "content": system_prompt},
            *history,                                   # prior context only
            {"role": "user", "content": goal},         # current message (always last)
        ]

        log.debug(
            f"[orchestrator] Planning with {len(history)} history messages "
            f"({self.memory.get_total_turns()} prior turns stored)"
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=self._routing_tools,
            temperature=0.1,
            max_tokens=2048,
        )

        message = response.choices[0].message
        plan_steps: list[SubTask] = []
        order = 0

        if message.tool_calls:
            for tool_call in message.tool_calls:
                fn_name = tool_call.function.name

                # Handle direct_reply (conversation mode)
                if fn_name == "direct_reply":
                    try:
                        args = json.loads(tool_call.function.arguments)
                    except json.JSONDecodeError:
                        args = {"message": "Hello! How can I help you?"}

                    state.intent_type = "conversation"
                    state.direct_reply = args.get("message", "Hello! How can I help you?")
                    state.status = "running"
                    log.info("[orchestrator] Intent: conversation → direct reply")
                    return state

                # Handle agent routing
                agent_name = self._tool_to_agent.get(fn_name)
                if not agent_name:
                    continue

                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    continue

                # Get compensating action from agent class
                agent_cls = self._agent_registry.get(agent_name)
                comp_actions = getattr(agent_cls, "compensating_actions", {}) if agent_cls else {}
                action = args.get("action", "")
                compensating = comp_actions.get(action, "")

                requires_hitl = self._needs_hitl(agent_name, args)
                step = SubTask(
                    agent=agent_name,
                    description=args.get("description", fn_name),
                    input={k: v for k, v in args.items() if k != "description"},
                    requires_hitl=requires_hitl,
                    order=order,
                    compensating_action=compensating,
                )
                plan_steps.append(step)
                order += 1
                log.info(
                    f"[orchestrator] → Planned: {agent_name}  "
                    f"hitl={requires_hitl}  compensating={'yes' if compensating else 'none'}  "
                    f"order={order - 1}"
                )

        # Fallback: if no tool calls, treat text response as a conversation
        if not plan_steps and not state.direct_reply:
            if message.content:
                state.intent_type = "conversation"
                state.direct_reply = message.content
                state.status = "running"
                log.info("[orchestrator] No tool calls — treating as conversation")
                return state

        plan = ExecutionPlan(goal=goal, steps=plan_steps)
        state.execution_plan = [s.model_dump() for s in plan.steps]
        state.intent_type = "task"
        state.status = "running"
        log.info(
            f"[orchestrator] Plan ready — {len(plan_steps)} steps: "
            f"{[s.agent for s in plan_steps]}"
        )
        return state

    # ── Step 2: Execute plan with PEI monitoring ──────────────────────────────

    async def _execute_plan(self, state: AgentState) -> AgentState:
        """Execute subtasks in order with PEI monitoring and saga-ready checkpoints."""
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
                    action={"tool": agent_name, "input": step.input, "confidence": 1.0},
                    state=state,
                    checkpoint_store=self.checkpoint,
                )
                if not approved:
                    log.warning(f"[orchestrator] HITL rejected {agent_name} — skipping")
                    state.log_error(agent_name, "HITL rejected by human")
                    state.mark_agent_complete(
                        agent_name,
                        {"status": "skipped", "reason": "HITL rejected"},
                    )
                    self.checkpoint.save(state)
                    continue

            # Inject failure for demo mode
            if agent_name in self.inject_failures:
                await self._inject_demo_failure(agent_name, self.inject_failures[agent_name])

            # Start PEI monitoring
            pei_ctx = self.pei.start_step(agent_name, step.description)

            # Dispatch to sub-agent
            try:
                result = await self._dispatch_agent(agent_name, step.input, state)
            except Exception as exc:
                # Check PEI violations before propagating
                if pei_ctx.violations:
                    for v in pei_ctx.violations:
                        state.log_pei_violation(agent_name, v)
                raise

            # Record PEI report
            pei_report = self.pei.get_report(pei_ctx)
            if pei_ctx.violations:
                for v in pei_ctx.violations:
                    state.log_pei_violation(agent_name, v)
                if pei_ctx.killed:
                    state.log_error(agent_name, f"PEI killed: {pei_ctx.violations[-1]}")
                    raise RuntimeError(f"PEI monitor killed {agent_name}: {pei_ctx.violations[-1]}")

            # Validate output
            try:
                validated = validate_agent_output(agent_name, result)
                result = validated.model_dump()
                log.info(f"[orchestrator] ✅ Validated {agent_name} output")
            except Exception as exc:
                log.warning(f"[orchestrator] ⚠ Validation failed for {agent_name}: {exc}")
                state.increment_retry(agent_name)
                state.log_error(agent_name, f"Validation error: {exc}")
                # Retry once
                result = await self._dispatch_agent(agent_name, step.input, state)
                validated = validate_agent_output(agent_name, result)
                result = validated.model_dump()

            # Checkpoint after successful validation (saga snapshot)
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

        # Build a content-focused prompt that includes the actual data
        agent_data_str = json.dumps(state.agent_results, indent=2, default=str)

        summary_prompt = (
            f"You have completed a multi-agent task.\n\n"
            f"Original goal: {state.goal}\n\n"
            f"Agent results:\n{agent_data_str}\n\n"
            f"Write a comprehensive summary based on the ACTUAL DATA returned by the agents. "
            f"Include specific facts, numbers, headlines, and details from the results. "
            f"Do NOT write a meta-description of what agents did — write the actual answer "
            f"the user was looking for.\n\n"
            f"Respond ONLY with valid JSON:\n"
            f'{{"status":"complete","goal":"...","summary":"<detailed answer with actual data>","highlights":["<specific fact 1>","<specific fact 2>","..."]}}'
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": get_prompt("orchestrator").replace("{{AGENT_TABLE}}", self._agent_table)},
                {"role": "user", "content": summary_prompt}
            ],
            temperature=0.1,
            max_tokens=2048,
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
                "summary": raw_text[:1000],
                "highlights": [],
            }

        # Ensure summary is not empty — fall back to agent results directly
        if not final.get("summary") or len(final["summary"]) < 20:
            # Pull summary from agent results if the LLM failed
            for agent_name, result in state.agent_results.items():
                if isinstance(result, dict) and result.get("summary"):
                    final["summary"] = result["summary"]
                    break

        final["task_id"] = state.task_id
        final["intent_type"] = state.intent_type
        final["completed_agents"] = state.completed_agents
        final["agent_results"] = state.agent_results
        final["error_log"] = state.error_log
        final["retry_counts"] = state.retry_counts
        final["saga_log"] = state.saga_log
        final["pei_violations"] = state.pei_violations
        final["completed_at"] = datetime.utcnow().isoformat()

        # Extract and promote research sources/key_facts for frontend display
        for agent_name, result in state.agent_results.items():
            if isinstance(result, dict):
                if result.get("sources") and "sources" not in final:
                    final["sources"] = result["sources"]
                if result.get("key_facts") and "key_facts" not in final:
                    final["key_facts"] = result["key_facts"]

        return final

    # ── Agent dispatch ────────────────────────────────────────────────────────

    async def _dispatch_agent(
        self,
        agent_name: str,
        task_input: dict,
        state: AgentState,
    ) -> dict:
        """Lazily instantiate and run the sub-agent for the given name."""
        if agent_name not in self._agents:
            agent_cls = self._agent_registry.get(agent_name)
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
        write_actions = {"create", "append", "send", "create_issue", "post_comment", "delete"}
        action = task_input.get("action", "")
        return action in write_actions

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
        await asyncio.sleep(0)

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
            "intent_type":       state.intent_type,
            "current_agent":     state.current_agent,
            "completed_agents":  state.completed_agents,
            "retry_counts":      state.retry_counts,
            "error_count":       len(state.error_log),
            "hitl_pending":      self.hitl.get_pending(),
            "mcp_health":        self.mcp.get_health(),
            "saga_log":          state.saga_log,
            "pei_violations":    state.pei_violations,
            "direct_reply":      state.direct_reply,
            "final_result":      state.final_result or {},
        }
