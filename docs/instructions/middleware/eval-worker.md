# Eval Worker: Async Ragas Scoring

**Entrypoint:** `eval/worker_main.py`
**Scoring logic:** `eval/worker.py`
**Redis layer:** `eval/redis_store.py`
**Embeddings:** `eval/gemini_embeddings.py`
**Type:** Separate Docker container (not middleware, but part of the data pipeline)

## Purpose

Consumes call records from the Redis `eval:pending` queue, runs Ragas quality metrics (faithfulness, answer relevancy, context precision, context recall), computes a weighted composite score, and writes results back to Redis. Runs as a standalone process isolated from the LiteLLM proxy so scoring never impacts request latency.

## Architecture

```
Redis eval:pending
      │
      ▼ (BRPOP, 30s timeout)
eval_worker() loop
      │
      ├─ score_record()
      │   ├─ Lazy import ragas + datasets (heavy deps)
      │   ├─ Configure metrics based on available data
      │   ├─ Build Dataset from single call record
      │   ├─ Run ragas.evaluate() with LLM-as-judge + embeddings
      │   ├─ Extract scores from result
      │   └─ Return enriched record
      │
      ├─ compute_composite()
      │   └─ Weighted blend of available metrics
      │
      └─ write_scored_call()
          ├─ HSET eval:call:{call_id}
          ├─ ZADD eval:scores:all
          ├─ ZADD eval:scores:cat:{category}
          ├─ ZADD eval:scores:prompt:{prompt_id}
          └─ HINCRBY eval:meta:stats
```

## Scoring metrics

### With contexts (RAG calls)

| Metric | Weight | What it measures | Requirements |
|--------|--------|-----------------|-------------|
| Faithfulness | 0.3 | Are answer claims grounded in context? | contexts |
| Answer Relevancy | 0.3 | How relevant is answer to question? | embeddings |
| Context Precision | 0.2 | Is context relevant to question? | contexts + ground_truth |
| Context Recall | 0.2 | Does context cover ground truth? | ground_truth |

Metric weights are dynamic — if a metric returns NaN or is unavailable (no ground_truth for context_recall), its weight is excluded and the denominator adjusts:
```python
total = sum(score[m] * weight[m] for m in available_metrics)
denom = sum(weight[m] for m in available_metrics)
composite = total / denom
```

### Without contexts (non-RAG calls)

| Metric | Weight |
|--------|--------|
| Answer Relevancy | 1.0 |

Faithfulness is skipped entirely — every claim would be trivially "unfaithful" against empty context. Composite = answer_relevancy alone.

### Faithfulness edge case (contexts without ground_truth)

When contexts exist but ground_truth doesn't:
- Faithfulness: 0.3 (scored)
- Answer Relevancy: 0.3
- Context Precision: skipped (needs ground_truth)
- Context Recall: skipped (needs ground_truth)
- Composite: weighted on faithfulness + answer_relevancy (denom = 0.6)

## LLM-as-Judge configuration

The eval worker creates an OpenAI-compatible client pointed at the LiteLLM proxy:

```python
client = OpenAI(base_url="http://litellm:4000/v1", api_key=gateway_master_key)
ragas_llm = llm_factory("ragas-eval", client=client, temperature=0.1, max_tokens=2048)
```

- Model alias `ragas-eval` resolves to `deepseek/deepseek-v4-flash` in `litellm_config.yaml`
- Temperature 0.1 for deterministic evaluation
- Max 2048 tokens for judge responses

## Embeddings configuration

```python
ragas_embeddings = GeminiEmbeddings(api_key=os.environ["GEMINI_API_KEY"])
```

Uses `models/gemini-embedding-001` via Gemini REST API (httpx, no PyTorch). Extends `BaseRagasEmbedding` directly — no deprecated `LangchainEmbeddingsWrapper`.

## Ragas version compat

Ragas 0.4.3 has a strictness warning: `InstructorLLM.generate()` always returns exactly ONE result regardless of `n`. The worker sets `strictness=1` on `answer_relevancy` to suppress the warning:

```python
if isinstance(answer_relevancy, ResponseRelevancy):
    answer_relevancy.strictness = 1
```

## NaN / Inf sanitization

Redis ZADD rejects non-finite floats. All metric scores pass through:
```python
def _sanitize(val):
    if val is None: return 0.0
    if math.isnan(val) or math.isinf(val): return 0.0
    return val
```

## Worker loop

```python
def eval_worker(once=False, llm=None, embeddings=None):
    while True:
        record = dequeue_call_record(timeout=30)  # BRPOP
        if record is None:  # timeout — queue empty
            if once: return
            continue

        scored = score_record(record, llm=llm, embeddings=embeddings)
        write_scored_call(scored)
        # log composite + remaining queue size

        if once: return
```

- `once=True` — process one record and return (test mode)
- `once=False` — infinite loop with heartbeat logging on empty queue
- Exponential backoff on Redis errors: 1s → 2s → 4s → ... → 60s max, resets on success

## Graceful disable

When `RAGAS_EVAL_ENABLED != "true"`, the worker exits immediately:

```python
if os.environ.get("RAGAS_EVAL_ENABLED", "").lower() != "true":
    logger.info("RAGAS_EVAL_ENABLED is not 'true' — eval worker is disabled.")
    sys.exit(0)
```

This allows running the gateway without eval (e.g., when Gemini API key is unavailable).

## VertexAI compat shims

Ragas 0.4.3 unconditionally imports `ChatVertexAI` / `VertexAI` from `langchain_community` at module load. Lightweight stubs are registered to prevent import errors without pulling in the Google Cloud SDK:

```python
for mod_name, cls_name in [
    ("langchain_community.chat_models.vertexai", "ChatVertexAI"),
    ("langchain_community.llms.vertexai", "VertexAI"),
]:
    mod = types.ModuleType(mod_name)
    setattr(mod, cls_name, type(cls_name, (), {}))
    sys.modules[mod_name] = mod
```

## Test coverage

`tests/test_ragas_runner.py` — tests `score_record()` and `compute_composite()` logic, including metric selection with/without contexts, NaN handling, and weight normalization.

`tests/test_redis_store.py` — tests queue operations, scored record persistence, leaderboard queries, and hydration.
