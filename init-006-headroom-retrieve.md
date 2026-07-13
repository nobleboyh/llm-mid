# FEAT-headroom-retrieve — Local CCR (Compress-Cache-Retrieve)

**Status:** Draft  
**Author:** Hoang  
**Created:** 2026-07-13  
**Stack:** llm-mid / Headroom v0.23.0 + LiteLLM ASGI

---

## 1. Problem Statement

Headroom compression is currently **one-way**. SmartCrusher compresses JSON tool outputs (e.g., 500 API results → 20 representative rows), and the LLM only sees the compressed subset. When the LLM needs detail that was compressed away — a specific record, a log line, an edge case buried in the 480 dropped items — it has no way to retrieve the original data. The model guesses, hallucinates, or asks the user to re-run the tool.

The Headroom docs frame this as the core CCR trade-off:

> Traditional compression forces a dilemma: aggressive compression risks losing needed data, while conservative approaches leave savings on the table. CCR removes that trade-off entirely — compress heavily, then restore on demand.

**Headroom v0.23.0 already ships** the CCR infrastructure — `CompressionStore`, `CCRToolInjector`, `CCRResponseHandler`, `StreamingCCRHandler`, `ContextTracker`, and SmartCrusher's `<<ccr:HASH>>` marker pipeline. SmartCrusher already stores originals and emits retrieval markers when it drops rows on the lossy path. What's missing is the **ASGI-level orchestration** — tool injection, response interception, and a persistent backend — that turns this latent capability into a working feature.

---

## 2. Goal

Enable **local CCR in GateMid** without a Headroom Cloud subscription. When SmartCrusher compresses a payload and emits `<<ccr:HASH>>` markers, the gateway:

1. **Injects** the `headroom_retrieve` tool into the request so the LLM can request original data
2. **Persists** the compression store in Redis (survives proxy restarts, shared across workers)
3. **Intercepts** `headroom_retrieve` tool calls in the response (both streaming and non-streaming)
4. **Re-invokes** LiteLLM with the retrieved content, then returns the final response to the client — all transparent to the caller

```
Before (one-way):
  Tool output (5,000 tokens) → SmartCrusher → (500 tokens) → LLM → "I don't know, re-run the tool"

After (CCR):
  Tool output (5,000 tokens) → SmartCrusher → (500 tokens + <<ccr:abc123>> marker) → LLM
  LLM: "need more detail" → headroom_retrieve(hash="abc123") → original 5,000 tokens → LLM → answer
```

---

## 3. Scope

### In scope

- **Redis backend** for `CompressionStore` — implements Headroom's `CompressionStoreBackend` protocol
- **CCR tool injection** — scan compressed messages for markers, inject `headroom_retrieve` tool definition into requests
- **Non-streaming response handling** — intercept complete JSON responses, detect CCR tool calls, handle via `CCRResponseHandler`
- **Streaming response handling** — buffer SSE stream until CCR detection decision, handle via `StreamingCCRHandler`
- **Continuation API call** — `api_call_fn` that re-posts to LiteLLM for the multi-turn retrieval loop (max 3 rounds, configurable)
- **Startup wiring** — initialize Redis-backed compression store in `entrypoint.py` before first request
- **Configuration** — env vars for enabling/disabling, Redis TTL, max retrieval rounds
- **Observability** — Redis analytics for CCR retrievals (counts, latencies, cache hit rates)

### Out of scope (future FEATs)

- **ContextTracker** (proactive expansion) — Headroom's `ContextTracker` can auto-expand compressed content when user later asks about it; wiring this requires per-session state tracking across multi-turn conversations
- **BM25 search** within cached content — the `headroom_retrieve` tool supports a `query` parameter for BM25 search; the retrieval infrastructure supports it but we defer an explicit search integration
- **TOIN feedback loop** — learning from retrieval patterns to improve future compression decisions
- **MCP server** integration — `HeadroomMCPServer` for retrieval via MCP protocol
- **Cloud-mode CCR** — managed Headroom Cloud API for CCR (the `api_key` path)

---

## 4. User Story

