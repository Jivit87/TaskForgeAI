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
