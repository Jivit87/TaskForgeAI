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