> As a developer using GateMid, when the LLM encounters compressed tool output with CCR markers, it can call `headroom_retrieve` to fetch the full original data — so aggressive compression never results in permanent data loss, and I don't need to re-run expensive tool calls.

**Acceptance criteria**

1. SmartCrusher-compressed messages contain `<<ccr:HASH>>` retrieval markers when rows are dropped on the lossy path.
2. The `headroom_retrieve` tool definition is injected into requests that contain CCR markers.
3. Original compressed data is stored in Redis (not in-process memory) and survives proxy restarts.
4. When the LLM calls `headroom_retrieve`, the gateway intercepts the tool call, retrieves the original from Redis, and continues the conversation with the full data.
5. The client never sees the CCR tool call — the gateway handles the multi-turn loop internally.
6. Streaming responses with CCR tool calls are buffered transparently; the client receives the final continuation response.
7. CCR can be disabled via environment variable; the gateway operates identically to current behavior when disabled.
8. CCR retrieval stats (count, latency, cache hit/miss) are recorded in Redis analytics.

---

## 5. Architecture

### 5.1 How CCR is already wired in Headroom v0.23.0

Headroom v0.23.0 has a complete CCR pipeline that's **active but missing the top layer**:

```
SmartCrusher (Rust)                      ContentRouter
     │                                        │
     │  When rows dropped:                     │  _get_smart_crusher() passes
     │  1. Emits <<ccr:HASH>> marker           │  CCRConfig(enabled=True,
     │  2. Stores original in Rust CCR store   │    inject_retrieval_marker=True)
     │  3. _mirror_ccr_to_python_store()       │
     │     syncs Rust entries → Python store   │
     │                                        │
     ▼                                        ▼
  CompressionStore                     CCDRResponseHandler
  (InMemoryBackend default)            (not used by ASGI middleware)
     │                                        │
     │  Has all the originals                  │  Can detect + handle
     │  but data lost on restart               │  headroom_retrieve calls
     │                                        │
  ┌──────────MISSING─────────────────────────────────────┐
  │ 1. Redis backend for CompressionStore                │
  │ 2. Tool injection in ASGI request path               │
  │ 3. Response interception in ASGI response path       │
  │ 4. Continuation API call for multi-turn loop         │
  └──────────────────────────────────────────────────────┘
```

Key insight: `ContentRouterConfig` defaults `ccr_enabled=True` and `ccr_inject_marker=True`. Our monkey-patch in `entrypoint.py` does NOT override these, so SmartCrusher already emits `<<ccr:HASH>>` markers **when it executes the lossy row-drop path**. The markers look like:

```
<<ccr:a1b2c3d4e5f6a1b2c3d4e5f6>>
```

or in the text:

```
[500 items compressed to 20. Retrieve more: hash=a1b2c3d4e5f6a1b2c3d4e5f6]
```

### 5.2 GateMid CCR pipeline (what we add)

```
Inbound HTTP Request
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ Existing Middleware Stack (unchanged pipeline)             │
│                                                            │
│ ApiKeyMasking → CaptureOriginal → SkillInjector →          │
│ CompressionMiddleware (SmartCrusher emits <<ccr:HASH>>)    │
└───────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ CCRRetrieveMiddleware (NEW — innermost, runs LAST inbound) │
│                                                            │
│ Inbound:                                                   │
│  1. Scan compressed messages for <<ccr:HASH>> markers      │
│  2. Inject headroom_retrieve tool into tools array         │
│  3. Forward to LiteLLM                                     │
│                                                            │
│ Outbound (non-streaming):                                  │
│  1. Use CCRResponseHandler.has_ccr_tool_calls()            │
│  2. If detected: handle_response(api_call_fn=...)          │
│  3. api_call_fn → HTTP POST to localhost:4000 with         │
│     _ccr_continuation flag (skip compression)              │
│  4. Return final response to client                        │
│                                                            │
│ Outbound (streaming):                                      │
│  1. Pass through StreamingCCRHandler.process_stream()      │
│  2. If CCR detected mid-stream: buffer, handle, re-stream  │
│  3. Otherwise: passthrough with ~zero overhead             │
└───────────────────────────────────────────────────────────┘
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ LiteLLM Proxy Engine                                       │
│   ComplexityRouter → Model Provider                        │
│                                                            │
│   Continuation calls bypass the middleware stack:          │
│   - Use litellm.completion() directly for non-streaming    │
│   - Avoid re-compression, re-injection                     │
└───────────────────────────────────────────────────────────┘
        │
        ▼
    Provider (Gemini / DeepSeek)
```

