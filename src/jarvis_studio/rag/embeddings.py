"""Embeddings Factory for Local RAG and Knowledge Base."""

from __future__ import annotations

import hashlib
import math
import time

from langchain_core.embeddings import Embeddings

from jarvis_studio.config.settings import get_settings
from jarvis_studio.core.observability.logger import logger


class DeterministicLocalEmbeddings(Embeddings):
    """Zero-dependency, deterministic local embedding provider for offline operation and testing.

    Uses character n-grams and hashed feature projection with L2 normalization to produce
    consistent, dimensionally accurate float vectors without downloading multi-GB models.
    """

    def __init__(self, dimension: int = 1536):
        self.dimension = dimension

    def _embed_text(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        clean_text = text.lower().strip()
        if not clean_text:
            return vector

        tokens = clean_text.split()
        for tok in tokens:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dimension
            vector[idx] += 1.0

        for i in range(len(clean_text) - 2):
            ngram = clean_text[i : i + 3]
            h = int(hashlib.sha256(ngram.encode("utf-8")).hexdigest(), 16)
            idx = h % self.dimension
            vector[idx] += 0.5

        norm = math.sqrt(sum(x * x for x in vector))
        if norm > 0.0:
            vector = [x / norm for x in vector]

        return vector

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_text(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_text(text)


class BatchedRetryEmbeddings(Embeddings):
    """Wraps an Embeddings backend with batching and transient-failure retries."""

    def __init__(
        self,
        inner: Embeddings,
        *,
        batch_size: int = 32,
        max_retries: int = 2,
    ) -> None:
        self._inner = inner
        self.batch_size = max(1, batch_size)
        self.max_retries = max(0, max_retries)

    def _call_with_retry(self, fn, arg):
        attempts = self.max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return fn(arg)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    f"Embedding call failed (attempt {attempt + 1}/{attempts}): {exc}"
                )
                if attempt + 1 < attempts:
                    time.sleep(0.4 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        all_vecs: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            all_vecs.extend(self._call_with_retry(self._inner.embed_documents, batch))
        return all_vecs

    def embed_query(self, text: str) -> list[float]:
        return self._call_with_retry(self._inner.embed_query, text)


class EmbeddingsFactory:
    """Factory to instantiate embeddings models based on configuration."""

    @classmethod
    def create_embeddings(
        cls,
        provider: str | None = None,
        model: str | None = None,
        dimension: int | None = None,
    ) -> Embeddings:
        """Create an Embeddings instance according to provider and settings."""
        settings = get_settings()
        prov = (provider or settings.rag.embedding_provider).lower()
        dim = dimension or settings.rag.embedding_dimension
        model_name = model or settings.rag.embedding_model

        is_mock_key = bool(
            settings.llm.openai_api_key
            and (
                settings.llm.openai_api_key.startswith("sk-test")
                or settings.llm.openai_api_key.startswith("mock")
            )
        )

        if (prov == "openai" and not is_mock_key) or (
            prov == "auto" and settings.llm.openai_api_key and not is_mock_key
        ):
            try:
                from langchain_openai import OpenAIEmbeddings

                logger.info(f"Using OpenAIEmbeddings (model={model_name}, dim={dim})")
                inner = OpenAIEmbeddings(
                    model=model_name,
                    dimensions=dim,
                    api_key=settings.llm.openai_api_key,
                    base_url=settings.llm.openai_base_url,
                )
                return BatchedRetryEmbeddings(inner)
            except Exception as e:
                logger.warning(
                    f"Failed to initialize OpenAIEmbeddings ({e}), falling back to DeterministicLocalEmbeddings"
                )

        logger.info(f"Using DeterministicLocalEmbeddings (dimension={dim})")
        return DeterministicLocalEmbeddings(dimension=dim)
