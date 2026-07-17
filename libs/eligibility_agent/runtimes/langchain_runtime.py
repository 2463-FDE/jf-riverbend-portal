"""LangChain/LangGraph comparison-spike AgentRuntime.

A minimal 2-node LangGraph (agent <-> tools, bounded, then END) wired to the
SAME check_eligibility tool and the SAME safety properties (allowlist, strict
argument validation, bounded turns, safe errors, deterministic termination)
as the raw_bedrock runtime — this exists to compare LangGraph's own
orchestration primitives against a hand-rolled loop, not to add a second,
differently-behaved agent. tests/test_eligibility_agent_runtimes.py runs the
identical test functions against both runtimes for exactly this reason.

All langchain_core/langgraph/langchain_aws imports are lazy (inside methods,
never at module scope), so importing this module — or this whole package —
never requires LangChain installed. Real dependencies live in
libs/eligibility_agent/requirements-langchain.txt, never requirements-dev.txt
(installing it is what CI's "tests" job never does) and never a service's own
requirements.txt (nothing is wired into a running service in Stage 2 —
Stage 3 does that and is responsible for the Docker/libs-gap fix that comes
with it).

Checkpointer: LangGraph requires one to compile and run a graph at all. Per
the approved plan, it must be Redis/Postgres-backed in production, never
InMemorySaver — see _default_checkpointer(). IMPORTANT: this does not mean
conversation history persists across handle_message calls via that
checkpointer. It does not. Cross-turn continuity comes ONLY from
VisitMemoryPort's structured fields, exactly like raw_bedrock, because "do
not persist raw chat, prompts, or model responses" overrides LangGraph's
usual persist-the-whole-thread convenience. The checkpointer here is scoped
to a single, disposable thread_id per handle_message call (a fresh uuid4,
never the visit_id) used only for LangGraph's own within-call step
bookkeeping, and nothing reads it again after the call returns.

Caveat (disclosed, not hidden): this has never been run against a real
langgraph/langchain_aws install — that's by design (see the hard rule against
adding LangChain to requirements-dev.txt). Tests fake langchain_core/langgraph
via sys.modules, mirroring tests/test_bedrock_provider.py's established fake-
boto3 pattern for the same reason. They validate this module's OWN
control-flow logic against a self-authored fake of LangGraph's documented
StateGraph/conditional-edge API shape, not compatibility with the real
library.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from libs.safe_logging import get_safe_logger

from ..contracts import EligibilityStatus, TerminationReason, VisitContext, VisitTurnResult, parse_as_of
from ..eligibility_tool import TOOL_NAME, TOOL_SPEC, CheckEligibilityTool, EligibilityToolConfig
from ..memory import VisitMemoryPort

log = get_safe_logger(__name__)

_ALLOWED_TOOLS = frozenset({TOOL_NAME})

_SAFE_PROVIDER_ERROR_REPLY = (
    "I couldn't reach the eligibility assistant just now. Please try again in a "
    "moment, or check eligibility manually."
)
_SAFE_MAX_TURNS_REPLY = (
    "I wasn't able to finish checking this in the time I'm allowed. Please try "
    "again, or check eligibility manually."
)


class LangChainAgentRuntime:
    def __init__(
        self,
        *,
        memory: VisitMemoryPort,
        max_turns: int = 4,
        timeout_seconds: float = 20.0,
        tool_config: Optional[EligibilityToolConfig] = None,
        tool_transport=None,
        chat_model_factory=None,
        checkpointer_factory=None,
        now=lambda: datetime.now(timezone.utc),
    ):
        self._memory = memory
        self._max_turns = max_turns
        self._timeout_seconds = timeout_seconds
        self._tool_config = tool_config
        self._tool_transport = tool_transport
        self._chat_model_factory = chat_model_factory or self._default_chat_model
        self._checkpointer_factory = checkpointer_factory or self._default_checkpointer
        self._now = now

    @staticmethod
    def _default_chat_model():
        import os

        from langchain_aws import ChatBedrockConverse  # lazy — see module docstring

        model_id = os.getenv("BEDROCK_MODEL_ID")
        region = os.getenv("AWS_REGION")
        if not model_id or model_id == "changeme":
            from libs.llm_client.errors import ProviderNotConfiguredError

            raise ProviderNotConfiguredError("BEDROCK_MODEL_ID is not configured")
        return ChatBedrockConverse(model=model_id, region_name=region)

    @staticmethod
    def _default_checkpointer():
        import os

        from langgraph.checkpoint.redis import RedisSaver  # lazy; Redis/Postgres only, never InMemorySaver

        return RedisSaver.from_conn_string(os.getenv("REDIS_URL", "redis://redis:6379/0"))

    def handle_message(self, visit_id: str, user_message: str) -> VisitTurnResult:
        from langchain_core.messages import HumanMessage, ToolMessage
        from langgraph.graph import END, StateGraph

        context = self._memory.get(visit_id) or VisitContext(visit_id=visit_id, updated_at=self._now())
        tool = CheckEligibilityTool(context, config=self._tool_config, transport=self._tool_transport)
        chat_model = self._chat_model_factory()
        bound_model = chat_model.bind_tools([TOOL_SPEC])

        outcome = {"tool_called": False, "eligibility_status": context.eligibility_status, "context": context}
        max_turns = self._max_turns

        def agent_node(state):
            state["turns"] = state.get("turns", 0) + 1
            try:
                response = bound_model.invoke(state["messages"])
            except Exception as exc:
                # Unlike raw_bedrock (whose ToolCapableModel port normalizes
                # failures to the LLMClientError base we could catch precisely),
                # the real langchain_aws model raises library-specific
                # exceptions we cannot enumerate without installing the dep.
                # The contract ("never raise for a provider failure") wins:
                # catch broadly around ONLY this single external call and
                # degrade to a safe PROVIDER_ERROR turn, logging TYPE only.
                log.warning(
                    "agent provider call failed (turn=%s, error_type=%s)", state["turns"], type(exc).__name__
                )
                state["provider_error"] = type(exc).__name__
                return state
            state["messages"] = state["messages"] + [response]
            return state

        def tools_node(state):
            last = state["messages"][-1]
            results = []
            for call in getattr(last, "tool_calls", None) or []:
                if call["name"] not in _ALLOWED_TOOLS:
                    log.warning("agent tool call rejected (reason=unknown_tool)")
                    payload = {"error": "unknown_tool"}
                else:
                    result = tool.invoke(call.get("args") or {})
                    payload = result.payload
                    if result.ok:
                        outcome["tool_called"] = True
                        if "status" in payload:
                            status = EligibilityStatus(payload["status"])
                            outcome["eligibility_status"] = status
                            # Persist the payer's real verification time (as_of),
                            # NOT now() — parity with raw_bedrock; a stale
                            # fallback must not be recorded as freshly checked.
                            checked_at = (
                                parse_as_of(payload.get("as_of"))
                                or outcome["context"].eligibility_checked_at
                            )
                            outcome["context"] = outcome["context"].model_copy(
                                update={
                                    "eligibility_status": status,
                                    "eligibility_checked_at": checked_at,
                                    "updated_at": self._now(),
                                }
                            )
                            self._memory.put(outcome["context"])
                    else:
                        log.warning("agent tool call rejected (reason=%s)", payload.get("error", "invalid"))
                results.append(ToolMessage(content=str(payload), tool_call_id=call["id"]))
            state["messages"] = state["messages"] + results
            return state

        def route_after_agent(state):
            if state.get("provider_error"):
                return "end"
            last = state["messages"][-1]
            if not getattr(last, "tool_calls", None):
                return "end"
            return "tools"  # always execute a requested tool call, even on the final permitted turn

        def route_after_tools(state):
            if state.get("turns", 0) >= max_turns:
                return "end"
            return "agent"

        graph = StateGraph(dict)
        graph.add_node("agent", agent_node)
        graph.add_node("tools", tools_node)
        graph.set_entry_point("agent")
        graph.add_conditional_edges("agent", route_after_agent, {"tools": "tools", "end": END})
        graph.add_conditional_edges("tools", route_after_tools, {"agent": "agent", "end": END})
        compiled = graph.compile(checkpointer=self._checkpointer_factory())

        thread_id = f"scratch:{uuid.uuid4().hex}"  # disposable — never the visit_id, never reused
        final_state = compiled.invoke(
            {"messages": [HumanMessage(content=user_message)], "turns": 0},
            config={"configurable": {"thread_id": thread_id}},
        )

        turns_used = final_state.get("turns", 0)
        if final_state.get("provider_error"):
            return VisitTurnResult(
                visit_id=visit_id,
                reply=_SAFE_PROVIDER_ERROR_REPLY,
                tool_called=outcome["tool_called"],
                eligibility_status=outcome["eligibility_status"],
                termination_reason=TerminationReason.PROVIDER_ERROR,
                turns_used=turns_used,
            )

        last = final_state["messages"][-1]
        if isinstance(last, ToolMessage):
            # route_after_tools cut us off right after executing a tool call —
            # the model never got a chance to respond to that result.
            return VisitTurnResult(
                visit_id=visit_id,
                reply=_SAFE_MAX_TURNS_REPLY,
                tool_called=outcome["tool_called"],
                eligibility_status=outcome["eligibility_status"],
                termination_reason=TerminationReason.MAX_TURNS,
                turns_used=turns_used,
            )

        return VisitTurnResult(
            visit_id=visit_id,
            reply=getattr(last, "content", "") or "",
            tool_called=outcome["tool_called"],
            eligibility_status=outcome["eligibility_status"],
            termination_reason=TerminationReason.ANSWERED,
            turns_used=turns_used,
        )
