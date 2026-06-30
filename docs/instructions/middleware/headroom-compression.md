# Middleware: Headroom Compression

**Library:** `headroom-ai` (third-party, locally patched)
**Position:** Innermost (runs LAST inbound)
**Order:** 4 of 4

## Purpose

Compresses LLM prompt context before it reaches the model, reducing token usage by 60-95%. Operates at the ASGI level so it covers all endpoints and streaming responses.

## Architecture

```
Inbound:  ApiKeyMasking → CaptureOriginal → SkillInjector → Compression → LiteLLM
```

As the innermost middleware, compression runs AFTER skill injection (so skill text gets compressed) and AFTER question capture (so eval uses the original question). This is the last transform before the request reaches LiteLLM's routing engine.

## Compression transforms

Headroom applies three transforms in sequence:

### 1. SmartCrusher
Targets JSON payloads (FHIR/HL7 resources, API responses, structured data):
- Collapses redundant JSON structures
- Removes empty/null fields
- Shortens repeated patterns
- 70-85% reduction on medical data payloads

### 2. CodeCompressor
AST-based code compression:
- Removes comments (unless significant)
- Collapses whitespace
- Shortens variable names (optional)
- Preserves semantic structure

### 3. CacheAligner
KV-cache prefix alignment:
- Stabilizes message prefixes to improve cache hit rate
- A KV cache hit cuts TTFT from ~3s to ~0.3s on Claude
- Minimal token savings, significant latency savings

## Patches applied in entrypoint.py

Three monkey-patches are necessary because Headroom's default configuration doesn't match GateMid's needs:

### Patch 1: Force `compress_user_messages=True`

Headroom's `CompressConfig` defaults `compress_user_messages=False` — meaning it only compresses system prompts, not user messages. GateMid needs full-message compression:

```python
def _patched_compress(messages, **kwargs):
    config = CompressConfig(compress_user_messages=True, min_tokens_to_compress=250)
    result = _original_compress(messages, config=config, **kwargs)
    # ... Redis storage after compression ...
    return result

sys.modules["headroom.compress"].compress = _patched_compress
```

### Patch 2: Force `enable_kompress=True`, `skip_user_messages=False`

The `ContentRouter` class controls which transforms are applied. GateMid targets JSON payloads with SmartCrusher, which requires `enable_kompress=True` and `skip_user_messages=False`:

```python
def _patched_cr_init(self, config=None, observer=None):
    _original_cr_init(self, config=config, observer=observer)
    self.config.enable_kompress = True
    self.config.skip_user_messages = False
```

### Patch 3: Redis storage hook

The patched `compress()` also stores compression results in Redis (fire-and-forget):

```python
if result and result.tokens_saved > 0:
    store_headroom_result(
        call_id=call_id,
        tokens_before=result.tokens_before,
        tokens_after=result.tokens_after,
        tokens_saved=result.tokens_saved,
        compression_ratio=result.compression_ratio,
        ...
    )
```

If skills were injected, their analytics are also attached to the Redis hash.

## Configuration

Middleware registration in `entrypoint.py`:

```python
app.add_middleware(
    CompressionMiddleware,
    min_tokens=300,    # skip small payloads
    # api_key not set → local mode (compresses in-process)
)
```

- `min_tokens=300` — payloads below 300 tokens skip compression (CPU savings)
- No `api_key` → local mode (Headroom compresses in-process, not via cloud API)
- Legacy `startup.py` had `disable_ml=True`, but current entrypoint doesn't — Kompress is enabled via Patch 2, but ONNX model download (~500MB) is avoided

## What's NOT compressed

- Non-LLM endpoints: `/health`, `/metrics`, `/v1/embeddings`, `/v1/moderations`
- Small payloads (< 300 tokens)
- The middleware itself doesn't filter by path — LiteLLM's routing ensures compression only fires for LLM endpoints

## Compression result object

```python
CompressionResult(
    messages=[...],              # compressed message list
    tokens_before=5000,          # token count before compression
    tokens_after=1500,           # token count after compression
    tokens_saved=3500,           # tokens eliminated
    compression_ratio=0.70,      # saved / before
    transforms_applied=[         # which transforms were applied
        "SmartCrusher",
        "CacheAligner"
    ]
)
```

## Redis storage

Results are stored via `eval.redis_store.store_headroom_result()` — see [Data Models](../DATA-MODELS.md) for full schema.

Storage is fire-and-forget: if Redis is down, the exception is caught and compression continues normally.

## Monitoring

View compression stats:
```bash
docker exec -it gatemid-headroom python -m eval.cli headroom
docker exec -it gatemid-headroom python -m eval.cli headroom --days 14
```

Interactive TUI shows daily aggregates, per-call details, and toggleable before/after prompt diff views.

## Test coverage

`tests/test_compression.py` — integration test requiring Docker. Verifies that compressed requests produce valid LLM responses and token savings are non-zero.
