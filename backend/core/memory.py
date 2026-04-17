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