### 5.3 Middleware positioning

```
Registration order (last = outermost = runs first inbound):

  1. app.add_middleware(CCRRetrieveMiddleware)          ← NEW innermost
  2. app.add_middleware(CompressionMiddleware)           ← existing
  3. app.add_middleware(SkillInjectorMiddleware)         ← existing
  4. app.add_middleware(CaptureOriginalQuestionMiddleware) ← existing
  5. app.add_middleware(ApiKeyMaskingMiddleware)         ← existing

Inbound:
  ApiKeyMasking → CaptureOriginal → SkillInjector → Compression → CCRRetrieve → LiteLLM

Outbound:
  LiteLLM → CCRRetrieve → Compression → SkillInjector → CaptureOriginal → ApiKeyMasking
```

CCRRetrieve is **innermost** so it runs last inbound — after Headroom has compressed the messages and emitted CCR markers. This way it scans the compressed output for markers, not the original.

### 5.4 Component overview

```
proxy/
├── ccr/
│   ├── __init__.py
│   ├── redis_backend.py          ← NEW — RedisCompressionStoreBackend
│   └── retrieve_middleware.py    ← NEW — CCRRetrieveMiddleware
│
├── entrypoint.py                 ← MODIFIED — init Redis backend, register CCR middleware

eval/
└── redis_store.py                ← REFERENCE — existing Redis patterns to mirror
```

---

## 6. Detailed Design

### 6.1 Redis backend for CompressionStore

Headroom defines a `CompressionStoreBackend` protocol (`headroom.cache.backends.base`). The built-in `InMemoryBackend` stores everything in a Python dict — data is lost on restart and not shared across workers.

We implement `RedisCompressionStoreBackend` implementing the same protocol, backed by the existing Redis connection:

```python
# proxy/ccr/redis_backend.py

import json
import logging
from typing import Any

from headroom.cache.backends.base import CompressionStoreBackend
from headroom.cache.compression_store import CompressionEntry

import redis

logger = logging.getLogger(__name__)

CCR_KEY_PREFIX = "ccr:entry:"          # hash key → entry data
CCR_STATS_KEY = "ccr:stats"            # global CCR stats hash


class RedisCompressionStoreBackend:
    """Redis-backed storage for CompressionStore.

    Keys:     ``ccr:entry:{hash}`` → JSON-serialized CompressionEntry
    Stats:    ``ccr:stats`` → Hash with counters (hits, misses, stores, evictions)
    TTL:      Each entry has its own TTL (from CompressionEntry.ttl), default 3600s.
    """

    def __init__(self, redis_client: redis.Redis, key_prefix: str = CCR_KEY_PREFIX):
        self._r = redis_client
        self._prefix = key_prefix

    def _key(self, hash_key: str) -> str:
        return f"{self._prefix}{hash_key}"

    # ── Protocol methods ──────────────────────────────────────────────────

    def get(self, hash_key: str) -> CompressionEntry | None:
        data = self._r.get(self._key(hash_key))
        if data is None:
            self._r.hincrby(CCR_STATS_KEY, "misses", 1)
            return None
        self._r.hincrby(CCR_STATS_KEY, "hits", 1)
        return CompressionEntry(**json.loads(data))

    def set(self, hash_key: str, entry: CompressionEntry) -> None:
        ttl = getattr(entry, 'ttl', 3600)
        self._r.setex(
            self._key(hash_key),
            ttl,
            json.dumps(entry.__dict__, default=str),
        )
        self._r.hincrby(CCR_STATS_KEY, "stores", 1)

    def delete(self, hash_key: str) -> bool:
        return self._r.delete(self._key(hash_key)) > 0

    def exists(self, hash_key: str) -> bool:
        return self._r.exists(self._key(hash_key)) > 0

    def clear(self) -> None:
        keys = self._r.keys(f"{self._prefix}*")
        if keys:
            self._r.delete(*keys)

    def count(self) -> int:
        return len(self._r.keys(f"{self._prefix}*"))

    def keys(self) -> list[str]:
        prefix_len = len(self._prefix)
        return [
            k.decode("utf-8")[prefix_len:]
            for k in self._r.keys(f"{self._prefix}*")
        ]

    def items(self) -> list[tuple[str, CompressionEntry]]:
        result = []
        for raw_key in self._r.keys(f"{self._prefix}*"):
            hash_key = raw_key.decode("utf-8")[len(self._prefix):]
            entry = self.get(hash_key)
            if entry is not None:
                result.append((hash_key, entry))
        return result

    def get_stats(self) -> dict[str, Any]:
        stats = self._r.hgetall(CCR_STATS_KEY)
        return {
            "backend_type": "redis",
            "entry_count": self.count(),
            "hits": int(stats.get("hits", 0)),
            "misses": int(stats.get("misses", 0)),
            "stores": int(stats.get("stores", 0)),
        }
```

