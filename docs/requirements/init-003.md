# PRD: Ragas Eval Layer — MVP

**Version:** 1.0.0 — Draft
**Date:** June 2026
**Status:** In Review
**Supersedes:** PRD v1.2.0 (DSPy full loop)

---

## 1. Executive Summary

This document defines the MVP scope for the Ragas Eval Layer — a lightweight, async quality-scoring pipeline that sits alongside the existing LiteLLM + Headroom ASGI proxy stack.

> **Decision:** Build the measurement layer first. Understand what is actually failing before automating the fix. DSPy prompt optimisation (PRD v1.2.0) is deferred until real Ragas data confirms the prompt is the root cause of quality degradation.

The MVP delivers three things:

1. Every LLM call is asynchronously logged — zero impact on response latency.
2. Ragas scores each call (faithfulness, answer relevancy, context precision) and writes the result to Redis.
3. A score view surfaces the top-20 worst and best scoring calls per rolling window for human review.

Nothing auto-promotes or auto-rewrites prompts. The output is signal — clean, scored, human-readable signal that makes the next decision (whether to run DSPy, fix retrieval, or tune chunking) evidence-based rather than assumed.

---

## 2. Background & Context

### 2.1 Existing Stack

| Component | Technology | Role |
|-----------|------------|------|
| LLM Proxy | LiteLLM + Docker Compose | Model routing, multi-provider failover, master key auth |
| Compression | Headroom ASGI middleware | Token compression on every request — reduces prompt size before forwarding to LiteLLM |
| Code Graph | understand-anything + VSCode ext. | Graph-based RAG context for FHIR/HL7 codebase queries |
| Eval (planned) | Ragas (this PRD) | Async quality scoring — faithfulness, relevancy, context precision |

### 2.2 The Gap

The current stack processes and compresses requests efficiently but has no visibility into whether the responses are actually good. There is no feedback signal: a hallucinated FHIR mapping, an off-topic code suggestion, or a faithfulness failure all look identical to the proxy — a `200 OK` and a token count.

PRD v1.2.0 defined a full self-optimising loop (Ragas → DSPy MIPROv2 → auto-promote). That architecture is sound but premature: it assumes the prompt is the primary quality lever, assumes `ground_truth` is available at call time, and assumes the Ragas baseline is already established. None of those assumptions have been validated against real production traffic.

> **Why MVP first:** Measurement before optimisation. The DSPy loop optimises the system prompt — but if `context_precision` is the consistently low metric, the bottleneck is retrieval, not the prompt. Building an automated optimisation cycle on an unvalidated assumption is wasteful. This MVP produces the evidence.

---

## 3. Goals & Non-Goals

### 3.1 Goals

- Capture every LLM call (prompt, response, retrieved context) asynchronously via LiteLLM callback — zero added latency.
- Score each call with Ragas composite metric: `faithfulness × 0.4 + answer_relevancy × 0.4 + context_precision × 0.2`.
- Persist scores and call metadata to Redis with a 30-day TTL — no heavyweight DB dependency for MVP.
- Surface worst-20 and best-20 calls per window via a simple query interface for human review.
- Capture per-metric breakdowns so the team can diagnose whether prompt, retrieval, or model behaviour is the root cause of failures.
- Tag each call with a `request_category` so score variance can be sliced by domain (`fhir_query`, `hl7_transform`, `code_qa`, `general`).

### 3.2 Non-Goals (deferred to v2)

| Out of Scope | Why Deferred |
|---|---|
| DSPy prompt auto-optimisation | Requires validated Ragas baseline and confirmed `ground_truth` pipeline — neither exists yet |
| Automatic prompt promotion / rollback | Premature without understanding score distribution and root cause |
| Real-time per-request scoring | Ragas uses an LLM-as-judge internally; synchronous scoring would add 1–3s to every response |
| Custom eval metrics beyond Ragas | Start with Ragas standard metrics; extend after baseline is established |
| `ground_truth` collection pipeline | Requires a labelling workflow; designed as a later phase add-on |
| PostgreSQL persistence | Redis is sufficient for MVP; migrate if retention or query complexity demands it |

---

## 4. System Architecture

### 4.1 Request Flow

