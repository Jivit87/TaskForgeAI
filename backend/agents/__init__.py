from __future__ import annotations
"""agents — Master Orchestrator + 4 Groq-powered Sub-Agents."""

from .base_agent import BaseAgent
from .research_agent import ResearchAgent
from .code_agent import CodeAgent
from .knowledge_agent import KnowledgeAgent
from .comms_agent import CommsAgent

# Agent registry — maps agent_name → class
AGENT_REGISTRY: dict[str, type[BaseAgent]] = {
    "research_agent":  ResearchAgent,
    "code_agent":      CodeAgent,
    "knowledge_agent": KnowledgeAgent,
    "comms_agent":     CommsAgent,
}

__all__ = [
    "BaseAgent",
    "ResearchAgent",
    "CodeAgent",
    "KnowledgeAgent",
    "CommsAgent",
    "AGENT_REGISTRY",
]
