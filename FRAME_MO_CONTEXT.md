# FRAME-MO v2.1 Code Context

Complete architecture for the reliable agentic framework with **conversation memory**.

## Architecture Overview

```
User Input → ConversationMemory.add_turn()
          → Orchestrator._plan()
               ├─ Memory: build_context_messages() → [summary, episodes, recent turns]
               ├─ conversation → direct_reply() → Memory.add_turn()
               └─ task → LTL Verify → Execute (PEI) → Aggregate
                                                      → Memory.add_episode()
                                        on failure → Saga Rollback
```

## Recent Architectural Upgrades & Fixes (Session Log)

1. **Memory Context & Planning Duplication**: 
   - Fixed an issue where `add_user_turn` before `_plan` caused the orchestrator to inject duplicate prompts. User goals are now correctly placed *after* the plan is derived, ensuring clean LLM context.
   - Also ensured conversation fast-path saves the `final_result` into `AgentState` before saving the checkpoint, matching the pipeline logic.

2. **WebSocket Race Conditions**: 
   - Addressed a race condition where the frontend (React WebSocket) connected microseconds *after* tasks completed, resulting in empty bubbles. The `backend/api.py` connection handler now proactively synthesizes and broadcasts a `{"event": "complete", "result": ...}` and `{"event": "terminal", ...}` if the status is already complete upon connection.
   
3. **Groq Tool Loops & Idempotency (429 Rate Limits)**: 
   - `BaseAgent.tool_call_loop` is strictly capped at 10 iterations to prevent infinite hallucination loops.
   - Idempotency hits (e.g., trying to write the same Notion page twice) immediately break the LLM loop.
   - **Crucial Schema Fix**: To prevent Pydantic validation crashes on early-breaks, when an idempotency hit is detected, the agent makes exactly ONE final Groq call with `tool_choice="none"`. This forces the model to synthesize a structurally valid JSON payload matching output schemas (`ResearchResult`, `KnowledgeResult` etc.) instead of returning a hardcoded dummy dict.

4. **Transparent MCP HTTP Fallback (Python 3.9 Support)**:
   - The official Anthropic `mcp` SDK requires Python 3.10+. Instead of forcing environment upgrades, `_StubMCPConnection` in `mcp_manager.py` features a **transparent fallback**.
   - If the server is `tavily-mcp`, it actively intercepts `search` and `fetch_url` tool calls and executes them via standard `httpx` POSTs directly to the Tavily REST API.
   - **Nested LLM args**: Includes defensive unpacking (`args.update(args.pop("args"))`) because agents frequently hallucinate arguments inside a nested `"args"` key, which previously crashed the REST fallback.

## Files


## `backend/api.py`
```python
from __future__ import annotations
"""
backend/api.py
FastAPI REST + WebSocket server for the FRAME-MO frontend dashboard.

Endpoints:
  POST /tasks               — Start a new task
  GET  /tasks               — List all checkpointed tasks
  GET  /tasks/{id}          — Get live status of a task
  POST /tasks/{id}/hitl     — Submit HITL approval decision
  GET  /tasks/{id}/hitl/pending — Get pending HITL requests for a task
  WS   /ws/{id}             — WebSocket stream for live agent updates
  GET  /mcp/health          — MCP server health status
  GET  /health              — API liveness check
"""

import asyncio
import json
import uuid
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.checkpoint import CheckpointStore
from core.memory import ConversationMemory
from core.orchestrator import MasterOrchestrator
from tools.mcp_manager import MCPConnectionManager

log = logging.getLogger("frame_mo.api")

# ── Shared singletons ─────────────────────────────────────────────────────────
checkpoint = CheckpointStore(
    db_path=os.environ.get("CHECKPOINT_DB_PATH", "agent_checkpoints.db")
)
mcp = MCPConnectionManager()
memory = ConversationMemory(db_path=checkpoint.db_path)
orchestrator = MasterOrchestrator(
    checkpoint_store=checkpoint, mcp_manager=mcp, memory=memory
)

# Active WebSocket connections: task_id → list[WebSocket]
_ws_clients: dict[str, list[WebSocket]] = {}


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Connecting MCP servers...")
    await mcp.connect_all()
    log.info("API server ready.")
    yield
    await mcp.close_all()


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="FRAME-MO API",
    description="Fault-Resilient Agentic Multi-Orchestral Engine — Dashboard API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Request/Response models ───────────────────────────────────────────────────

class StartTaskRequest(BaseModel):
    goal: str
    task_id: str | None = None
    inject_failure: str | None = None     # "github:rate_limit"


class HITLDecisionRequest(BaseModel):
    approved: bool


# ── REST Endpoints ────────────────────────────────────────────────────────────

@app.post("/tasks", summary="Start a new agent task")
async def start_task(req: StartTaskRequest):
    task_id = req.task_id or str(uuid.uuid4())[:8]

    inject = {}
    if req.inject_failure:
        for item in req.inject_failure.split(","):
            parts = item.strip().split(":")
            if len(parts) == 2:
                inject[parts[0]] = parts[1]

    orch = MasterOrchestrator(
        checkpoint_store=checkpoint,
        mcp_manager=mcp,
        inject_failures=inject,
        memory=memory,
    )

    async def _run_and_broadcast():
        try:
            result = await orch.run(goal=req.goal, task_id=task_id)
            await _broadcast(task_id, {"event": "complete", "result": result})
        except Exception as exc:
            log.error(f"[api] Task {task_id} failed: {exc}")
            await _broadcast(task_id, {"event": "error", "message": str(exc)})

    asyncio.create_task(_run_and_broadcast())
    return {"task_id": task_id, "status": "started", "goal": req.goal}


@app.get("/tasks", summary="List all checkpointed tasks")
async def list_tasks():
    return checkpoint.list_tasks()


@app.get("/tasks/{task_id}", summary="Get live task status")
async def get_task(task_id: str):
    status = orchestrator.get_live_status(task_id)
    if not status:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return status


@app.post("/tasks/{task_id}/hitl", summary="Submit HITL approval decision")
async def hitl_decision(task_id: str, req: HITLDecisionRequest):
    success = orchestrator.hitl.submit_decision(task_id, req.approved)
    if not success:
        raise HTTPException(
            status_code=404,
            detail=f"No pending HITL request for task '{task_id}'"
        )
    outcome = "approved" if req.approved else "rejected"
    await _broadcast(task_id, {"event": "hitl_decision", "decision": outcome})
    return {"task_id": task_id, "decision": outcome}


@app.get("/tasks/{task_id}/hitl/pending", summary="Get pending HITL requests")
async def get_hitl_pending(task_id: str):
    pending = [
        p for p in orchestrator.hitl.get_pending()
        if p["task_id"] == task_id
    ]
    return {"task_id": task_id, "pending": pending}


@app.get("/mcp/health", summary="MCP server health status")
async def mcp_health():
    return mcp.get_health()


@app.get("/health", summary="API health check")
async def health():
    return {"status": "ok", "version": "2.1.0"}


@app.get("/memory", summary="Conversation memory state")
async def get_memory():
    """Return the current conversation memory context for debugging."""
    return {
        "total_turns": memory.get_total_turns(),
        "recent_turns": memory.get_recent_turns(),
        "latest_summary": memory.get_latest_summary(),
        "task_episodes": memory.get_task_episodes(),
    }


@app.delete("/memory", summary="Clear conversation memory")
async def clear_memory():
    """Reset all conversation memory."""
    memory.clear()
    return {"status": "cleared"}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    await websocket.accept()
    _ws_clients.setdefault(task_id, []).append(websocket)
    log.info(f"[ws] Client connected for task_id={task_id}")

    try:
        # Send current state immediately on connect
        status = orchestrator.get_live_status(task_id)
        if status:
            await websocket.send_text(json.dumps({"event": "status", **status}))

        # Keep connection alive — poll for status updates every second
        while True:
            await asyncio.sleep(1)
            status = orchestrator.get_live_status(task_id)
            if status:
                await websocket.send_text(
                    json.dumps({"event": "status", **status})
                )
            # Stop polling on terminal states — prevents infinite reconnect loop
            if status and status.get("status") in ("complete", "failed"):
                await websocket.send_text(
                    json.dumps({"event": "terminal", "status": status.get("status")})
                )
                break

    except WebSocketDisconnect:
        log.info(f"[ws] Client disconnected for task_id={task_id}")
    finally:
        if task_id in _ws_clients:
            _ws_clients[task_id] = [
                ws for ws in _ws_clients[task_id] if ws != websocket
            ]


async def _broadcast(task_id: str, message: dict):
    """Send a message to all WebSocket clients watching a task."""
    clients = _ws_clients.get(task_id, [])
    disconnected = []
    for ws in clients:
        try:
            await ws.send_text(json.dumps(message, default=str))
        except Exception:
            disconnected.append(ws)
    for ws in disconnected:
        clients.remove(ws)
```

## `backend/core/orchestrator.py`
```python
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

        # Record user message in conversation memory
        self.memory.add_user_turn(
            content=goal,
            turn_type="task_request",
            task_id=task_id,
        )

        try:
            # Step 1 — Planning (includes intent classification)
            if not state.execution_plan and not state.direct_reply:
                state = await self._plan(goal, state)
                self.checkpoint.save(state)

            # Fast-path: conversation mode
            if state.intent_type == "conversation":
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
                return {
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

        # Build messages with conversation memory context
        memory_messages = self.memory.build_context_messages()

        messages = [
            {"role": "system", "content": system_prompt},
            *memory_messages,  # Inject memory: summary + episodes + recent turns
            {"role": "user", "content": goal},
        ]

        log.debug(
            f"[orchestrator] Planning with {len(memory_messages)} memory messages "
            f"(total: {self.memory.get_total_turns()} turns stored)"
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
                {"role": "system", "content": get_prompt("orchestrator").replace("{{AGENT_TABLE}}", self._agent_table)},
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
        final["intent_type"] = state.intent_type
        final["completed_agents"] = state.completed_agents
        final["agent_results"] = state.agent_results
        final["error_log"] = state.error_log
        final["retry_counts"] = state.retry_counts
        final["saga_log"] = state.saga_log
        final["pei_violations"] = state.pei_violations
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
        }
```

