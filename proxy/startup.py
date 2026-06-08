# proxy/startup.py
# This file is executed by LiteLLM's --startup_file hook before the server starts.
# It registers Headroom as ASGI middleware on the LiteLLM FastAPI app.

from litellm.proxy.proxy_server import app
from headroom.integrations.asgi import CompressionMiddleware
import logging

logger = logging.getLogger("headroom.startup")

app.add_middleware(
    CompressionMiddleware,

    # Skip compression on small payloads — saves CPU for short queries
    # that wouldn't benefit much anyway
    min_tokens=300,

    # FHIR/HL7 payloads are JSON — SmartCrusher gives 70-85% reduction.
    # Disabling the ML model (Kompress-base) cuts compression latency
    # from ~100-200ms to ~15-50ms. Re-enable if you have prose-heavy RAG.
    disable_ml=True,

    # CacheAligner stabilises message prefixes to improve KV cache hit rate.
    # Free latency saving — a KV cache hit cuts TTFT from ~3s to ~0.3s on Claude.
    enable_cache_aligner=True,

    # Routes that should NEVER be compressed:
    # - /health, /metrics — not LLM calls
    # - /v1/embeddings — embeddings don't benefit from message compression
    excluded_paths=[
        "/health",
        "/metrics",
        "/v1/embeddings",
        "/v1/moderations",
    ],
)

logger.info("Headroom CompressionMiddleware registered on LiteLLM proxy")
