# Data Models — GateMid

All persistent state lives in Redis. No SQL database, no migrations. Data is schema-less (Redis hashes) with TTL-based expiry.

## Eval data layout

### Queue: `eval:pending`

```
Type: List
Direction: LPUSH (enqueue) / BRPOP (dequeue)
TTL: None (ephemeral — consumed by worker)
```

**Enqueue format** (pushed by `RagasLogger` callback):
```json
{
  "call_id": "uuid4",
  "timestamp": "2026-06-30T14:23:05.123456+00:00",
  "question": "How do I refactor this service?",
  "answer": "Here's the refactored code...",
  "contexts": [],
  "ground_truth": "",
  "request_category": "general",
  "prompt_id": "default",
  "model": "deepseek-pro",
  "tokens_in": 1234,
  "tokens_out": 567,
  "skill_name": "ponytail, caveman",
  "skill_tokens_pre_compression": 850
}
```

### Scored record: `eval:call:{call_id}`

```
Type: Hash
TTL: 30 days (configurable via EVAL_RECORD_TTL_DAYS)
```

Fields:
| Field | Type | Description |
|-------|------|-------------|
| `call_id` | string | UUID v4 |
| `timestamp` | string | ISO 8601 UTC |
| `question` | string | First 2000 chars of user question |
| `answer` | string | First 4000 chars of LLM answer |
| `composite_score` | float | Weighted metric composite (0.0–1.0) |
| `scores_json` | string | JSON: `{"faithfulness": 0.85, "answer_relevancy": 0.72, ...}` |
| `model` | string | Model alias that generated the answer |
| `tokens_in` | string | Prompt token count |
| `tokens_out` | string | Completion token count |
| `request_category` | string | e.g. `general`, `fhir_query`, `code_qa` |
| `prompt_id` | string | Prompt version identifier |
| `skill_name` | string | Comma-separated skill names (if any) |
| `skill_tokens_pre_compression` | string | Total tokens of injected skills |

### Leaderboards: ZSets

```
eval:scores:all                ZSet  {call_id: composite_score}
eval:scores:cat:{category}     ZSet  {call_id: composite_score}
eval:scores:prompt:{prompt_id} ZSet  {call_id: composite_score}
```

- Scored on `composite_score` (float, 0.0–1.0)
- No TTL (IDs + scores only, lightweight)
- Queried with `ZRANGE` (ascending = worst first) or `ZREVRANGE` (best first)

### Metadata: `eval:meta:stats`

```
Type: Hash
TTL: None (running counters)
```

| Field | Type | Description |
|-------|------|-------------|
| `total_scored` | int | Cumulative count of scored calls |
| `last_scored_at` | string | ISO timestamp of most recent score |

## Headroom compression data layout

### Call record: `headroom:call:{call_id}`

```
Type: Hash
TTL: 30 days
```

| Field | Type | Description |
|-------|------|-------------|
| `call_id` | string | UUID v4 |
| `timestamp` | string | ISO 8601 UTC |
| `tokens_before` | string | Token count before compression |
| `tokens_after` | string | Token count after compression |
| `tokens_saved` | string | Tokens eliminated |
| `compression_ratio` | string | `tokens_saved / tokens_before` |
| `model` | string | Target model |
| `transforms_json` | string | JSON array of applied transforms |
| `prompt_before` | string | Full JSON of original messages (optional) |
| `prompt_after` | string | Full JSON of compressed messages (optional) |
| `skill_name` | string | Comma-separated skill names (if injected) |
| `skill_tokens_pre_compression` | string | Pre-compression token count of skills |

### Daily aggregate: `headroom:day:{YYYY-MM-DD}`

```
Type: Hash
TTL: 30 days
```

| Field | Type | Description |
|-------|------|-------------|
| `total_tokens_before` | float | Sum of pre-compression tokens |
| `total_tokens_after` | float | Sum of post-compression tokens |
| `total_tokens_saved` | float | Sum of saved tokens |
| `call_count` | int | Number of compressed calls |

### Index: `headroom:days`

```
Type: ZSet
Score: Unix timestamp of date
Member: "YYYY-MM-DD"
```

