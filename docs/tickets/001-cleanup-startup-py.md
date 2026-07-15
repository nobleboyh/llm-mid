# TICKET-001: Clean up dead code in `proxy/startup.py`

**Priority**: Low — maintenance
**Area**: Proxy middleware registration
**Reported by**: ASGI Middleware Audit (2026-07-15)

## Problem

`proxy/startup.py` is a dead code path. It registers `CompressionMiddleware` with
parameters (`disable_ml=True`, `enable_cache_aligner=True`) that:

1. Are not valid `CompressionMiddleware.__init__` kwargs — calling them would raise
   `TypeError` at runtime.
2. Conflict with the active path (`proxy/entrypoint.py`) which intentionally enables
   the ML model and does not enable CacheAligner.

The file exists because LiteLLM supports a `--startup_file` hook, but `entrypoint.py`
bypasses that by calling `run_server()` directly. The `--startup_file` path is never
exercised.

## Impact

Low — no runtime impact today. But misleading to future readers trying to understand
middleware configuration. Someone editing this file will get the wrong impression
about how compression is configured.

## Options

1. **Delete** `proxy/startup.py` — it's a complete orphan, documented as legacy.
2. **Sync** it with `entrypoint.py` — keep it as a reference alternative, but update
   params to match the active config.

Option 1 is recommended. The file's purpose is fully replaced by `entrypoint.py`.

## How to verify

- `grep -r "startup_file\|startup\.py\|--startup" --include="*.py" --include="*.yml" --include="*.yaml" .`
- Confirm no references remain.
