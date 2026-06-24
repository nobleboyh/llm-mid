"""Lightweight embedding provider using Google Gemini's embedding API.

Avoids installing PyTorch / sentence-transformers / google-genai SDK in the
eval container.  Uses the Gemini REST API through httpx (already a dependency
of openai).

Extends ragas v0.4 ``BaseRagasEmbedding`` so it can be used directly without
the now-deprecated ``LangchainEmbeddingsWrapper``.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from ragas.embeddings.base import BaseRagasEmbedding

logger = logging.getLogger("eval.gemini_embeddings")

GEMINI_MODEL = "models/gemini-embedding-001"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiEmbeddings(BaseRagasEmbedding):
    """Ragas-native Gemini embedding provider (REST API via httpx).

    Satisfies the ``BaseRagasEmbedding`` contract — implement ``embed_text`` and
    ``aembed_text``; ``embed_texts`` / ``aembed_texts`` are inherited.
    """

    def __init__(self, api_key: str | None = None, cache=None):
        super().__init__(cache=cache)
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._api_key:
            raise ValueError(
                "GeminiEmbeddings requires a GEMINI_API_KEY. "
                "Set the GEMINI_API_KEY environment variable or pass api_key=."
            )
        self._client = httpx.Client(timeout=30)
        self._async_client = httpx.AsyncClient(timeout=30)

    def embed_text(self, text: str, **kwargs) -> list[float]:
        return self._embed_one(text, self._client, self._api_key)

    async def aembed_text(self, text: str, **kwargs) -> list[float]:
        return await self._aembed_one(text, self._async_client, self._api_key)

    # ── ragas 0.4.3 backwards-compat methods ──────────────────────────────────
    # _answer_relevance.py calls embed_query / embed_documents directly (a ragas
    # bug fixed upstream).  embed_text is in-class so aliasing works; inherited
    # methods (embed_texts) need a forwarding wrapper.

    def embed_query(self, text: str, **kwargs) -> list[float]:
        return self.embed_text(text, **kwargs)

    def embed_documents(self, texts: list[str], **kwargs) -> list[list[float]]:
        return self.embed_texts(texts, **kwargs)

    async def aembed_query(self, text: str, **kwargs) -> list[float]:
        return await self.aembed_text(text, **kwargs)

    async def aembed_documents(self, texts: list[str], **kwargs) -> list[list[float]]:
        return await self.aembed_texts(texts, **kwargs)

    # ── internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _embed_one(text: str, client: httpx.Client, api_key: str) -> list[float]:
        if not text.strip():
            text = " "
        response = client.post(
            f"{GEMINI_API_URL}/{GEMINI_MODEL}:embedContent",
            params={"key": api_key},
            json={
                "model": GEMINI_MODEL,
                "content": {"parts": [{"text": text}]},
            },
        )
        response.raise_for_status()
        data = response.json()
        values: list[float] = data.get("embedding", {}).get("values", [])
        if not values:
            logger.warning("Empty embedding returned by Gemini — using zeros")
            values = [0.0] * 768
        return values

    @staticmethod
    async def _aembed_one(
        text: str, client: httpx.AsyncClient, api_key: str
    ) -> list[float]:
        if not text.strip():
            text = " "
        response = await client.post(
            f"{GEMINI_API_URL}/{GEMINI_MODEL}:embedContent",
            params={"key": api_key},
            json={
                "model": GEMINI_MODEL,
                "content": {"parts": [{"text": text}]},
            },
        )
        response.raise_for_status()
        data = response.json()
        values: list[float] = data.get("embedding", {}).get("values", [])
        if not values:
            logger.warning("Empty embedding returned by Gemini — using zeros")
            values = [0.0] * 768
        return values

    def __del__(self):
        if hasattr(self, "_client"):
            try:
                self._client.close()
            except Exception:
                pass
        if hasattr(self, "_async_client"):
            try:
                asyncio.run(self._async_client.aclose())
            except Exception:
                pass