The eval layer is entirely out-of-band. The hot path (user → Headroom → LiteLLM → model → response) is unchanged.

```
User Request
     │
     ▼
┌──────────────────────────────────────────────────┐
│  Headroom ASGI Middleware (token compression)    │
└──────────────────────┬───────────────────────────┘
                       │
                       ▼
┌──────────────────────────────────────────────────┐
│              LiteLLM Proxy                       │
│  • Model routing  • Master key auth              │
│  • RagasLogger callback (fires async, no block)  │
└──────────┬───────────────────────────────────────┘
           │                        │
           ▼                        ▼  (async — does NOT block response)
    Response → User         RagasLogger.log_success_event()
                                    │
                            ┌───────▼────────┐
                            │  Ragas Runner  │  scores call asynchronously
                            └───────┬────────┘
                                    │
                            ┌───────▼────────┐
                            │  Redis Writer  │  stores score + metadata
                            └───────┬────────┘
                                    │
                            ┌───────▼────────┐
                            │  Score View    │  worst-20 / best-20 per window
                            └────────────────┘
```

### 4.2 Component Summary

| Component | Technology | Responsibility |
|-----------|------------|----------------|
| RagasLogger | LiteLLM custom callback | Captures every call and enqueues for async scoring |
| Ragas Runner | Ragas + asyncio queue | Dequeues call records, runs metrics, writes to Redis |
| Redis Store | Redis (hash + sorted sets) | Persists scored call records with 30-day TTL; sorted sets for fast worst/best queries |
| Score View | Python CLI | Queries Redis for worst-20 and best-20 calls in a given window |

### 4.3 Headroom Integration Point

Headroom compresses the prompt before it reaches LiteLLM. This means the `question` logged by `RagasLogger` is the compressed form by default. The logger should also capture the original (pre-compression) question from request metadata if Headroom exposes it, so human reviewers see readable text in the score view.

> **Note:** If Headroom does not expose the original question in metadata, log the compressed question. Flag this as a known limitation. It does not block MVP delivery.

---

## 5. RagasLogger — LiteLLM Callback

### 5.1 Purpose

Intercepts every successful LiteLLM call via the custom callback interface. Writes a structured record to an in-process `asyncio.Queue`. Returns immediately — the scoring worker processes the queue independently.

### 5.2 Data Captured Per Call

| Field | Source | Required for Ragas | Notes |
|-------|--------|--------------------|-------|
| `question` | Last user message in `messages[]` | Yes | Use original if Headroom exposes it in metadata |
| `answer` | Model response content | Yes | |
| `contexts` | `metadata.retrieved_context` (list) | Yes | Empty list if no RAG context — context metrics will be 0.0 |
| `ground_truth` | `metadata.ground_truth` | Conditional | Optional in MVP — faithfulness + relevancy still run without it |
| `request_category` | `metadata.request_category` | No | Caller sets: `fhir_query \| hl7_transform \| code_qa \| general` |
| `model` | LiteLLM response object | No | Cost / model analytics |
| `tokens_in` | `response.usage.prompt_tokens` | No | Cost tracking |
| `tokens_out` | `response.usage.completion_tokens` | No | Cost tracking |
| `prompt_id` | `metadata.prompt_id` | No | Which system prompt was active — critical for future DSPy correlation |
| `timestamp` | System UTC | No | Used for time-window queries |

### 5.3 Implementation

```python
# proxy/callback.py
import asyncio, datetime, uuid
from litellm.integrations.custom_logger import CustomLogger
from eval.worker import eval_queue   # shared asyncio.Queue

class RagasLogger(CustomLogger):
    """
    Fire-and-forget callback. Writes to queue; worker does the heavy lifting.
    Zero impact on LiteLLM response latency.
    """

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        meta = kwargs.get('metadata', {})

        record = {
            'call_id':          str(uuid.uuid4()),
            'timestamp':        datetime.datetime.utcnow().isoformat(),
            'question':         meta.get('original_question')   # pre-compression if available
                                or kwargs['messages'][-1]['content'],
            'answer':           response_obj.choices[0].message.content,
            'contexts':         meta.get('retrieved_context', []),
            'ground_truth':     meta.get('ground_truth', ''),   # empty string = skip correctness
            'request_category': meta.get('request_category', 'general'),
            'prompt_id':        meta.get('prompt_id', 'default'),
            'model':            kwargs.get('model'),
            'tokens_in':        response_obj.usage.prompt_tokens,
            'tokens_out':       response_obj.usage.completion_tokens,
        }

        # Non-blocking enqueue — callback must return fast
        try:
            eval_queue.put_nowait(record)
        except asyncio.QueueFull:
            # Log dropped record — do not raise; never block the response path
            pass
```

