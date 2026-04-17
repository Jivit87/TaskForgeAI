from __future__ import annotations
"""
tools/native_tools.py
Pure Python function tools registered in the global TOOL_REGISTRY.

These tools are called by both the Master Orchestrator and sub-agents.
They don't require MCP — they run in-process.

Registered tools:
  - extract_structured_data   → LLM-assisted structured extraction
  - validate_and_checkpoint   → Pydantic validation + SQLite checkpoint
  - summarize_content         → Fit long text into sub-agent context windows
  - calculate_confidence      → Score an output based on field completeness
"""

import json
import logging
from typing import TYPE_CHECKING

from tools.registry import tool, dispatch_tool  # noqa: F401

if TYPE_CHECKING:
    from core.checkpoint import CheckpointStore
    from schemas.agent_state import AgentState

log = logging.getLogger("frame_mo.native_tools")


# ── Lazy Groq client (avoids circular imports at module load) ─────────────────

_groq_client = None


def _get_groq_client():
    global _groq_client
    if _groq_client is None:
        import os
        from groq import Groq
        _groq_client = Groq(api_key=os.environ["GROQ_API_KEY"])
    return _groq_client


# ── Tool 1: extract_structured_data ──────────────────────────────────────────

@tool(
    name="extract_structured_data",
    description=(
        "Extract structured data from unstructured text using a named schema. "
        "Returns a JSON object matching that schema."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "text":        {"type": "string", "description": "Raw text to extract from"},
            "schema_name": {"type": "string", "description": "Name of the target schema"},
        },
        "required": ["text", "schema_name"],
    },
)
def extract_structured_data(text: str, schema_name: str) -> dict:
    """
    Use Groq with JSON mode to extract structured data matching a named schema.
    The schema_name must exist in schemas.agent_outputs.OUTPUT_SCHEMAS.
    """
    from schemas.agent_outputs import OUTPUT_SCHEMAS

    schema_cls = OUTPUT_SCHEMAS.get(schema_name)
    if schema_cls is None:
        raise ValueError(f"Unknown schema: '{schema_name}'. "
                         f"Available: {list(OUTPUT_SCHEMAS.keys())}")

    schema_json = json.dumps(schema_cls.model_json_schema(), indent=2)
    prompt = (
        f"Extract data from the text below and return a JSON object "
        f"that strictly matches this schema:\n\n{schema_json}\n\n"
        f"Text:\n{text}"
    )

    client = _get_groq_client()
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=1024,
    )

    raw = response.choices[0].message.content
    log.debug(f"[extract_structured_data] schema={schema_name}  "
              f"raw_len={len(raw)}")
    return json.loads(raw)


# ── Tool 2: validate_and_checkpoint ──────────────────────────────────────────

@tool(
    name="validate_and_checkpoint",
    description=(
        "Validate a sub-agent output dict against its Pydantic schema, "
        "then write a SQLite checkpoint if valid. "
        "Returns status 'valid' or 'invalid' with error details."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "output":      {"type": "object", "description": "Raw agent output dict"},
            "agent_name":  {"type": "string", "description": "Agent name (maps to schema)"},
            "step_name":   {"type": "string", "description": "Checkpoint step label"},
        },
        "required": ["output", "agent_name", "step_name"],
    },
)
def validate_and_checkpoint(
    output: dict,
    agent_name: str,
    step_name: str,
    state: "AgentState | None" = None,
    checkpoint_store: "CheckpointStore | None" = None,
) -> dict:
    """
    Validate output against its registered schema and write a checkpoint.
    The checkpoint is only written if validation passes — corrupted output
    never reaches the checkpoint layer.
    """
    from schemas.agent_outputs import validate_agent_output

    try:
        validated = validate_agent_output(agent_name, output)

        if state is not None:
            state.agent_results[step_name] = validated.model_dump()
            if step_name not in state.completed_agents:
                state.completed_agents.append(step_name)

        if checkpoint_store is not None and state is not None:
            checkpoint_store.save(state)
            log.info(f"[validate_and_checkpoint] ✅ {step_name} checkpointed")

        return {
            "status": "valid",
            "checkpointed": checkpoint_store is not None,
            "validated_data": validated.model_dump(),
        }

    except Exception as exc:
        log.warning(f"[validate_and_checkpoint] ❌ {step_name} failed: {exc}")
        return {
            "status": "invalid",
            "error": str(exc),
            "checkpointed": False,
        }


# ── Tool 3: summarize_content ─────────────────────────────────────────────────

@tool(
    name="summarize_content",
    description=(
        "Summarize long text to fit within a sub-agent context window. "
        "Returns the original text if it's already under the word limit."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "content":    {"type": "string", "description": "Text to summarize"},
            "max_words":  {"type": "integer", "description": "Target word count (default 500)"},
        },
        "required": ["content"],
    },
)
def summarize_content(content: str, max_words: int = 500) -> str:
    """
    If content is under the word limit, return as-is.
    Otherwise call Groq to produce a concise summary.
    """
    word_count = len(content.split())
    if word_count <= max_words:
        log.debug(f"[summarize_content] Under limit ({word_count} words) — returning as-is")
        return content

    log.debug(f"[summarize_content] Summarizing {word_count} words → {max_words}")
    client = _get_groq_client()
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{
            "role": "user",
            "content": (
                f"Summarize the following text in under {max_words} words. "
                f"Preserve all key facts, numbers, and conclusions.\n\n{content}"
            ),
        }],
        temperature=0,
        max_tokens=max_words * 2,
    )
    summary = response.choices[0].message.content
    log.debug(f"[summarize_content] Output: {len(summary.split())} words")
    return summary


# ── Tool 4: calculate_confidence ─────────────────────────────────────────────

@tool(
    name="calculate_confidence",
    description=(
        "Calculate a confidence score (0.0–1.0) for an agent output dict "
        "based on how many expected fields are present and non-empty."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "output":          {"type": "object", "description": "Agent output dict to score"},
            "expected_fields": {"type": "array",  "description": "List of field names to check"},
        },
        "required": ["output", "expected_fields"],
    },
)
def calculate_confidence(output: dict, expected_fields: list[str]) -> float:
    """
    Simple field-presence confidence scorer.
    A field counts as present if it exists AND is truthy (non-empty, non-None).
    """
    if not expected_fields:
        return 1.0
    present = sum(
        1 for f in expected_fields
        if output.get(f) not in (None, "", [], {})
    )
    score = round(present / len(expected_fields), 2)
    log.debug(
        f"[calculate_confidence] {present}/{len(expected_fields)} fields present → {score}"
    )
    return score
