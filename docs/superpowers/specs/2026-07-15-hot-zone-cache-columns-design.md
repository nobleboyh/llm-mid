# Design — Cache Metrics Columns in Router Cache-Impact TUI

**Status**: Draft  
**Owner**: Ito  
**Depends on**: Session Model-Switch Logger (Phase 0 — already shipped)  
**Precedes**: PRD #2 (Cache-Value-Adjusted Routing)

---

## 1. Correction: How LLM Input Caching Actually Works

Our original design (v1) proposed computing a SHA-256 hash of the "hot zone" (compressed messages outside the `protect_recent` window) and comparing it across events to detect cache breaks. **This was fundamentally wrong.**

### The actual cache key mechanism

LLM providers use **prefix caching** — the cache key is computed from only the content *up to the last `cache_control` breakpoint*, NOT from the full conversation:

| Provider | Cache key scope | Everything after breakpoint |
|----------|----------------|---------------------------|
| Anthropic | `SHA-256(model + system[:last_cc] + tools)` | Excluded from key entirely |
| Gemini (implicit) | System instruction + cached content object | Excluded from key |
| OpenAI | System prompt prefix | Excluded from key |

### Why our hash was wrong

In a growing conversation with `protect_recent=4`:

```
Turn 10:  [sys] [tools] [msg1] [msg2] [msg3] ... [msg6] [asst7] [user8] [asst9] [user10]
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  ^^^^^^^^^^^^^^^^^^^^^^^^
           hot zone (hashed)                                 live zone (excluded)
           ↑ but provider's cache key is:
             SHA-256(model + system[:cc] + tools)
             ↑ this NEVER changed!
```

Every 2 turns, a message slides from live zone into hot zone → our hash changes → we flag "✗ cache broken". But the provider's cache key (system prompt + tools) never changed — **the cache was still hitting**. We were detecting false positives on every conversation with more than ~5 turns.

### What actually breaks the cache

| Change | Breaks cache? | Our hash (v1) would flag |
|--------|---------------|--------------------------|
| New user message (conversation grows) | ❌ No | ✗ False positive on every slide |
| System prompt changes | ✅ Yes | ✓ Correct |
| Tools change | ✅ Yes | ✓ Correct |
| Model switches | ✅ Yes | ✓ Correct |
| TTL expires (gap > 300s) | ✅ Yes | ✓ Correct (TTL? column) |
| Timestamp in system prompt | ✅ Yes (silent) | Depends |

The hash approach produces **~80–90% false positive** rate in real conversations (every 2 of ~5 turns the hot zone slides, but cache was never broken).

## 2. The Right Approach: Provider Cache Metrics

The LLM provider **already tells us** exactly what happened — via `usage` fields in every response. LiteLLM passes these through:

### Field mapping

| Field in LiteLLM response | Type | What it tells us | Available on |
|---------------------------|------|-----------------|--------------|
| `usage.cache_read_input_tokens` | int | Tokens served from cache | Anthropic |
| `usage.cache_creation_input_tokens` | int | Tokens written to cache (costs 1.25×) | Anthropic |
| `usage.prompt_tokens_details.cached_tokens` | int | Tokens served from cache (OpenAI-style) | OpenAI, Gemini, others |
| `usage.prompt_tokens` | int | Total prompt tokens | All |

LiteLLM's `ModelResponse.usage` object normalizes these across providers. The callback already reads `usage.get("prompt_tokens", 0)`.

### Simple cache detection

```python
# The truth, directly from the provider:
cache_read = usage.get("cache_read_input_tokens", 0) or 0
# Fallback for OpenAI/Gemini format:
if not cache_read:
    details = usage.get("prompt_tokens_details") or {}
    cache_read = details.get("cached_tokens", 0) or 0

cache_active = cache_read > 0                      # ✅ Provider confirms cache hit
cache_created = usage.get("cache_creation_input_tokens", 0) or 0  # New cache was written
pct_cached = cache_read / total * 100  if total > 0 else 0  # Exact
```

| Status | Logic | Meaning |
|--------|-------|---------|
| ✓ Cache hit | `cache_read > 0` | The last `cache_control` breakpoint's prefix was found in cache |
| ✗ Cache miss | `cache_read == 0` and `cache_created == 0` | No cache entry matched the prefix |
| ∆ Cache written | `cache_created > 0` | A new cache entry was created (first request, or prefix changed) |
| — Unknown | All cache fields are 0 or missing | Provider doesn't return cache metrics, or feature not used |

