# Design — Hot-Zone Cache Columns in Router Cache-Impact TUI

**Status**: Draft
**Owner**: Ito
**Depends on**: Session Model-Switch Logger (Phase 0 — already shipped)
**Precedes**: PRD #2 (Cache-Value-Adjusted Routing)

---

## 1. Problem

The router cache-impact TUI (`scripts/analyze_router_cache_impact.py`) shows a `TTL?` column that only checks **time-based expiry** — whether `seconds_since_last > 300`. It cannot detect whether the **content** of the hot zone changed, which is the real cause of LLM input-cache misses in most cases.

A session like:

| Turn | Model | Gap | Hot Zone | TTL? | Actual cache status |
|------|-------|-----|----------|------|-------------------|
| 1→2 | flash→flash | 42s | 3,400 | ✓ | ✅ Intact |
| 2→3 | flash→flash | 600s | **8,900** | ✗ | ❌ Expired + content grew |
| 3→4 | flash→pro | 10s | 8,900 | ✓ | ❌ Model switched |
| 4→5 | pro→pro | 50s | **12,400** | ✓ | ❌ New content entered hot zone |

The TTL column gives ✓ on turn 4 despite a model switch (the real cache break), and ✓ on turn 5 despite content growth. We need columns that detect the actual cache status.

## 2. Decisions (confirmed)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **A1: Hash scope** | Full hot zone — compressed messages | SHA-256 of canonical JSON of all hot-zone messages. Matches provider behavior — any hot-zone content change changes the hash. |
| **B2: Computation site** | `_log_session_switch` in `proxy/callback.py` | Compressed messages flow through the request body (middleware swaps them in-place). No ContextVar thread-boundary problem. `protect_recent` read from the dataclass default. |
| **C: Total prompt tokens** | From `_get_usage(response_obj)` | Already extracted in `log_success_event`. Passed as parameter to `_log_session_switch`. |
| **D1: % Cached meaning** | `hot_zone_tokens / total_prompt_tokens` | Always shown. When hash matches previous turn + within TTL, it's the actual cache-hit ratio. When cache is broken, it's "what *could* have been cached." |
| **E2: Column layout** | Keep `TTL?` + add both new columns | 3 total cache columns: `Cache` (hash + ✓/✗/Δ flag), `% Cached`, `TTL?`. Time-based and content-based signals are complementary. |

## 3. How the middleware body-swap enables B2

```
Request → CompressionMiddleware → LiteLLM + callback
           │
           body_json["messages"] = raw
           compress(raw) → result.messages (compressed)
           body_json["messages"] = result.messages
           re-serialize full_body
                                    │
                              LiteLLM receives modified body
                                    │
                              callback reads compressed messages
                              from kwargs.litellm_params.proxy_server_request.body.messages
```

When `tokens_saved == 0`, the middleware does NOT swap (raw messages pass through). The callback handles this identically — the hash and token count are computed on whatever messages are in the body, which matches what was sent to the LLM.

## 4. Event record changes

Two new fields added to the Router event JSON (stored in `router:session:{fp}` List elements):

```json
{
  "timestamp": "2026-07-15T14:23:05.123456+00:00",
  "model": "deepseek-pro",
  "previous_model": "gemini-flash",
  "seconds_since_last": 42.3,
  "hot_zone_tokens": 3400,
  "hot_zone_hash": "a1b2c3d4",       ← NEW (8-char hex)
  "total_prompt_tokens": 15200        ← NEW (from response usage)
}
```

| Field | Type | Source | Description |
|-------|------|--------|-------------|
| `hot_zone_hash` | string | computed | First 8 hex chars of SHA-256 canonical JSON of compressed hot-zone messages |
| `total_prompt_tokens` | int | `response_obj.usage.prompt_tokens` | Total prompt tokens reported by the LLM provider |

### What "hot zone" means for the slice

```python
from headroom.compress import CompressConfig
protect_recent = CompressConfig().protect_recent  # = 4

compressed_messages = body.get("messages", [])
if len(compressed_messages) > protect_recent:
    hot_messages = compressed_messages[:-protect_recent]
else:
    hot_messages = compressed_messages  # session still growing into shape
```