### 5.4 Registration

Register in `litellm_config.yaml` — no other proxy changes required:

```yaml
# litellm_config.yaml
litellm_settings:
  callbacks: ['proxy.callback.RagasLogger']

  # Optional: expose original question before Headroom compression
  # Set this in your Headroom middleware config so it appears in metadata
  # metadata_key_original_question: 'original_question'
```

---

## 6. Ragas Eval Runner

### 6.1 Purpose

An async worker process that consumes the eval queue, runs Ragas scoring, and writes results to Redis. Runs as a separate process in Docker Compose — isolated from the proxy so a slow Ragas call never affects request throughput.

### 6.2 Metrics

| Metric | Weight | What It Measures | Requires `ground_truth`? |
|--------|--------|------------------|--------------------------|
| `faithfulness` | 40% | Are all claims grounded in the provided context? Primary hallucination detector. | No |
| `answer_relevancy` | 40% | Does the answer address the question asked? Penalises off-topic responses. | No |
| `context_precision` | 20% | Is the retrieved context actually relevant to the question? Measures retrieval quality. | No |

**Composite** = `faithfulness × 0.4 + answer_relevancy × 0.4 + context_precision × 0.2`

> **Ground truth note:** All three metrics above run without `ground_truth`. If `ground_truth` is present, `context_recall` is scored and logged as a bonus signal — it is not included in the composite score for MVP to avoid penalising calls where it was not set.

> **Missing context:** If `contexts` is empty (non-RAG calls), `context_precision` is skipped and the composite rebalances to `faithfulness × 0.5 + answer_relevancy × 0.5` to avoid artificially penalising non-RAG calls.

### 6.3 Implementation

```python
# eval/worker.py
import asyncio, json
from ragas import evaluate
from ragas.metrics import faithfulness, answer_relevancy, context_precision, context_recall
from datasets import Dataset
from eval.redis_store import write_scored_call

eval_queue: asyncio.Queue = asyncio.Queue(maxsize=500)

METRIC_WEIGHTS = {'faithfulness': 0.4, 'answer_relevancy': 0.4, 'context_precision': 0.2}
METRIC_WEIGHTS_NO_CTX = {'faithfulness': 0.5, 'answer_relevancy': 0.5}


def compute_composite(scores: dict, has_context: bool) -> float:
    weights = METRIC_WEIGHTS if has_context else METRIC_WEIGHTS_NO_CTX
    return sum(scores.get(m, 0.0) * w for m, w in weights.items())


def score_record(record: dict) -> dict:
    """Run Ragas on a single call record. Returns record enriched with scores."""
    has_ground_truth = bool(record.get('ground_truth'))
    has_context      = bool(record.get('contexts'))

    metrics = [faithfulness, answer_relevancy]
    if has_context:
        metrics.append(context_precision)
    if has_ground_truth:
        metrics.append(context_recall)

    dataset = Dataset.from_dict({
        'question':    [record['question']],
        'answer':      [record['answer']],
        'contexts':    [record['contexts'] or ['']],
        'ground_truth':[record.get('ground_truth', '')],
    })

    result = evaluate(dataset, metrics=metrics).to_pandas()
    row = result.iloc[0].to_dict()

    record['scores'] = {
        'faithfulness':      round(row.get('faithfulness', 0.0), 4),
        'answer_relevancy':  round(row.get('answer_relevancy', 0.0), 4),
        'context_precision': round(row.get('context_precision', 0.0), 4) if has_context else None,
        'context_recall':    round(row.get('context_recall', 0.0), 4) if has_ground_truth else None,
    }
    record['composite_score'] = round(compute_composite(record['scores'], has_context), 4)
    return record


async def eval_worker():
    """Runs indefinitely — consumes queue, scores, writes to Redis."""
    print('[eval_worker] started')
    while True:
        record = await eval_queue.get()
        try:
            scored = score_record(record)
            write_scored_call(scored)
        except Exception as e:
            print(f'[eval_worker] scoring error for {record["call_id"]}: {e}')
        finally:
            eval_queue.task_done()
```