**Design decisions:**

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Serialization | JSON via `CompressionEntry.__dict__` | Matches InMemoryBackend shape; avoids pickle (security, cross-version compat) |
| TTL per entry | Uses `entry.ttl` (default 3600 from `CompressionStore`) | Headroom sets per-entry TTL; Redis SETEX enforces it |
| Stats key | Separate `ccr:stats` hash with counters | Lightweight atomic counters; viewable via `HGETALL` |
| Prefix | `ccr:entry:` prepended to hash keys | Avoids namespace collisions with existing `eval:*` and `headroom:*` keys |
| `items()` / `keys()` | Scans with KEYS, then GETs | OK for admin/TUI use (not hot path); the hot path is `get()` by hash |

### 6.2 CCR Retrieve Middleware

New ASGI middleware injected as the innermost layer. Handles both the inbound (tool injection) and outbound (response interception + multi-turn loop).

```python
# proxy/ccr/retrieve_middleware.py

from starlette.types import ASGIApp, Receive, Scope, Send

class CCRRetrieveMiddleware:
    """ASGI middleware for CCR tool injection and response handling."""

    def __init__(
        self,
        app: ASGIApp,
        enabled: bool = True,
        max_retrieval_rounds: int = 3,
    ) -> None:
        self.app = app
        self._enabled = enabled
        self._max_rounds = max_retrieval_rounds
```

#### 6.2.1 Inbound: tool injection

Uses Headroom's built-in `CCRToolInjector` which scans messages for `<<ccr:HASH>>`, `[...compressed...hash=xxx]`, and `[...compressed. hash=xxx]` patterns:

1. Call `injector.scan_for_markers(messages)` — detects all CCR hashes in compressed content
2. If `injector.has_compressed_content`:
   - `tools = injector.inject_tool(tools)` — adds `headroom_retrieve` tool to tools array
   - `messages = injector.inject_system_instructions(messages)` — appends retrieval instructions to system prompt

Provider format (Anthropic vs OpenAI) is detected from the request path or content structure.

#### 6.2.2 Outbound (non-streaming): CCR response handling

For non-streaming requests (`stream: false` or not set):

1. Buffer the full response body
2. Parse as JSON
3. Use `CCRResponseHandler` to check for `headroom_retrieve` tool calls:
   ```python
   handler = CCRResponseHandler(ResponseHandlerConfig(max_retrieval_rounds=self._max_rounds))
   if handler.has_ccr_tool_calls(response_json, provider=provider):
       final_response = await handler.handle_response(
           response_json, messages, tools,
           api_call_fn=self._continuation_call,
           provider=provider,
       )
   ```
4. Return `final_response` as the HTTP response body

#### 6.2.3 Outbound (streaming): buffered CCR detection

For streaming requests (`stream: true`):

