# Middleware: Capture Original Question

**File:** `proxy/capture_original.py`
**Position:** Second-outermost (runs SECOND inbound)
**Order:** 2 of 4

## Purpose

Snapshots the raw user question before Headroom compression transforms the message content. Compression can significantly alter or shorten user messages — the eval system needs the original question to score answer quality accurately.

## Architecture

```
Inbound:  ApiKeyMasking → CaptureOriginal → SkillInjector → Compression → LiteLLM
```

Positioned after API key masking (so keys are already sanitized) but before skill injection and compression. This ensures it captures the question as the user typed it, including any `$trigger` tokens (skill injection strips them afterward).

## How it works

1. **Buffers the full request body** from the ASGI receive stream
2. **Parses the JSON** to find messages
3. **Scans messages in reverse** for the last `role: "user"` message
4. **Extracts content** — handles both string content (OpenAI) and list-of-blocks (Anthropic)
5. **Sets a context variable** `original_question_var` with the extracted text
6. **Forwards the request** with `modified_receive` (sends buffered body exactly once, then delegates to real receive)

### Content extraction

```python
for msg in reversed(messages):
    if msg.get("role") == "user":
        content = msg.get("content", "")
        if isinstance(content, list):
            # Anthropic block format: [{"type":"text","text":"..."}]
            original_question = " ".join(
                p.get("text", "") for p in content
                if p.get("type") == "text"
            )
        else:
            original_question = str(content)
        break
```

## Context variable

```python
original_question_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "original_question", default=""
)
```

This is a **side-channel** for the RagasLogger callback. On passthrough endpoints (`/v1/messages`), LiteLLM never calls `add_litellm_data_to_request`, so the request-body metadata never reaches `model_call_details`. The context var bridges this gap — the callback reads it directly.

## Fallback in callback

If `original_question_var` is empty (e.g., the callback fired on a different thread), the callback falls back to reading `proxy_server_request.body.messages` from `litellm_params`:

```python
if not question.strip():
    _psr = kwargs.get("litellm_params", {}).get("proxy_server_request") or {}
    for msg in _psr.get("body", {}).get("messages", []):
        if msg.get("role") == "user":
            question = extract_content(msg["content"])
            break
```

## Path filtering

Only intercepts POST to:
- `/v1/messages`
- `/v1/chat/completions`
- `/chat/completions`

Must match `capture_original.py` and `skill_injector.py` for consistent coverage.

## Error handling

- Empty user message → sets context var to empty string (callback skips scoring)
- Non-JSON body → passes through, no context var set
- JSON parse error → caught, passes through
- Any exception → logged at ERROR, request continues

## Modified receive pattern

```python
body_sent = False
async def modified_receive():
    nonlocal body_sent
    if not body_sent:
        body_sent = True
        return {"type": "http.request", "body": full_body, "more_body": False}
    return await receive()  # delegate to real receive for streaming
```

Sends buffered body exactly once, then delegates to the real receive. This is critical for streaming — LiteLLM needs to detect actual client disconnects, not a fake disconnect from a depleted iterator.

## Logging

At DEBUG:
- `CaptureOriginal — scope type=http method=POST path=/v1/messages`
- `CaptureOriginal — read <N> bytes body`
- `CaptureOriginal — stored original_question="..." path=/v1/messages`

At INFO (from `modified_receive` logging in entrypoint):
- All middleware dispatch is confirmed via logger setup

## Test coverage

`tests/test_callback.py` — tests that the context var is consumed correctly and the fallback path works.