---

## 7. Redis Data Store

### 7.1 Why Redis

Redis is already a common dependency in the LiteLLM ecosystem and is trivially added to Docker Compose. For MVP purposes — storing scored call records with TTL, sorted lookups for worst/best — it is more than sufficient. No schema migrations, no connection pool management, no query planner.

If retention requirements grow beyond 30 days, or if the team needs complex SQL aggregations, the Redis store can be replaced or supplemented with Postgres in v2 without changing any upstream code — `redis_store.py` is the only file that changes.

### 7.2 Data Layout

| Redis Key Pattern | Type | Content | TTL |
|-------------------|------|---------|-----|
| `eval:call:{call_id}` | Hash | Full scored record — all fields from section 5.2 plus `scores` dict and `composite_score` | 30 days |
| `eval:scores:all` | Sorted Set | `call_id → composite_score`. Enables O(log N) top/bottom-N queries across all categories. | None |
| `eval:scores:cat:{category}` | Sorted Set | `call_id → composite_score`, scoped to `request_category`. | None |
| `eval:scores:prompt:{prompt_id}` | Sorted Set | `call_id → composite_score`, scoped to `prompt_id`. Enables per-prompt quality tracking. | None |
| `eval:meta:stats` | Hash | Running counters: `total_calls`, `total_scored`, `last_scored_at` | None |

### 7.3 Implementation

```python
# eval/redis_store.py
import redis, json, os

REDIS_URL   = os.getenv('REDIS_URL', 'redis://localhost:6379')
TTL_SECONDS = 30 * 24 * 3600   # 30-day retention

r = redis.from_url(REDIS_URL, decode_responses=True)


def write_scored_call(record: dict):
    call_id   = record['call_id']
    score     = record['composite_score']
    category  = record.get('request_category', 'general')
    prompt_id = record.get('prompt_id', 'default')

    # 1. Store full record as hash
    key = f'eval:call:{call_id}'
    r.hset(key, mapping={
        'call_id':          call_id,
        'timestamp':        record['timestamp'],
        'question':         record['question'][:2000],   # truncate for storage
        'answer':           record['answer'][:4000],
        'composite_score':  score,
        'scores_json':      json.dumps(record['scores']),
        'model':            record.get('model', ''),
        'tokens_in':        record.get('tokens_in', 0),
        'tokens_out':       record.get('tokens_out', 0),
        'request_category': category,
        'prompt_id':        prompt_id,
    })
    r.expire(key, TTL_SECONDS)

    # 2. Add to sorted sets
    r.zadd('eval:scores:all',                  {call_id: score})
    r.zadd(f'eval:scores:cat:{category}',      {call_id: score})
    r.zadd(f'eval:scores:prompt:{prompt_id}',  {call_id: score})

    # 3. Update running stats
    r.hincrby('eval:meta:stats', 'total_scored', 1)
    r.hset('eval:meta:stats', 'last_scored_at', record['timestamp'])


def get_worst_calls(n: int = 20, category: str = None, prompt_id: str = None) -> list[dict]:
    """Return n lowest-scoring call records."""
    key = _resolve_key(category, prompt_id)
    call_ids = r.zrange(key, 0, n - 1)   # ascending = lowest scores first
    return [_hydrate(cid) for cid in call_ids if cid]


def get_best_calls(n: int = 20, category: str = None, prompt_id: str = None) -> list[dict]:
    """Return n highest-scoring call records."""
    key = _resolve_key(category, prompt_id)
    call_ids = r.zrange(key, 0, n - 1, rev=True)   # descending = highest first
    return [_hydrate(cid) for cid in call_ids if cid]


def _resolve_key(category, prompt_id):
    if prompt_id:
        return f'eval:scores:prompt:{prompt_id}'
    if category:
        return f'eval:scores:cat:{category}'
    return 'eval:scores:all'


def _hydrate(call_id: str) -> dict:
    raw = r.hgetall(f'eval:call:{call_id}')
    if raw and 'scores_json' in raw:
        raw['scores'] = json.loads(raw['scores_json'])
        del raw['scores_json']
    return raw
```