1. Wrap the streaming response body in an async generator
2. Pass through `StreamingCCRHandler.process_stream()`:
   ```python
   streaming_handler = StreamingCCRHandler(response_handler, provider=provider)
   async for chunk in streaming_handler.process_stream(
       response_body_stream, messages, tools,
       api_call_fn=self._continuation_call,
   ):
       yield chunk
   ```
3. If no CCR detected → chunks pass through immediately (no latency added)
4. If CCR detected → buffer rest of stream, handle, re-stream continuation response

The `StreamingCCRBuffer` uses substring scanning on accumulated bytes to detect CCR tool calls (`"headroom_retrieve"` in the SSE stream), making detection fast and zero-overhead in the common (no-CCR) case.

#### 6.2.4 Continuation API call

The `api_call_fn` passed to `CCRResponseHandler.handle_response()` needs to call LiteLLM with the augmented messages (original + assistant tool_use + tool results). The continuation call must NOT go through compression/injection again.

**Approach: direct `litellm.completion()` call**

The most reliable approach is to use `litellm.completion()` directly from within the middleware, bypassing the HTTP layer entirely:

```python
async def _continuation_call(self, messages, tools):
    import litellm
    response = await litellm.acompletion(
        model=self._continuation_model,  # The model being used (captured from original request)
        messages=messages,
        tools=tools,
        stream=False,
        metadata={"_ccr_continuation": True},
    )
    # Convert litellm response to provider-format dict
    return response.model_dump()
```

This avoids:
- Another pass through the ASGI middleware stack (no re-compression)
- Another pass through ComplexityRouter (the model is already chosen)
- Another SkillInjector trigger detection
- Additional HTTP overhead

**Alternative (if litellm.acompletion has side effects):** HTTP POST to `localhost:4000/v1/chat/completions` with a `X-GateMid-CCR-Continuation: 1` header that each middleware checks to skip processing. This is more resilient because it uses the same code path as normal requests. The downside is HTTP overhead per round and more middleware changes.

**Decision: start with `litellm.acompletion()` directly.** If the LiteLLM callback or other side effects are needed for continuation calls, fall back to the HTTP header approach.

### 6.3 Startup wiring in entrypoint.py

```python
# ── CCR: Initialize Redis-backed CompressionStore BEFORE any compression ──
# The get_compression_store() singleton is used by SmartCrusher's
# _mirror_ccr_to_python_store() to persist <<ccr:HASH>> markers.
# Must be called before the first request triggers compression.
import os
from headroom.cache.compression_store import get_compression_store
from proxy.ccr.redis_backend import RedisCompressionStoreBackend
from eval.redis_store import r as redis_client  # existing Redis connection

_ccr_enabled = os.getenv("GATEMID_CCR_ENABLED", "1") == "1"
_ccr_max_entries = int(os.getenv("GATEMID_CCR_MAX_ENTRIES", "5000"))
_ccr_default_ttl = int(os.getenv("GATEMID_CCR_TTL_SECONDS", "3600"))
_ccr_max_rounds = int(os.getenv("GATEMID_CCR_MAX_ROUNDS", "3"))

if _ccr_enabled:
    _ccr_backend = RedisCompressionStoreBackend(redis_client)
    get_compression_store(
        max_entries=_ccr_max_entries,
        default_ttl=_ccr_default_ttl,
        backend=_ccr_backend,
    )
    logger.info(
        "CCR: Redis-backed CompressionStore initialized "
        "(max_entries=%d, ttl=%ds)", _ccr_max_entries, _ccr_default_ttl,
    )
else:
    logger.info("CCR: Disabled via GATEMID_CCR_ENABLED=0")
```

Then register the CCR middleware as the innermost layer:

```python
# 1z. Register CCR retrieve middleware (innermost — handles tool calls)
from proxy.ccr.retrieve_middleware import CCRRetrieveMiddleware

app.add_middleware(
    CCRRetrieveMiddleware,
    enabled=_ccr_enabled,
    max_retrieval_rounds=_ccr_max_rounds,
)

logger.info(
    "CCRRetrieveMiddleware registered (enabled=%s, max_rounds=%d)",
    _ccr_enabled, _ccr_max_rounds,
)
```