Only messages that have crossed beyond the `protect_recent` live zone are included — these are the messages the LLM should have in its input cache.

## 5. Hash calculation

### Algorithm

```python
import hashlib, json

raw = json.dumps(hot_messages, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
hot_zone_hash = hashlib.sha256(raw.encode()).hexdigest()[:8]
```

- **Canonical JSON**: `sort_keys=True, separators=(",", ":")` — matches how providers compute their own cache keys
- **8 hex chars**: Compact enough for TUI display (fits ~8 columns), collision-safe for cache-break detection
- **SHA-256**: Industry standard for both Anthropic and Gemini cache key computation

### Cache status from hash comparison

The TUI compares each event's `hot_zone_hash` against the **previous event in the same session**:

| Status | Icon | Condition |
|--------|------|-----------|
| **Cache intact** | ✓ | Hash matches previous event AND `seconds_since_last ≤ CACHE_TTL_SECONDS` |
| **Content changed** | ✗ | Hash differs from previous event |
| **Model switched** | ⚡ | Hash matches but model changed (rare — model is part of canonical JSON so this is actually covered by ✗) |
| **First event** | — | No previous event to compare against |
| **TTL expired** | ⏱ | Hash matches but `seconds_since_last > CACHE_TTL_SECONDS` |

## 6. % Cached calculation

```python
pct_cached = hot_zone_tokens / total_prompt_tokens * 100  if total_prompt_tokens > 0 else 0.0
```

### Token-counting approximation

The `hot_zone_tokens` used in this ratio is computed via `_count_tokens(json.dumps(hot_messages, ensure_ascii=False))` — the JSON-serialized form of the compressed message objects. This is an approximation because:

- `_count_tokens` uses `cl100k_base` (tiktoken) or a 4-char-per-token fallback — not the LLM provider's own tokenizer for the specific model
- The provider may count tokens slightly differently for the same JSON body

This approximation is consistent with the existing codebase (the same `_count_tokens` is already used for `hot_zone_tokens` in `_patched_compress`). The ratio is labeled as **Est.** in the TUI to make this clear.

Shown unconditionally per D1:
- **When cache intact** — the actual cache-hit ratio: the portion of the prompt that was served from cache
- **When cache broken** — "what portion of the prompt would have been cached if cache were intact"

This is informative in both states: a growing percentage over time shows Headroom is effectively building a stable cached prefix, even if individual requests miss due to model switches.

## 7. New TUI columns in session detail view

### Current columns

```
 #  | Time       | Model               | Prev Model          | Gap     | Hot Zone | TTL?
----|------------|---------------------|---------------------|---------|----------|-----
```

### New columns (replacing `Hot Zone` with more detail and adding two more)

```
 #  | Time       | Model               | Prev Model          | Gap     | Cache Hash | % Cached | TTL?
----|------------|---------------------|---------------------|---------|-----------|----------|-----
 1  | 14:23:05   | gemini-flash        | —                   | —       | a1b2c3d4  — | —       | —
 2  | 14:23:47   | gemini-flash        | gemini-flash        | 42s     | a1b2c3d4  ✓ | 22.4%   | ✓
 3  | 14:33:47   | gemini-flash        | gemini-flash        | 600s    | a1b2c3d4  ⏱ | 22.4%   | ✗
 4  | 14:33:57   | deepseek-pro        | gemini-flash        | 10s     | d4e5f6a7  ✗ | 58.6%   | ✓
 5  | 14:34:47   | deepseek-pro        | deepseek-pro        | 50s     | x9f8e7d6  ✗ | 62.3%   | ✓
```

### Column details

| Column | Width | Content |
|--------|-------|---------|
| `Cache Hash` | 12 chars | First 8 hex chars + space + ✓/✗/⏱/— flag. Color-coded: green ✓, red ✗, yellow ⏱, dim — |
| `% Cached` | 8 chars | `22.4%` → colored green when >50%, yellow when >20%, dim otherwise |

### Column header rendering