---

## 8. Score View — Worst / Best Calls

### 8.1 Purpose

The primary human-facing output of the MVP. Returns the 20 worst and 20 best scoring calls for a given window, optionally filtered by `request_category` or `prompt_id`. Output is JSON (for downstream tooling) or a formatted table (for CLI review).

### 8.2 Implementation

```python
# eval/score_view.py
import json, argparse
from eval.redis_store import get_worst_calls, get_best_calls


def print_table(calls: list[dict], label: str):
    print(f'\n{"─"*80}')
    print(f'  {label} ({len(calls)} calls)')
    print(f'{"─"*80}')
    print(f'  {"Score":>6}  {"Category":14}  {"Model":16}  {"Question (truncated)"}')
    print(f'{"─"*80}')
    for c in calls:
        score = float(c.get('composite_score', 0))
        cat   = c.get('request_category', 'general')[:14]
        model = c.get('model', '')[:16]
        q     = c.get('question', '')[:55]
        print(f'  {score:>6.3f}  {cat:14}  {model:16}  {q}')
    print()


def main():
    parser = argparse.ArgumentParser(description='Ragas score view')
    parser.add_argument('--category',  help='Filter by request_category')
    parser.add_argument('--prompt-id', help='Filter by prompt_id')
    parser.add_argument('--n',         type=int, default=20, help='Number of calls per bucket')
    parser.add_argument('--json',      action='store_true', help='Output JSON instead of table')
    args = parser.parse_args()

    worst = get_worst_calls(args.n, args.category, args.prompt_id)
    best  = get_best_calls(args.n,  args.category, args.prompt_id)

    if args.json:
        print(json.dumps({'worst': worst, 'best': best}, indent=2))
    else:
        print_table(worst, '⚠  WORST SCORING CALLS')
        print_table(best,  '✓  BEST SCORING CALLS')


if __name__ == '__main__':
    main()
```

### 8.3 Example CLI Usage

```bash
# All categories, default 20 per bucket
python -m eval.score_view

# FHIR queries only
python -m eval.score_view --category fhir_query

# Specific prompt version
python -m eval.score_view --prompt-id v2_system_prompt

# JSON output for dashboarding
python -m eval.score_view --json > scores.json
```

---

## 9. Docker Compose Integration

### 9.1 New Services

Two new services added to the existing stack. The `litellm-proxy` and Headroom services are unchanged.

```yaml
# docker-compose.yml (additions only)

services:

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    ports:
      - '6379:6379'
    volumes:
      - redis_data:/data
    command: redis-server --save 60 1 --loglevel warning

  eval-worker:
    build:
      context: .
      dockerfile: eval/Dockerfile
    restart: unless-stopped
    environment:
      - REDIS_URL=redis://redis:6379
      - OPENAI_API_KEY=${OPENAI_API_KEY}
      - RAGAS_EVAL_MODEL=${RAGAS_EVAL_MODEL:-gpt-4o-mini}
    depends_on:
      - redis
      - litellm-proxy
    volumes:
      - ./eval:/app/eval

volumes:
  redis_data:
```

