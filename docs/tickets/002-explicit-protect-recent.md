# TICKET-002: Make `protect_recent` explicit in `proxy/entrypoint.py`

**Priority**: Low — observability / maintainability
**Area**: Headroom CompressConfig
**Reported by**: ASGI Middleware Audit (2026-07-15)

## Problem

`proxy/entrypoint.py` patches `headroom.compress.compress` to inject
`CompressConfig(compress_user_messages=True, min_tokens_to_compress=250)`, but
it does **not** set `protect_recent`. This means the value silently inherits
Headroom's default (`protect_recent=4`).

If Headroom ever changes its default, the proxy's behavior would change without
any code diff in the proxy repo. Also, a future reader has to trace through
three layers (entrypoint → compress → CompressConfig dataclass) to know what
the value actually is.

## Impact

No functional impact today — `protect_recent=4` is intentionally left at default.
This is purely a self-documenting maintenance change.

## Fix

In `entrypoint.py:_patched_compress`, add `protect_recent=4` to the
`CompressConfig()` call:

```python
config = CompressConfig(
    compress_user_messages=True,
    min_tokens_to_compress=250,
    protect_recent=4,   # explicit — no longer relies on Headroom default
)
```

Also add `protect_recent=4` to the else-branch at line 83 where it mutates a
caller-provided config:

```python
config.compress_user_messages = True
config.protect_recent = 4   # explicit
```

## How to verify

- `grep -n "protect_recent" proxy/entrypoint.py` shows the value.
- `docker compose restart litellm` — no behavior change.
