# Middleware: API Key Masking

**File:** `proxy/guardrails/api_key_masking.py`
**Position:** Outermost (runs FIRST inbound, LAST outbound)
**Order:** 1 of 4

## Purpose

Sanitizes API keys from LLM request and response bodies before any other middleware or logging sees them. This prevents accidental key leakage through logs, compression analytics, or evaluation records.

## Architecture

```
Inbound:  ApiKeyMasking → CaptureOriginal → SkillInjector → Compression → LiteLLM
Outbound: LiteLLM → Compression → SkillInjector → CaptureOriginal → ApiKeyMasking
```

As the outermost middleware, it's the first to see incoming requests and the last to see outgoing responses. This ensures no other middleware processes plaintext keys.

## Detection patterns (8 regexes, ordered)

Patterns are applied in priority order — most specific first, generic catch-alls last:

| # | Pattern name | Regex | Example |
|---|-------------|-------|---------|
| 1 | `gemini_key` | `\b(AIzaSy[A-Za-z0-9_-]{26,})\b` | `AIzaSyD...` |
| 2 | `huggingface_token` | `\b(hf_[A-Za-z0-9_-]{20,})\b` | `hf_abc...` |
| 3 | `github_token` | `\b(gh[ops]_[a-zA-Z0-9]{36,})\b` | `ghp_...`, `ghs_...` |
| 4 | `aws_access_key` | `\b(AKIA[0-9A-Z]{16})\b` | `AKIAIOSFODNN7EXAMPLE` |
| 5 | `openai_key` | `\b(sk-[a-zA-Z0-9_-]{20,})\b` | `sk-proj-...`, `sk-ant-...` |
| 6 | `bearer_token` | `\b(Bearer\s+[A-Za-z0-9._\-\/+=]{20,})\b` | `Bearer sk-...` |
| 7 | `api_key_value` | `\b(api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}['\"]?)` | `api_key = "..."` |
| 8 | `generic_long_key` | `\b([A-Za-z0-9_-]{36,})\b` | Any long alphanumeric string |

Patterns 5 and 6 are ordered so `Bearer sk-proj-...` is caught by `bearer_token` before `openai_key` splits it.

## Masking strategy

The `_mask_single_value()` function preserves recognizable structure so debugging is possible:

| Key type | Masked result |
|----------|--------------|
| `AIzaSyXXXX...` | `AIzaSy***MASKED***` |
| `sk-proj-XXXX` | `sk-proj-***MASKED***` |
| `sk-ant-XXXX-YYYY` | `sk-ant-XXXX-***MASKED***` |
| `Bearer sk-XXXX` | `Bearer sk-***MASKED***` |
| `api_key = "XXXX"` | `api_key = "***MASKED***"` |
| Generic long string | `***MASKED***` |

Rules:
- Hyphen-separated tokens: preserve all but last segment
- Known prefixes: preserve prefix, mask remainder
- Bearer tokens: preserve `Bearer ` label, mask token portion
- API key assignments: preserve up to `=`/`:`, mask value

## Scope of masking

### Request bodies
Only content-bearing fields are scanned:
- `$.system` (Anthropic top-level system prompt)
- `$.messages[*].content` (string or list-of-blocks)
- `$.content` (Anthropic top-level user content)

Fields NOT scanned: `tools`, `metadata`, `stop_sequences`, `temperature`, etc. — these don't carry user content that could leak keys.

### Response bodies
Only content-bearing fields:
- `$.choices[*].message.content` (OpenAI)
- `$.choices[*].delta.content` (OpenAI streaming chunks)
- `$.content[*].text` (Anthropic content blocks)

### Streaming responses
Streaming (SSE) responses are forwarded as-is without masking. Parsing individual SSE events for key patterns adds complexity beyond the initial use case.

## Path filtering

Masking only applies to POST requests to these paths:
- `/v1/messages` (Anthropic)
- `/v1/chat/completions` (OpenAI)
- `/v1/responses` (OpenAI Responses API)
- `/chat/completions` (LiteLLM without `/v1` prefix)

All other paths (GET, health checks, embeddings, etc.) pass through unmodified.

## Response header handling

`Content-Length` header is stripped from responses because body size may change after masking. The ASGI server recalculates it.

## Logging

Every masking event is logged at INFO:
```
ApiKeyMask REQUEST /v1/messages — masked openai_key×1; github_token×2 at $.messages[0].content, $.system
ApiKeyMask RESPONSE /v1/messages — masked openai_key×1 at $.choices[0].message.content
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

mask_api_keys_in_response(body: dict) -> tuple[dict, list[dict]]
# Masks only content-bearing response fields
```

## Test coverage

`tests/test_guardrails.py` — covers all 8 patterns, masking preservation, request/response field scoping, and integration with the live gateway.