### 9.2 eval/Dockerfile

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements-eval.txt .
RUN pip install --no-cache-dir -r requirements-eval.txt
COPY . .
CMD ["python", "-m", "eval.worker_main"]
```

### 9.3 requirements-eval.txt

```
ragas>=0.1.0
datasets>=2.14.0
redis>=5.0.0
openai>=1.0.0
```

---

## 10. Project Structure

```
llm-proxy/
├── docker-compose.yml              # +redis +eval-worker services
├── litellm_config.yaml             # +RagasLogger callback registration
│
├── proxy/
│   └── callback.py                 # NEW — RagasLogger custom callback
│
├── eval/
│   ├── Dockerfile                  # NEW — eval-worker container
│   ├── worker.py                   # NEW — asyncio queue + score_record()
│   ├── worker_main.py              # NEW — entrypoint: starts eval_worker()
│   ├── redis_store.py              # NEW — Redis read/write helpers
│   └── score_view.py               # NEW — CLI: worst/best call query
│
├── tests/
│   ├── test_callback.py            # NEW
│   ├── test_ragas_runner.py        # NEW
│   └── test_redis_store.py         # NEW
│
└── .env.example                    # +REDIS_URL, +RAGAS_EVAL_MODEL
```

---

## 11. Environment Variables

| Variable | Default | Required | Notes |
|----------|---------|----------|-------|
| `REDIS_URL` | `redis://localhost:6379` | Yes | Redis connection string |
| `RAGAS_EVAL_MODEL` | `gpt-4o-mini` | Yes | Model used by Ragas LLM-as-judge. Use cheapest model that gives reliable scores. |
| `OPENAI_API_KEY` | — | Yes | Used by Ragas eval model. Route through LiteLLM proxy if `LITELLM_BASE_URL` is set. |
| `LITELLM_BASE_URL` | — | Recommended | Route Ragas eval calls through your own proxy to track eval costs separately. |
| `EVAL_QUEUE_MAXSIZE` | `500` | No | Max in-memory queue depth before drops. Increase if high traffic. |
| `EVAL_RECORD_TTL_DAYS` | `30` | No | Redis key TTL for scored call records. |
| `EVAL_CALL_TRUNCATE_CHARS` | `2000 / 4000` | No | Truncation limits for question / answer stored in Redis hash. |

---

## 12. Safety & Operational Guardrails

| Guardrail | Behaviour |
|-----------|-----------|
| Queue overflow protection | `asyncio.Queue` has `maxsize=500`. If the eval worker falls behind, `put_nowait()` silently drops records rather than blocking the proxy. Drops are counted in `eval:meta:stats`. |
| Ragas eval model cost control | Route eval calls through LiteLLM so they appear in usage logs. Use `gpt-4o-mini` as default. Each Ragas faithfulness call makes 2–3 LLM sub-calls — budget ~$0.0003 per scored call at gpt-4o-mini rates. |
| No `ground_truth` required | Composite score uses only faithfulness + answer_relevancy + context_precision. Missing `ground_truth` does not block scoring. |
| Missing context handling | If `contexts` is empty, `context_precision` is skipped and composite rebalances to `faithfulness × 0.5 + answer_relevancy × 0.5` to avoid penalising non-RAG calls. |
| 30-day Redis TTL | All `eval:call:{id}` keys expire automatically. `_hydrate()` returns empty dict for expired members; `score_view` skips them. |
| Data privacy | `question` and `answer` are truncated before writing to Redis. If calls contain patient data, add a PII stripping hook in the callback before enqueuing. |

---

## 13. Success Metrics

| Metric | Target | How Measured |
|--------|--------|--------------|
| Callback latency overhead | < 1ms added to p99 response time | LiteLLM response time logs before/after callback registration |
| Eval worker queue depth | < 50 at steady state | `LLEN eval:queue` monitored in Redis |
| Scoring throughput | > 95% of calls scored within 60s | Timestamp diff: `record.timestamp` vs Redis write time |
| Dropped records | < 0.1% of total calls | `eval:meta:stats.total_dropped / total_calls` |
| Worst-20 call review | Weekly human review session conducted | Team ritual — calendar entry |
| Ragas eval cost per call | < $0.001 per scored call | LiteLLM usage logs filtered by `prompt_id=ragas_eval` |
| Baseline established | 7-day rolling avg available within 14 days of deployment | `eval:meta:stats` populated |

---

## 14. Milestones & Timeline

| Phase | Duration | Deliverables | Exit Criteria |
|-------|----------|--------------|---------------|
| Phase 1 | Week 1 | RagasLogger callback + asyncio queue + unit tests | Callback fires on every LiteLLM success event; queue enqueues without blocking |
| Phase 2 | Week 1–2 | Ragas Runner (`score_record`) + Redis writer + Docker Compose redis service | Single call scored end-to-end; record visible in Redis hash |
| Phase 3 | Week 2 | eval-worker Docker service + `worker_main` entrypoint + integration test | Full flow: proxy call → queue → score → Redis write in Docker Compose |
| Phase 4 | Week 2–3 | `score_view` CLI (worst/best 20) + category + prompt_id filters | Reviewer can query worst-20 calls by category from command line |
| Phase 5 | Week 3 | 7-day baseline burn-in on real traffic + first human review session | `avg_composite_7d` populated; first worst/best review completed; root cause hypothesis documented |

