from __future__ import annotations
"""
schemas/execution_plan.py
ExecutionPlan and SubTask — the orchestrator's decomposed plan for a user goal.
Master Orchestrator generates this before routing to sub-agents.
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional


class SubTask(BaseModel):
    """A single unit of work routed to one sub-agent."""

    # Which agent handles this subtask
    agent: Literal[
        "research_agent", "code_agent", "knowledge_agent", "comms_agent"
    ]

    # Human-readable description of what should be done
    description: str = Field(
        ...,
        min_length=5,
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


class ExecutionPlan(BaseModel):
    """
    Ordered list of subtasks produced by the Master Orchestrator.
    Subtasks are executed in order, with dependency checks respected.
    """

    goal: str = Field(..., min_length=5, description="Original user goal")
    steps: list[SubTask] = Field(
        ...,
        min_length=1,
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
