"""Ragas Eval Worker — async scoring loop.

Consumes call records from the Redis eval:pending queue, runs Ragas metrics
(faithfulness, answer_relevancy, context_precision), computes a composite score,
and writes the result back to Redis.

Runs as a standalone process in the eval-worker Docker container, isolated from
the LiteLLM proxy so that a slow Ragas call never affects request throughput.

Loop prevention:
    Ragas internally calls an LLM-as-judge. Those calls are routed through the
    LiteLLM proxy (via OPENAI_BASE_URL) for cost tracking, but they use the
    model alias "ragas-eval" which the RagasLogger callback skips to prevent
    an infinite scoring loop.
"""

import json
import logging
import math
import time

from eval.redis_store import (
    dequeue_call_record,
    queue_length,
    write_scored_call,
)

logger = logging.getLogger("eval.worker")

# ── Metric weights ────────────────────────────────────────────────────────────
# Composite = faithfulness × 0.4 + answer_relevancy × 0.4 + context_precision × 0.2
# When contexts is empty (non-RAG calls), context_precision is skipped and the
# composite rebalances to faithfulness × 0.5 + answer_relevancy × 0.5.

METRIC_WEIGHTS = {
    "faithfulness": 0.3,
    "answer_relevancy": 0.3,
    "context_precision": 0.2,
    "context_recall": 0.2,
}
METRIC_WEIGHTS_NO_CTX = {
    "answer_relevancy": 1.0,
}


def _sanitize(val: float | None) -> float:
    """Replace NaN / inf / None with 0.0 — Redis ZADD rejects non-finite floats."""
    if val is None:
        return 0.0
    if math.isnan(val) or math.isinf(val):
        return 0.0
    return val


def compute_composite(scores: dict, has_context: bool) -> float:
    """Weighted composite of available Ragas metric scores, normalised to [0, 1].

    With context:
        * faithfulness  × 0.3
        * answer_relevancy × 0.3
        * context_precision × 0.2
        * context_recall × 0.2

    When ground truth is absent *context_recall* will be None — the weights
    are re-normalised dynamically so the composite still maxes at 1.0.

    Without context:
        * answer_relevancy × 1.0
    """
    weights = METRIC_WEIGHTS if has_context else METRIC_WEIGHTS_NO_CTX
    total = 0.0
    denom = 0.0
    for m, w in weights.items():
        v = scores.get(m)
        if v is not None and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
            total += v * w
            denom += w
    return total / denom if denom > 0 else 0.0