```python
col = Text()
col.append(" #  ", style="bold underline")
col.append("Time       ", style="bold underline")
col.append("Model               ", style="bold underline")
col.append("Prev Model          ", style="bold underline")
col.append("Gap     ", style="bold underline")
col.append("Cache Hash", style="bold underline")   # ← NEW
col.append("% Cached", style="bold underline")     # ← NEW
col.append("TTL?", style="bold underline")         # ← existing
```

### Session overview changes

The overview table gains a "Cache OK" column showing what fraction of events have intact cache:

```
 Session           Sw  Models           Events  Cache OK  Cost
 fp:a1b2...         3  flash→pro→...      12    75.0%   $0.08
 header:sess-abc    0  flash→flash→...     8    100%      —
 fp:d4e5...         5  flash↔pro           6    16.7%   $0.21
```

## 8. Changes to `proxy/callback.py`

### `_log_session_switch` — new logic (replaces inline hot_zone computation)

```python
def _log_session_switch(self, kwargs: dict, total_prompt_tokens: int = 0) -> None:
    try:
        from eval.redis_store import r as redis_client
        from proxy.skill_injector import _count_tokens
        from headroom.compress import CompressConfig

        protect_recent = CompressConfig().protect_recent

        session_key = _session_fingerprint(kwargs)
        if not session_key:
            return

        model = kwargs.get("model", "unknown")

        psr = (kwargs.get("litellm_params") or {}).get("proxy_server_request") or {}
        body = psr.get("body") or {}
        messages = body.get("messages", [])  # already compressed (or raw if no compression)

        # Slice hot zone: everything except the protect_recent live zone
        if len(messages) > protect_recent:
            hot_messages = messages[:-protect_recent]
        else:
            hot_messages = messages  # session still growing, use what we have

        # SHA-256 hash of canonical JSON (A1)
        hot_zone_hash = hashlib.sha256(
            json.dumps(hot_messages, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False).encode()
        ).hexdigest()[:8]

        # Compressed hot-zone token count (or raw when no compression applied)
        hot_zone_tokens = _count_tokens(
            json.dumps(hot_messages, ensure_ascii=False)
        )

        # ... rest of existing logic (meta, gap, event dict) ...

        event = {
            "timestamp": now_iso,
            "model": model,
            "previous_model": last_model or None,
            "seconds_since_last": round(gap, 1) if gap is not None else None,
            "hot_zone_tokens": hot_zone_tokens,
            "hot_zone_hash": hot_zone_hash,                # NEW
            "total_prompt_tokens": total_prompt_tokens,     # NEW
        }

        # ... lpush, hset, etc. (unchanged) ...
    except Exception:
        pass  # fire-and-forget — never block the response
```

### `log_success_event` — pass `total_prompt_tokens`

```python
def log_success_event(self, kwargs, response_obj, start_time, end_time):
    # ... existing logic ...
    usage = _get_usage(response_obj)
    # ... existing enqueue ...

    # ── Log session model choice for cache-miss measurement ────────────
    self._log_session_switch(kwargs, total_prompt_tokens=usage.get("prompt_tokens", 0))
```

## 9. Changes to `scripts/analyze_router_cache_impact.py`

### `_render_session_detail` — new columns and cache-status logic

```python
CACHE_TTL_SECONDS = 300

def _format_cache_cell(prev_hash: str | None, curr_hash: str, gap: float | None) -> tuple[str, str]:
    """Return (hash_display, status_icon) for the Cache Hash column."""
    if prev_hash is None:
        return curr_hash, "—"  # first event
    if curr_hash != prev_hash:
        return curr_hash, "✗"  # content changed → cache broken
    if gap is not None and gap > CACHE_TTL_SECONDS:
        return curr_hash, "⏱"  # hash matches but TTL expired
    return curr_hash, "✓"  # hash matches + within TTL → cache intact

def _format_pct_cached(hot_zone: int, total: int) -> str:
    """Format % cached, or '—' if no data."""
    if total <= 0:
        return "—"
    pct = hot_zone / total * 100
    return f"{pct:.1f}%"
```

### Session detail loop — track previous hash across events

