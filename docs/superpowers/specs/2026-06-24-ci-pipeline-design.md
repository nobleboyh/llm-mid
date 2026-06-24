# CI Pipeline: GitHub Actions for llm-mid

**Date:** 2026-06-24  
**Author:** Claude (via brainstorming)  
**Status:** Approved design

## Goals

- Run unit tests automatically on every `push` (any branch) and `pull_request` (any branch)
- Support Docker-dependent integration tests — skip them when Docker is unavailable, allow them to fail without blocking the pipeline
- Minimal configuration, single job, no composite actions or matrices

## Triggers

```yaml
on:
  push:
  pull_request:
```

Both trigger on any branch — no branch filters.

## Job: `test`

**Runner:** `ubuntu-latest`  
**Python version:** 3.11 (matches production Dockerfile)

### Step 1 — Checkout & Setup
- `actions/checkout@v4`
- `actions/setup-python@v5` with Python 3.11

### Step 2 — Install dependencies
- `pip install -r proxy/requirements.txt` (LiteLLM proxy + Headroom + redis)
- `pip install -r requirements-eval.txt` (Ragas, datasets, etc.)
- `pip install pytest httpx openai` (test runner and HTTP client for integration tests)

### Step 3 — Run unit tests (must pass)
- These are tests that don't require a running Docker proxy:
  - `test_callback.py` — pure mocks
  - `test_ragas_runner.py` — pure mocks
  - `test_redis_store.py` — pure mocks
  - `test_guardrails.py` — unit tests run fine; integration classes auto-skip via `@pytest.mark.skipif(not os.environ.get("GATEWAY_MASTER_KEY"))`
  - `test_skill_injector.py` — unit tests run fine; integration classes auto-skip via same `skipif`
- Command: `python -m pytest tests/ --ignore=tests/test_compression.py --ignore=tests/test_routing.py --tb=short -v`
- Must pass for the pipeline to succeed

### Step 4 — Docker-dependent tests (allowed to fail)
- These tests require a live proxy (Docker compose):
  - `test_compression.py` — uses `gateway_ready` fixture, will `pytest.fail()` after 30s timeout
  - `test_routing.py` — uses `gateway_ready` fixture, will `pytest.fail()` after 30s timeout
- Uses `continue-on-error: true` — never blocks the pipeline regardless of outcome
- Command: `python -m pytest tests/test_compression.py tests/test_routing.py --tb=short -v`
- When a self-hosted runner with Docker support is added, this step will naturally pass

## Docker test handling strategy

| Test file | In CI | With Docker |
|-----------|-------|-------------|
| `test_callback.py` | ✅ Runs (mocked) | ✅ Runs |
| `test_ragas_runner.py` | ✅ Runs (mocked) | ✅ Runs |
| `test_redis_store.py` | ✅ Runs (mocked) | ✅ Runs |
| `test_guardrails.py` (unit) | ✅ Runs (pure logic) | ✅ Runs |
| `test_skill_injector.py` (unit) | ✅ Runs (pure logic) | ✅ Runs |
| `test_compression.py` | ❌ Fails (wait timeout) | ✅ Runs (needs proxy) |
| `test_routing.py` | ❌ Fails (wait timeout) | ✅ Runs (needs proxy) |
| `test_guardrails.py` (integration) | ⏭️ Skipped (skipif) | ✅ Runs |
| `test_skill_injector.py` (integration) | ⏭️ Skipped (skipif) | ✅ Runs |

## Output expectations

- A green checkmark when all unit tests pass (even if Docker step is skipped/failing)
- A yellow/orange mark when Docker step fails but unit tests pass (allowed failure)
- A red X when unit tests fail

## File

- `.github/workflows/ci.yml`

## Future considerations

- Adding a self-hosted runner with Docker makes the integration tests run automatically
- A scheduled weekly run with Docker containers could catch integration regressions
- Caching `pip` packages between runs would speed up CI (~1 min saved)
- Matrix testing across Python versions if needed later
