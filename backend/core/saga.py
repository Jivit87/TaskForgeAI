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

# ── Compensating action → MCP tool mapping ────────────────────────────────────
# Maps agent names to their MCP server and rollback tool configurations.
COMPENSATING_MCP_MAP = {
    "code_agent": {
        "mcp_server": "github-mcp",
        "actions": {
            "close_issue": lambda result: {
                "tool": "update_issue",
                "args": {
                    "owner": result.get("repo", "").split("/")[0] if "/" in result.get("repo", "") else "",
                    "repo": result.get("repo", "").split("/")[1] if "/" in result.get("repo", "") else "",
                    "issue_number": result.get("issue_number"),
                    "state": "closed",
                },
            },
        },
    },
    "knowledge_agent": {
        "mcp_server": "notion-mcp",
        "actions": {
            "delete_page": lambda result: {
                "tool": "API-delete-a-block",
                "args": {"block_id": result.get("page_id", "")},
            },
        },
    },
}


class SagaEngine:
    """
    Executes compensating actions in reverse order when a pipeline step fails.

    The engine reads compensating_actions from the agent's class metadata
    and dispatches real MCP tool calls to undo the action. Falls back to
    logging when MCP is unavailable.
    """

    def __init__(self, checkpoint_store: "CheckpointStore", mcp_manager=None):
        self.checkpoint = checkpoint_store
        self.mcp = mcp_manager

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
        Execute a single compensating action via MCP if available.
        Falls back to log-only mode when MCP is not connected.
        """
        agent_result = state.agent_results.get(step.agent, {})

        # Try to dispatch a real MCP compensating call
        if self.mcp and step.agent in COMPENSATING_MCP_MAP:
            agent_config = COMPENSATING_MCP_MAP[step.agent]
            mcp_server = agent_config["mcp_server"]
            # Find matching compensating action builder
            for action_key, builder_fn in agent_config["actions"].items():
                if action_key in compensating.lower().replace(" ", "_"):
                    try:
                        call_spec = builder_fn(agent_result)
                        tool_name = call_spec["tool"]
                        tool_args = call_spec["args"]
                        # Only call if we have the required data
                        if all(v for v in tool_args.values()):
                            log.info(
                                f"[saga] MCP rollback → {mcp_server}.{tool_name}"
                                f"({list(tool_args.keys())})"
                            )
                            await self.mcp.call_tool(
                                mcp_server, tool_name, tool_args, idempotent=False
                            )
                        else:
                            log.warning(
                                f"[saga] Missing data for MCP rollback of {step.agent} "
                                f"— logging only"
                            )
                    except Exception as exc:
                        log.warning(
                            f"[saga] MCP compensating call failed for {step.agent}: {exc}. "
                            f"Logging action as best-effort."
                        )
                    break
        else:
            log.info(
                f"[saga] No MCP available for {step.agent} — "
                f"logging compensating action: {compensating}"
            )

        # Remove the agent from completed list since it's been rolled back
        if step.agent in state.completed_agents:
            state.completed_agents.remove(step.agent)
        if step.agent in state.agent_results:
            del state.agent_results[step.agent]
