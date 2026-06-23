"""Redis data store for the Ragas Eval Layer.

Data layout:
  eval:pending          List    — Call records awaiting scoring (LPUSH / BLPOP)
  eval:call:{call_id}   Hash    — Full scored record
  eval:scores:all       ZSet    — call_id → composite_score (global ranking)
  eval:scores:cat:{cat} ZSet    — call_id → composite_score (by category)
  eval:scores:prompt:{pid} ZSet — call_id → composite_score (by prompt_id)
  eval:meta:stats       Hash    — Running counters

TTL: 30 days on eval:call:{call_id} hashes.
     Sorted sets are kept indefinitely (lightweight, just IDs + scores).
     Deleted records show up as missing during hydration.
"""

import json
import logging
import os

import redis

logger = logging.getLogger("eval.redis_store")

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")
TTL_SECONDS = int(os.getenv("EVAL_RECORD_TTL_DAYS", "30")) * 24 * 3600
QUEUE_KEY = "eval:pending"

r = redis.from_url(
    REDIS_URL,
    decode_responses=True,
    retry_on_timeout=True,
    health_check_interval=30,
    socket_keepalive=True,
    socket_timeout=None,  # required for brpop — default is 5s in redis-py 8.x
)


# ── Queue helpers (used by both the callback and the worker) ──────────────────

def enqueue_call_record(record: dict) -> None:
    """Push a raw (unscored) call record onto the eval pending queue."""
    r.lpush(QUEUE_KEY, json.dumps(record))


def dequeue_call_record(timeout: int = 30) -> dict | None:
    """BLPOP a pending call record. Returns None if timeout expires."""
    result = r.brpop(QUEUE_KEY, timeout=timeout)
    if result is None:
        return None
    # brpop returns (key, value)
    return json.loads(result[1])


def queue_length() -> int:
    """Return the number of pending (unscored) call records."""
    return r.llen(QUEUE_KEY)


# ── Write helpers (used by the eval worker) ───────────────────────────────────

def write_scored_call(record: dict) -> None:
    """Persist a scored call record to Redis hash + sorted sets."""
    call_id = record["call_id"]
    score = record["composite_score"]
    category = record.get("request_category", "general")
    prompt_id = record.get("prompt_id", "default")

    # 1. Full record as hash
    key = f"eval:call:{call_id}"
    r.hset(key, mapping={
        "call_id":          call_id,
        "timestamp":        record["timestamp"],
        "question":         record["question"][:2000],
        "answer":           record["answer"][:4000],
        "composite_score":  score,
        "scores_json":      json.dumps(record["scores"]),
        "model":            record.get("model", ""),
        "tokens_in":        str(record.get("tokens_in", 0)),
        "tokens_out":       str(record.get("tokens_out", 0)),
        "request_category": category,
        "prompt_id":        prompt_id,
    })
    r.expire(key, TTL_SECONDS)

    # 2. Sorted sets for fast ranking queries
    r.zadd("eval:scores:all", {call_id: score})
    r.zadd(f"eval:scores:cat:{category}", {call_id: score})
    r.zadd(f"eval:scores:prompt:{prompt_id}", {call_id: score})

    # 3. Running stats
    r.hincrby("eval:meta:stats", "total_scored", 1)
    r.hset("eval:meta:stats", "last_scored_at", record["timestamp"])


# ── Read helpers (used by score_view) ─────────────────────────────────────────

def get_worst_calls(n: int = 20, category: str = None,
                    prompt_id: str = None) -> list[dict]:
    """Return n lowest-scoring call records (ascending = worst first)."""
    key = _resolve_key(category, prompt_id)
    call_ids = r.zrange(key, 0, n - 1)
    return [_hydrate(cid) for cid in call_ids if cid and r.exists(f"eval:call:{cid}")]


def get_best_calls(n: int = 20, category: str = None,
                   prompt_id: str = None) -> list[dict]:
    """Return n highest-scoring call records (descending = best first)."""
    key = _resolve_key(category, prompt_id)
    call_ids = r.zrevrange(key, 0, n - 1)
    return [_hydrate(cid) for cid in call_ids if cid and r.exists(f"eval:call:{cid}")]


