"""Provider-neutral, framework-neutral AgentRuntime contract + fail-closed
factory.

Both concrete runtimes (raw_bedrock, langchain) implement AgentRuntime
identically — same method, same VisitTurnResult shape, same safety
invariants (bounded turns, tool allowlist, no raw-chat persistence). That is
the whole point: swappability is a design goal here, not an implementation
detail, and tests/test_eligibility_agent_runtimes.py runs the SAME test
functions against both so a passing suite is evidence the contract actually
holds for each, not just that each runtime does something reasonable on its
own terms.

Runtime construction (`from .runtimes... import ...`) is deferred inside
build_agent_runtime() rather than imported at module scope, so importing
THIS module (or this whole package) never requires boto3 or LangChain
installed — only whichever runtime is actually selected pulls in its SDK,
and only when actually built.
"""
import os
from abc import ABC, abstractmethod

from .contracts import VisitTurnResult

_KNOWN_RUNTIMES = ("raw_bedrock", "langchain")


class AgentRuntime(ABC):
    @abstractmethod
    def handle_message(self, visit_id: str, user_message: str) -> VisitTurnResult:
        """Handle one inbound chat message for the given visit.

        Loads/creates visit-scoped structured memory (never raw chat) via the
        injected VisitMemoryPort, runs a bounded tool-calling loop against the
        provider, persists only structured eligibility fields back to memory,
        and returns exactly one VisitTurnResult. Must never raise for a
        provider or tool failure — those become a safe reply plus a
        PROVIDER_ERROR/MAX_TURNS termination_reason instead.
        """
        raise NotImplementedError


def build_agent_runtime(name: str = None, **kwargs) -> AgentRuntime:
    """Fail closed: an unset/unrecognized ELIGIBILITY_AGENT_RUNTIME raises
    rather than silently falling back to any default, mirroring
    libs/llm_client/client.py::_build_provider's existing unknown-provider
    behavior. `raw_bedrock` must be requested explicitly (by name or via the
    env var) — it is the configured default, not an implicit fallback for an
    unrecognized value.
    """
    name = name or os.getenv("ELIGIBILITY_AGENT_RUNTIME", "raw_bedrock")

    if name == "raw_bedrock":
        from .runtimes.raw_bedrock import RawBedrockAgentRuntime

        return RawBedrockAgentRuntime(**kwargs)

    if name == "langchain":
        from .runtimes.langchain_runtime import LangChainAgentRuntime

        return LangChainAgentRuntime(**kwargs)

    raise ValueError(
        f"Unknown ELIGIBILITY_AGENT_RUNTIME '{name}' — expected one of: {', '.join(_KNOWN_RUNTIMES)}"
    )
