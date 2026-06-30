# Middleware Layer — GateMid

GateMid's proxy pipeline consists of four ASGI middlewares plus one LiteLLM callback. Each is documented individually.

## Pipeline order (inbound)

```
Request
  │
  ▼
┌──────────────────────────────────────┐
│ 1. ApiKeyMaskingMiddleware           │  Outer — runs FIRST
│    Masks API keys in request/response │
├──────────────────────────────────────┤
│ 2. CaptureOriginalQuestionMiddleware │  Second
│    Snapshots raw user question        │
├──────────────────────────────────────┤
│ 3. SkillInjectorMiddleware           │  Third
│    Detects $triggers, injects skills  │
├──────────────────────────────────────┤
│ 4. Headroom CompressionMiddleware    │  Inner — runs LAST
│    Compresses prompt context          │
└──────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────┐
│ LiteLLM Proxy Engine                 │
│   ComplexityRouter → Model Provider   │
└──────────────────────────────────────┘
  │
  ▼
┌──────────────────────────────────────┐
│ RagasLogger Callback                 │  Post-response
│   Enqueues response for async scoring │
└──────────────────────────────────────┘
  │
  ▼
Response
```

## Outbound order (reverse)

```
Response
  │
  ▼
RagasLogger → LiteLLM → Compression → SkillInjector → CaptureOriginal → ApiKeyMasking → Client
```

## Docs

| Layer | Type | Doc |
|-------|------|-----|
| API Key Masking | ASGI Middleware | [api-key-masking.md](api-key-masking.md) |
| Capture Original | ASGI Middleware | [capture-original.md](capture-original.md) |
| Skill Injector | ASGI Middleware | [skill-injector.md](skill-injector.md) |
| Headroom Compression | ASGI Middleware | [headroom-compression.md](headroom-compression.md) |
| RagasLogger | LiteLLM Callback | [ragas-callback.md](ragas-callback.md) |
| Eval Worker | Separate Container | [eval-worker.md](eval-worker.md) |

## Key invariants across all middleware

1. **Never block the request** — exceptions are caught and logged; request continues
2. **Buffer once, forward once** — `modified_receive` sends the buffered body exactly once
3. **Non-JSON passthrough** — non-JSON bodies are forwarded unmodified
4. **Content-Length stripping** — any middleware that changes body size must strip Content-Length
5. **Streaming disconnect** — delegate to real `receive()` after sending modified body
