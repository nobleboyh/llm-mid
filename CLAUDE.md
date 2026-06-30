# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Before anything else

Read [`docs/instructions/AGENTS.md`](docs/instructions/AGENTS.md) — it covers agent workflows, file map, common tasks, and the rule that **docs must be updated after every feature or bug fix**. When your change conflicts with those docs, ask before overwriting either.

Full project reference is under `docs/instructions/`:
- `PROJECT-CONTEXT.md` — architecture, service topology, design decisions
- `TECH-STACK.md` — dependencies, models, env vars, version compat
- `CODING-STANDARDS.md` — conventions, middleware patterns, Redis naming, anti-patterns
- `DATA-MODELS.md` — Redis schema and data flows
- `API-CONTRACTS.md` — endpoint formats, router contract, eval contract
- `UI-UX-GUIDELINES.md` — TUI patterns, keyboard nav, color coding
- `middleware/` — one doc per middleware layer (pipeline order, edge cases, behavior)

## Commands

```bash
# Start the gateway (all three containers)
docker compose up -d

# View logs
docker compose logs litellm
docker compose logs eval-worker

# Rebuild after code changes
docker compose build litellm && docker compose up -d litellm
docker compose build eval-worker && docker compose up -d eval-worker

# Health check
curl -s http://localhost:4000/health -H "Authorization: Bearer sk-local-dev-key"

# Run tests (gateway must be running for integration tests)
pip install pytest openai httpx
GATEMID_URL=http://localhost:4000 pytest tests/ -v

# Run a single test file
GATEMID_URL=http://localhost:4000 pytest tests/test_routing.py -v

# Run unit tests only (no Docker needed)
pytest tests/ --ignore=tests/test_compression.py --ignore=tests/test_routing.py -v

# Eval CLI — interactive score board
docker exec -it gatemid-headroom python -m eval.cli score

# Eval CLI — compression stats
docker exec -it gatemid-headroom python -m eval.cli headroom

# Clear Redis for fresh test data
docker exec gatemid-headroom python -m eval.cli clear-redis
```

## Architecture

GateMid is an AI gateway proxy on `localhost:4000` — it sits between coding tools (Claude Code, Open Code) and LLM providers (Gemini, DeepSeek). Three Docker containers: proxy (LiteLLM + Headroom + ASGI middleware), Redis (queue + scores + stats), eval-worker (async Ragas scoring).

**Request pipeline (inbound):**
```
ApiKeyMasking → CaptureOriginal → SkillInjector → HeadroomCompression → LiteLLM (ComplexityRouter → Provider)
```
After response: `RagasLogger` callback RPUSHes to Redis `eval:pending` for async scoring.

**Key constraints:**
- Middleware registration order is reverse — last `app.add_middleware()` call = outermost = runs FIRST inbound
- Middleware must never block the request — all exceptions caught, logged, passed through
- Litellm_config.yaml is bind-mounted — edit it and `docker compose restart litellm`, no rebuild
- Skills are auto-discovered from `.md` files in `proxy/skills/` — drop one in, rebuild proxy, use `$<stem>`
- Entrypoint is `proxy/entrypoint.py` (not `proxy/startup.py` — that's legacy)
- `entrypoint.py` monkey-patches Headroom's `compress()` and `ContentRouter.__init__` before registering middleware

**Eval loop prevention (two-tier):** RagasLogger skips calls where model starts with `ragas-eval`, or metadata flag `_ragas_eval_call` is set, or the call is internal LiteLLM → deepseek-v4 without `proxy_server_request`.

**Metric weights (dynamic):**
| Metric | With ctx | No ctx |
|--------|----------|--------|
| Faithfulness | 0.3 | skipped |
| Answer Relevancy | 0.3 | 1.0 |
| Context Precision | 0.2 | skipped |
| Context Recall | 0.2 | skipped |

Denominator adjusts when metrics are unavailable — composite always normalizes to [0, 1].

**IMPORTANT NOTE: THE WORK IN THIS PROJECT WILL BE USED FOR GEMINI AND CODEX REVIEW**
