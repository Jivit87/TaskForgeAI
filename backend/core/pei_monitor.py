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
