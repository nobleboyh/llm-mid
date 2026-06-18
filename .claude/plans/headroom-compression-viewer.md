# Plan: Headroom Compression Interactive Viewer

## Goal
Build an interactive script (`eval/headroom_view_interactive.py`) — same UX as `eval/score_view_interactive.py` — but for Headroom compression results. 10 latest days, totals at the top, Redis-backed.

## Architecture

```
proxy/entrypoint.py          ── _patched_compress() stores results in Redis (best-effort)
        │
        ▼
eval/redis_store.py          ── NEW: headroom storage/retrieval functions
        │
        ▼
eval/headroom_view_interactive.py  ── NEW: interactive viewer script
```

## Files to change / create

### 1. `eval/redis_store.py` — Add headroom compression storage functions

New Redis keys:
| Key | Type | Purpose |
|-----|------|---------|
| `headroom:call:{call_id}` | Hash | Individual compression result |
| `headroom:day:{YYYY-MM-DD}` | Hash | Daily aggregate (total_tokens_before, total_tokens_after, total_tokens_saved, call_count) |
| `headroom:days` | ZSet | date_str → unix_ts (for listing latest N days) |
| `headroom:totals` | Hash | Running grand totals |

New functions:
- `store_headroom_result(call_id, timestamp, tokens_before, tokens_after, tokens_saved, compression_ratio, model, transforms_applied)` — TTL 30 days on call hashes, increments daily/total counters
- `get_daily_headroom_stats(n_days=10)` — returns list of daily aggregates ordered by date desc
- `get_total_headroom_stats()` — returns `{total_tokens_saved, total_tokens_before, total_tokens_after, total_compression_ratio, total_calls}`
- `get_day_headroom_calls(date_str)` — returns individual calls for a specific day (for detail view)

### 2. `proxy/entrypoint.py` — Hook compression results into Redis

In `_patched_compress()`, after calling `_original_compress()`, store the `CompressResult` in Redis (best-effort, try/except, non-blocking). Only store when `tokens_saved > 0`.

### 3. `eval/headroom_view_interactive.py` — New interactive viewer

Same terminal-interaction pattern as `score_view_interactive.py`:
- Raw terminal input (`_getch()`, same code)
- Terminal mode management (`_enable_raw_mode` / `_restore_terminal`)
- Rich-based rendering

**Main view (daily table):**
```
╔══════════════════════════════════════════════════════════════╗
║  TOTAL: 1,234,567 tokens saved (68.5% reduction) | 42,133 calls  ║
╠══════════════════════════════════════════════════════════════╣
║  Date          Calls   Before       After        Saved      %     ║
║▶ 2026-06-18    1,234   5,678,901    1,823,456    3,855,445  67.9% ║
║  2026-06-17    1,189   5,234,567    1,723,456    3,511,111  67.1% ║
║  ...                                                             ║
╚══════════════════════════════════════════════════════════════════╝
  ↑/↓: move (1/10) | Enter/Space: daily detail | q: quit
```

**Detail view (per-call for a selected day):**
Shows individual compression calls: call_id, model, tokens before/after/saved, ratio, transforms applied.

### 4. Script CLI entry point

```
Usage:
    python -m eval.headroom_view_interactive
    python -m eval.headroom_view_interactive --days 10
```

## Implementation order
1. Add headroom Redis functions to `eval/redis_store.py`
2. Hook `_patched_compress` in `proxy/entrypoint.py` to store results
3. Create `eval/headroom_view_interactive.py`