## 3. Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Cache status** | `cache_read > 0` from provider | 100% accurate — provider tells us exactly whether cache hit. No hashing, no approximation. |
| **% Cached** | `cache_read / prompt_tokens` | Exact ratio of prompt served from cache. Zero approximation needed. |
| **% Created** | `cache_creation / prompt_tokens` | Shows new cache writes (important for cost analysis — write costs 1.25×) |
| **Old columns** | Replace `Hot Zone` with `Cache`, `Rd%`, `Wr%` | More useful signals. Hot-zone tokens still shown in detail panel. |
| **Drop hash** | Remove `hot_zone_hash` entirely | Non-functional due to fundamental misunderstanding of cache key scope |

## 4. What was already correct

- **`total_prompt_tokens`** in events ✅ — passes through from `response_obj.usage.prompt_tokens`
- **`hot_zone_tokens`** in events ✅ — still useful as an estimate of compressed conversation size (just not for cache-hit computation)
- **TTL? column** ✅ — time-based expiry is still a real cache-break reason (though `cache_read` already tells us this)
- **`_get_usage()` extraction** ✅ — just needs new fields added

The `total_prompt_tokens` field stays. `hot_zone_tokens` stays (informational). `hot_zone_hash` is **removed**. Two new fields added: `cache_read_input_tokens` and `cache_creation_input_tokens`.

## 5. Event record format

```json
{
  "timestamp": "2026-07-15T14:23:05.123456+00:00",
  "model": "deepseek-pro",
  "previous_model": "gemini-flash",
  "seconds_since_last": 42.3,
  "hot_zone_tokens": 3400,
  "total_prompt_tokens": 15200,
  "cache_read_input_tokens": 3400,       ← NEW (from provider usage)
  "cache_creation_input_tokens": 0       ← NEW (from provider usage)
}
```

| Field | Type | Source |
|-------|------|--------|
| `cache_read_input_tokens` | int | `usage.cache_read_input_tokens` or `usage.prompt_tokens_details.cached_tokens` |
| `cache_creation_input_tokens` | int | `usage.cache_creation_input_tokens` (Anthropic only; 0 for others) |

## 6. New TUI columns

### Session detail view

```
 #  | Time       | Model               | Prev Model    | Gap   | Cache | Rd%    | Wr%    | TTL?
----|------------|---------------------|---------------|-------|-------|--------|--------|-----
 1  | 14:23:05   | gemini-flash        | —             | —     | ✓     | 22.4%  | —      | —
 2  | 14:23:47   | gemini-flash        | gemini-flash  | 42s   | ✓     | 22.4%  | —      | ✓
 3  | 14:23:57   | gemini-flash        | gemini-flash  | 10s   | ✓     | 58.6%  | —      | ✓
 4  | 14:33:57   | deepseek-pro        | gemini-flash  | 10m0s | ✗     | 0%     | 58.6%  | ✗
 5  | 14:34:47   | deepseek-pro        | deepseek-pro  | 50s   | ✓     | 58.6%  | —      | ✓
```

| Column | Width | Content | Source |
|--------|-------|---------|--------|
| `Cache` | 6 chars | ✓/✗/∆/— (green/red/yellow/dim) | `cache_read > 0` |
| `Rd%` | 6 chars | `22.4%` or — | `cache_read / total` |
| `Wr%` | 6 chars | `58.6%` or — | `cache_creation / total` (Anthropic only) |

#### Color coding

| Status | Color | Condition |
|--------|-------|-----------|
| ✓ | green | `cache_read > 0` — cache hit |
| ✗ | red | `cache_read == 0 and cache_creation == 0` — cold miss |
| ∆ | yellow | `cache_creation > 0` — new cache written |
| — | dim | All cache fields are 0 or absent |

### Session overview — remove `Cache OK`, add `Cache%`

```
 Session           Sw  Models           Events  Cache%  Cost
 fp:a1b2...         3  flash→pro→...      12   65.0%  $0.08
 header:sess-abc    0  flash→flash→...     8   92.0%     —
 fp:d4e5...         5  flash↔pro           6   12.0%  $0.21
```

`Cache%` = percentage of events with `cache_read > 0` (across all events in session).

## 7. Changes to `proxy/callback.py`

### `_get_usage` — add cache fields

