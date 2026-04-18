from __future__ import annotations
"""
schemas/agent_outputs.py
Pydantic output schemas for all 4 sub-agents.
Every sub-agent response is validated against its schema before the
orchestrator processes it. Invalid outputs never leave the validation gate.
"""

from pydantic import BaseModel, Field
from typing import Literal, Optional
from datetime import datetime


# ── Research Sub-Agent ────────────────────────────────────────────────────────

class ResearchResult(BaseModel):
    """Output schema for the Research Sub-Agent (web search + fetch)."""

    query: str = Field(..., description="The research query that was executed")
    summary: str = Field(
        ...,
        min_length=1,
        description="Synthesized summary of findings"
    )
    sources: list[str] = Field(
        default_factory=list,
        description="List of source URLs used"
    )
    key_facts: list[str] = Field(
        default_factory=list,
        description="Bullet-point key facts extracted"
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score 0.0–1.0 based on source quality"
    )
    status: Literal["complete", "partial", "failed"] = "complete"
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


# ── Code Sub-Agent ────────────────────────────────────────────────────────────

class CodeResult(BaseModel):
    """Output schema for the Code Sub-Agent (GitHub interactions)."""

    repo: str = Field(..., description="GitHub repo in owner/repo format")
    action_taken: str = Field(..., description="Exact action performed")
    status: Literal["success", "skipped", "failed"] = "success"
    details: str = Field(default="", description="Additional context or result body")
    pr_number: Optional[int] = Field(default=None, description="PR number if applicable")
    issue_number: Optional[int] = Field(default=None, description="Issue number if created")
    comment_id: Optional[str] = Field(default=None, description="Comment ID if posted")
    url: Optional[str] = Field(default=None, description="URL of the created resource")


# ── Knowledge Sub-Agent ───────────────────────────────────────────────────────

class KnowledgeResult(BaseModel):
    """Output schema for the Knowledge Sub-Agent (Notion read/write)."""

    action: Literal["read", "create", "append", "search_databases", "search_notion", "search"] = Field(
        ..., description="Notion operation performed"
    )
    page_id: str = Field(default="", description="Notion page ID acted upon")
    page_title: str = Field(default="", description="Title of the Notion page")
    status: Literal["success", "failed"] = "success"
    content_preview: str = Field(
        default="",
        description="First 300 chars of the page content"
    )
    page_url: Optional[str] = Field(default=None, description="Public Notion page URL")


# ── Communication Sub-Agent ───────────────────────────────────────────────────

class CommsResult(BaseModel):
    """Output schema for the Communication Sub-Agent (Gmail)."""

    action: Literal["read", "draft", "send"] = Field(
        ..., description="Gmail operation performed"
    )
    status: Literal["sent", "drafted", "read", "failed"] = "drafted"
    recipient: str = Field(default="", description="Email recipient address")
    subject: str = Field(default="", description="Email subject line")
    message_id: str = Field(default="", description="Gmail message ID")
    thread_id: str = Field(default="", description="Gmail thread ID")
    preview: str = Field(default="", description="First 200 chars of email body")


# ── Registry ──────────────────────────────────────────────────────────────────

OUTPUT_SCHEMAS: dict[str, type[BaseModel]] = {
    "research_agent": ResearchResult,
    "code_agent":     CodeResult,
    "knowledge_agent": KnowledgeResult,
    "comms_agent":    CommsResult,
}


def validate_agent_output(agent_name: str, raw_output: dict) -> BaseModel:
    """
    Validate a sub-agent's raw dict output against its registered schema.
    Raises ValidationError if the output does not conform — the calling
    orchestrator must handle or retry.
    """
    schema = OUTPUT_SCHEMAS.get(agent_name)
    if schema is None:
        raise KeyError(f"No output schema registered for agent: '{agent_name}'")
    return schema.model_validate(raw_output)