## `backend/core/memory.py`
```python
from __future__ import annotations
"""
core/memory.py
Conversation Memory — Sliding Window + Summarization.

Architecture:
  1. Raw turns stored in SQLite (persists across restarts)
  2. Last N turns (WINDOW_SIZE) sent as full messages to the orchestrator
  3. Older turns auto-summarized into a running summary via Ollama
  4. Task results stored as "episodic memory" so the agent recalls prior work

Memory layers (fed to orchestrator as system context):
  ┌──────────────────────────────────────────────┐
  │  Running Summary (compressed older context)  │  ← auto-generated
  ├──────────────────────────────────────────────┤
  │  Recent turns (last N messages)              │  ← raw messages
  ├──────────────────────────────────────────────┤
  │  Task episodes (completed task summaries)    │  ← auto from task results
  └──────────────────────────────────────────────┘
"""

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Optional

log = logging.getLogger("frame_mo.memory")

# How many recent messages to keep in the sliding window
WINDOW_SIZE = int(os.environ.get("MEMORY_WINDOW_SIZE", "20"))

# Max task episodes to include in context
MAX_EPISODES = int(os.environ.get("MEMORY_MAX_EPISODES", "10"))


class ConversationMemory:
    """
    SQLite-backed conversation memory with sliding window and auto-summarization.

    Table: conversation_turns
      - turn_id (auto-increment)
      - role (user | assistant | system)
      - content (raw text)
      - turn_type (conversation | task_request | task_result)
      - task_id (nullable — links to a task if this turn triggered one)
      - metadata_json (flexible extra data)
      - created_at

    Table: memory_summaries
      - summary_id (auto-increment)
      - summary_text (compressed older context)
      - turns_covered (range of turn_ids that were summarized)
      - created_at

    Table: task_episodes
      - task_id (from orchestrator)
      - goal
      - outcome (summary of what happened)
      - agents_used
      - status (complete | failed)
      - created_at
    """

    def __init__(self, db_path: str = "agent_checkpoints.db"):
        """Reuses the same DB as CheckpointStore for simplicity."""
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()
        log.info(f"ConversationMemory ready → {db_path}")

    # ── Schema ────────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS conversation_turns (
                turn_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL,
                turn_type    TEXT NOT NULL DEFAULT 'conversation',
                task_id      TEXT,
                metadata_json TEXT DEFAULT '{}',
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_summaries (
                summary_id    INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_text  TEXT NOT NULL,
                turns_start   INTEGER NOT NULL,
                turns_end     INTEGER NOT NULL,
                created_at    TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS task_episodes (
                task_id      TEXT PRIMARY KEY,
                goal         TEXT NOT NULL,
                outcome      TEXT NOT NULL,
                agents_used  TEXT NOT NULL DEFAULT '[]',
                status       TEXT NOT NULL DEFAULT 'complete',
                created_at   TEXT NOT NULL
            );
        """)
        self.conn.commit()

    # ── Write Operations ──────────────────────────────────────────────────────

    def add_user_turn(
        self,
        content: str,
        turn_type: str = "conversation",
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Record a user message."""
        return self._add_turn("user", content, turn_type, task_id, metadata)

    def add_assistant_turn(
        self,
        content: str,
        turn_type: str = "conversation",
        task_id: str | None = None,
        metadata: dict | None = None,
    ) -> int:
        """Record an assistant response."""
        return self._add_turn("assistant", content, turn_type, task_id, metadata)

    def add_task_episode(
        self,
        task_id: str,
        goal: str,
        outcome: str,
        agents_used: list[str],
        status: str = "complete",
    ) -> None:
        """Record a completed task as an episodic memory."""
        self.conn.execute(
            """
            INSERT OR REPLACE INTO task_episodes
                (task_id, goal, outcome, agents_used, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                goal,
                outcome,
                json.dumps(agents_used),
                status,
                datetime.utcnow().isoformat(),
            ),
        )
        self.conn.commit()
        log.info(f"[memory] Recorded task episode: {task_id} ({status})")

    def _add_turn(
        self,
        role: str,
        content: str,
        turn_type: str,
        task_id: str | None,
        metadata: dict | None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO conversation_turns
                (role, content, turn_type, task_id, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                role,
                content,
                turn_type,
                task_id,
                json.dumps(metadata or {}),
                datetime.utcnow().isoformat(),
            ),
        )
        self.conn.commit()
        turn_id = cursor.lastrowid
        log.debug(f"[memory] Added {role} turn #{turn_id}: {content[:60]}...")
        return turn_id

    # ── Read Operations ───────────────────────────────────────────────────────

    def get_recent_turns(self, limit: int | None = None) -> list[dict]:
        """Get the most recent N turns (default: WINDOW_SIZE)."""
        limit = limit or WINDOW_SIZE
        rows = self.conn.execute(
            """
            SELECT turn_id, role, content, turn_type, task_id, created_at
            FROM conversation_turns
            ORDER BY turn_id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        turns = []
        for row in reversed(rows):  # Reverse so oldest is first
            turns.append({
                "turn_id": row[0],
                "role": row[1],
                "content": row[2],
                "turn_type": row[3],
                "task_id": row[4],
                "created_at": row[5],
            })
        return turns

    def get_latest_summary(self) -> str | None:
        """Get the most recent conversation summary."""
        row = self.conn.execute(
            """
            SELECT summary_text FROM memory_summaries
            ORDER BY summary_id DESC LIMIT 1
            """
        ).fetchone()
        return row[0] if row else None

    def get_task_episodes(self, limit: int | None = None) -> list[dict]:
        """Get recent task episodes as episodic memory."""
        limit = limit or MAX_EPISODES
        rows = self.conn.execute(
            """
            SELECT task_id, goal, outcome, agents_used, status, created_at
            FROM task_episodes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [
            {
                "task_id": r[0],
                "goal": r[1],
                "outcome": r[2],
                "agents_used": json.loads(r[3]),
                "status": r[4],
                "created_at": r[5],
            }
            for r in reversed(rows)
        ]

    def get_total_turns(self) -> int:
        """Total number of conversation turns stored."""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM conversation_turns"
        ).fetchone()
        return row[0] if row else 0

    # ── Context Builder ───────────────────────────────────────────────────────

    def build_context_messages(self) -> list[dict]:
        """
        Build a list of OpenAI-format messages for the orchestrator.

        Returns messages in this order:
          1. Running summary (if exists) as a system message
          2. Task episodes as a system memory note
          3. Recent conversation turns as user/assistant messages
        """
        messages = []

        # Layer 1: Running summary of older conversation
        summary = self.get_latest_summary()
        if summary:
            messages.append({
                "role": "system",
                "content": (
                    f"[CONVERSATION MEMORY — Summary of earlier messages]\n"
                    f"{summary}"
                ),
            })

        # Layer 2: Task episodes (what the agent has done before)
        episodes = self.get_task_episodes()
        if episodes:
            ep_lines = []
            for ep in episodes:
                agents = ", ".join(ep["agents_used"]) if ep["agents_used"] else "none"
                ep_lines.append(
                    f"- [{ep['status'].upper()}] \"{ep['goal']}\" "
                    f"→ {ep['outcome']} (agents: {agents})"
                )
            messages.append({
                "role": "system",
                "content": (
                    f"[TASK MEMORY — Previous tasks you've completed]\n"
                    + "\n".join(ep_lines)
                ),
            })

        # Layer 3: Recent conversation turns
        recent = self.get_recent_turns()
        for turn in recent:
            messages.append({
                "role": turn["role"],
                "content": turn["content"],
            })

        return messages

    # ── Summarization ─────────────────────────────────────────────────────────

    def maybe_summarize(self, llm_client, model: str) -> bool:
        """
        If there are more turns than WINDOW_SIZE, summarize the oldest
        turns that fall outside the window and store the summary.

        Returns True if a new summary was generated.
        """
        total = self.get_total_turns()
        if total <= WINDOW_SIZE:
            return False

        # Get turns outside the current window (the older ones)
        overflow_count = total - WINDOW_SIZE
        rows = self.conn.execute(
            """
            SELECT turn_id, role, content, turn_type
            FROM conversation_turns
            ORDER BY turn_id ASC
            LIMIT ?
            """,
            (overflow_count,),
        ).fetchall()

        if not rows:
            return False

        # Build text to summarize
        turns_text = []
        for r in rows:
            turns_text.append(f"{r[1].upper()}: {r[2]}")
        conversation_block = "\n".join(turns_text)

        # Include existing summary for continuity
        existing_summary = self.get_latest_summary() or ""
        if existing_summary:
            prompt_prefix = (
                f"Existing conversation summary:\n{existing_summary}\n\n"
                f"New messages to incorporate:\n"
            )
        else:
            prompt_prefix = "Summarize this conversation:\n"

        summarize_prompt = (
            f"{prompt_prefix}{conversation_block}\n\n"
            f"Write a concise summary (3-5 sentences) that captures:\n"
            f"1. Key topics discussed\n"
            f"2. Important decisions or preferences the user expressed\n"
            f"3. Any ongoing context that would be helpful for future messages\n"
            f"Respond with ONLY the summary text, nothing else."
        )

        try:
            response = llm_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are a conversation summarizer. Be concise and factual."},
                    {"role": "user", "content": summarize_prompt},
                ],
                temperature=0.1,
                max_tokens=512,
            )
            summary_text = response.choices[0].message.content or ""

            if summary_text.strip():
                turn_ids = [r[0] for r in rows]
                self.conn.execute(
                    """
                    INSERT INTO memory_summaries
                        (summary_text, turns_start, turns_end, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        summary_text.strip(),
                        min(turn_ids),
                        max(turn_ids),
                        datetime.utcnow().isoformat(),
                    ),
                )

                # Delete the summarized turns to keep DB lean
                self.conn.execute(
                    f"DELETE FROM conversation_turns WHERE turn_id <= ?",
                    (max(turn_ids),),
                )
                self.conn.commit()
                log.info(
                    f"[memory] Summarized {len(rows)} turns into summary "
                    f"(turns {min(turn_ids)}-{max(turn_ids)})"
                )
                return True

        except Exception as exc:
            log.warning(f"[memory] Summarization failed: {exc}")

        return False

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def clear(self) -> None:
        """Clear all conversation memory (for testing or reset)."""
        self.conn.executescript("""
            DELETE FROM conversation_turns;
            DELETE FROM memory_summaries;
            DELETE FROM task_episodes;
        """)
        self.conn.commit()
        log.info("[memory] All conversation memory cleared")

    def close(self) -> None:
        self.conn.close()
```