**Why must Redis store init happen before middleware registration?** The `get_compression_store()` is a module-level singleton. SmartCrusher's first `apply()` call will call `get_compression_store()` internally (via `_mirror_ccr_to_python_store()`). If we haven't pre-initialized it with a Redis backend, the `InMemoryBackend` default is used and all stored entries are lost on restart.

### 6.4 Continuation call: avoiding eval loops

The `RagasLogger` callback in `proxy/callback.py` already has loop prevention:

```python
# Existing loop prevention (callback.py):
# - Skips when model starts with "ragas-eval"
# - Skips when metadata._ragas_eval_call is True
# - Skips when call is internal LiteLLM → deepseek-v4 without proxy_server_request
```

For CCR continuation calls via `litellm.acompletion()`, we set `metadata={"_ccr_continuation": True}`. The `RagasLogger` already skips calls without `proxy_server_request` when the model is `deepseek-v4`. However, to be safe, we should:

1. Add `"_ccr_continuation"` to the RagasLogger skip list
2. Track CCR continuation rounds as a separate metric (not as regular calls)

**Decision: extend RagasLogger's skip check** with `or metadata.get("_ccr_continuation")`. Continuation calls are internal plumbing, not real user requests, and should not be eval-scored.

---

## 7. Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `GATEMID_CCR_ENABLED` | `1` | Enable CCR (0=off) |
| `GATEMID_CCR_MAX_ENTRIES` | `5000` | Max cached compression entries in Redis |
| `GATEMID_CCR_TTL_SECONDS` | `3600` | Default TTL per entry (1 hour) |
| `GATEMID_CCR_MAX_ROUNDS` | `3` | Max CCR retrieve→continue rounds per request (circuit breaker) |

**When CCR is disabled** (`GATEMID_CCR_ENABLED=0`):
- `CCRRetrieveMiddleware` becomes a passthrough (no tool injection, no response interception)
- `CompressionStore` stays on `InMemoryBackend` (existing behavior)
- SmartCrusher may still emit `<<ccr:HASH>>` markers in text, but the LLM has no tool to call — markers are inert

---

## 8. Observability

### 8.1 Redis analytics keys

| Key | Type | Content |
|-----|------|---------|
| `ccr:stats` | Hash | `hits`, `misses`, `stores`, `evictions` counters |
| `ccr:entry:{hash}` | String | JSON-serialized `CompressionEntry` |
| `headroom:call:{call_id}` | Hash | Existing — add `ccr_retrievals` (count) and `ccr_retrieval_items` (total items retrieved) fields |

### 8.2 Log signals

| Signal | Level | Content |
|--------|-------|---------|
| `CCR: Redis-backed CompressionStore initialized` | INFO | Startup confirmation |
| `CCR: Disabled via GATEMID_CCR_ENABLED=0` | INFO | Startup — CCR off |
| `CCR: Injected headroom_retrieve tool (N hashes)` | DEBUG | Per-request tool injection |
| `CCR: Handling N retrieval(s) in round X` | INFO | Multi-turn round |
| `CCR: Retrieved N items (M searches, K full)` | DEBUG | Per-retrieval stats |
| `CCR: Detected tool call in stream, switching to buffered mode` | INFO | Streaming detection |
| `CCR: Hit max retrieval rounds (3), returning partial` | WARNING | Circuit breaker |
| `CCR: Continuation API call failed` | ERROR | Continuation error |

### 8.3 Response headers

| Header | Value | When |
|--------|-------|------|
| `X-GateMid-CCR-Retrievals` | `2` | Number of CCR retrieve rounds executed |
| `X-GateMid-CCR-Items-Retrieved` | `480` | Total items retrieved from compression store |

---