```python
def _get_usage(response) -> dict:
    if response is None:
        return {}
    if hasattr(response, "usage"):
        u = response.usage
        if u is None:
            return {}
        result = {"prompt_tokens": u.prompt_tokens or 0,
                  "completion_tokens": u.completion_tokens or 0}
        # Cache metrics
        result["cache_read_input_tokens"] = getattr(u, "cache_read_input_tokens", None) or 0
        result["cache_creation_input_tokens"] = getattr(u, "cache_creation_input_tokens", None) or 0
        # OpenAI/Gemini fallback
        if hasattr(u, "prompt_tokens_details") and u.prompt_tokens_details:
            ptd = u.prompt_tokens_details
            cached = getattr(ptd, "cached_tokens", None)
            if cached and not result["cache_read_input_tokens"]:
                result["cache_read_input_tokens"] = cached
        return result
    if isinstance(response, dict):
        u = response.get("usage", {})
        if not isinstance(u, dict):
            return {}
        result = {"prompt_tokens": u.get("prompt_tokens", 0),
                  "completion_tokens": u.get("completion_tokens", 0)}
        result["cache_read_input_tokens"] = u.get("cache_read_input_tokens", 0) or 0
        result["cache_creation_input_tokens"] = u.get("cache_creation_input_tokens", 0) or 0
        details = u.get("prompt_tokens_details", {})
        if isinstance(details, dict):
            cached = details.get("cached_tokens", 0)
            if cached and not result["cache_read_input_tokens"]:
                result["cache_read_input_tokens"] = cached
        return result
    return {}
```

### `_log_session_switch` — remove hash, use provider metrics

```python
def _log_session_switch(self, kwargs: dict, total_prompt_tokens: int = 0,
                        cache_read_input_tokens: int = 0,
                        cache_creation_input_tokens: int = 0) -> None:
    try:
        from eval.redis_store import r as redis_client

        session_key = _session_fingerprint(kwargs)
        if not session_key:
            return

        model = kwargs.get("model", "unknown")

        # Hot-zone tokens (compressed conversation size — informational)
        psr = (kwargs.get("litellm_params") or {}).get("proxy_server_request") or {}
        body = psr.get("body") or {}
        messages = body.get("messages", [])
        from headroom.compress import CompressConfig
        from proxy.skill_injector import _count_tokens
        protect_recent = CompressConfig().protect_recent
        if len(messages) > protect_recent:
            hot_messages = messages[:-protect_recent]
        else:
            hot_messages = messages
        hot_zone_tokens = _count_tokens(json.dumps(hot_messages, ensure_ascii=False))

        meta_key = f"router:session:{session_key}:meta"
        meta = redis_client.hgetall(meta_key) or {}
        last_model = meta.get("latest_model")
        last_ts = meta.get("latest_timestamp")

        now = datetime.datetime.now(datetime.timezone.utc)
        now_iso = now.isoformat()
        gap = _compute_seconds_since(last_ts, now)

        event = {
            "timestamp": now_iso,
            "model": model,
            "previous_model": last_model or None,
            "seconds_since_last": round(gap, 1) if gap is not None else None,
            "hot_zone_tokens": hot_zone_tokens,
            "total_prompt_tokens": total_prompt_tokens,
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
        }

        list_key = f"router:session:{session_key}"
        redis_client.lpush(list_key, json.dumps(event))
        redis_client.expire(list_key, SESSION_TTL)

        redis_client.hset(meta_key, mapping={
            "latest_model": model,
            "latest_timestamp": now_iso,
        })
        redis_client.expire(meta_key, SESSION_TTL)

        if last_model is None:
            redis_client.hset(meta_key, "created_at", now_iso)
            redis_client.zadd(SESSION_DAYS_KEY, {now_iso[:10]: now.timestamp()})

    except Exception:
        logger.exception("_log_session_switch — Redis write failed")
```

### `log_success_event` — pass cache fields

```python
def log_success_event(self, kwargs, response_obj, start_time, end_time):
    # ... existing logic ...
    usage = _get_usage(response_obj)
    # ... existing enqueue ...

    # ── Log session model choice for cache-miss measurement ────────────
    self._log_session_switch(
        kwargs,
        total_prompt_tokens=usage.get("prompt_tokens", 0),
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0),
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0),
    )
```

## 8. Changes to `scripts/analyze_router_cache_impact.py`

### Helper functions

