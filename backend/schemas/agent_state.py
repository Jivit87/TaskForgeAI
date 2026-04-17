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