## 9. Error Handling & Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| CCR disabled (`GATEMID_CCR_ENABLED=0`) | Middleware passthrough — zero overhead, no tool injection |
| No CCR markers in compressed messages | Tool not injected; response passes through unchanged |
| SmartCrusher takes lossless path (no rows dropped) | No `<<ccr:HASH>>` emitted — nothing to retrieve |
| Redis unavailable during store | SmartCrusher mirrors to InMemoryBackend fallback; markers still in text but retrieval may 404 |
| Redis unavailable during retrieve | `headroom_retrieve` returns error; LLM continues with compressed context |
| Continuation call times out | Break the loop, return last response (may still have CCR tool call) |
| `max_retrieval_rounds` reached | Break the loop, log WARNING, return last response |
| Non-JSON response body | Skip CCR detection, passthrough |
| LLM calls tools other than `headroom_retrieve` | Pass through — only CCR tool calls are intercepted |
| Multiple CCR markers in one message | All hashes detected; `headroom_retrieve` tool available for each |
| `litellm.acompletion()` raises | Catch, log ERROR, return original response (with unhandled tool call) |
| Streaming client disconnect mid-CCR-handling | Detect disconnect on continuation send, clean up |

---

## 10. Testing Plan

### Unit tests — `tests/test_ccr.py`

| Test | Assertion |
|------|-----------|
| `test_redis_backend_store_and_retrieve` | Store entry → retrieve → fields match |
| `test_redis_backend_ttl_expiry` | Entry expires after TTL |
| `test_redis_backend_missing_key_returns_none` | `get(nonexistent)` → None, miss counter incremented |
| `test_redis_backend_clear_and_count` | `clear()` → `count()` returns 0 |
| `test_tool_injector_detects_ccr_markers` | `CCRToolInjector` finds hashes in `<<ccr:HASH>>` markers |
| `test_tool_injector_detects_text_markers` | `CCRToolInjector` finds hashes in `[...compressed...hash=xxx]` format |
| `test_tool_injector_no_markers_no_injection` | No markers → `has_compressed_content` is False |
| `test_tool_injector_injects_anthropic_format` | Anthropic-format tool definition has `input_schema` |
| `test_tool_injector_injects_openai_format` | OpenAI-format tool definition has `function.parameters` |
| `test_response_handler_detects_ccr_tool_call` | `has_ccr_tool_calls()` returns True for `headroom_retrieve` tool_use |
| `test_response_handler_no_ccr_tool_call` | `has_ccr_tool_calls()` returns False for non-CCR tool_use |
| `test_middleware_disabled_passthrough` | When `enabled=False`, request/response pass through unchanged |

### Integration tests — `tests/test_ccr_integration.py` (requires Docker)

| Test | Assertion |
|------|-----------|
| `test_ccr_marker_emitted_on_large_json` | Request with large JSON array → response messages contain `<<ccr:` marker |
| `test_headroom_retrieve_tool_injected` | Request with compressed content → tools array includes `headroom_retrieve` |
| `test_ccr_retrieval_round_trip` | Mock a response with `headroom_retrieve` tool call → middleware handles it |
| `test_ccr_disabled_no_tool_injection` | `GATEMID_CCR_ENABLED=0` → no tool injection |

### Manual verification

```bash
# Check CCR stats
docker exec gatemid-redis redis-cli HGETALL ccr:stats

# Check stored entries
docker exec gatemid-redis redis-cli KEYS "ccr:entry:*" | head -10

# Check if markers are being emitted
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-dev-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "team-smart-router",
    "messages": [
      {"role": "user", "content": "Here is a large JSON array to compress: [...]"}
    ]
  }' | jq '.choices[0].message.content' | grep -o '<<ccr:[^>]*>>' | head -5
```

---

## 11. SmartCrusher marker verification

A critical pre-implementation check: verify that SmartCrusher actually emits `<<ccr:HASH>>` markers in the running Docker container with `enable_kompress=True`.

**Test procedure:**

```bash
# 1. Start the gateway
docker compose up -d

# 2. Send a request with a large JSON array (500+ items, likely to trigger lossy path)
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-local-dev-key" \
  -H "Content-Type: application/json" \
  -d @tests/fixtures/large_json_array.json | \
  python3 -c "import sys,json; d=json.load(sys.stdin); content=d['choices'][0]['message']['content']; print('HAS CCR MARKER:', '<<ccr:' in content)"

# 3. Check logs for SmartCrusher activity
docker compose logs litellm | grep -i "crusher\|ccr\|smart"
```

