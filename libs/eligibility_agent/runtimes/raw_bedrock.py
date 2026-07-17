"""Default AgentRuntime: an explicit Bedrock Converse tool-calling loop.

No framework — a hand-written loop over ToolCapableModel.converse(), per the
approved plan's "no framework" requirement for the default runtime. Turns are
bounded by a plain `for` loop over a fixed range (not a manually-incremented
counter that could be gotten wrong), so termination is structurally
guaranteed, not just intended. Every tool call is dispatched through an
explicit allowlist and strict Pydantic argument validation before the tool
ever runs; a provider failure or an exhausted turn budget always produces a
safe, generic reply rather than raising or leaking any diagnostic detail to
the user.
"""
from datetime import datetime, timezone
from typing import Optional

from libs.llm_client.errors import ProviderTimeoutError, ProviderTransientError
from libs.safe_logging import get_safe_logger

from ..bedrock_tool_port import BedrockConverseToolModel, ConverseTurn, ToolCapableModel
from ..contracts import EligibilityStatus, TerminationReason, VisitContext, VisitTurnResult
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


class RawBedrockAgentRuntime:
    def __init__(
        self,
        *,
        memory: VisitMemoryPort,
        model: Optional[ToolCapableModel] = None,
        max_turns: int = 4,
        timeout_seconds: float = 20.0,
        tool_config: Optional[EligibilityToolConfig] = None,
        tool_transport=None,
        now=lambda: datetime.now(timezone.utc),
    ):
        self._memory = memory
        self._model = model if model is not None else BedrockConverseToolModel()
        self._max_turns = max_turns
        self._timeout_seconds = timeout_seconds
        self._tool_config = tool_config
        self._tool_transport = tool_transport
        self._now = now

    def handle_message(self, visit_id: str, user_message: str) -> VisitTurnResult:
        context = self._memory.get(visit_id) or VisitContext(visit_id=visit_id, updated_at=self._now())
        tool = CheckEligibilityTool(context, config=self._tool_config, transport=self._tool_transport)

        messages: list = [{"role": "user", "content": [{"text": user_message}]}]
        tool_called = False
        eligibility_status: Optional[EligibilityStatus] = context.eligibility_status

        for turn in range(1, self._max_turns + 1):
            try:
                response = self._model.converse(messages, [TOOL_SPEC], timeout=self._timeout_seconds)
            except (ProviderTimeoutError, ProviderTransientError) as exc:
                log.warning("agent provider call failed (turn=%s, error_type=%s)", turn, type(exc).__name__)
                return VisitTurnResult(
                    visit_id=visit_id,
                    reply=_SAFE_PROVIDER_ERROR_REPLY,
                    tool_called=tool_called,
                    eligibility_status=eligibility_status,
                    termination_reason=TerminationReason.PROVIDER_ERROR,
                    turns_used=turn,
                )

            if not response.tool_calls:
                return VisitTurnResult(
                    visit_id=visit_id,
                    reply=response.text or "",
                    tool_called=tool_called,
                    eligibility_status=eligibility_status,
                    termination_reason=TerminationReason.ANSWERED,
                    turns_used=turn,
                )

            messages.append({"role": "assistant", "content": _assistant_content(response)})
            tool_result_blocks = []
            for call in response.tool_calls:
                if call.name not in _ALLOWED_TOOLS:
                    log.warning("agent tool call rejected (reason=unknown_tool)")
                    payload = {"error": "unknown_tool"}
                else:
                    result = tool.invoke(call.arguments)
                    payload = result.payload
                    if result.ok:
                        tool_called = True
                        if "status" in payload:
                            eligibility_status = EligibilityStatus(payload["status"])
                            context = context.model_copy(
                                update={
                                    "eligibility_status": eligibility_status,
                                    "eligibility_checked_at": self._now(),
                                    "updated_at": self._now(),
                                }
                            )
                            self._memory.put(context)
                    else:
                        log.warning("agent tool call rejected (reason=%s)", payload.get("error", "invalid"))
                tool_result_blocks.append(
                    {"toolResult": {"toolUseId": call.id, "content": [{"json": payload}]}}
                )
            messages.append({"role": "user", "content": tool_result_blocks})

        return VisitTurnResult(
            visit_id=visit_id,
            reply=_SAFE_MAX_TURNS_REPLY,
            tool_called=tool_called,
            eligibility_status=eligibility_status,
            termination_reason=TerminationReason.MAX_TURNS,
            turns_used=self._max_turns,
        )


def _assistant_content(response: ConverseTurn) -> list:
    blocks = []
    if response.text:
        blocks.append({"text": response.text})
    for call in response.tool_calls:
        blocks.append({"toolUse": {"toolUseId": call.id, "name": call.name, "input": call.arguments}})
    return blocks
