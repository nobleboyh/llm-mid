# Coding Standards — GateMid

## Philosophy

This project follows **Ponytail principles** (see `proxy/skills/ponytail.md`):
- Shortest working diff wins
- No unrequested abstractions
- Deletion over addition
- Boring over clever
- Fewest files possible

Code is written to be read, not admired. Patterns that appear repeatedly are deliberate; one-off code doesn't get generalized until the third use.

## Python conventions

### Language level
- Python 3.11+ — use modern syntax freely:
  - `dict | None` union types (not `Optional[dict]`)
  - `list[dict]` generics (not `List[dict]` from typing)
  - f-strings for string formatting
  - Structural pattern matching (`match/case`) where it clarifies control flow

### Imports
- `from __future__ import annotations` at top of every module
- Standard library first, then third-party, then local
- Avoid `import *` — be explicit

### Type hints
- All public functions have type hints
- Use `| None` not `Optional`
- Use built-in generics: `list[str]`, `dict[str, Any]`
- ContextVars typed with generic parameter: `contextvars.ContextVar[str]`

### Docstrings
- Module-level: one-line purpose, sometimes with usage example
- Class-level: brief purpose, registration order context for middleware
- Function-level: one-line for simple, multi-line with `Parameters`/`Returns` for complex
- No docstrings for obvious private helpers

### Error handling
- **Never let middleware exceptions block the request** — catch, log, pass through
- Redis operations are fire-and-forget with `try/except: pass` — eval is best-effort
- Log exceptions with `logger.exception()` for full tracebacks
- `_sanitize()` pattern for NaN/inf floats before Redis ZADD

### Logging
```python
logger = logging.getLogger(__name__)  # or explicit name like "proxy.callback"
```
- All custom loggers use single stdout `StreamHandler`
- Format: `HH:MM:SS  LEVEL     name  message`
- Levels: DEBUG (detailed flow), INFO (significant events), WARNING (recoverable issues)
- LiteLLM verbose loggers suppressed to WARNING except ComplexityRouter

## Middleware patterns

Every ASGI middleware in this project follows the same pattern:

```python
class XxxMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # 1. Guard: skip non-HTTP, wrong method, wrong path
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope.get("method") != "POST" or not matches_path(path):
            await self.app(scope, receive, send)
            return

        # 2. Buffer: read full request body
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body: chunks.append(body)
                if not message.get("more_body", False): break

        full_body = b"".join(chunks)

        # 3. Transform: modify the body
        try:
            data = json.loads(full_body)
            # ... transform data ...
            full_body = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass  # non-JSON → pass through

        # 4. Forward: modified_receive sends the (possibly) modified body exactly once,
        #    then delegates to the real receive for streaming disconnect detection
        body_sent = False
        async def modified_receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {"type": "http.request", "body": full_body, "more_body": False}
            return await receive()

        await self.app(scope, modified_receive, send)
```

Key middleware invariants:
- **Buffer once** — read the full body, transform it, forward it
- **JSON only** — non-JSON bodies pass through unmodified
- **modified_receive** pattern — sends modified body exactly once, then delegates to real receive
- **modified_send** pattern — intercepts response headers to add custom headers or strip Content-Length

## Testing patterns

- Tests live in `tests/` at project root
- `conftest.py` provides `gateway_url` and `gateway_ready` session fixtures
- Tests that need the gateway running set `GATEMID_URL` env var
- Unit tests for pure logic (routing, scoring, guardrails) can run without Docker
- Integration tests need `docker compose up -d` first

### Test file conventions
```python
"""Module docstring — what's being tested."""
import pytest

def test_specific_scenario():
    """What specific scenario is being tested."""
    result = function_under_test(input)
    assert result == expected

class TestComponent:
    """Group related tests."""
    def test_case_1(self): ...
    def test_case_2(self): ...
```

## Redis naming

| Pattern | Example | Type |
|---------|---------|------|
| `eval:pending` | — | List (queue) |
| `eval:call:{call_id}` | `eval:call:a1b2c3` | Hash (scored record) |
| `eval:scores:all` | — | ZSet (global leaderboard) |
| `eval:scores:cat:{category}` | `eval:scores:cat:fhir_query` | ZSet (category leaderboard) |
| `eval:scores:prompt:{prompt_id}` | `eval:scores:prompt:v2_system_prompt` | ZSet (prompt version ranking) |
| `eval:meta:stats` | — | Hash (running counters) |
| `headroom:call:{call_id}` | `headroom:call:x1y2z3` | Hash (compression result) |
| `headroom:day:{YYYY-MM-DD}` | `headroom:day:2026-06-30` | Hash (daily aggregate) |
| `headroom:days` | — | ZSet (date index) |
| `headroom:totals` | — | Hash (grand totals) |

Rules:
- Use colon separators
- 30-day TTL on call records and daily stats
- Sorted sets kept indefinitely (IDs + scores only)

## Environment variable conventions

- All uppercase with underscores: `GEMINI_API_KEY`, `REDIS_URL`
- Secrets in `.env` (never committed)
- Defaults in `docker-compose.yml` with `${VAR:-default}` syntax
- Config values from env, not hardcoded
- Use `os.environ.get()` or `os.getenv()` with sensible defaults

## Commit conventions

- Follow standard conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`
- Messages end with:
  ```
  Co-Authored-By: Claude <noreply@anthropic.com>
  ```
- PR descriptions end with:
  ```
  🤖 Generated with [Claude Code](https://claude.com/claude-code)
  ```

## Anti-patterns (avoid)

- ❌ Abstracting a one-line function into a utility module
- ❌ Adding a config option for something that never changes
- ❌ Creating an interface/ABC with one implementation
- ❌ Breaking middleware pattern to do something "clever"
- ❌ Silent error suppression without at least a debug log
- ❌ Hardcoding values that should come from environment
- ❌ Modifying `proxy/startup.py` (it's legacy — use `entrypoint.py`)
