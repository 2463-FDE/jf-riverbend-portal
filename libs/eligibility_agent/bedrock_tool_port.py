"""A tool-capable Bedrock Converse port — separate from libs/llm_client's
completion-only Provider interface. Provider.complete() takes a prompt and
returns text; it has no notion of tools, and every existing caller (e.g.
libs/rag_eval, anything using LLMClient) relies on that being simple and
stable. Bolting tool-calling onto it would distort a general-purpose
completion client to serve one agent-specific use case. This port exists
instead, purpose-built for a tool-calling loop, and libs/llm_client is left
untouched.

boto3 is imported lazily (inside converse()), exactly like
libs/llm_client/providers/bedrock_provider.py, so nothing that merely imports
this module — or this whole package — requires it installed. Error
translation reuses libs.llm_client.errors' existing ProviderTimeoutError/
ProviderTransientError/ProviderNotConfiguredError vocabulary rather than
inventing a parallel one, since it's the same retryable-vs-not distinction
for the same underlying AWS SDK, just behind a different method shape.
"""
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional

from libs.llm_client.errors import (
    ProviderCallError,
    ProviderNotConfiguredError,
    ProviderTimeoutError,
    ProviderTransientError,
)

_RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "ModelTimeoutException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
    "InternalServerException",
}


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class ConverseTurn:
    text: Optional[str]
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop_reason: str = ""


class ToolCapableModel(ABC):
    """A single-turn, tool-capable model call — the seam that makes the
    raw_bedrock AgentRuntime swappable/testable, mirroring how
    libs/llm_client/providers/base.py's Provider does for LLMClient."""

    @abstractmethod
    def converse(self, messages: list, tools: list, *, timeout: float) -> ConverseTurn:
        raise NotImplementedError


class BedrockConverseToolModel(ToolCapableModel):
    def __init__(self, model_id: str = None, region: str = None):
        self._model_id = model_id or os.getenv("BEDROCK_MODEL_ID")
        self._region = region or os.getenv("AWS_REGION")
        if not self._model_id or self._model_id == "changeme":
            raise ProviderNotConfiguredError("BEDROCK_MODEL_ID is not configured")
        if not self._region:
            raise ProviderNotConfiguredError("AWS_REGION is not configured")

    def converse(self, messages: list, tools: list, *, timeout: float) -> ConverseTurn:
        import boto3  # lazy import — see module docstring
        from botocore.config import Config
        from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

        client = boto3.client(
            "bedrock-runtime",
            region_name=self._region,
            config=Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 0}),
        )
        tool_config = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t["name"],
                        "description": t["description"],
                        "inputSchema": {"json": t["input_schema"]},
                    }
                }
                for t in tools
            ]
        }
        try:
            response = client.converse(modelId=self._model_id, messages=messages, toolConfig=tool_config)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise ProviderTimeoutError(type(exc).__name__) from exc
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in _RETRYABLE_ERROR_CODES:
                raise ProviderTransientError(type(exc).__name__) from exc
            # Non-retryable (AccessDenied, ValidationException, ...). Unlike the
            # completion-only bedrock_provider (which re-raises so a developer
            # sees the misconfiguration), this agent-facing port normalizes it
            # to ProviderCallError so the runtime can degrade gracefully to a
            # safe reply rather than letting a raw SDK exception escape
            # handle_message. Only the error TYPE crosses the boundary.
            raise ProviderCallError(error_code or type(exc).__name__) from exc

        try:
            content = response["output"]["message"]["content"]
            text_parts = [block["text"] for block in content if "text" in block]
            tool_calls = [
                ToolCall(
                    id=block["toolUse"]["toolUseId"],
                    name=block["toolUse"]["name"],
                    arguments=block["toolUse"].get("input") or {},
                )
                for block in content
                if "toolUse" in block
            ]
        except (KeyError, TypeError, IndexError) as exc:
            # An unexpected Converse response shape (a model/SDK change) must
            # not throw a raw KeyError out of the agent — surface it as a
            # controlled provider failure, TYPE only.
            raise ProviderCallError(type(exc).__name__) from exc

        return ConverseTurn(
            text="".join(text_parts) if text_parts else None,
            tool_calls=tool_calls,
            stop_reason=response.get("stopReason", ""),
        )