### Grand totals: `headroom:totals`

```
Type: Hash
TTL: None
```

| Field | Type | Description |
|-------|------|-------------|
| `total_tokens_before` | float | All-time tokens before compression |
| `total_tokens_after` | float | All-time tokens after compression |
| `total_tokens_saved` | float | All-time tokens saved |
| `total_calls` | int | All-time compressed call count |

## Data flow diagrams

### Eval scoring flow

```
RagasLogger.log_success_event()
  │
  ├─ Extracts: question, answer, contexts, metadata
  ├─ Attaches: skill info from context var (if any)
  └─ LPUSH eval:pending  ─────────────────────────────────────┐
                                                               │
eval_worker() loop                                             │
  ├─ BRPOP eval:pending (30s timeout)  ◄───────────────────────┘
  ├─ score_record() → Ragas evaluate()
  ├─ compute_composite() → weighted metric blend
  └─ write_scored_call()
       ├─ HSET eval:call:{call_id}
       ├─ ZADD eval:scores:all
       ├─ ZADD eval:scores:cat:{category}
       ├─ ZADD eval:scores:prompt:{prompt_id}
       └─ HINCRBY eval:meta:stats
```

### Compression stats flow

```
patched compress() in entrypoint.py
  │
  ├─ Calls Headroom's real compress()
  ├─ If tokens_saved > 0:
  └─ store_headroom_result()
       ├─ HSET headroom:call:{call_id}
       ├─ HINCRBYFLOAT headroom:day:{YYYY-MM-DD}
       ├─ ZADD headroom:days
       └─ HINCRBYFLOAT headroom:totals
```

## NaN / Inf handling

Redis ZADD rejects non-finite floats. All scores pass through `_sanitize()`:
```python
def _sanitize(val: float | None) -> float:
    if val is None: return 0.0
    if math.isnan(val) or math.isinf(val): return 0.0
    return val
```

## Numeric storage convention

Hash fields store numbers as strings (Redis hash values are always strings):
- `tokens_in`, `tokens_out`: `str(int)`
- `composite_score`: `float` (ZADD uses float directly)
- `scores_json`: JSON-stringified dict

Hydration converts back:
- `int(float(str_val))` for integer fields
- `float(str_val)` for float fields
- `json.loads(str_val)` for JSON fields

## Router session-switch data layout

### Session event log: `router:session:{session_key}`

```
Type: List
Direction: LPUSH (newest first) / LRANGE (read all)
TTL: 14 days (refreshed on each write)
```

**Event record format** (each List element is a JSON string):
```json
{
  "timestamp": "2026-07-15T14:23:05.123456+00:00",
  "model": "deepseek-pro",
  "previous_model": "gemini-flash",
  "seconds_since_last": 42.3,
  "hot_zone_tokens": 3400
}
```

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | string | ISO 8601 UTC |
| `model` | string | Model chosen by the router for this request |
| `previous_model` | string or null | Model chosen for the previous request in this session |
| `seconds_since_last` | float or null | Seconds since previous request (null for first) |
| `hot_zone_tokens` | int | Headroom-estimated hot-zone token count |

### Session metadata: `router:session:{session_key}:meta`

```
Type: Hash
TTL: 14 days (refreshed on each write)
```

| Field | Type | Description |
|-------|------|-------------|
| `latest_model` | string | Most recent model chosen |
| `latest_timestamp` | string | ISO timestamp of most recent event |
| `created_at` | string | ISO timestamp of first event |

### Day index: `router:session:days`

```
Type: ZSet
Score: Unix timestamp
Member: "YYYY-MM-DD"
TTL: None (lightweight index)
```

### Session fingerprint methods

1. **Method A** — Client-supplied session headers checked in order:
   - `x-headroom-session-id`
   - `x-session-id`
   - `x-conversation-id`
   If found, session key is `header:<value>`.

2. **Method B** — Structural fingerprint (fallback):
   `sha256(system_prompt[:500] + first_user_message[:500] + sorted(tool_names))[:16]`
   Session key is `fp:<16-char-hex>`.
