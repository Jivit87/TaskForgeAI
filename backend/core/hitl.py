from __future__ import annotations
"""
core/hitl.py
Human-in-the-Loop (HITL) Gate.

Pauses agent execution before irreversible actions (send email,
create GitHub issue, write to Notion, delete pages) and waits
for explicit human approval via terminal prompt.

State is checkpointed BEFORE the pause — if the process is killed
while waiting, the task can be safely resumed.
"""

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from schemas.agent_state import AgentState

log = logging.getLogger("frame_mo.hitl")

# ── Actions that always require human approval ────────────────────────────────
IRREVERSIBLE_ACTIONS = {
    "send_email",
    "create_github_issue",
    "post_github_comment",
    "create_notion_page",
    "append_notion_block",
    "delete_notion_page",
}

# Confidence below this threshold → ask human to verify
CONFIDENCE_THRESHOLD = 0.6

# Retry count at or above this → ask human whether to continue
MAX_AUTO_RETRIES = 2


@dataclass
class HITLRequest:
    task_id: str
    agent_name: str
    proposed_action: dict
    reason: str
    requested_at: float = 0.0

    def __post_init__(self):
        self.requested_at = self.requested_at or time.time()


class HITLGate:
    """
    Manages Human-in-the-Loop approval checkpoints.

    Usage:
        gate = HITLGate(checkpoint_store)

        if gate.should_require_approval(action, state):
            approved = gate.request_approval(task_id, action, state)
            if not approved:
                # fallback or skip
    """

    def __init__(self, checkpoint_store):
        self.store = checkpoint_store
        self.pending: dict[str, HITLRequest] = {}
        self._decision_log: list[dict] = []

    # ── Decision logic ────────────────────────────────────────────────────────

    def should_require_approval(
        self,
        action: dict,
        state: AgentState,
    ) -> bool:
        """
        Returns True if the proposed action needs human sign-off.
        Three triggers:
          1. Action is irreversible (whitelist check)
          2. Agent confidence is below threshold
          3. Agent has already retried >= MAX_AUTO_RETRIES times
        """
        tool = action.get("tool", "")
        confidence = float(action.get("confidence", 1.0))
        retry_count = state.retry_counts.get(state.current_agent, 0)

        reasons = []
        if tool in IRREVERSIBLE_ACTIONS:
            reasons.append(f"irreversible action '{tool}'")
        if confidence < CONFIDENCE_THRESHOLD:
            reasons.append(f"low confidence ({confidence:.0%})")
        if retry_count >= MAX_AUTO_RETRIES:
            reasons.append(f"high retry count ({retry_count})")

        if reasons:
            log.info(
                f"[hitl] Approval required for task={state.task_id} "
                f"agent={state.current_agent} — reasons: {', '.join(reasons)}"
            )
            return True
        return False

    # ── Approval flow ─────────────────────────────────────────────────────────

    def request_approval(
        self,
        task_id: str,
        action: dict,
        state: AgentState,
        checkpoint_store=None,
    ) -> bool:
        """
        Pause execution and request human approval via terminal.
        Checkpoints state before pausing so the task can be resumed
        even if the process is killed during the wait.

        Returns True if approved, False if rejected.
        """
        # Checkpoint before pausing — safe restart point
        state.status = "paused_hitl"
        store = checkpoint_store or self.store
        if store:
            store.save(state)

        # Register the pending request
        req = HITLRequest(
            task_id=task_id,
            agent_name=state.current_agent,
            proposed_action=action,
            reason=self._build_reason(action, state),
        )
        self.pending[task_id] = req

        # Terminal prompt
        self._print_hitl_banner(req)

        try:
            decision_raw = input("   ▶  Approve? [y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            decision_raw = "n"
            print("\n[hitl] Input interrupted — defaulting to REJECT")

        approved = decision_raw == "y"

        # Update state and log
        state.status = "running"
        del self.pending[task_id]
        self._decision_log.append({
            "task_id": task_id,
            "agent": state.current_agent,
            "action": action.get("tool"),
            "approved": approved,
            "timestamp": time.time(),
        })

        outcome = "✅ APPROVED" if approved else "❌ REJECTED"
        log.info(
            f"[hitl] {outcome} → task={task_id}  "
            f"agent={state.current_agent}  tool={action.get('tool')}"
        )
        print(f"\n   {outcome}\n")
        return approved

    # ── Headless / API mode (for frontend integration) ────────────────────────

    def submit_decision(self, task_id: str, approved: bool) -> bool:
        """
        Called programmatically (e.g., from FastAPI endpoint) instead of
        blocking on terminal input. Returns True if the pending request existed.
        """
        if task_id not in self.pending:
            return False
        self._decision_log.append({
            "task_id": task_id,
            "agent": self.pending[task_id].agent_name,
            "approved": approved,
            "timestamp": time.time(),
        })
        del self.pending[task_id]
        return True

    def get_pending(self) -> list[dict]:
        """Return all pending HITL requests (for frontend polling)."""
        return [
            {
                "task_id": r.task_id,
                "agent": r.agent_name,
                "action": r.proposed_action,
                "reason": r.reason,
                "waiting_since": r.requested_at,
            }
            for r in self.pending.values()
        ]

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _build_reason(self, action: dict, state: AgentState) -> str:
        parts = []
        if action.get("tool") in IRREVERSIBLE_ACTIONS:
            parts.append("irreversible action")
        conf = action.get("confidence", 1.0)
        if float(conf) < CONFIDENCE_THRESHOLD:
            parts.append(f"low confidence ({float(conf):.0%})")
        retry = state.retry_counts.get(state.current_agent, 0)
        if retry >= MAX_AUTO_RETRIES:
            parts.append(f"retry #{retry}")
        return " · ".join(parts) if parts else "policy rule"

    @staticmethod
    def _print_hitl_banner(req: HITLRequest) -> None:
        border = "─" * 60
        print(f"\n┌{border}┐")
        print(f"│  ⚠️   HITL APPROVAL REQUIRED")
        print(f"│  Task    : {req.task_id}")
        print(f"│  Agent   : {req.agent_name}")
        print(f"│  Reason  : {req.reason}")
        print(f"│  Action  :")
        for line in json.dumps(req.proposed_action, indent=4).splitlines():
            print(f"│      {line}")
        print(f"└{border}┘")
