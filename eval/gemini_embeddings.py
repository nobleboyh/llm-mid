"""Lightweight embedding provider using Google Gemini's embedding API.

Avoids installing PyTorch / sentence-transformers in the eval container.
Uses the Gemini REST API through httpx (already a dependency of openai).
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("eval.gemini_embeddings")

GEMINI_MODEL = "models/gemini-embedding-001"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiEmbeddings:
    """Minimal embeddings class compatible with langchain's Embeddings duck-type.

    Exposes ``embed_query(text) -> list[float]`` and
    ``embed_documents(texts) -> list[list[float]]``.
    """

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not self._api_key:
            raise ValueError(
                "GeminiEmbeddings requires a GEMINI_API_KEY. "
                "Set the GEMINI_API_KEY environment variable or pass api_key=."
            )
        self._client = httpx.Client(timeout=30)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)

    # ── internal ──────────────────────────────────────────────────────────────

    def _embed_one(self, text: str) -> list[float]:
        if not text.strip():
            text = " "
        response = self._client.post(
            f"{GEMINI_API_URL}/{GEMINI_MODEL}:embedContent",
            params={"key": self._api_key},
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
