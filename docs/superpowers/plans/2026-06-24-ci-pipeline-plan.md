# CI Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create a GitHub Actions workflow that runs unit tests on push/PR and lets Docker-dependent tests fail gracefully.

**Architecture:** Single job (`test`) on `ubuntu-latest` with Python 3.11. Unit tests run as the main step (must pass). Docker-dependent tests run in a separate `continue-on-error: true` step (non-blocking). Integration tests auto-skip in CI via existing `@pytest.mark.skipif` guards.

**Tech Stack:** GitHub Actions, Python 3.11, pytest

## Global Constraints

- Python 3.11 only (matches production Dockerfile)
- Single workflow file at `.github/workflows/ci.yml`
- No composite actions or build matrices
- Trigger on `push` (any branch) and `pull_request` (any branch)
- Docker-dependent tests must not block the pipeline (`continue-on-error: true`)
- Must use `actions/checkout@v4` and `actions/setup-python@v5`

---

### Task 1: Create CI workflow file

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: nothing — this is the only task
- Produces: a workflow that GitHub Actions can run

- [ ] **Step 1: Write the workflow file**

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python 3.11
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install -r proxy/requirements.txt
          pip install -r requirements-eval.txt
          pip install pytest httpx openai

      - name: Run unit tests
        run: python -m pytest tests/ --ignore=tests/test_compression.py --ignore=tests/test_routing.py --tb=short -v

      - name: Run Docker-dependent tests (allowed to fail)
        continue-on-error: true
        run: python -m pytest tests/test_compression.py tests/test_routing.py --tb=short -v
```

- [ ] **Step 2: Verify workflow syntax**

No local validation tool needed — GitHub validates workflow YAML on push. Visually check:
- Indentation is consistent (2-space YAML)
- `continue-on-error: true` is at the correct nesting level (under the step, not the job)
- `on:` blocks have no branch filters (any branch)
- Matrix uses single version `["3.11"]`

- [ ] **Step 3: Commit**

```bash
mkdir -p /Users/itobeo/code/llm-mid/.github/workflows
git -C /Users/itobeo/code/llm-mid add .github/workflows/ci.yml
git -C /Users/itobeo/code/llm-mid commit -m "ci: add GitHub Actions pipeline for unit tests

Runs unit tests on push and PR to any branch. Docker-dependent
compression and routing tests run separately with continue-on-error,
so they never block the pipeline.

Co-Authored-By: Claude <noreply@anthropic.com>"
```
