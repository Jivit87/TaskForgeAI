from __future__ import annotations
"""
tools/registry.py
Global tool registry with @tool decorator.

The registry holds every function tool exposed to agents.
Both the Master Orchestrator (Anthropic SDK) and sub-agents (Groq)
use the registry to resolve tool names → handlers and build
Anthropic-compatible tool schemas from Python type annotations.
"""

import inspect
import logging
from typing import Callable, Any, get_type_hints

log = logging.getLogger("frame_mo.registry")

# ── Global registry ──────────────────────────────────────────────────────────

TOOL_REGISTRY: dict[str, "ToolDefinition"] = {}


class ToolDefinition:
    """Holds the metadata and handler for a registered tool."""

    def __init__(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable,
    ):
        self.name = name
        self.description = description
        self.input_schema = input_schema
        self.handler = handler

    def to_anthropic_schema(self) -> dict:
        """Return Anthropic SDK-compatible tool definition dict."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_groq_schema(self) -> dict:
        """Return Groq/OpenAI-compatible tool definition dict."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def __call__(self, **kwargs) -> Any:
        return self.handler(**kwargs)


# ── Type → JSON Schema mapping ────────────────────────────────────────────────

_PY_TO_JSON: dict[type, str] = {
    str:   "string",
    int:   "integer",
    float: "number",
    bool:  "boolean",
    list:  "array",
    dict:  "object",
}


def _python_type_to_json(annotation) -> str:
    return _PY_TO_JSON.get(annotation, "string")


def _build_schema_from_annotations(func: Callable) -> dict:
    """
    Auto-generate a JSON Schema dict from a function's type annotations.
    Only top-level (non-nested) parameter types are supported here.
    For complex schemas, pass `input_schema` explicitly to @tool.
    """
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}

    sig = inspect.signature(func)
    properties: dict[str, dict] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        if param_name in ("self", "state"):
            continue  # skip implicit params

        annotation = hints.get(param_name, str)
        json_type = _python_type_to_json(annotation)

        properties[param_name] = {"type": json_type}

        # Required if no default value
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


# ── @tool decorator ───────────────────────────────────────────────────────────

def tool(
    name: str,
    description: str,
    input_schema: dict | None = None,
) -> Callable:
    """
    Decorator to register a Python function as an agent tool.

    Args:
        name:         Tool name used in tool calls (must be unique).
        description:  Description shown to the LLM.
        input_schema: Optional explicit JSON Schema. If None, auto-generated
                      from type annotations.

    Example:
        @tool("search_web", "Search the web for a given query")
        def search_web(query: str, max_results: int = 5) -> dict:
            ...
    """
    def decorator(func: Callable) -> Callable:
        schema = input_schema or _build_schema_from_annotations(func)
        td = ToolDefinition(
            name=name,
            description=description,
            input_schema=schema,
            handler=func,
        )
        TOOL_REGISTRY[name] = td
        log.debug(f"Tool registered → {name}")
        return func

    return decorator


# ── Dispatch ──────────────────────────────────────────────────────────────────

def dispatch_tool(name: str, args: dict) -> Any:
    """
    Look up a tool by name and call it with the given args dict.
    Raises KeyError if the tool is not found (hallucination guard).
    """
    if name not in TOOL_REGISTRY:
        raise KeyError(
            f"Tool '{name}' is not registered. "
            f"Available: {list(TOOL_REGISTRY.keys())}"
        )
    log.info(f"[dispatch] Calling tool={name} args={list(args.keys())}")
    return TOOL_REGISTRY[name].handler(**args)


# ── Schema export helpers ─────────────────────────────────────────────────────

def get_anthropic_schemas(tool_names: list[str] | None = None) -> list[dict]:
    """Return Anthropic-format schemas for all (or specified) tools."""
    registry = TOOL_REGISTRY
    if tool_names:
        registry = {k: v for k, v in registry.items() if k in tool_names}
    return [td.to_anthropic_schema() for td in registry.values()]


def get_groq_schemas(tool_names: list[str] | None = None) -> list[dict]:
    """Return Groq/OpenAI-format schemas for all (or specified) tools."""
    registry = TOOL_REGISTRY
    if tool_names:
        registry = {k: v for k, v in registry.items() if k in tool_names}
    return [td.to_groq_schema() for td in registry.values()]