> **Gate to v2:** DSPy optimisation (PRD v1.2.0) is gated on Phase 5 output. The team must review the Ragas data, identify whether the prompt or retrieval is the primary failure mode, and confirm `ground_truth` strategy before v2 begins.

---

## 15. Open Questions

| # | Question | Owner | Resolution Needed By |
|---|----------|-------|----------------------|
| 1 | Does Headroom expose the pre-compression question in metadata, or does the callback see only the compressed text? | Hoang | Phase 1 |
| 2 | Which callers will set `request_category` in metadata? Does this need middleware enforcement or a fallback default? | Tech Lead | Phase 1 |
| 3 | Are any prompt contexts patient-identifiable? If yes, what stripping logic is needed before enqueuing? | Compliance | Phase 1 |
| 4 | Should Ragas eval model calls route through the LiteLLM proxy (for cost tracking) or direct to OpenAI? | Hoang | Phase 2 |
| 5 | What is the expected call volume? At >500 calls/hour the default queue `maxsize` may need tuning. | Hoang | Phase 2 |
| 6 | After Phase 5 review: is the prompt or the retrieval layer the primary driver of low scores? This gates v2 direction. | Team | Phase 5 |

---

## 16. Risks & Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Ragas eval model cost unexpectedly high at production call volume | Medium | Route eval calls through LiteLLM proxy to track cost. Cap queue depth. Sample if cost exceeds budget. |
| Headroom-compressed questions make Ragas scores less meaningful | Medium | Log both compressed and original question if available. Annotate score view. Does not block MVP. |
| PII in call logs — question/answer contain patient identifiers | High | Add PII stripping hook in callback before enqueue. Compliance review required before Phase 1 completion. |
| Redis data loss on restart (no AOF persistence) | Low | Enable `redis --save` in docker-compose. For MVP, losing a few hours of scores is acceptable; full AOF persistence is a v2 hardening task. |
| Ragas LLM-as-judge reliability on FHIR/HL7 domain language | Medium | Manually spot-check a sample of scored calls in Phase 5 to validate that Ragas scores correlate with human quality judgement on domain-specific outputs. |

---

## Appendix A: Ragas Metrics Reference

| Metric | What It Measures |
|--------|-----------------|
| `faithfulness` | Are all claims in the answer grounded in the provided context? Catches hallucination. |
| `answer_relevancy` | Does the answer address the question asked? Penalises off-topic responses. |
| `context_precision` | Is the retrieved context relevant to the question? Measures retrieval quality. |
| `context_recall` | Does the context contain enough to answer? Measures retrieval completeness. (Requires `ground_truth` — bonus metric only in MVP.) |

---

## Appendix B: Request Category Reference

Callers set `request_category` in LiteLLM request metadata. Suggested values for the healthcare dev team stack:

| Category | Description |
|----------|-------------|
| `fhir_query` | Questions about FHIR resource structure, mapping, or validation |
| `hl7_transform` | HL7 v2 / v3 message parsing, segment mapping, transformation |
| `code_qa` | General codebase Q&A via the understand-anything graph RAG layer |
| `general` | Catch-all for uncategorised requests |

This categorisation is the primary lens for Phase 5 analysis — score variance by category is the fastest way to identify whether quality problems are domain-specific.

---

## Appendix C: Environment Variables — Full Reference

```env
# Redis
REDIS_URL=redis://localhost:6379
EVAL_RECORD_TTL_DAYS=30
EVAL_QUEUE_MAXSIZE=500

# Ragas eval model
RAGAS_EVAL_MODEL=gpt-4o-mini
OPENAI_API_KEY=...

# Optional: route eval calls through proxy for cost tracking
LITELLM_BASE_URL=http://localhost:4000
LITELLM_MASTER_KEY=...

# Truncation limits for Redis storage
EVAL_QUESTION_TRUNCATE_CHARS=2000
EVAL_ANSWER_TRUNCATE_CHARS=4000
```

---

*End of document — Ragas Eval Layer MVP PRD v1.0.0*