def score_record(record: dict, llm=None, embeddings=None) -> dict:
    """Run Ragas metrics on a single call record.

    Ragas is imported lazily (inside this function) to avoid heavy
    import-time dependency chains in the LiteLLM proxy process and
    to simplify test mocking.

    Parameters
    ----------
    record : dict
        Call record with question, answer, contexts, etc.
    llm : optional
        Pre-configured Ragas LLM instance. If None, Ragas will create a
        default (which defaults to gpt-4o-mini).
    embeddings : optional
        Pre-configured Ragas embeddings instance. Needed for metrics like
        answer_relevancy that compute cosine similarity in embedding space.
        If None, metrics that require embeddings will return NaN.

    Returns the record enriched with a ``scores`` dict and ``composite_score``.
    """
    # Lazy import — ragas pulls in many transitive deps
    from datasets import Dataset
    from ragas import evaluate
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )
    from ragas.metrics._answer_relevance import ResponseRelevancy

    has_ground_truth = bool(record.get("ground_truth"))
    has_context = bool(record.get("contexts"))

    # ── Metric configuration ────────────────────────────────────────────────────
    # Ragas 0.4.3 llm_factory() creates an InstructorLLM for OpenAI-compatible
    # clients.  InstructorLLM.generate() always returns exactly ONE result
    # regardless of the `n` parameter, so asking for strictness=3 (default)
    # generates the warning "LLM returned 1 generations instead of requested 3".
    #
    # Setting strictness=1 matches what InstructorLLM can actually produce and
    # eliminates the warning.  DeepSeek-Flash also tends to return empty JSON
    # for the question-generation prompt; a single valid question is sufficient
    # for answer_relevancy scoring.
    #
    # See https://github.com/vibrantlabsai/ragas/issues for upstream tracking.
    # Reduce from default strictness=3 — InstructorLLM only ever returns 1 gen
    if isinstance(answer_relevancy, ResponseRelevancy):
        answer_relevancy.strictness = 1

    # Build the metric list. Faithfulness requires retrieved_contexts to
    # judge whether the answer's claims can be inferred. When the call has
    # no contexts, every claim is trivially "unfaithful" against an empty
    # context, so faithfulness is skipped and the composite rebalances to
    # answer_relevancy alone (METRIC_WEIGHTS_NO_CTX).
    metrics = [answer_relevancy]
    if has_context:
        metrics.insert(0, faithfulness)
        if has_ground_truth:
            metrics.append(context_precision)
    if has_ground_truth:
        metrics.append(context_recall)

    # Ragas expects a dataset with at least one row
    dataset = Dataset.from_dict({
        "question":    [record["question"]],
        "answer":      [record["answer"]],
        "contexts":    [record.get("contexts") or [""]],
        "ground_truth": [record.get("ground_truth", "")],
    })

    result = evaluate(dataset, metrics=metrics, llm=llm,
                      embeddings=embeddings)
    # evaluate() returns a Result object; convert via .to_pandas() for row access
    row = result.to_pandas().iloc[0].to_dict()

    record["scores"] = {
        "faithfulness":      _sanitize(round(float(row.get("faithfulness", 0.0)), 4))
                             if has_context else None,
        "answer_relevancy":  _sanitize(round(float(row.get("answer_relevancy", 0.0)), 4)),
        "context_precision": _sanitize(round(float(row.get("context_precision", 0.0)), 4))
                             if has_context and has_ground_truth else None,
        "context_recall":    _sanitize(round(float(row.get("context_recall", 0.0)), 4))
                             if has_ground_truth else None,
    }
    record["composite_score"] = round(
        compute_composite(record["scores"], has_context), 4
    )
    return record


def eval_worker(once: bool = False, llm=None, embeddings=None):
    """Main worker loop.

    BLPOPs call records from the Redis queue, scores them, and writes results.
    Set ``once=True`` to process a single record and return (useful for tests).

    Parameters
    ----------
    once : bool
        Process one record and return (test mode).
    llm : optional
        Pre-configured Ragas LLM instance. Passed through to score_record().
        Create one with ``ragas.llms.llm_factory(model, client=openai.Client(...))``.
    embeddings : optional
        Pre-configured Ragas embeddings instance. Needed for metrics like
        answer_relevancy. Passed through to score_record().
    """
    logger.info("Eval worker started — waiting for call records")
    if embeddings is None:
        logger.warning("embeddings is None — answer_relevancy "
                       "(%.0f%%+ of composite) will score as 0.0",
                       max(w * 100 for w in METRIC_WEIGHTS.values()))
    retry_delay = 1
    while True:
        try:
            record = dequeue_call_record(timeout=30)
        except Exception:
            logger.exception("Redis connection error — retrying in %ds", retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 60)
            continue
        retry_delay = 1

        if record is None:
            # Timeout — queue is empty; log a heartbeat and keep polling
            logger.debug("No pending records (queue empty)")
            if once:
                return
            continue

        call_id = record.get("call_id", "unknown")
        logger.info("Scoring call %s (model=%s, category=%s)",
                     call_id, record.get("model"), record.get("request_category"))

        try:
            scored = score_record(record, llm=llm, embeddings=embeddings)
            write_scored_call(scored)
            pending = queue_length()
            logger.info(
                "Scored call %s — composite=%.4f (faith=%s, relev=%.4f, "
                "ctx_prec=%s) — %d remaining in queue",
                call_id,
                scored["composite_score"],
                scored["scores"].get("faithfulness", "N/A") or "N/A",
                scored["scores"].get("answer_relevancy", 0.0),
                scored["scores"].get("context_precision", "N/A") or "N/A",
                pending,
            )
        except Exception as exc:
            logger.exception("Scoring failed for call %s: %s", call_id, exc)

        if once:
            return
