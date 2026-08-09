"""Provider-neutral, framework-swappable eligibility agent.

Selected by ELIGIBILITY_AGENT_RUNTIME (raw_bedrock = default, no framework;
langchain = comparison spike; ollama = Stage 2 feature-readiness local demo —
same raw_bedrock loop, an OllamaToolCapableModel in place of
BedrockConverseToolModel). Wired into services/eligibility-service's
POST /visits/{visit_id}/messages endpoint — see that service's
agent_wiring.py.
"""
from .contracts import (
    CheckEligibilityArgs,
    EligibilityStatus,
    TerminationReason,
    ToolInvocationResult,
    VisitContext,
    VisitTurnResult,
)
from .memory import RedisVisitMemory, VisitMemoryPort
from .runtime import AgentRuntime, build_agent_runtime

__all__ = [
    "AgentRuntime",
    "build_agent_runtime",
    "VisitMemoryPort",
    "RedisVisitMemory",
    "EligibilityStatus",
    "VisitContext",
    "VisitTurnResult",
    "TerminationReason",
    "CheckEligibilityArgs",
    "ToolInvocationResult",
]