def _resolve_key(category: str | None, prompt_id: str | None) -> str:
    if prompt_id:
        return f"eval:scores:prompt:{prompt_id}"
    if category:
        return f"eval:scores:cat:{category}"
    return "eval:scores:all"


# ── Admin / teardown helpers ──────────────────────────────────────────────────

def flush_eval() -> int:
    """Delete ALL eval:* keys from Redis and return the count of removed keys.

    Uses SCAN so it won't block Redis even with large datasets.
    Call this when you want a clean slate for testing.
    """
    deleted = 0
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match="eval:*", count=1000)
        if keys:
            deleted += r.delete(*keys)
        if cursor == 0:
            break
    logger.info("Flushed %d eval:* keys from Redis", deleted)
    return deleted


# ── CLI entry point: python -m eval.redis_store ─────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    count = flush_eval()
    print(f"Done. Removed {count} eval:* keys from {REDIS_URL}")


def _hydrate(call_id: str) -> dict | None:
    raw = r.hgetall(f"eval:call:{call_id}")
    if not raw:
        return None
    if "scores_json" in raw:
        raw["scores"] = json.loads(raw["scores_json"])
        del raw["scores_json"]
    # Cast numeric fields back from strings
    for field in ("composite_score", "tokens_in", "tokens_out"):
        if field in raw:
            raw[field] = float(raw[field]) if "." in str(raw[field]) else int(raw[field])
    return raw


# ── Headroom compression storage ─────────────────────────────────────────────

HEADROOM_TTL = TTL_SECONDS  # Same 30-day TTL as eval records
HEADROOM_CALL_PREFIX = "headroom:call:"
HEADROOM_DAY_PREFIX = "headroom:day:"
HEADROOM_DAYS_KEY = "headroom:days"
HEADROOM_TOTALS_KEY = "headroom:totals"


def store_headroom_result(
    call_id: str,
    timestamp: str,
    tokens_before: int,
    tokens_after: int,
    tokens_saved: int,
    compression_ratio: float,
    model: str = "",
    transforms_applied: list[str] | None = None,
    prompt_before: str | None = None,
    prompt_after: str | None = None,
) -> None:
    """Persist a single compression result to Redis (fire-and-forget)."""
    day_str = timestamp[:10]  # YYYY-MM-DD

    # 1. Individual call record
    mapping = {
        "call_id": call_id,
        "timestamp": timestamp,
        "tokens_before": str(tokens_before),
        "tokens_after": str(tokens_after),
        "tokens_saved": str(tokens_saved),
        "compression_ratio": str(compression_ratio),
        "model": model,
        "transforms_json": json.dumps(transforms_applied or []),
    }
    if prompt_before is not None:
        mapping["prompt_before"] = prompt_before
    if prompt_after is not None:
        mapping["prompt_after"] = prompt_after
    r.hset(f"{HEADROOM_CALL_PREFIX}{call_id}", mapping=mapping)
    r.expire(f"{HEADROOM_CALL_PREFIX}{call_id}", HEADROOM_TTL)

    # 2. Daily aggregate
    r.hincrbyfloat(f"{HEADROOM_DAY_PREFIX}{day_str}", "total_tokens_before", tokens_before)
    r.hincrbyfloat(f"{HEADROOM_DAY_PREFIX}{day_str}", "total_tokens_after", tokens_after)
    r.hincrbyfloat(f"{HEADROOM_DAY_PREFIX}{day_str}", "total_tokens_saved", tokens_saved)
    r.hincrby(f"{HEADROOM_DAY_PREFIX}{day_str}", "call_count", 1)
    r.expire(f"{HEADROOM_DAY_PREFIX}{day_str}", HEADROOM_TTL)

    # 3. Days index (for listing latest N days)
    import time
    from datetime import datetime, timezone
    try:
        day_ts = datetime.strptime(day_str, "%Y-%m-%d").replace(
            tzinfo=timezone.utc,
        ).timestamp()
    except ValueError:
        day_ts = time.time()
    r.zadd(HEADROOM_DAYS_KEY, {day_str: day_ts})

    # 4. Running totals
    r.hincrbyfloat(HEADROOM_TOTALS_KEY, "total_tokens_before", tokens_before)
    r.hincrbyfloat(HEADROOM_TOTALS_KEY, "total_tokens_after", tokens_after)
    r.hincrbyfloat(HEADROOM_TOTALS_KEY, "total_tokens_saved", tokens_saved)
    r.hincrby(HEADROOM_TOTALS_KEY, "total_calls", 1)