```python
def _cache_status(read_tokens: int, created_tokens: int) -> tuple[str, str]:
    """Return (icon, style_name) for a single event's cache status."""
    if read_tokens > 0:
        return "✓", "green"
    if created_tokens > 0:
        return "∆", "yellow"
    return "✗", "red"


def _fmt_cache_pct(numerator: int, denominator: int) -> str:
    """Format a cache percentage, or '—' if no data."""
    if denominator <= 0:
        return "—"
    pct = numerator / denominator * 100
    return f"{pct:.1f}%"


def _cache_display(numerator: int, denominator: int) -> tuple[str, str]:
    """Return (display_str, style_name) for a cache percentage."""
    if denominator <= 0:
        return "—", "dim"
    pct = numerator / denominator * 100
    if pct > 50:
        return f"{pct:.1f}%", "bold green"
    if pct > 0:
        return f"{pct:.1f}%", "yellow"
    return "0%", "dim"
```

### Session detail columns

```
col.append("Cache", style="bold underline")    # ✓/✗/∆/—
col.append(" Rd%", style="bold underline")      # cache_read / total
col.append(" Wr%", style="bold underline")      # cache_creation / total
```

```python
for i, e in enumerate(session["events"]):
    # ... existing ts, model, gap, etc. ...
    read_tokens = e.get("cache_read_input_tokens", 0) or 0
    created_tokens = e.get("cache_creation_input_tokens", 0) or 0
    total = e.get("total_prompt_tokens", 0) or 0

    cache_icon, cache_color = _cache_status(read_tokens, created_tokens)
    rd_str, rd_color = _cache_display(read_tokens, total)
    wr_str, wr_color = _cache_display(created_tokens, total)

    row.append(f"{cache_icon:<6}", style=cache_color)
    row.append(f" {rd_str:>6}", style=rd_color)
    row.append(f" {wr_str:>6}", style=wr_color)
```

### Session overview — Cache% column

```python
# Per-session: % of events with cache_read > 0
cache_hit_events = sum(1 for e in session["events"]
                       if e.get("cache_read_input_tokens", 0) or 0 > 0)
cache_pct = cache_hit_events / max(1, len(session["events"])) * 100
```

## 9. Edge cases

| Scenario | Cache | Rd% | Wr% | Behavior |
|----------|-------|-----|-----|----------|
| First request in session | ∆ | 0% | 58.6% | No cache to read, but new cache written. Yellow marker shows cost incurred. |
| Cache hit (consecutive) | ✓ | 22.4% | 0% | Stable prefix matched. Rd% shows portion served from cache. |
| Model switch | ✗ | 0% | 0% | Full miss — model changed, cache key changed. Cold. |
| TTL expired (same prefix) | ✗ | 0% | 58.6% | Prefix matches but past TTL. ∆ shows write cost. |
| Provider doesn't support caching | — | — | — | All fields 0. Dim markers. |
| Old events (pre-feature) | — | — | — | Fields missing, default to 0. No false positives. |
| Anthropic first request | ∆ | 0% | 22.4% | `cache_creation > 0` captures the write cost. |

## 10. Files changed

| File | Change | Risk |
|------|--------|------|
| `proxy/callback.py` | Add cache fields to `_get_usage`. Remove hash from `_log_session_switch`. Pass cache fields from `log_success_event`. | Low — adds extraction, removes computation |
| `scripts/analyze_router_cache_impact.py` | Replace `Cache Hash` + `% Cached` with `Cache`, `Rd%`, `Wr%`. Replace `Cache OK` with `Cache%`. New helpers. | Medium — column layout reshuffle |
| `tests/test_session_switch_logger.py` | Replace hot_zone_hash tests with cache field tests. Update integration test. | Low |

## 11. Testing

| Test | What it validates |
|------|-------------------|
| `test_cache_fields_extracted` | `_get_usage` extracts `cache_read_input_tokens` and `cache_creation_input_tokens` from response |
| `test_cache_fields_in_event` | Event dict contains both cache fields |
| `test_cache_hit_status` | `cache_read > 0` → status ✓ |
| `test_cache_miss_status` | `cache_read == 0, cache_creation == 0` → status ✗ |
| `test_cache_created_status` | `cache_creation > 0` → status ∆ |
| `test_no_cache_fields` | Missing/zero fields → — |
| `test_cache_pct_display` | Percentage computed from cache_read / total |

## 12. Migration

Old events (stored before this change) lack `cache_read_input_tokens` and `cache_creation_input_tokens`. The TUI defaults to 0 for missing fields, showing "—" for both Rd% and Wr%. This is correct — we have no cache data for those events.

The `hot_zone_hash` field from old events is simply ignored (no TUI column reads it anymore).