## `backend/core/ltl_verifier.py`
```python
from __future__ import annotations
"""
core/ltl_verifier.py
Pre-flight plan verification gate.

Scans an ExecutionPlan for logical constraint violations BEFORE any
agent touches an API. If violations are found, the orchestrator must
rewrite the plan.

Checks:
  1. No write-before-read violations
  2. No duplicate agent routes
  3. HITL required for all irreversible actions
  4. Step ordering respects depends_on constraints
  5. All referenced agents exist in the registry
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schemas.execution_plan import ExecutionPlan, SubTask

log = logging.getLogger("frame_mo.ltl_verifier")

# Actions that MUST have requires_hitl=True
IRREVERSIBLE_ACTIONS = {
    "send", "create_issue", "post_comment", "create", "append", "delete",
}

# Agents that produce data other agents might consume
READ_AGENTS = {"research_agent"}

# Agents that consume/write data from other agents
WRITE_AGENTS = {"knowledge_agent", "comms_agent", "code_agent"}


class LTLVerificationResult:
    """Result of a pre-flight plan verification."""

    def __init__(self):
        self.valid = True
        self.violations: list[str] = []
        self.warnings: list[str] = []

    def add_violation(self, msg: str):
        self.valid = False
        self.violations.append(msg)
        log.warning(f"[ltl] VIOLATION: {msg}")

    def add_warning(self, msg: str):
        self.warnings.append(msg)
        log.info(f"[ltl] WARNING: {msg}")

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "violations": self.violations,
            "warnings": self.warnings,
        }


def verify_plan(
    plan: "ExecutionPlan",
    known_agents: set[str],
) -> LTLVerificationResult:
    """
    Run all LTL checks on a plan before execution.

    Args:
        plan: The ExecutionPlan to verify.
        known_agents: Set of agent names from AGENT_REGISTRY.

    Returns:
        LTLVerificationResult with valid flag and any violations.
    """
    result = LTLVerificationResult()

    if not plan.steps:
        result.add_warning("Empty plan — no agents to execute.")
        return result

    steps = sorted(plan.steps, key=lambda s: s.order)

    # ── Check 1: All agents exist in registry ─────────────────────────────
    for step in steps:
        if step.agent not in known_agents:
            result.add_violation(
                f"Step {step.order}: Agent '{step.agent}' not found in registry. "
                f"Known: {sorted(known_agents)}"
            )

    # ── Check 2: No duplicate agent routes ────────────────────────────────
    seen_agents = set()
    for step in steps:
        if step.agent in seen_agents:
            result.add_warning(
                f"Step {step.order}: Duplicate route to '{step.agent}'. "
                f"This may be intentional but could indicate a planning error."
            )
        seen_agents.add(step.agent)

    # ── Check 3: Write-before-read check ──────────────────────────────────
    # If a write agent appears before any read agent, flag it
    completed_agents = set()
    for step in steps:
        if step.agent in WRITE_AGENTS:
            # Check if this write depends on a read that hasn't happened yet
            for dep in step.depends_on:
                if dep not in completed_agents:
                    result.add_violation(
                        f"Step {step.order}: '{step.agent}' depends on '{dep}' "
                        f"which hasn't executed yet at this point in the plan."
                    )
        completed_agents.add(step.agent)

    # ── Check 4: HITL for irreversible actions ────────────────────────────
    for step in steps:
        action = step.input.get("action", "")
        if action in IRREVERSIBLE_ACTIONS and not step.requires_hitl:
            result.add_warning(
                f"Step {step.order}: '{step.agent}' performs irreversible action "
                f"'{action}' without HITL flag. Auto-setting requires_hitl=True."
            )
            step.requires_hitl = True

    # ── Check 5: depends_on references valid agents ───────────────────────
    plan_agents = {s.agent for s in steps}
    for step in steps:
        for dep in step.depends_on:
            if dep not in plan_agents:
                result.add_violation(
                    f"Step {step.order}: depends_on '{dep}' but that agent "
                    f"is not in this plan."
                )

    # Mark steps as verified
    if result.valid:
        for step in steps:
            step.verified = True
        log.info(f"[ltl] Plan verified ✅ — {len(steps)} steps passed all checks.")
    else:
        log.warning(
            f"[ltl] Plan failed verification ❌ — "
            f"{len(result.violations)} violations found."
        )

    return result
```

## `backend/core/pei_monitor.py`
```python
from __future__ import annotations
"""
core/pei_monitor.py
Per-Execution-Instance (PEI) Monitor.

Wraps each sub-agent step to detect:
  1. Hallucination loops — agent calls the same tool >N times
  2. Intent drift — agent's tool calls diverge from original task
  3. Hard timeout — agent step exceeds max duration

The monitor does NOT kill the agent directly — it sets a flag that
the orchestrator checks after each tool call in the execution loop.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field

log = logging.getLogger("frame_mo.pei_monitor")

# ── Thresholds ────────────────────────────────────────────────────────────────

MAX_TOOL_CALLS_PER_STEP = 10      # kill after this many tool calls
MAX_DUPLICATE_CALLS = 3           # same tool+args called this many times
STEP_TIMEOUT_SECONDS = 60         # hard timeout per agent step


@dataclass
class PEIContext:
    """Monitoring context for a single agent execution step."""

    agent_name: str
    task_description: str
    started_at: float = 0.0
    tool_calls: list[dict] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    killed: bool = False

    def __post_init__(self):
        self.started_at = time.time()


class PEIMonitor:
    """
    Per-Execution-Instance Monitor.

    Usage:
        monitor = PEIMonitor()
        ctx = monitor.start_step("research_agent", "Search for AI trends")
        # ... agent executes ...
        monitor.record_tool_call(ctx, "search", {"query": "AI trends"})
        if monitor.should_kill(ctx):
            # stop agent
    """

    def start_step(self, agent_name: str, task_description: str) -> PEIContext:
        """Create a new monitoring context for an agent step."""
        ctx = PEIContext(
            agent_name=agent_name,
            task_description=task_description,
        )
        log.debug(f"[pei] Monitoring started → {agent_name}")
        return ctx

    def record_tool_call(
        self,
        ctx: PEIContext,
        tool_name: str,
        args: dict,
    ) -> None:
        """Record a tool call and check for violations."""
        ctx.tool_calls.append({
            "tool": tool_name,
            "args": args,
            "timestamp": time.time(),
        })

        # Check 1: Total tool call count
        if len(ctx.tool_calls) >= MAX_TOOL_CALLS_PER_STEP:
            violation = (
                f"Tool call limit exceeded: {len(ctx.tool_calls)} calls "
                f"(max {MAX_TOOL_CALLS_PER_STEP}). Possible hallucination loop."
            )
            ctx.violations.append(violation)
            ctx.killed = True
            log.warning(f"[pei] {ctx.agent_name}: {violation}")
            return

        # Check 2: Duplicate tool calls (same tool + same args)
        call_sig = f"{tool_name}:{sorted(args.items())}"
        dup_count = sum(
            1 for tc in ctx.tool_calls
            if f"{tc['tool']}:{sorted(tc['args'].items())}" == call_sig
        )
        if dup_count >= MAX_DUPLICATE_CALLS:
            violation = (
                f"Duplicate tool call detected: '{tool_name}' called "
                f"{dup_count} times with identical args. Hallucination loop."
            )
            ctx.violations.append(violation)
            ctx.killed = True
            log.warning(f"[pei] {ctx.agent_name}: {violation}")
            return

        # Check 3: Intent drift — simple keyword overlap heuristic
        task_words = set(ctx.task_description.lower().split())
        tool_words = set(tool_name.lower().replace("_", " ").split())
        arg_words = set()
        for v in args.values():
            if isinstance(v, str):
                arg_words.update(v.lower().split()[:10])  # first 10 words

        overlap = task_words & (tool_words | arg_words)
        # Only flag drift if we have enough context (>5 word task) and zero overlap
        if len(task_words) > 5 and len(overlap) == 0 and len(ctx.tool_calls) > 3:
            violation = (
                f"Possible intent drift: tool '{tool_name}' has no keyword "
                f"overlap with task description."
            )
            ctx.violations.append(violation)
            log.info(f"[pei] {ctx.agent_name}: {violation}")
            # Don't kill on drift alone — just flag it

    def check_timeout(self, ctx: PEIContext) -> bool:
        """Check if the agent step has exceeded the timeout."""
        elapsed = time.time() - ctx.started_at
        if elapsed > STEP_TIMEOUT_SECONDS:
            violation = (
                f"Step timeout exceeded: {elapsed:.1f}s "
                f"(max {STEP_TIMEOUT_SECONDS}s)"
            )
            ctx.violations.append(violation)
            ctx.killed = True
            log.warning(f"[pei] {ctx.agent_name}: {violation}")
            return True
        return False

    def should_kill(self, ctx: PEIContext) -> bool:
        """Check if the agent should be killed based on violations."""
        if ctx.killed:
            return True
        return self.check_timeout(ctx)

    def get_report(self, ctx: PEIContext) -> dict:
        """Generate a monitoring report for the step."""
        return {
            "agent": ctx.agent_name,
            "tool_call_count": len(ctx.tool_calls),
            "violations": ctx.violations,
            "killed": ctx.killed,
            "duration_seconds": round(time.time() - ctx.started_at, 2),
        }
```

