"""Bedrock embedding provider for the policy corpus (w-9-2-planner P2,
embeddings/retrieval slice).

Deliberately separate from libs/embedding_client's env-var-based selection
(EMBEDDING_PROVIDER=fake|ollama) — that client's _build_provider factory
only ever builds fake/ollama, committing to "no PHI leaves the environment"
for the patient-encounter corpus (see its ollama_provider.py). This adapter
is never registered there; it's wired in only via explicit constructor
injection into EmbeddingClient(provider=...), so flipping EMBEDDING_PROVIDER
can never send patient-encounter text to Bedrock. The policy corpus is
synthetic, non-PHI text (manifest.json's own `patient_data_in_corpus:
false`), so a real cloud embedding call doesn't carry that risk.

Reads its own POLICY_EMBEDDING_MODEL_ID, never BEDROCK_MODEL_ID (a chat
model id, not an embedding one — vector-rag.md forbids inferring one from
the other). Uses InvokeModel (not Converse — embedding models don't speak
Converse) with Amazon Titan Embed's request/response shape.
"""
import json
import os
from typing import List

from libs.embedding_client.errors import ProviderNotConfiguredError, ProviderTimeoutError, ProviderTransientError
from libs.embedding_client.providers.base import EmbeddingProvider, EmbeddingResponse

_RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "ModelTimeoutException",
    "ServiceUnavailableException",
    "ModelNotReadyException",
    "InternalServerException",
}


class BedrockPolicyEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_id: str = None, region: str = None):
        self._model_id = model_id or os.getenv("POLICY_EMBEDDING_MODEL_ID")
        self._region = region or os.getenv("AWS_REGION")
        if not self._model_id or self._model_id == "changeme":
            raise ProviderNotConfiguredError("POLICY_EMBEDDING_MODEL_ID is not configured")
        if not self._region:
            raise ProviderNotConfiguredError("AWS_REGION is not configured")

    def embed(self, texts: List[str], *, timeout: float) -> EmbeddingResponse:
        import boto3  # lazy import — mirrors bedrock_provider.py/bedrock_tool_port.py
        from botocore.config import Config
        from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

        client = boto3.client(
            "bedrock-runtime",
            region_name=self._region,
            config=Config(connect_timeout=timeout, read_timeout=timeout, retries={"max_attempts": 0}),
        )

        vectors: List[List[float]] = []
        input_tokens = 0
        try:
            # Titan Embed has no batch endpoint — one InvokeModel call per
            # text. The corpus this feeds is a handful of documents' worth of
            # chunks (foundation-scale, per vector-rag.md), so this is cheap.
            for text in texts:
                response = client.invoke_model(
                    modelId=self._model_id,
                    body=json.dumps({"inputText": text}),
                    contentType="application/json",
                    accept="application/json",
                )
                body = json.loads(response["body"].read())
                vectors.append(body["embedding"])
                input_tokens += body.get("inputTextTokenCount", 0)
        except (ConnectTimeoutError, ReadTimeoutError) as exc:
            raise ProviderTimeoutError(type(exc).__name__) from exc
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in _RETRYABLE_ERROR_CODES:
                raise ProviderTransientError(type(exc).__name__) from exc
            raise

        return EmbeddingResponse(vectors=vectors, input_tokens=input_tokens)