def get_daily_headroom_stats(n_days: int = 10) -> list[dict]:
    """Return the latest *n_days* of daily compression aggregates (descending)."""
    days = r.zrevrange(HEADROOM_DAYS_KEY, 0, n_days - 1)
    results: list[dict] = []
    for day_str in days:
        raw = r.hgetall(f"{HEADROOM_DAY_PREFIX}{day_str}")
        if not raw:
            continue
        tb = float(raw.get("total_tokens_before", 0))
        ta = float(raw.get("total_tokens_after", 0))
        ts = float(raw.get("total_tokens_saved", 0))
        count = int(raw.get("call_count", 0))
        results.append({
            "date": day_str,
            "call_count": count,
            "tokens_before": int(tb),
            "tokens_after": int(ta),
            "tokens_saved": int(ts),
            "compression_ratio": ts / tb if tb > 0 else 0.0,
        })
    return results


def get_total_headroom_stats() -> dict:
    """Return running grand totals for all headroom compression."""
    raw = r.hgetall(HEADROOM_TOTALS_KEY)
    if not raw:
        return {
            "total_tokens_before": 0,
            "total_tokens_after": 0,
            "total_tokens_saved": 0,
            "total_calls": 0,
            "compression_ratio": 0.0,
        }
    tb = float(raw.get("total_tokens_before", 0))
    ta = float(raw.get("total_tokens_after", 0))
    ts = float(raw.get("total_tokens_saved", 0))
    tc = int(raw.get("total_calls", 0))
    return {
        "total_tokens_before": int(tb),
        "total_tokens_after": int(ta),
        "total_tokens_saved": int(ts),
        "total_calls": tc,
        "compression_ratio": ts / tb if tb > 0 else 0.0,
    }


def get_day_headroom_calls(date_str: str) -> list[dict]:
    """Return individual headroom calls for a specific date.

    Scans all headroom:call:* keys for matching date prefix in timestamp.
    For small-to-medium datasets this is fine; for very large datasets
    consider adding a per-day call-index ZSet.
    """
    calls: list[dict] = []
    cursor = 0
    prefix = f"{HEADROOM_CALL_PREFIX}*"
    match_prefix = date_str  # YYYY-MM-DD prefix in ISO timestamp
    while True:
        cursor, keys = r.scan(cursor=cursor, match=prefix, count=500)
        for key in keys:
            raw = r.hgetall(key)
            if not raw:
                continue
            ts = raw.get("timestamp", "")
            if not ts.startswith(match_prefix):
                continue
            calls.append(_hydrate_headroom(raw))
        if cursor == 0:
            break
    # Sort by timestamp descending (most recent first)
    calls.sort(key=lambda c: c.get("timestamp", ""), reverse=True)
    return calls


def _hydrate_headroom(raw: dict) -> dict:
    """Cast headroom hash fields back to native types."""
    for field in ("tokens_before", "tokens_after", "tokens_saved"):
        if field in raw:
            raw[field] = int(float(raw[field]))
    if "compression_ratio" in raw:
        raw["compression_ratio"] = float(raw["compression_ratio"])
    if "transforms_json" in raw:
        raw["transforms_applied"] = json.loads(raw["transforms_json"])
        del raw["transforms_json"]
    # prompt_before and prompt_after are already strings — pass through
    return raw
