"""Embedding client abstractions for semantic retrieval."""

import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

from .config import EmbeddingConfig
from .logging import get_logger

logger = get_logger(__name__)


@dataclass
class EmbeddingResult:
    """Embedding response for one text payload."""

    vector: list[float]
    model: str


class EmbeddingClient(ABC):
    """Provider-agnostic embedding client."""

    @abstractmethod
    def embed_text(self, text: str) -> EmbeddingResult:
        """Embed one text value."""

    @abstractmethod
    def check_health(self) -> bool:
        """Return whether provider is reachable."""


class NvidiaNIMEmbeddingClient(EmbeddingClient):
    """Embedding client for NVIDIA-compatible /v1/embeddings endpoint."""

    def __init__(self, config: EmbeddingConfig):
        self.config = config

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(self.config.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def embed_text(self, text: str) -> EmbeddingResult:
        if not text.strip():
            raise ValueError("Cannot embed empty text")

        payload = {"model": self.config.model, "input": text}
        last_error = None
        for attempt in range(1, self.config.max_retries + 1):
            try:
                response = requests.post(
                    self.config.endpoint,
                    json=payload,
                    headers=self._headers(),
                    timeout=self.config.timeout_seconds,
                )
                response.raise_for_status()
                data = response.json()
                vector = data["data"][0]["embedding"]
                return EmbeddingResult(vector=vector, model=data.get("model", self.config.model))
            except Exception as exc:
                last_error = exc
                if attempt < self.config.max_retries:
                    sleep_s = self.config.retry_backoff_seconds * attempt
                    logger.warning("Embedding request failed (attempt %s), retrying in %.1fs", attempt, sleep_s)
                    time.sleep(sleep_s)
        raise RuntimeError(f"Embedding request failed after retries: {last_error}") from last_error

    def check_health(self) -> bool:
        try:
            self.embed_text("radio check")
            return True
        except Exception as exc:
            logger.debug("Embedding health check failed: %s", exc)
            return False


def create_embedding_client(config: EmbeddingConfig) -> EmbeddingClient:
    """Factory for embedding client providers."""
    provider = config.provider.lower()
    if provider in {"nvidia", "nvidia-nim", "nim"}:
        return NvidiaNIMEmbeddingClient(config)
    raise ValueError(f"Unsupported embedding provider: {config.provider}")
