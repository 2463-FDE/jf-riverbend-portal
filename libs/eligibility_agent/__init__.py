"""Provider-neutral, framework-swappable eligibility agent.

Selected by ELIGIBILITY_AGENT_RUNTIME (raw_bedrock = default, no framework;
langchain = comparison spike). Neither runtime is wired into a running
service yet — see Stage 3 for the visit-chat endpoint and the Docker/libs-
gap fix that comes with actually importing this package from a service.
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
