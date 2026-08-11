# Middleware: API Key Masking

**File:** `proxy/guardrails/api_key_masking.py`
**Position:** Outermost (runs FIRST inbound; outbound is pass-through)
**Order:** 1 of 4

## Purpose

Sanitizes API keys from LLM **request** bodies before any other middleware or logging sees them. This prevents accidental key leakage through logs, compression analytics, or evaluation records.

Output masking was removed as redundant — see [Why response masking was removed](#why-response-masking-was-removed).

## Architecture

```
Inbound:  ApiKeyMasking → CaptureOriginal → SkillInjector → Compression → LiteLLM
Outbound: LiteLLM → Compression → SkillInjector → CaptureOriginal → ApiKeyMasking (pass-through)
```

As the outermost middleware, it's the first to see incoming requests — so every downstream middleware (CaptureOriginal, SkillInjector, Compression) and LiteLLM itself only ever sees masked request bodies. The outbound side is a pure pass-through: response bodies are forwarded byte-for-byte, unmodified.

## Why response masking was removed

Output masking never protected a real leak surface:

- **Eval records** — `RagasLogger` extracts the answer from LiteLLM's in-process `response_obj` (not the ASGI response body), so the eval `answer` stored in Redis was never masked by this middleware, streaming or not.
- **Streaming** — SSE responses (the dominant case for coding tools) were already forwarded unmodified.
- **Request masking makes echo impossible** — keys are scrubbed before the model ever sees them, so model output can't echo a key it never received.
- **Costs** — the `generic_long_key` catch-all could rewrite legitimate long strings in model output (base64, JWTs, hashes), and non-streaming responses were fully buffered before the first byte reached the client.

If eval `answer` records ever need scrubbing, mask in `proxy/callback.py` before `enqueue_call_record` — not in this middleware.

## Detection patterns (6 regexes, ordered)

Patterns are applied in priority order — most specific first, generic catch-alls last:

| # | Pattern name | Regex | Example |
|---|-------------|-------|---------|
| 1 | `gemini_key` | `\b(AIzaSy[A-Za-z0-9_-]{26,})\b` | `AIzaSyD...` |
| 2 | `huggingface_token` | `\b(hf_[A-Za-z0-9_-]{20,})\b` | `hf_abc...` |
| 3 | `github_token` | `\b(gh[ops]_[a-zA-Z0-9]{36,})\b` | `ghp_...`, `ghs_...` |
| 4 | `aws_access_key` | `\b(AKIA[0-9A-Z]{16})\b` | `AKIAIOSFODNN7EXAMPLE` |
| 5 | `openai_key` | `\b(sk-[a-zA-Z0-9_-]{20,})\b` | `sk-proj-...`, `sk-ant-...` |
| 6 | `bearer_token` | `\b(Bearer\s+[A-Za-z0-9._\-\/+=]{20,})\b` | `Bearer sk-...` |

> **Removed patterns.** Two original patterns were dropped as over-broad — they
> fired on non-key content:
> - `api_key_value` — any value assigned to an `api_key=`-like label (label heuristic)
> - `generic_long_key` — any 36+ char alphanumeric run (base64, JWTs, hashes, UUIDs, long identifiers)

Patterns 5 and 6 are ordered so `Bearer sk-proj-...` is caught by `bearer_token` before `openai_key` splits it.

## Masking strategy

The `_mask_single_value()` function preserves recognizable structure so debugging is possible:

| Key type | Masked result |
|----------|--------------|
| `AIzaSyXXXX...` | `AIzaSy***MASKED***` |
| `sk-proj-XXXX` | `sk-proj-***MASKED***` |
| `sk-ant-XXXX-YYYY` | `sk-ant-XXXX-***MASKED***` |
| `Bearer sk-XXXX` | `Bearer sk-***MASKED***` |

Rules:
- Hyphen-separated tokens: preserve all but last segment
- Known prefixes: preserve prefix, mask remainder
- Bearer tokens: preserve `Bearer ` label, mask token portion

## Scope of masking

### Request bodies
Only content-bearing fields are scanned:
- `$.system` (Anthropic top-level system prompt)
- `$.messages[*].content` (string or list-of-blocks)
- `$.content` (Anthropic top-level user content)

Within content-block lists, both `text` and `tool_result` blocks are scanned.
`tool_result` blocks carry file-read results and their `content` field (string
or nested text-blocks list) is recursively masked.

Fields NOT scanned: `tools`, `metadata`, `stop_sequences`, `temperature`,
`image` blocks, etc. — these don't carry user content that could leak keys.

## Path filtering

Masking only applies to POST requests to these paths:
- `/v1/messages` (Anthropic)
- `/v1/chat/completions` (OpenAI)
- `/v1/responses` (OpenAI Responses API)
- `/chat/completions` (LiteLLM without `/v1` prefix)

All other paths (GET, health checks, embeddings, etc.) pass through unmodified.

## Logging

Every masking event is logged at INFO:
```
ApiKeyMask REQUEST /v1/messages — masked openai_key×1; github_token×2 at $.messages[0].content, $.system
```

Format: `direction PATH — masked TYPE×COUNT at PATH, PATH, ...`

## Error handling

- Non-JSON bodies: passed through unmodified
- JSON parse errors: caught silently, body forwarded as-is
- All exceptions caught — middleware never blocks a request

## Public functions (for testing)

```python
mask_api_keys_in_text(text: str) -> tuple[str, list[dict]]
# Scans a single string, returns (masked_text, events)

mask_api_keys_in_json(data, path="$") -> tuple[Any, list[dict]]
# Recursively walks JSON, masks all string values

mask_api_keys_in_request(body: dict) -> tuple[dict, list[dict]]
# Masks only content-bearing request fields
```

## Test coverage

`tests/test_guardrails.py` — covers all 6 patterns, masking preservation, request field scoping, and integration with the live gateway.