If markers are NOT emitted (SmartCrusher takes lossless-only path or `router:mixed` overrides):
- Investigate ContentRouter's `_classify_content()` to understand why JSON_ARRAY isn't detected
- Consider patching `enable_smart_crusher` or the content classification
- Fallback: inject CCR markers manually at the message level (coarser granularity than per-SmartCrusher-row)

---

## 12. Future work

### 12.1 ContextTracker (proactive expansion)

Headroom's `ContextTracker` tracks which compressed content was retrieved and proactively expands it when the user later asks about related topics. Example:

1. Turn 1: User asks "find all users" → SmartCrusher compresses 1000 users → 20 rows + `<<ccr:abc>>` marker
2. Turn 3: User asks "tell me about the auth middleware" → ContextTracker detects that the compressed "users" result contains auth-related entries → proactively retrieves `<<ccr:abc>>` before the LLM even asks

This requires per-session state tracking — the `ContextTracker` needs to know what was compressed in each conversation and map new queries against cached content. The infrastructure exists (`headroom.ccr.context_tracker`) but requires:
- Session ID tracking (extract from request metadata or generate)
- Per-session `ContextTracker` instances (or a session-keyed store)
- Hook into the request path to run `tracker.analyze_new_query()` before compression

### 12.2 BM25 search within cached content

The `headroom_retrieve` tool accepts an optional `query` parameter that runs BM25 search over cached items. The `CompressionStore.search()` method is already implemented:

```python
results = store.search(hash_key, "auth middleware", max_results=20)
```

This enables "smart retrieval" where the LLM doesn't need the full payload — just the relevant subset. Integration requires:
- No backend changes (search is done in Python after retrieving the full entry)
- Only the tool injection needs the `query` parameter documented in the tool description

### 12.3 TOIN feedback loop

When users retrieve compressed content, TOIN (Headroom's learning system) records which rows were retrieved and adjusts future compression decisions to preserve those rows. This is a Headroom Cloud feature but the infrastructure exists locally — it requires `_record_to_toin()` integration with the retrieval events.

---

## 13. Delivery

| Task | Estimate | Depends on |
|------|----------|------------|
| `proxy/ccr/redis_backend.py` | 2h | — |
| `proxy/ccr/retrieve_middleware.py` (non-streaming path) | 3h | redis_backend |
| `proxy/ccr/retrieve_middleware.py` (streaming path) | 2h | non-streaming |
| Continuation call (`litellm.acompletion` integration) | 2h | retrieve_middleware |
| `entrypoint.py` startup wiring | 1h | redis_backend + retrieve_middleware |
| RagasLogger skip list extension | 0.5h | continuation call |
| SmartCrusher marker verification | 1h | — (pre-req gate) |
| Unit tests (`test_ccr.py`) | 2h | redis_backend + retrieve_middleware |
| Integration tests (`test_ccr_integration.py`) | 2h | all above |
| Docs update (`headroom-compression.md` + middleware README) | 1h | all above |
| **Total** | **~16.5h** | |

**Pre-implementation gate (1h):** SmartCrusher marker verification. If markers are not emitted, add 2-4h for marker injection fallback.

---

## 14. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| SmartCrusher doesn't emit markers on lossy path | Medium | High | Gate task (1h verification); fallback to manual marker injection if needed |
| `litellm.acompletion()` has side effects with proxy state | Medium | Medium | Test; fallback to HTTP header approach for continuation |
| Streaming CCR buffering adds latency | Low | Medium | `StreamingCCRHandler` only buffers when CCR detected; 99% of streams pass through unbuffered |
| Redis memory pressure from many large entries | Low | Medium | TTL defaults to 1h; `max_entries=5000` hard cap; per-entry TTL from Headroom |
| Multi-round loops hit timeout | Low | Low | `max_retrieval_rounds=3` circuit breaker; error fallback returns best-effort response |