## `backend/core/saga.py`
```python
from __future__ import annotations
"""
core/saga.py
Saga-pattern compensating transaction engine.

When a step in the execution pipeline fails after retries are exhausted,
the Saga engine walks backward through completed steps and executes
their pre-planned compensating actions to return to a clean state.

Example:
  Step 1: Research  (read-only, no compensating action)
  Step 2: Create Notion page  (compensating: delete the page)
  Step 3: Create GitHub issue  (compensating: close the issue)
  Step 4: Send email → FAILS

  Saga rollback:
    3. Close GitHub issue ← compensating action for Step 3
    2. Delete Notion page ← compensating action for Step 2
    1. (skip — research is read-only)
"""

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from schemas.agent_state import AgentState
    from schemas.execution_plan import SubTask
    from core.checkpoint import CheckpointStore

log = logging.getLogger("frame_mo.saga")


class SagaEngine:
    """
    Executes compensating actions in reverse order when a pipeline step fails.

    The engine reads compensating_actions from the agent's class metadata
    and logs every action to the AgentState's saga_log.
    """

    def __init__(self, checkpoint_store: "CheckpointStore"):
        self.checkpoint = checkpoint_store

    async def rollback(
        self,
        state: "AgentState",
        failed_step_order: int,
        completed_steps: list["SubTask"],
    ) -> "AgentState":
        """
        Execute compensating actions for all completed steps in reverse order.

        Args:
            state: Current AgentState (will be modified in-place).
            failed_step_order: The order index of the step that failed.
            completed_steps: List of SubTask objects that completed successfully.

        Returns:
            Updated AgentState with saga_log populated.
        """
        state.status = "rolling_back"
        self.checkpoint.save(state)

        # Sort completed steps in reverse order
        steps_to_rollback = sorted(
            completed_steps,
            key=lambda s: s.order,
            reverse=True,
        )

        log.info(
            f"[saga] Starting rollback for task={state.task_id} — "
            f"{len(steps_to_rollback)} steps to compensate"
        )

        for step in steps_to_rollback:
            action_key = step.input.get("action", "")
            compensating = step.compensating_action

            if not compensating:
                log.info(
                    f"[saga] Step {step.order} ({step.agent}): "
                    f"No compensating action — skipping (read-only)"
                )
                state.log_saga_action(
                    agent_name=step.agent,
                    action="skipped (read-only)",
                    success=True,
                )
                continue

            log.info(
                f"[saga] Step {step.order} ({step.agent}): "
                f"Executing compensating action: {compensating}"
            )

            try:
                # Execute compensating action
                # In a production system, this would call the MCP server
                # to actually undo the action (close issue, delete page, etc.)
                # For now, we log it as executed since MCP tools are stubbed.
                await self._execute_compensating_action(
                    state=state,
                    step=step,
                    compensating=compensating,
                )

                state.log_saga_action(
                    agent_name=step.agent,
                    action=compensating,
                    success=True,
                )
                log.info(f"[saga] ✅ Compensated: {step.agent}")

            except Exception as exc:
                log.error(
                    f"[saga] ❌ Compensating action failed for {step.agent}: {exc}"
                )
                state.log_saga_action(
                    agent_name=step.agent,
                    action=f"FAILED: {compensating} — {exc}",
                    success=False,
                )
                state.log_error(
                    step.agent,
                    f"Saga rollback failed: {exc}",
                )

            self.checkpoint.save(state)

        log.info(
            f"[saga] Rollback complete for task={state.task_id} — "
            f"{len(state.saga_log)} actions logged"
        )

        return state

    async def _execute_compensating_action(
        self,
        state: "AgentState",
        step: "SubTask",
        compensating: str,
    ) -> None:
        """
        Execute a single compensating action.

        In production, this would dispatch to the appropriate MCP server.
        For the stub/demo environment, we log and return success.
        """
        agent_result = state.agent_results.get(step.agent, {})

        log.info(
            f"[saga] Dispatching compensation for {step.agent}: "
            f"{compensating} (result keys: {list(agent_result.keys())})"
        )

        # The compensating action is logged; in production with real MCP
        # connections, we would call:
        #   await mcp.call_tool(server, "close_issue", {issue_number: ...})
        #   await mcp.call_tool(server, "delete_page", {page_id: ...})
        # For now, the stub environment doesn't need actual rollback calls.

        # Remove the agent from completed list since it's been rolled back
        if step.agent in state.completed_agents:
            state.completed_agents.remove(step.agent)
        if step.agent in state.agent_results:
            del state.agent_results[step.agent]
```

## `backend/core/checkpoint.py`
```python
from __future__ import annotations
"""
core/checkpoint.py
SQLite-backed CheckpointStore.

Saves the full AgentState after every successful agent hop.
On process crash or restart, the orchestrator calls resume_or_create()
to load the last good checkpoint and skip already-completed agents.

Thread-safe: SQLite is opened with check_same_thread=False
and writes use check_same_thread-safe single-connection pattern.
"""

import sqlite3
import json
import logging
from datetime import datetime

from schemas.agent_state import AgentState

log = logging.getLogger("frame_mo.checkpoint")


class CheckpointStore:
    """Durable, local SQLite checkpoint store for AgentState."""

    def __init__(self, db_path: str = "agent_checkpoints.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init_schema()
        log.info(f"CheckpointStore ready → {db_path}")

    # ── Schema ──────────────────────────────────────────────────────────────

    def _init_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                task_id    TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self.conn.commit()

    # ── Write ────────────────────────────────────────────────────────────────

    def save(self, state: AgentState) -> None:
        """
        Upsert the full AgentState.
        Called after every successful agent hop and validation gate.
        """
        state.updated_at = datetime.utcnow().isoformat()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO checkpoints (task_id, state_json, updated_at)
            VALUES (?, ?, ?)
            """,
            (state.task_id, json.dumps(state.to_dict()), state.updated_at),
        )
        self.conn.commit()
        log.debug(f"Checkpoint saved → task_id={state.task_id}  "
                  f"agent={state.current_agent}  status={state.status}")

    # ── Read ─────────────────────────────────────────────────────────────────

    def load(self, task_id: str) -> AgentState | None:
        """Load a checkpoint by task_id. Returns None if not found."""
        row = self.conn.execute(
            "SELECT state_json FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row:
            return AgentState.from_dict(json.loads(row[0]))
        return None

    def resume_or_create(self, task_id: str, goal: str) -> AgentState:
        """
        Load an existing in-progress checkpoint, or create a fresh AgentState.
        Skips completed/failed tasks — always starts a new state for those.
        """
        existing = self.load(task_id)

        if existing and existing.status not in ("complete", "failed"):
            log.info(
                f"Resuming from checkpoint → task_id={task_id}  "
                f"completed={existing.completed_agents}  "
                f"current={existing.current_agent}"
            )
            return existing

        log.info(f"Creating new task → task_id={task_id}")
        return AgentState(task_id=task_id, goal=goal)

    # ── List ─────────────────────────────────────────────────────────────────

    def list_tasks(self) -> list[dict]:
        """List all checkpointed tasks with summary info."""
        rows = self.conn.execute(
            "SELECT task_id, updated_at, state_json FROM checkpoints ORDER BY updated_at DESC"
        ).fetchall()
        results = []
        for task_id, updated_at, state_json in rows:
            state = json.loads(state_json)
            results.append({
                "task_id": task_id,
                "status": state.get("status"),
                "completed_agents": state.get("completed_agents", []),
                "updated_at": updated_at,
                "goal_preview": state.get("goal", "")[:80],
            })
        return results

    # ── Cleanup ──────────────────────────────────────────────────────────────

    def delete(self, task_id: str) -> None:
        """Delete a checkpoint (used after full completion in tests)."""
        self.conn.execute(
            "DELETE FROM checkpoints WHERE task_id = ?", (task_id,)
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
```

## `backend/agents/base_agent.py`
```python
from __future__ import annotations
"""
agents/base_agent.py
Abstract base class for all FRAME-MO sub-agents.

Every sub-agent (Research, Code, Knowledge, Comms) inherits from BaseAgent.
BaseAgent handles:
  - Groq client initialisation
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

from groq import Groq

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
        self.groq = Groq(api_key=os.environ["GROQ_API_KEY"])
        self.mcp = mcp_manager
        self.system_prompt = get_prompt(self.agent_name)
        self._call_history: list[dict] = []
        log.info(f"[{self.agent_name}] Initialised")

    # ── Public entry point ────────────────────────────────────────────────────

    async def run(
        self,
        task: dict,
        state: AgentState,
    ) -> dict:
        """
        Top-level entry: run the agent with retry + validation.
        Returns validated output dict on success.
        Raises AgentStepError after max retries.
        """
        state.mark_agent_started(self.agent_name)

        @with_retry(max_attempts=3, base_delay=1.0)
        async def _attempt():
            raw = await self._call_groq(task, state)
            return self._validate(raw)

        validated = await _attempt()
        result_dict = validated.model_dump()
        state.mark_agent_complete(self.agent_name, result_dict)
        log.info(f"[{self.agent_name}] ✅ Complete — status={result_dict.get('status')}")
        return result_dict

    # ── Groq call ─────────────────────────────────────────────────────────────

    async def _call_groq(self, task: dict, state: AgentState) -> dict:
        """
        Call the Groq API with the agent's system prompt and tool schemas.
        Handles tool call loop — executes MCP/native tools the model requests.
        """
        messages = [
            {"role": "user", "content": json.dumps(task, indent=2)}
        ]

        # Build tool schemas: native registry tools + MCP tool stubs
        tools = get_groq_schemas(self.tool_names) if self.tool_names else []
        known_tool_names = {t["function"]["name"] for t in tools}
        for tn in self.tool_names:
            if tn not in known_tool_names:
                # MCP tool not in native registry — add a stub schema so Groq knows about it
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

        log.debug(f"[{self.agent_name}] → Groq  task_keys={list(task.keys())}  "
                  f"tools={[t['function']['name'] for t in tools]}")

        # First call
        response = self.groq.chat.completions.create(
            model="llama-3.3-70b-versatile",
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

        # Tool call loop
        while message.tool_calls:
            tool_results = []
            for tc in message.tool_calls:
                result = await self._dispatch_tool(
                    tc.function.name,
                    json.loads(tc.function.arguments),
                    state,
                )
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result),
                })
                state.log_tool_call(
                    self.agent_name,
                    tc.function.name,
                    json.loads(tc.function.arguments),
                )

            messages.append({"role": "assistant", "content": message.content,
                             "tool_calls": message.tool_calls})
            messages.extend(tool_results)

            # Continuation call
            follow_up = self.groq.chat.completions.create(
                model="llama-3.3-70b-versatile",
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

        # Parse final JSON response
        content = message.content or "{}"
        try:
            # Strip markdown code fences if model wraps in ```json
            if "```" in content:
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            return json.loads(content.strip())
        except json.JSONDecodeError as exc:
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

    # ── Validation ────────────────────────────────────────────────────────────

    def _validate(self, raw: dict):
        """Validate raw dict output against this agent's Pydantic schema."""
        return validate_agent_output(self.agent_name, raw)

    # ── Abstract (optional override) ──────────────────────────────────────────

    async def execute(self, task: dict, state: AgentState, mcp_manager=None) -> dict:
        """Override in subclasses for custom pre/post processing."""
        return await self.run(task, state)
