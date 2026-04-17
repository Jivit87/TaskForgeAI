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
