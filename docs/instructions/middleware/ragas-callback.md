# Callback: RagasLogger (Eval Enqueue)

**File:** `proxy/callback.py`
**Type:** LiteLLM Custom Callback (not ASGI middleware)
**Position:** Post-response (fires after LiteLLM returns)

## Purpose

Captures every successful LiteLLM call and pushes a structured record to Redis for async quality scoring. Zero impact on response latency — the callback only does a Redis RPUSH.

## Architecture

```
LiteLLM returns response
        │
        ▼
RagasLogger.async_log_success_event()  ← LiteLLM calls this for all responses
        │
        ├─ _should_skip()? → return (eval loop prevention)
        │
        ├─ Extract question from context var or proxy_server_request
        ├─ Extract answer from response object
        ├─ Extract usage stats (token counts)
        ├─ Build record dict with metadata
        ├─ Attach skill injection info (if any)
        │
        └─ enqueue_call_record() → Redis LPUSH eval:pending
```

## Loop prevention (two-tier)

Ragas scoring calls the LLM-as-judge through LiteLLM. If those calls were also enqueued for scoring, it would create an infinite loop: score A → call LLM → enqueue B → score B → call LLM → ...

```python
def _should_skip(self, kwargs: dict) -> bool:
    # Tier 1: Model name prefix
    if model.startswith("ragas-eval"):
        return True

    # Tier 1b: Metadata flag
    if meta.get("_ragas_eval_call"):
        return True

    # Tier 2: Original request body model check
    if body_model.startswith("ragas-eval"):
        return True

    # Tier 2b: Internal Router-resolved calls without proxy_server_request
    if not _lp.get("proxy_server_request") and model.startswith("deepseek-v4"):
        return True

    return False
```

- Tier 1 catches the eval worker's direct calls with `model="ragas-eval"`
- Tier 2 catches ComplexityRouter-resolved calls where the original model was `ragas-eval` but got resolved to `deepseek-v4-flash`

## Question extraction

The callback tries two sources, in order:

1. **Context variable** `original_question_var` — set by `CaptureOriginalQuestionMiddleware`. The preferred source for `/v1/messages` passthrough endpoints.

2. **Fallback** `proxy_server_request.body.messages` — read from `litellm_params` when the context var is empty (happens on router-resolved calls on different threads).

If neither source yields a question, the record is skipped with a WARNING log.

## Answer extraction

Handles three response formats:

```python
def _extract_content(content) -> str:
    # LiteLLM 1.82+: content is Union[str, List[...]]
    if isinstance(content, list):
        return " ".join(block["text"] for block in content if block.get("type") == "text")
    if isinstance(content, str):
        return content
    return ""

def _get_choices(response):
    if hasattr(response, "choices"):      # ModelResponse object
        return response.choices
    if isinstance(response, dict):
        return response.get("choices") or response.get("content", [])
```

Response format detection:
- **OpenAI message**: `choices[0].message.content`
- **Anthropic text block**: `choices[0].type == "text"` → `choices[0].text`
- **Anthropic thinking block**: `choices[0].type == "thinking"` → scan for first `text` block
- **Streaming delta**: `choices[0].delta.content`

## Record structure

```python
{
    "call_id":          str(uuid4()),
    "timestamp":        ISO 8601 UTC,
    "question":         original user question,
    "answer":           extracted response text,
    "contexts":         user_meta["retrieved_context"] or [],
    "ground_truth":     user_meta["ground_truth"] or "",
    "request_category": user_meta["request_category"] or "general",
    "prompt_id":        user_meta["prompt_id"] or "default",
    "model":            kwargs["model"],
    "tokens_in":        usage["prompt_tokens"],
    "tokens_out":       usage["completion_tokens"],
    "skill_name":       "ponytail, caveman" (if injected),
    "skill_tokens_pre_compression": 850 (if injected),
}
```

## Metadata sources

LiteLLM uses different metadata keys depending on the endpoint:

- `/v1/chat/completions` → `kwargs["metadata"]`
- `/v1/messages` (Anthropic API) → `kwargs["litellm_metadata"]` with user metadata under `requester_metadata`

```python
user_meta = kwargs.get("metadata") or kwargs.get("litellm_metadata") or {}
rm = user_meta.get("requester_metadata") or {}
```

## Fire-and-forget

Redis operations are wrapped in `try/except` — if Redis is down, the record is silently dropped and the response proceeds normally. Eval is best-effort.

## Async/sync duality

```python
class RagasLogger(CustomLogger):
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        # shared sync logic

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        # delegates to sync logic
        self.log_success_event(kwargs, response_obj, start_time, end_time)
```

LiteLLM calls `async_log_success_event` for all requests (streaming + non-streaming). The async hook delegates to the shared sync logic.

## Registration

In `litellm_config.yaml`:
```yaml
litellm_settings:
  callbacks: ['proxy.callback.ragas_callback']
```

The `ragas_callback` module-level instance is imported by LiteLLM as a string reference.

## Skip logging

When a record is skipped (no question found), it logs:
```
WARNING  Skipping — absent or empty original_question in metadata. model=deepseek-pro
```

## Skill analytics propagation

Skill injection info is read from `skill_info_var` (set by `SkillInjectorMiddleware`) and attached to the eval record. This propagates through to the scored record in Redis, visible in the interactive score board.

## Test coverage

`tests/test_callback.py` — covers skip logic (ragas-eval prefix, metadata flag, router-resolved), question extraction from both sources, answer extraction from multiple formats, and record enqueue.