```python
prev_hash = None
for i, e in enumerate(session["events"]):
    curr_hash = e.get("hot_zone_hash", "")
    gap = e.get("seconds_since_last")
    cache_display, cache_flag = _format_cache_cell(prev_hash, curr_hash, gap)
    pct_str = _format_pct_cached(e.get("hot_zone_tokens", 0), e.get("total_prompt_tokens", 0))

    row.append(f"{cache_display:<8} {cache_flag}  ")   # "a1b2c3d4 ✓  "
    row.append(f"{pct_str:>8}")                         # "   22.4%"
    prev_hash = curr_hash
```

### `_render_sessions_overview` — add Cache OK percentage

```python
# Per-session: count events where cache was intact
for s in sessions:
    intact = 0
    prev_h = None
    for e in s["events"]:
        h = e.get("hot_zone_hash", "")
        g = e.get("seconds_since_last")
        if prev_h is not None and h == prev_h and g is not None and g <= CACHE_TTL_SECONDS:
            intact += 1
        prev_h = h
    s["cache_ok_pct"] = intact / max(1, len(s["events"]) - 1) * 100  # skip first event
```

### Column header updated

```python
col.append(f"{'Cache OK':>9}", style="bold underline")
```

## 10. Edge cases

| Scenario | Hash | % Cached | Behavior |
|----------|------|----------|----------|
| Single-turn session | a1b2 (— flag) | — | No previous event to compare. First event always shows — for hash, % cached, and TTL? |
| Session with `tokens_saved=0` | Raw messages hashed | Based on raw tokens | Compressed == raw when no compression happened, so values are correct |
| Session just started (<protect_recent messages) | Non-empty hash of partial hot zone | Growing | Hot zone grows until it reaches `protect_recent` size. Hash changes every turn until stable |
| Model switch mid-session | New hash (model is part of canonical JSON) | Changes | Covered automatically — model string is in the JSON, so hash changes |
| Incomplete `total_prompt_tokens` (0) | Normal | Shows — | Might happen with some providers. Display — instead of 0% |
| `hot_zone_hash` missing from old events (pre-change) | Defaults to empty string | — | Every event with empty hash will show ✗ compared to previous. Mitigation: treat empty as first-event for sessions with mixed data |

## 11. Files changed

| File | Change | Risk |
|------|--------|------|
| `proxy/callback.py` | Replace inline hot_zone computation with hash + count from compressed messages. Add `total_prompt_tokens` parameter. | Low — same function, better data |
| `scripts/analyze_router_cache_impact.py` | Add `Cache Hash`, `% Cached` columns to detail view. Add `Cache OK` to overview. Add cache-status logic. | Medium — TUI rendering changes, visual regression risk |
| `tests/test_session_switch_logger.py` | Update tests: mock events include `hot_zone_hash` and `total_prompt_tokens`. Test cache-status comparison logic. | Low |
| `tests/test_sliding_boundary.py` | No changes needed (this tests Headroom compression stability, not the TUI) | None |

## 12. Testing

### Unit tests

| Test | What it validates |
|------|-------------------|
| `test_hot_zone_hash_stable` | Same compressed messages → same hash |
| `test_hot_zone_hash_changes_on_content` | Adding a message to hot zone → different hash |
| `test_cache_status_intact` | Hash matches + within TTL → ✓ |
| `test_cache_status_content_changed` | Hash differs → ✗ |
| `test_cache_status_ttl_expired` | Hash matches + over TTL → ⏱ |
| `test_cache_status_first_event` | No previous → — |
| `test_pct_cached` | `hot_zone_tokens / total_prompt_tokens` = expected |
| `test_pct_cached_zero_total` | Returns — |
| `test_event_has_new_fields` | Redis event contains `hot_zone_hash` and `total_prompt_tokens` |

### Manual verification

```bash
# Build and deploy
docker compose build litellm && docker compose up -d litellm

# Run a few requests through the gateway
curl -s http://localhost:4000/v1/chat/completions ...

# Launch the TUI
docker exec -it gatemid-headroom python -m eval.cli router

# Verify:
# 1. Session detail shows Cache Hash + % Cached columns
# 2. First event shows — for both
# 3. Consecutive same-model same-content shows ✓
# 4. Model switch shows ✗ on hash