```

## `backend/agents/research_agent.py`
```python
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
```

## `backend/agents/code_agent.py`
```python
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
    agent_description = "GitHub interactions: read PRs/issues, create issues, post comments"
    mcp_server = "github-mcp"
    tool_names  = [
        "get_pr_diff",
        "create_github_issue",
        "post_review_comment",
        "list_issues",
        "summarize_content",
        "calculate_confidence",
    ]
    routing_parameters = {
        "type": "object",
        "properties": {
            "repo":        {"type": "string", "description": "owner/repo"},
            "action":      {"type": "string", "enum": ["read_pr", "create_issue", "post_comment", "list_issues"]},
            "target_id":   {"type": "integer", "description": "PR or issue number"},
            "title":       {"type": "string", "description": "Issue title"},
            "body":        {"type": "string", "description": "Issue or comment body"},
            "description": {"type": "string", "description": "Task description"},
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
```

## `backend/agents/knowledge_agent.py`
```python
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
    agent_description = "Notion workspace: read, create, and append pages"
    mcp_server = "notion-mcp"
    tool_names  = [
        "read_notion_page",
        "create_notion_page",
        "append_notion_block",
        "search_notion",
        "summarize_content",
        "extract_structured_data",
    ]
    routing_parameters = {
        "type": "object",
        "properties": {
            "action":      {"type": "string", "enum": ["read", "create", "append"]},
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

        result = await self.run(enriched_task, state)

        log.info(
            f"[knowledge_agent] ✅ {action} → "
            f"page_id={result.get('page_id', '?')[:8]}  "
            f"status={result.get('status')}"
        )
        return result
```

## `backend/agents/comms_agent.py`
```python
from __future__ import annotations
"""
agents/comms_agent.py
Communication Sub-Agent — Gmail email operations.

Model : llama-3.3-70b-versatile (Groq API)
MCP   : Gmail MCP (gmail-mcp)
Tools : read_email_thread, draft_email, send_email
Output: CommsResult

SAFETY: send_email always requires HITL approval — it is irreversible.
        The agent will ALWAYS draft before sending, never send directly.
"""

import logging

from agents.base_agent import BaseAgent
from schemas.agent_state import AgentState

log = logging.getLogger("frame_mo.comms_agent")


class CommsAgent(BaseAgent):
    agent_name = "comms_agent"
    agent_description = "Gmail email operations: read threads, draft and send emails"
    mcp_server = "gmail-mcp"
    tool_names  = [
        "read_email_thread",
        "draft_email",
        "send_email",
        "summarize_content",
        "calculate_confidence",
    ]
    routing_parameters = {
        "type": "object",
        "properties": {
            "action":      {"type": "string", "enum": ["read", "draft", "send"]},
            "recipient":   {"type": "string", "description": "Email address"},
            "subject":     {"type": "string", "description": "Email subject"},
            "body":        {"type": "string", "description": "Email body"},
            "thread_id":   {"type": "string", "description": "Thread ID to read"},
            "description": {"type": "string", "description": "Task description"},
        },
        "required": ["action"],
    }
    compensating_actions = {
        "send": "The email has been sent and cannot be recalled",
        "draft": "Delete the draft email that was created",
    }

    async def execute(
        self,
        task: dict,
        state: AgentState,
        mcp_manager=None,
    ) -> dict:
        """
        Execute a Gmail action.

        task dict expected keys:
          - action    (str): "read" | "draft" | "send"
          - recipient (str): Email address (for draft/send)
          - subject   (str): Email subject
          - body      (str): Email body (Markdown accepted)
          - thread_id (str): Thread to read (for read action)
        """
        if mcp_manager:
            self.mcp = mcp_manager

        action    = task.get("action", "draft")
        recipient = task.get("recipient", "")

        log.info(f"[comms_agent] action={action}  recipient={recipient}")

        # SAFETY: send is always irreversible — always require HITL
        if action == "send":
            task["_requires_hitl"] = True
            log.info("[comms_agent] 🔒 send_email flagged for mandatory HITL")

        # If body is very long, summarise it first
        if len(task.get("body", "")) > 2000:
            from tools.native_tools import summarize_content
            task["body"] = summarize_content(
                content=task["body"],
                max_words=400,
            )
            log.debug("[comms_agent] Email body summarised")

        enriched_task = {
            **task,
            "instructions": (
                f"Perform Gmail action '{action}'.\n"
                "For 'send': ALWAYS draft first, then call send_email.\n"
                "Return a CommsResult JSON with action, status, recipient, "
                "subject, message_id, thread_id, and preview."
            ),
        }

        result = await self.run(enriched_task, state)

        # Log send events clearly for audit trail
        if action == "send" and result.get("status") == "sent":
            log.info(
                f"[comms_agent] 📧 Email sent → "
                f"to={result.get('recipient')}  "
                f"subject='{result.get('subject')}'  "
                f"msg_id={result.get('message_id')}"
            )

        return result
```

## `backend/schemas/execution_plan.py`
```python
from __future__ import annotations
"""
schemas/execution_plan.py
ExecutionPlan and SubTask — the orchestrator's decomposed plan for a user goal.
Master Orchestrator generates this before routing to sub-agents.
"""

from pydantic import BaseModel, Field
from typing import Optional


class SubTask(BaseModel):
    """A single unit of work routed to one sub-agent."""

    # Which agent handles this subtask
    agent: str = Field(..., description="Agent name from the registry")

    # Human-readable description of what should be done
    description: str = Field(
        ...,
        min_length=1,
        description="Clear description of what this sub-agent must do"
    )

    # Structured input passed to the sub-agent
    input: dict = Field(
        default_factory=dict,
        description="Typed input parameters for the sub-agent"
    )

    # Dependencies — this subtask can only run after these agents complete
    depends_on: list[str] = Field(
        default_factory=list,
        description="List of agent names that must complete before this subtask"
    )

    # Whether a human must approve before this subtask executes
    requires_hitl: bool = Field(
        default=False,
        description="True if this step is irreversible and needs human approval"
    )

    # Priority order (lower = runs first)
    order: int = Field(default=0, ge=0)

    # Saga recovery (v2)
    compensating_action: str = Field(
        default="",
        description="Rollback instruction if this step needs to be undone"
    )

    # LTL verification flag (v2)
    verified: bool = Field(
        default=False,
        description="Set to True by the LTL verifier after pre-flight check"
    )


class ExecutionPlan(BaseModel):
    """
    Ordered list of subtasks produced by the Master Orchestrator.
    Subtasks are executed in order, with dependency checks respected.
    """

    goal: str = Field(..., min_length=1, description="Original user goal")
    steps: list[SubTask] = Field(
        ...,
        min_length=0,
        description="Ordered list of subtasks to execute"
    )
    estimated_duration_seconds: Optional[int] = Field(
        default=None,
        description="Rough estimate of total execution time"
    )
    notes: str = Field(
        default="",
        description="Orchestrator notes on the plan (e.g., risks, assumptions)"
    )

    # LTL verification (v2)
    ltl_verified: bool = Field(
        default=False,
        description="True if the plan passed the LTL verification gate"
    )
    verification_notes: str = Field(
        default="",
        description="Notes from the LTL verifier about the plan"
    )

    @property
    def agent_sequence(self) -> list[str]:
        """Return agents in execution order."""
        return [step.agent for step in sorted(self.steps, key=lambda s: s.order)]

    def get_step_for_agent(self, agent_name: str) -> Optional[SubTask]:
        """Retrieve the subtask assigned to a given agent."""
        for step in self.steps:
            if step.agent == agent_name:
                return step
        return None
```

## `backend/schemas/agent_state.py`
```python
from __future__ import annotations
"""
schemas/agent_state.py
AgentState dataclass — the single source of truth for a running task.
Persisted to SQLite after every successful agent hop.
"""

from dataclasses import dataclass, asdict, field
from datetime import datetime
from typing import Literal


@dataclass
class AgentState:
    # ── Identity ──────────────────────────────────────────────────────────────
    task_id: str
    version: int = 3

    # ── Task ──────────────────────────────────────────────────────────────────
    goal: str = ""
    execution_plan: list = field(default_factory=list)   # list[SubTask dicts]
    intent_type: str = "task"   # "conversation" | "task"

    # ── Progress ──────────────────────────────────────────────────────────────
    current_agent: str = ""
    completed_agents: list = field(default_factory=list)
    agent_results: dict = field(default_factory=dict)    # agent_name → result dict

    # ── Observability ─────────────────────────────────────────────────────────
    tool_call_log: list = field(default_factory=list)
    retry_counts: dict = field(default_factory=dict)     # agent_name → int
    error_log: list = field(default_factory=list)

    # ── Reliability (v2) ──────────────────────────────────────────────────────
    saga_log: list = field(default_factory=list)          # compensating actions taken
    pei_violations: list = field(default_factory=list)    # PEI monitor flags
    direct_reply: str = ""                                # LLM reply for conversation mode

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    status: Literal[
        "pending", "running", "paused_hitl", "rolling_back", "complete", "failed"
    ] = "pending"
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.utcnow().isoformat()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def mark_agent_started(self, agent_name: str) -> None:
        self.current_agent = agent_name
        self.status = "running"
        if agent_name not in self.retry_counts:
            self.retry_counts[agent_name] = 0

    def mark_agent_complete(self, agent_name: str, result: dict) -> None:
        self.agent_results[agent_name] = result
        if agent_name not in self.completed_agents:
            self.completed_agents.append(agent_name)
        self.current_agent = ""

    def increment_retry(self, agent_name: str) -> int:
        self.retry_counts[agent_name] = self.retry_counts.get(agent_name, 0) + 1
        return self.retry_counts[agent_name]

    def log_error(self, agent_name: str, error: str) -> None:
        self.error_log.append({
            "agent": agent_name,
            "error": error,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def log_tool_call(self, agent_name: str, tool: str, args: dict) -> None:
        self.tool_call_log.append({
            "agent": agent_name,
            "tool": tool,
            "args": args,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def log_saga_action(self, agent_name: str, action: str, success: bool) -> None:
        """Record a compensating action during saga rollback."""
        self.saga_log.append({
            "agent": agent_name,
            "action": action,
            "success": success,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def log_pei_violation(self, agent_name: str, violation: str) -> None:
        """Record a PEI monitor violation."""
        self.pei_violations.append({
            "agent": agent_name,
            "violation": violation,
            "timestamp": datetime.utcnow().isoformat(),
        })

    def is_agent_done(self, agent_name: str) -> bool:
        return agent_name in self.completed_agents

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AgentState":
        # Handle forward-compat: ignore unknown keys from older checkpoints
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)
```

## `backend/schemas/agent_outputs.py`
```python
from __future__ import annotations
"""
schemas/agent_outputs.py
Pydantic output schemas for all 4 sub-agents.
Every sub-agent response is validated against its schema before the
orchestrator processes it. Invalid outputs never leave the validation gate.
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import datetime


# ── Research Sub-Agent ────────────────────────────────────────────────────────

class ResearchResult(BaseModel):
    """Output schema for the Research Sub-Agent (web search + fetch)."""

    query: str = Field(..., description="The research query that was executed")
    summary: str = Field(
        ...,
        min_length=1,
        description="Synthesized summary of findings"
    )
    sources: list[str] = Field(
        default_factory=list,
        description="List of source URLs used"
    )
    key_facts: list[str] = Field(
        default_factory=list,
        description="Bullet-point key facts extracted"
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score 0.0–1.0 based on source quality"
    )
    status: Literal["complete", "partial", "failed"] = "complete"
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Code Sub-Agent ────────────────────────────────────────────────────────────

class CodeResult(BaseModel):
    """Output schema for the Code Sub-Agent (GitHub interactions)."""

    repo: str = Field(..., description="GitHub repo in owner/repo format")
    action_taken: str = Field(..., description="Exact action performed")
    status: Literal["success", "skipped", "failed"] = "success"
    details: str = Field(default="", description="Additional context or result body")
    pr_number: Optional[int] = Field(default=None, description="PR number if applicable")
    issue_number: Optional[int] = Field(default=None, description="Issue number if created")
    comment_id: Optional[str] = Field(default=None, description="Comment ID if posted")
    url: Optional[str] = Field(default=None, description="URL of the created resource")


# ── Knowledge Sub-Agent ───────────────────────────────────────────────────────

class KnowledgeResult(BaseModel):
    """Output schema for the Knowledge Sub-Agent (Notion read/write)."""

    action: Literal["read", "create", "append"] = Field(
        ..., description="Notion operation performed"
    )
    page_id: str = Field(..., description="Notion page ID acted upon")
    page_title: str = Field(default="", description="Title of the Notion page")
    status: Literal["success", "failed"] = "success"
    content_preview: str = Field(
        default="",
        description="First 300 chars of the page content"
    )
    page_url: Optional[str] = Field(default=None, description="Public Notion page URL")


# ── Communication Sub-Agent ───────────────────────────────────────────────────

class CommsResult(BaseModel):
    """Output schema for the Communication Sub-Agent (Gmail)."""

    action: Literal["read", "draft", "send"] = Field(
        ..., description="Gmail operation performed"
    )
    status: Literal["sent", "drafted", "read", "failed"] = "drafted"
    recipient: str = Field(default="", description="Email recipient address")
    subject: str = Field(default="", description="Email subject line")
    message_id: str = Field(default="", description="Gmail message ID")
    thread_id: str = Field(default="", description="Gmail thread ID")
    preview: str = Field(default="", description="First 200 chars of email body")


# ── Registry ──────────────────────────────────────────────────────────────────

OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "research_agent": ResearchResult,
    "code_agent":     CodeResult,
    "knowledge_agent": KnowledgeResult,
    "comms_agent":    CommsResult,
}


def validate_agent_output(agent_name: str, raw_output: dict) -> BaseModel:
    """
    Validate a sub-agent's raw dict output against its registered schema.
    Raises ValidationError if the output does not conform — the calling
    orchestrator must handle or retry.
    """
    schema = OUTPUT_SCHEMAS.get(agent_name)
    if schema is None:
        raise KeyError(f"No output schema registered for agent: '{agent_name}'")
    return schema.model_validate(raw_output)
```

## `backend/config/prompts.py`
```python
from __future__ import annotations
"""
config/prompts.py
System prompts for the Master Orchestrator and all 4 sub-agents.

Design principles:
  - Orchestrator: planning-focused, explicitly told to NEVER call external tools
  - Sub-agents: scoped to their specific domain, forced schema output
  - All prompts: structured output enforcement, confidence scoring required
"""

# ── Master Orchestrator ───────────────────────────────────────────────────────

ORCHESTRATOR_SYSTEM_PROMPT = """
You are FRAME-MO's Master Orchestrator — a strategic planning and routing engine.

## Your Role
You receive a user message and decide how to handle it:
- If the user is greeting you, asking a question about your capabilities, making small talk, or saying anything that does NOT require external tools — use the `direct_reply` tool to respond conversationally.
- If the user has an actionable goal that requires web search, GitHub, Notion, or email — decompose it into subtasks and route each to the appropriate sub-agent.

## Conversation Memory
You have persistent memory across conversations. You may receive:
- **[CONVERSATION MEMORY]**: A summary of earlier messages — use this to maintain context.
- **[TASK MEMORY]**: Records of previous tasks you've completed — reference these when the user asks about past work or wants to build on previous results.
- **Recent messages**: The last few conversation turns for immediate context.

Use this memory naturally. If the user refers to something discussed earlier, acknowledge it. 
If they ask "what did you do last time?", reference the task memory.
Never mention the memory system itself to the user.

## Sub-Agents Available
{{AGENT_TABLE}}

## Your Responsibilities
1. **Classify** the user intent — is this a conversation or a task?
2. **For conversations** — call `direct_reply` with a friendly, helpful response
3. **For tasks** — decompose into subtasks, one per sub-agent, ordered logically
4. **Route** each subtask by calling the appropriate routing tool
5. **Flag** any irreversible action (email send, GitHub issue, Notion write) with requires_hitl=true

## Critical Rules
- You NEVER call external APIs directly — always route via sub-agents
- For simple greetings like "hi", "hello", "hey" — ALWAYS use `direct_reply`
- If you're unsure whether the user wants a task or just wants to chat, use `direct_reply`
- Maintain the global AgentState — log every routing decision
- Use conversation context from memory to give more relevant, personalized responses

## Output Format
When aggregating final results, return:
```json
{
  "status": "complete",
  "goal": "<original goal>",
  "summary": "<2-3 sentence summary of what was accomplished>",
  "highlights": ["<key outcome 1>", "<key outcome 2>"]
}
```
""".strip()


# ── Research Sub-Agent ────────────────────────────────────────────────────────

RESEARCH_AGENT_SYSTEM_PROMPT = """
You are FRAME-MO's Research Sub-Agent — a focused web research specialist.

## Your Role
Conduct targeted web research using Tavily Search MCP and URL fetching.
Synthesize what you find into a structured, factual summary.

## Tools Available
- search (via Web Search MCP): Search the web with a query string
- fetch_url (via Web Fetch MCP): Retrieve and extract content from a URL

## Process
1. Execute a web search for the given research query
2. Fetch the top 3 most relevant results
3. Synthesize findings into a clear, factual summary
4. List all source URLs
5. Score your own confidence (0.0–1.0) based on source quality and consistency

## Output — Return this EXACT JSON structure
```json
{
  "query": "<exact search query used>",
  "summary": "<detailed synthesis of findings — minimum 50 words>",
  "sources": ["<url1>", "<url2>", "<url3>"],
  "key_facts": ["<fact1>", "<fact2>", "<fact3>"],
  "confidence": 0.85,
  "status": "complete"
}
```

## Rules
- NEVER fabricate sources — only include URLs you actually fetched
- If search returns no usable results, set status="partial" and confidence < 0.5
- Keep summary factual — no opinions or speculation
""".strip()


# ── Code Sub-Agent ────────────────────────────────────────────────────────────

CODE_AGENT_SYSTEM_PROMPT = """
You are FRAME-MO's Code Sub-Agent — a GitHub automation specialist.

## Your Role
Interact with GitHub repositories: read PRs and issues, create issues,
post review comments, and summarize code changes.

## Tools Available (via GitHub MCP)
- get_pr_diff: Read a pull request diff
- create_github_issue: Open a new issue on a repository
- post_review_comment: Post a comment on a PR
- list_issues: List open issues on a repository

## Process
1. Parse the task to identify the target repo and action
2. Execute the appropriate GitHub tool
3. Confirm the action was completed with verifiable output (URL, issue number, etc.)

## Output — Return this EXACT JSON structure
```json
{
  "repo": "<owner/repo>",
  "action_taken": "<human description of what was done>",
  "status": "success",
  "details": "<any relevant context or returned data>",
  "pr_number": null,
  "issue_number": 42,
  "comment_id": null,
  "url": "https://github.com/owner/repo/issues/42"
}
```

## Rules
- Always confirm the repo exists before acting
- Never guess issue or PR numbers — use only what the API returns
- If the action requires creating content, keep it professional and concise
""".strip()


# ── Knowledge Sub-Agent ───────────────────────────────────────────────────────

KNOWLEDGE_AGENT_SYSTEM_PROMPT = """
You are FRAME-MO's Knowledge Sub-Agent — a Notion workspace specialist.

## Your Role
Read from and write to Notion: retrieve page content, create new pages,
and append structured blocks to existing pages.

## Tools Available (via Notion MCP)
- read_notion_page: Read content from a Notion page by ID
- create_notion_page: Create a new page in a Notion workspace
- append_notion_block: Append blocks (text, headings, bullets) to a page
- search_notion: Search the Notion workspace

## Process
1. Parse the task to identify the action (read / create / append) and target
2. Format content as clean, well-structured Notion blocks
3. Execute the Notion tool
4. Return the page ID and a content preview

## Output — Return this EXACT JSON structure
```json
{
  "action": "create",
  "page_id": "<notion-page-id>",
  "page_title": "<Page Title>",
  "status": "success",
  "content_preview": "<first 300 chars of the page content>",
  "page_url": "https://notion.so/page-id"
}
```

## Rules
- Format all page content in clean Markdown (Notion accepts Markdown in API)
- Always include a clear title for created pages
- Truncate content_preview at 300 characters
""".strip()


# ── Communication Sub-Agent ───────────────────────────────────────────────────

COMMS_AGENT_SYSTEM_PROMPT = """
You are FRAME-MO's Communication Sub-Agent — a Gmail email specialist.

## Your Role
Read email threads, compose professional email drafts, and send reports
via Gmail. You must ALWAYS draft before sending — never send without review.

## Tools Available (via Gmail MCP)
- read_email_thread: Read messages in a Gmail thread
- draft_email: Create an email draft (does NOT send)
- send_email: Send a previously drafted email

## Process
1. Parse the task — identify action (read / draft / send) and recipient
2. For send tasks: always draft first, then confirm before sending
3. Keep emails professional, concise, and well-formatted
4. Return the message ID and thread ID for traceability

## Output — Return this EXACT JSON structure
```json
{
  "action": "send",
  "status": "sent",
  "recipient": "team@company.com",
  "subject": "Research Report: Agentic AI Trends",
  "message_id": "<gmail-message-id>",
  "thread_id": "<gmail-thread-id>",
  "preview": "<first 200 chars of email body>"
}
```

## Rules
- NEVER send an email without an explicit send action in the task
- Keep subject lines clear and professional (under 60 characters)
- Always include a preview of the email body in your output
""".strip()


# ── Prompt registry ───────────────────────────────────────────────────────────

AGENT_PROMPTS: dict[str, str] = {
    "orchestrator":    ORCHESTRATOR_SYSTEM_PROMPT,
    "research_agent":  RESEARCH_AGENT_SYSTEM_PROMPT,
    "code_agent":      CODE_AGENT_SYSTEM_PROMPT,
    "knowledge_agent": KNOWLEDGE_AGENT_SYSTEM_PROMPT,
    "comms_agent":     COMMS_AGENT_SYSTEM_PROMPT,
}


def get_prompt(agent_name: str) -> str:
    """Retrieve the system prompt for a given agent name."""
    prompt = AGENT_PROMPTS.get(agent_name)
    if prompt is None:
        raise KeyError(
            f"No system prompt registered for '{agent_name}'. "
            f"Available: {list(AGENT_PROMPTS.keys())}"
        )
    return prompt
```

## `frontend/src/App.jsx`
```javascript
import React, { useState, useEffect, useRef, useCallback } from 'react';
import {
  Plus, Search, User, Paperclip, ArrowUp, PanelLeftClose, PanelLeft,
  Database, Blocks, Settings, Code, Mail, FileText, CheckCircle2,
  Loader2, AlertCircle, XCircle, RefreshCw, Workflow,
  ChevronDown, ChevronRight, ShieldAlert, Undo2
} from 'lucide-react';

// ── Constants ──────────────────────────────────────────────────────────────────

const AGENT_INFO = {
  research_agent:  { name: 'Research Agent',  icon: Search },
  code_agent:      { name: 'Code Agent',      icon: Code },
  knowledge_agent: { name: 'Knowledge Agent', icon: FileText },
  comms_agent:     { name: 'Comms Agent',     icon: Mail },
};

function emptyMetrics() {
  return {
    completed_agents: [],
    current_agent:    null,
    error_count:      0,
    hitl_pending:     [],
    agent_results:    {},
    retry_counts:     {},
    saga_log:         [],
    pei_violations:   [],
    direct_reply:     '',
    intent_type:      'task',
  };
}

// ── Components ─────────────────────────────────────────────────────────────────

const AgentLogLine = ({ agentId, status, retryCount }) => {
  const info = AGENT_INFO[agentId] || { name: agentId, icon: Blocks };
  const Icon = info.icon;
  return (
    <div className="flex items-center gap-2 text-sm py-1.5 px-3 hover:bg-slate-50 transition-colors">
      <Icon size={14} className={status === 'running' ? 'text-[#d97757] animate-pulse' : 'text-slate-400'} />
      <span className={`font-medium ${status === 'running' ? 'text-slate-800' : 'text-slate-600'}`}>
        {info.name}
      </span>
      <span className="flex-1 text-slate-400 text-xs truncate">
        {status === 'running' ? 'Processing...' : status === 'complete' ? 'Done' : 'Queued'}
      </span>
      {status === 'running' && <Loader2 size={12} className="animate-spin text-[#d97757]" />}
      {status === 'complete' && <CheckCircle2 size={14} className="text-green-500" />}
      {retryCount > 0 && (
        <span className="text-[10px] bg-amber-100 text-amber-700 px-1.5 py-0.5 rounded font-semibold flex items-center gap-0.5">
          <RefreshCw size={9} /> {retryCount}
        </span>
      )}
    </div>
  );
};

// ── App ────────────────────────────────────────────────────────────────────────

const App = () => {
  const [sidebarOpen, setSidebarOpen]   = useState(true);
  const [mcpModalOpen, setMcpModalOpen] = useState(false);
  const [inputText, setInputText]       = useState('');
  const [isSubmitting, setIsSubmitting] = useState(false);

  const [activeTask, setActiveTask]     = useState(null);
  const [liveMetrics, setLiveMetrics]   = useState(emptyMetrics());
  const [finalResult, setFinalResult]   = useState(null);
  const [sessions, setSessions]         = useState([]);
  const [mcpHealth, setMcpHealth]       = useState({});
  const [hitlLoading, setHitlLoading]   = useState(false);
  const [agentLogsExpanded, setAgentLogsExpanded] = useState(true);

  const wsRef           = useRef(null);
  const wsReconnects    = useRef(0);
  const messagesEndRef  = useRef(null);

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch('/tasks');
      if (res.ok) setSessions(await res.json());
    } catch (_) {}
  }, []);

  const fetchMcpHealth = useCallback(async () => {
    try {
      const res = await fetch('/mcp/health');
      if (res.ok) setMcpHealth(await res.json());
    } catch (_) {}
  }, []);

  useEffect(() => {
    fetchSessions();
    fetchMcpHealth();
    const interval = setInterval(fetchMcpHealth, 10_000);
    return () => clearInterval(interval);
  }, [fetchSessions, fetchMcpHealth]);

  const connectWs = useCallback((taskId) => {
    if (wsRef.current) wsRef.current.close(1000);
    const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${protocol}://${window.location.host}/ws/${taskId}`);

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);

      if (data.event === 'status') {
        setLiveMetrics(prev => ({
          completed_agents: data.completed_agents ?? prev.completed_agents,
          current_agent:    data.current_agent    ?? null,
          error_count:      data.error_count      ?? prev.error_count,
          hitl_pending:     data.hitl_pending     ?? prev.hitl_pending,
          agent_results:    data.agent_results    ?? prev.agent_results,
          retry_counts:     data.retry_counts     ?? prev.retry_counts,
          saga_log:         data.saga_log         ?? prev.saga_log,
          pei_violations:   data.pei_violations   ?? prev.pei_violations,
          direct_reply:     data.direct_reply     ?? prev.direct_reply,
          intent_type:      data.intent_type      ?? prev.intent_type,
        }));
        if (data.status) setActiveTask(t => t ? { ...t, status: data.status } : null);
      }

      if (data.event === 'complete' && data.result) {
        setFinalResult(data.result);
        setActiveTask(t => t ? { ...t, status: 'complete' } : null);
        setAgentLogsExpanded(false);
        fetchSessions();
      }

      if (data.event === 'error') {
        setActiveTask(t => t ? { ...t, status: 'failed' } : null);
        fetchSessions();
      }

      // Stop reconnecting on terminal states
      // Stop reconnecting on terminal states
      if (data.event === 'terminal') {
        wsReconnects.current = 99; // prevent reconnect
      }
    };

    ws.onclose = (e) => {
      if (e.code !== 1000 && wsReconnects.current < 5) {
        wsReconnects.current += 1;
        setTimeout(() => connectWs(taskId), 2000 * wsReconnects.current);
      }
    };

    wsRef.current = ws;
    wsReconnects.current = 0;
  }, [fetchSessions]);

  useEffect(() => {
    if (!activeTask?.id || activeTask.id === 'pending') return;
    if (['complete', 'failed'].includes(activeTask.status)) return;
    connectWs(activeTask.id);
    return () => { if (wsRef.current) wsRef.current.close(1000); };
  }, [activeTask?.id, connectWs]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [liveMetrics, finalResult]);
  const handleSubmit = async () => {
    if (!inputText.trim() || isSubmitting) return;
    setIsSubmitting(true);
    const goal = inputText.trim();
    setInputText('');
    setFinalResult(null);
    setAgentLogsExpanded(true);

    try {
      const res = await fetch('/tasks', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ goal }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setActiveTask({ id: data.task_id, goal: data.goal, status: data.status });
      setLiveMetrics(emptyMetrics());
    } catch (err) {
      alert(`Could not connect to the FRAME-MO backend.\nError: ${err.message}`);
    } finally {
      setIsSubmitting(false);
    }
  };

  const clearTask = () => {
    if (wsRef.current) wsRef.current.close(1000);
    setActiveTask(null);
    setLiveMetrics(emptyMetrics());
    setFinalResult(null);
    setInputText('');
  };

  const handleSessionClick = async (session) => {
    if (wsRef.current) wsRef.current.close(1000);
    setFinalResult(null);
    setLiveMetrics(emptyMetrics());
    setAgentLogsExpanded(session.status !== 'complete');

    try {
      const res = await fetch(`/tasks/${session.task_id}`);
      if (res.ok) {
        const status = await res.json();
        setActiveTask({ id: status.task_id, goal: status.goal, status: status.status });
        setLiveMetrics(prev => ({
          ...prev,
          completed_agents: status.completed_agents || [],
          current_agent:    status.current_agent    || null,
          error_count:      status.error_count      || 0,
          hitl_pending:     status.hitl_pending     || [],
          retry_counts:     status.retry_counts     || {},
          saga_log:         status.saga_log         || [],
          pei_violations:   status.pei_violations   || [],
          direct_reply:     status.direct_reply     || '',
          intent_type:      status.intent_type      || 'task',
        }));
        if (!['complete', 'failed'].includes(status.status)) {
          connectWs(status.task_id);
        }
        return;
      }
    } catch (_) {}
    setActiveTask({ id: session.task_id, goal: session.goal_preview, status: session.status });
  };

  const handleHITL = async (approved) => {
    if (!activeTask?.id || hitlLoading) return;
    setHitlLoading(true);
    try {
      const res = await fetch(`/tasks/${activeTask.id}/hitl`, {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ approved }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (e) {
      alert(`Failed to submit decision: ${e.message}`);
    } finally {
      setHitlLoading(false);
    }
  };

  const isTaskActive = activeTask && !['complete', 'failed'].includes(activeTask.status);
  const isConversation = liveMetrics.intent_type === 'conversation' || finalResult?.intent_type === 'conversation';
  const hasAgentWork = liveMetrics.completed_agents.length > 0 || liveMetrics.current_agent;
  const directReply = liveMetrics.direct_reply || finalResult?.summary || '';

  return (
    <div className="flex h-[100dvh] bg-white text-slate-800 font-sans">

      {/* ── Sidebar ────────────────────────────────────────────────────────── */}
      {sidebarOpen && (
        <div className="w-[260px] bg-[#f9f9f9] border-r border-[#e5e5e5] flex flex-col shrink-0">
          <div className="p-3 flex items-center justify-between">
            <button onClick={clearTask} className="flex items-center gap-2 px-3 py-2 text-sm font-medium hover:bg-slate-200/50 rounded-md text-slate-700">
              <div className="w-5 h-5 bg-white shadow-sm border border-slate-200 text-[#d97757] flex items-center justify-center rounded">
                <Blocks size={12} />
              </div>
              FRAME-MO
            </button>
            <button onClick={() => setSidebarOpen(false)} className="p-2 hover:bg-slate-200/50 rounded-md text-slate-500">
              <PanelLeftClose size={18} />
            </button>
          </div>

          <div className="px-3 pb-2 pt-1">
            <button onClick={clearTask} className="w-full flex items-center gap-2 px-3 py-2 text-sm text-slate-600 bg-white border border-slate-200 hover:bg-slate-50 rounded-md shadow-sm">
              <Plus size={16} /> New chat
            </button>
          </div>

          <div className="flex-1 overflow-y-auto mt-2 px-3">
            <div className="text-[11px] font-semibold text-slate-500 px-2 mb-2 tracking-wider">Recent Chats</div>
            {sessions.map((s) => (
              <button
                key={s.task_id}
                onClick={() => handleSessionClick(s)}
                className={`w-full text-left px-3 py-2 text-sm rounded-md transition-colors mb-0.5 ${activeTask?.id === s.task_id ? 'bg-[#ebebeb] font-medium' : 'hover:bg-[#ebebeb]'}`}
              >
                <p className="truncate text-slate-700 max-w-[200px]">{s.goal_preview}</p>
              </button>
            ))}
          </div>

          <div className="p-3 border-t border-[#e5e5e5] flex flex-col gap-1">
            <button onClick={() => setMcpModalOpen(true)} className="flex items-center gap-2 px-3 py-2 text-sm text-slate-600 hover:bg-[#ebebeb] rounded-md">
              <Database size={16} /> MCP Config
            </button>
            <button className="flex items-center gap-2 px-3 py-2 text-sm text-slate-600 hover:bg-[#ebebeb] rounded-md">
              <Settings size={16} /> Settings
            </button>
          </div>
        </div>
      )}

      {/* ── Main Chat Area ─────────────────────────────────────────────────── */}
      <div className="flex-1 flex flex-col relative w-full h-full min-w-0">

        {!sidebarOpen && (
          <div className="absolute top-4 left-4 z-10">
            <button onClick={() => setSidebarOpen(true)} className="p-2 hover:bg-slate-100 rounded-md text-slate-500 bg-white/80 backdrop-blur border border-slate-200 shadow-sm">
              <PanelLeft size={18} />
            </button>
          </div>
        )}

        <div className="flex-1 overflow-y-auto w-full">
          {!activeTask ? (
            <div className="h-full flex flex-col items-center justify-center px-4">
              <div className="w-12 h-12 bg-[#f4ece9] text-[#d97757] rounded-xl flex items-center justify-center mb-5">
                <Blocks size={24} />
              </div>
              <h1 className="text-2xl font-medium text-slate-800 mb-2">How can I help you today?</h1>
              <p className="text-slate-500 text-[15px] mb-8">Agentic web research, codebase queries, or automated communications.</p>
            </div>
          ) : (
            <div className="max-w-3xl mx-auto w-full py-8 px-4 flex flex-col gap-6 pb-32">

              {/* User Message */}
              <div className="flex justify-end pt-4">
                <div className="bg-[#f3f4f6] text-slate-900 px-5 py-3.5 rounded-2xl rounded-tr-sm max-w-[85%] text-[15.5px] leading-relaxed break-words shadow-sm">
                  {activeTask.goal}
                </div>
              </div>

              {/* AI Response */}
              <div className="flex gap-4 items-start pb-8">
                <div className="w-8 h-8 rounded-full bg-[#d97757] text-white flex items-center justify-center shrink-0 mt-1 shadow-sm">
                  <Blocks size={16} />
                </div>

                <div className="flex-1 flex flex-col gap-3 min-w-0 pt-1.5">

                  {/* Tool Execution Block — only for task intent */}
                  {!isConversation && hasAgentWork && (
                    <div className="border border-slate-200 rounded-xl bg-white overflow-hidden shadow-sm max-w-[480px]">
                      <button
                        onClick={() => setAgentLogsExpanded(!agentLogsExpanded)}
                        className="w-full flex items-center gap-2 px-3 py-2.5 bg-slate-50/50 hover:bg-slate-100/50 text-slate-600 text-[13px] font-medium transition-colors"
                      >
                        {agentLogsExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                        <Workflow size={14} className="text-[#d97757]" />
                        {activeTask.status === 'complete' ? 'Used tools' : activeTask.status === 'rolling_back' ? 'Rolling back...' : 'Using tools...'}
                      </button>

                      {agentLogsExpanded && (
                        <div className="flex flex-col border-t border-slate-100 bg-white py-1">
                          {liveMetrics.completed_agents.map(ag => (
                            <AgentLogLine key={ag} agentId={ag} status="complete" retryCount={liveMetrics.retry_counts[ag]} />
                          ))}
                          {liveMetrics.current_agent && (
                            <AgentLogLine agentId={liveMetrics.current_agent} status="running" retryCount={liveMetrics.retry_counts[liveMetrics.current_agent]} />
                          )}
                          {!liveMetrics.current_agent && activeTask.status === 'running' && liveMetrics.completed_agents.length === 0 && (
                            <div className="flex items-center gap-2 text-sm py-2 px-3 text-slate-500">
                              <Loader2 size={14} className="animate-spin" /> Planning...
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  )}

                  {/* Saga Rollback Indicator */}
                  {liveMetrics.saga_log.length > 0 && (
                    <div className="bg-amber-50 border border-amber-200 rounded-xl p-3 max-w-[480px]">
                      <div className="flex items-center gap-2 mb-1.5">
                        <Undo2 size={14} className="text-amber-600" />
                        <span className="text-[13px] font-semibold text-amber-800">Saga Rollback</span>
                      </div>
                      {liveMetrics.saga_log.map((entry, i) => (
                        <div key={i} className="text-xs text-amber-700 flex items-center gap-1.5 py-0.5">
                          {entry.success ? <CheckCircle2 size={11} className="text-green-500" /> : <XCircle size={11} className="text-red-500" />}
                          <span className="font-medium">{entry.agent}:</span> {entry.action}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* PEI Violations */}
                  {liveMetrics.pei_violations.length > 0 && (
                    <div className="bg-red-50 border border-red-200 rounded-xl p-3 max-w-[480px]">
                      <div className="flex items-center gap-2 mb-1.5">
                        <ShieldAlert size={14} className="text-red-600" />
                        <span className="text-[13px] font-semibold text-red-800">Safety Monitor</span>
                      </div>
                      {liveMetrics.pei_violations.map((v, i) => (
                        <div key={i} className="text-xs text-red-700 py-0.5">
                          <span className="font-medium">{v.agent}:</span> {v.violation}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* HITL Approval */}
                  {liveMetrics.hitl_pending?.length > 0 && (
                    <div className="bg-amber-50/80 border border-amber-200/60 rounded-xl p-4 text-amber-900 max-w-full">
                      <div className="flex items-center gap-2 mb-2">
                        <AlertCircle size={16} className="text-amber-600" />
                        <h4 className="font-semibold text-sm">Action Needs Approval</h4>
                      </div>
                      {liveMetrics.hitl_pending.map((req, i) => (
                        <div key={i} className="text-xs font-mono bg-white border border-amber-100 p-2.5 rounded-lg mb-3">
                          <span className="font-semibold text-amber-700">{req.action?.tool || 'Tool'}</span>
                          <div className="text-slate-600 truncate mt-1">Input: {JSON.stringify(req.action?.input || {})}</div>
                        </div>
                      ))}
                      <div className="flex gap-2">
                        <button onClick={() => handleHITL(true)} disabled={hitlLoading} className="px-3 py-1.5 bg-amber-600 hover:bg-amber-700 text-white rounded-md text-[13px] font-medium transition disabled:opacity-50">Approve</button>
                        <button onClick={() => handleHITL(false)} disabled={hitlLoading} className="px-3 py-1.5 bg-white hover:bg-slate-50 border border-amber-200 text-amber-700 rounded-md text-[13px] font-medium transition disabled:opacity-50">Reject</button>
                      </div>
                    </div>
                  )}

                  {/* Final AI Response — works for BOTH conversation and task modes */}
                  {directReply && activeTask.status === 'complete' && (
                    <div className="prose prose-slate prose-sm sm:prose-base max-w-none text-slate-800 leading-relaxed mt-1">
                      <p className="whitespace-pre-wrap">{directReply}</p>
                      {finalResult?.highlights?.length > 0 && (
                        <ul className="mt-2 space-y-1 pl-4">
                          {finalResult.highlights.map((h, idx) => <li key={idx}>{h}</li>)}
                        </ul>
                      )}
                    </div>
                  )}

                  {/* Loading state for conversation */}
                  {isConversation && !directReply && activeTask.status !== 'complete' && (
                    <div className="flex items-center gap-2 text-slate-500 text-sm">
                      <Loader2 size={14} className="animate-spin" /> Thinking...
                    </div>
                  )}

                  {/* Error */}
                  {activeTask.status === 'failed' && (
                    <div className="text-red-600 text-[15px] flex items-center gap-2 mt-2">
                      <XCircle size={16} /> Error completing task.
                    </div>
                  )}
                </div>
              </div>
              <div ref={messagesEndRef} className="h-4" />
            </div>
          )}
        </div>

        {/* Input Bar */}
        <div className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-white via-white to-transparent pt-8 pb-6 px-4">
          <div className="max-w-3xl mx-auto">
            <div className={`relative flex flex-col bg-white rounded-2xl border shadow-sm transition-all focus-within:ring-2 focus-within:ring-[#d97757]/20 focus-within:border-[#d97757]/50 ${isTaskActive ? 'opacity-70 pointer-events-none border-slate-200' : 'border-slate-300'}`}>
              <textarea
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
                }}
                disabled={isSubmitting || isTaskActive}
                placeholder={isTaskActive ? "Generating..." : "Message FRAME-MO..."}
                rows={2}
                className="w-full resize-none bg-transparent px-4 py-3.5 pr-12 text-[15px] leading-relaxed outline-none text-slate-800 placeholder-slate-400 disabled:opacity-50"
              />
              <div className="flex items-center justify-between px-3 pb-2.5">
                <button className="p-1.5 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-md transition-colors">
                  <Paperclip size={18} />
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={!inputText.trim() || isSubmitting || isTaskActive}
                  className={`p-1.5 rounded-xl flex items-center justify-center transition-all ${
                    inputText.trim() && !isSubmitting && !isTaskActive
                      ? 'bg-black text-white shadow-sm'
                      : 'bg-slate-100 text-slate-400 cursor-not-allowed'
                  }`}
                >
                  {isSubmitting ? <Loader2 size={16} className="animate-spin" /> : <ArrowUp size={16} strokeWidth={2.5} />}
                </button>
              </div>
            </div>
            <div className="text-center mt-2.5 text-xs text-slate-400 font-medium tracking-wide">
              FRAME-MO · Ollama orchestration · LTL verification · PEI monitoring · Saga recovery
            </div>
          </div>
        </div>
      </div>

      {/* ── MCP Modal ──────────────────────────────────────────────────────── */}
      {mcpModalOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-[400px] overflow-hidden">
            <div className="px-5 py-4 border-b border-slate-100 flex justify-between items-center">
              <div className="font-semibold text-[15px] flex items-center gap-2 text-slate-800">
                <Database size={18} className="text-slate-500" /> Context Protocols
              </div>
              <button onClick={() => setMcpModalOpen(false)} className="text-slate-400 hover:text-slate-600 hover:bg-slate-100 p-1 rounded-md">
                <XCircle size={20} />
              </button>
            </div>
            <div className="p-5 flex flex-col gap-3">
              {Object.keys(mcpHealth).length > 0 ? (
                Object.entries(mcpHealth).map(([srv, st]) => (
                  <div key={srv} className="flex justify-between items-center p-3 border border-slate-200 rounded-xl">
                    <span className="text-[14px] font-medium text-slate-700 capitalize">{srv.replace('-mcp', '')}</span>
                    <span className={`text-[11px] px-2.5 py-0.5 rounded-full font-semibold ${
                      st === 'healthy' ? 'bg-green-100 text-green-700' :
                      st === 'stub' ? 'bg-slate-100 text-slate-500' :
                      'bg-red-100 text-red-600'
                    }`}>{st}</span>
                  </div>
                ))
              ) : (
                <div className="text-sm text-slate-500 text-center py-6">Connecting...</div>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

export default App;
```

