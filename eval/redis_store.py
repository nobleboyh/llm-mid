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
