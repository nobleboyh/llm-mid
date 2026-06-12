"""Clear all eval:* keys from Redis for a fresh testing slate.

Usage:
    python eval/clear_redis.py          # normal
    python eval/clear_redis.py --hard   # FLUSHDB (nuclear — clears everything in this DB)

Run this against the right Redis instance by setting REDIS_URL.
In Docker: export REDIS_URL=redis://redis:6379
Locally:   defaults to redis://localhost:6379
"""
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")


def main() -> None:
    hard = "--hard" in sys.argv

    # Late import so we use the right REDIS_URL above
    import redis

    client = redis.from_url(
        REDIS_URL,
        decode_responses=True,
        retry_on_timeout=True,
        health_check_interval=30,
        socket_keepalive=True,
    )

    if hard:
        client.flushdb()
        print(f"💥 FLUSHDB done — all keys removed from {REDIS_URL}")
        return

    # SCAN-based delete for eval:* keys
    deleted = 0
    cursor = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, match="eval:*", count=1000)
        if keys:
            deleted += client.delete(*keys)
        if cursor == 0:
            break

    print(f"✅ Removed {deleted} eval:* keys from {REDIS_URL}")

    # Quick summary
    remaining = 0
    cursor = 0
    while True:
        cursor, keys = client.scan(cursor=cursor, count=1000)
        remaining += len(keys)
        if cursor == 0:
            break
    print(f"📊 Remaining keys in DB: {remaining} (should be 0 if Redis is dedicated to this project)")


if __name__ == "__main__":
    main()
