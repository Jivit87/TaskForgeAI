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
