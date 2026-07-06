"""Amazon Bedrock adapter. Model ID/region come from environment/config only.

Credentials are never read directly by this adapter — boto3's standard
credential chain resolves them from whichever of AWS_ACCESS_KEY_ID /
AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN / AWS_BEARER_TOKEN_BEDROCK / a
configured AWS profile is present in the environment.

The `boto3` package is imported lazily (inside complete()), so nothing that
merely imports libs.llm_client requires it to be installed — a real network
call only happens if this provider is actually selected and used.

Uses the Bedrock Converse API rather than InvokeModel: Converse gives a single
request/response shape across model families (Claude, Llama, Titan, ...), so
this adapter doesn't need model-specific body parsing. Boto3's built-in retry
is disabled (max_attempts=0) — LLMClient already owns retry/backoff, and
letting both layers retry independently would compound delays unpredictably.
"""
import os

from ..errors import ProviderNotConfiguredError, ProviderTimeoutError, ProviderTransientError
from .base import Provider, ProviderResponse

_RETRYABLE_ERROR_CODES = {"ThrottlingException", "ModelTimeoutException", "ServiceUnavailableException"}


class BedrockProvider(Provider):
    def __init__(self, model_id: str = None, region: str = None):
        self._model_id = model_id or os.getenv("BEDROCK_MODEL_ID")
        self._region = region or os.getenv("AWS_REGION")
        if not self._model_id or self._model_id == "changeme":
            raise ProviderNotConfiguredError("BEDROCK_MODEL_ID is not configured")
        if not self._region:
            raise ProviderNotConfiguredError("AWS_REGION is not configured")

    def complete(self, prompt: str, *, timeout: float, max_tokens: int) -> ProviderResponse:
        import boto3  # lazy import — see module docstring
        from botocore.config import Config
        from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

        client = boto3.client(
            "bedrock-runtime",
            region_name=self._region,
            config=Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 0}),
        )
        try:
            response = client.converse(
                modelId=self._model_id,
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens},
            )
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise ProviderTimeoutError(str(exc)) from exc
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in _RETRYABLE_ERROR_CODES:
                raise ProviderTransientError(str(exc)) from exc
            raise

        text = "".join(
            block["text"] for block in response["output"]["message"]["content"] if "text" in block
        )
        usage = response.get("usage", {})
        return ProviderResponse(
            text=text,
            input_tokens=usage.get("inputTokens", 0),
            output_tokens=usage.get("outputTokens", 0),
        )
