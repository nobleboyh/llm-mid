# API Contracts — GateMid

GateMid proxies LLM API calls. It accepts Anthropic-format and OpenAI-format requests, transforms them through its middleware pipeline, and translates between provider formats via LiteLLM.

## Supported endpoints

| Endpoint | Format | Purpose |
|----------|--------|---------|
| `POST /v1/messages` | Anthropic Messages API | Used by Claude Code |
| `POST /v1/chat/completions` | OpenAI Chat Completions | Used by Open Code, OpenAI SDKs |
| `POST /chat/completions` | OpenAI (no /v1 prefix) | LiteLLM-native path |
| `GET /health` | LiteLLM health check | Proxy readiness |
| `GET /v1/models` | OpenAI models list | Used by Open Code for model discovery |

## Request flow by endpoint

### Anthropic: `POST /v1/messages`

```
Client (Claude Code) sends:
{
  "model": "team-smart-router",
  "system": "You are a helpful assistant...",          // top-level system prompt
  "messages": [
    {"role": "user", "content": [{"type": "text", "text": "Refactor this $ponytail"}]}
  ],
  "max_tokens": 4096
}

Middleware pipeline transforms:
  ApiKeyMasking:
    - Scans $.system, $.messages[*].content[*].text for API keys
    - Masks any found keys (preserving prefix)

  CaptureOriginal:
    - Finds last user message → "Refactor this $ponytail"
    - Sets context var original_question_var

  SkillInjector:
    - Detects $ponytail trigger in user message
    - Strips $ponytail from content → "Refactor this"
    - Appends ponytail.md content to system prompt
    - Sets X-GateMid-Skill-Applied: ponytail response header

  CompressionMiddleware:
    - Headroom compresses messages (SmartCrusher for JSON, CacheAligner for KV alignment)
    - Stores compression stats in Redis

LiteLLM:
  - ComplexityRouter classifies: "Refactor this" → MEDIUM
  - Routes to: deepseek-flash
  - Translates Anthropic → DeepSeek format
  - Sends HTTPS request to DeepSeek API

Response:
  LiteLLM translates DeepSeek → Anthropic format
  {
    "id": "msg_...",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "..."}],
    "model": "deepseek-flash",
    "usage": {"input_tokens": 850, "output_tokens": 320}
  }

  RagasLogger extracts question & answer → LPUSH eval:pending
```

### OpenAI: `POST /v1/chat/completions`

```
Client (Open Code / OpenAI SDK) sends:
{
  "model": "team-smart-router",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Explain database indexing $caveman"}
  ],
  "stream": false
}

Middleware pipeline is identical — same middleware stack handles both formats.
LiteLLM translates between the two.

Response:
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "model": "deepseek-flash",
  "choices": [{
    "index": 0,
    "message": {"role": "assistant", "content": "..."},
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 500, "completion_tokens": 200}
}
```

## Complexity router contract

### Request classification

The `team-smart-router` model alias triggers LiteLLM's `ComplexityRouter`. It analyzes 7 dimensions:

| Dimension | Weight | What it measures |
|-----------|--------|-----------------|
| `reasoningMarkers` | 0.30 | Step-by-step, analysis, debugging keywords |
| `simpleIndicators` | 0.15 | Greetings, yes/no questions |
| `codePresence` | 0.10 | Code blocks, API patterns |
| `technicalTerms` | 0.10 | Domain-specific terminology |
| `tokenCount` | 0.05 | Prompt length (with system prompt baseline) |
| `multiStepPatterns` | 0.05 | Multi-part instructions |
| `questionComplexity` | 0.05 | Question structure depth |

### Tier resolution

| Tier | Score range | Default model |
|------|-------------|---------------|
| SIMPLE | 0.00 – 0.15 | deepseek-flash |
| MEDIUM | 0.15 – 0.30 | deepseek-flash |
| COMPLEX | 0.30 – 0.55 | deepseek-pro |
| REASONING | 0.55+ | deepseek-pro |

Token thresholds: simple ≤ 100 tokens, complex ≥ 2000 tokens.

### Bypassing the router

Set `ANTHROPIC_MODEL=deepseek-pro` (or any concrete model alias) to skip routing.

## Skill injector contract

### Trigger format

`$<skill-name>` anywhere in user message content (string or block list).

- Skill name = filename stem of `.md` file in `proxy/skills/` (case-insensitive)
- Multiple triggers supported: `$caveman $ponytail do this`
- Unknown triggers silently ignored
- Triggers stripped from forwarded message

### Response header

```
X-GateMid-Skill-Applied: ponytail, caveman
```

Set only when skills are activated. Comma-separated for multiple skills.

### Injection method

Skills appended to last system message (or prepended as new system message if none exists). Separator: `\n\n---\n\n`.

## Ragas evaluation contract

### Input (from callback)

```json
{
  "question": "original user question",
  "answer": "LLM response text",
  "contexts": ["retrieved context 1", ...],   // optional
  "ground_truth": "expected answer",            // optional
  "request_category": "general",
  "model": "deepseek-pro"
}
```

### Scoring metrics

| Metric | Requires | Weight (with ctx) | Weight (no ctx) |
|--------|----------|-------------------|-----------------|
| Faithfulness | contexts | 0.3 | — (skipped) |
| Answer Relevancy | embeddings | 0.3 | 1.0 |
| Context Precision | contexts + ground_truth | 0.2 | — (skipped) |
| Context Recall | ground_truth | 0.2 | — (skipped) |

Composite = weighted sum / sum of weights for available metrics.
Weights are dynamic — if a metric returns NaN/None, denominator adjusts.

### Output (written to Redis)

All scores normalized to [0.0, 1.0], rounded to 4 decimal places.

## Eval worker model contract

The eval worker calls LiteLLM with special alias:

```
POST /v1/chat/completions
{
  "model": "ragas-eval",
  "messages": [...],   // Ragas-generated evaluation prompts
  "temperature": 0.1,
  "max_tokens": 2048
}
```

`ragas-eval` resolves to `deepseek/deepseek-v4-flash`. The `RagasLogger` callback skips any call where model starts with `ragas-eval` or `deepseek-v4` (without `proxy_server_request`), preventing infinite scoring loops.

## Health check

```
GET /health
Authorization: Bearer sk-local-dev-key

Response: 200 OK (if proxy is running)
```

## Provider format translation

LiteLLM handles Anthropic ↔ OpenAI/Gemini/DeepSeek translation automatically:

- **System prompt**: Anthropic top-level `system` → OpenAI `messages[0].role=system`
- **Tool use**: Anthropic `tool_use` blocks ↔ OpenAI `tool_calls`
- **Streaming**: SSE translation between formats
- **Content blocks**: Anthropic `[{"type":"text","text":"..."}]` ↔ OpenAI `"content":"string"`
