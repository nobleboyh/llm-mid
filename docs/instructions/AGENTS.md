# Agent Instructions — GateMid

> How to work with this codebase as an AI agent or human developer.

## Project Identity

GateMid is a **local-dev AI gateway proxy** that sits between AI coding tools (Claude Code, Open Code) and LLM providers (Gemini, DeepSeek). It compresses prompts, auto-routes by complexity, injects skill directives, and asynchronously scores response quality — all without adding latency.

Think of it as middleware for your LLM traffic: every request and response passes through a pipeline of ASGI middlewares before reaching LiteLLM's routing engine.

## Key facts every agent must know

1. **This is Python 3.11+** — use modern type hints, f-strings, and `dict | None` syntax.

2. **Entrypoint is `proxy/entrypoint.py`** — not `proxy/startup.py` (that's legacy). `entrypoint.py` monkey-patches Headroom's compress function, registers all ASGI middleware, configures loggers, loads skills, and then hands off to LiteLLM's `run_server()`.

3. **Middleware order is critical.** FastAPI/Starlette applies middleware in reverse registration order:
   ```
   Registration order (last=outermost, runs first inbound):
     app.add_middleware(CompressionMiddleware)       # 1st registered = innermost = runs LAST inbound
     app.add_middleware(SkillInjectorMiddleware)      # 2nd = runs THIRD inbound
     app.add_middleware(CaptureOriginalMiddleware)    # 3rd = runs SECOND inbound
     app.add_middleware(ApiKeyMaskingMiddleware)      # 4th registered = outermost = runs FIRST inbound

   Inbound request flow:
     ApiKeyMasking → CaptureOriginal → SkillInjector → Compression → LiteLLM
   ```

4. **Monkey-patches are intentional.** `entrypoint.py` patches:
   - `headroom.compress.compress` — forces `compress_user_messages=True` and hooks Redis storage
   - `headroom.transforms.content_router.ContentRouter.__init__` — forces `enable_kompress=True`, `skip_user_messages=False`

5. **Three-container Docker architecture.** Services communicate over Docker network:
   - `litellm` (proxy) — ASGI middleware + LiteLLM routing on :4000
   - `redis` — eval queue + compression stats + leaderboards
   - `eval-worker` — BLPOPs from Redis, scores with Ragas, writes back

6. **Eval loop prevention is two-tier.** The `RagasLogger` callback skips scoring when:
   - Model name starts with `ragas-eval` (prefix check)
   - Metadata flag `_ragas_eval_call` is set
   - It also checks deepseek-v4 family models from internal LiteLLM calls

7. **Skills are auto-discovered.** Drop a `.md` file into `proxy/skills/` and it's live at next restart. The `$<stem>` trigger activates it. Multiple triggers per message are supported.

8. **Ponytail coding style** — the project itself follows minimalism: no unnecessary abstractions, shortest working diff, delete over add. See `proxy/skills/ponytail.md`.

## Before making changes

- Read `docs/instructions/PROJECT-CONTEXT.md` for architecture overview
- Read `docs/instructions/TECH-STACK.md` for dependency and environment details
- Read `docs/instructions/CODING-STANDARDS.md` for conventions
- Read the relevant middleware doc in `docs/instructions/middleware/` for the layer you're touching
- Check `docs/instructions/DATA-MODELS.md` for Redis schema and data flow
- Check `docs/instructions/API-CONTRACTS.md` for request/response formats

## After implementing a feature or bug fix

**Always update the relevant docs in this directory** to reflect the new reality. Stale docs are worse than no docs — they mislead the next agent. Specifically:

- **Feature added?** Update `PROJECT-CONTEXT.md` (architecture), `TECH-STACK.md` (new deps), `DATA-MODELS.md` (new schema), and the relevant middleware doc.
- **Middleware changed?** Update the corresponding middleware doc under `middleware/` — pipeline order, behavior, edge cases.
- **API contract changed?** Update `API-CONTRACTS.md` — request/response format, new endpoints, changed routing.
- **New config option?** Update `TECH-STACK.md` environment variables table.
- **Bug fixed that contradicts docs?** Update the doc that described the broken behavior.

## When your fix conflicts with these docs

**Always ask the user before making a change that contradicts the documentation.** If the code says one thing and these docs say another, surface it:

> "The API-CONTRACTS.md says X, but the code in `callback.py:45` does Y. Which one is the intended behavior?"

Do not silently overwrite either. The doc might be stale, or the code might be wrong. The user decides.


## Common tasks

### Adding a new skill
1. Create `proxy/skills/<name>.md` with your skill prompt
2. Rebuild the proxy container: `docker compose build litellm && docker compose up -d litellm`
3. Use `$<name>` in any message

### Adding a new ASGI middleware
1. Create the middleware class (follow pattern in existing ones)
2. Register in `proxy/entrypoint.py` with `app.add_middleware()`
3. Ensure correct order in the pipeline
4. Add logging setup in `_setup_logger()` calls

### Changing routing configuration
1. Edit `litellm_config.yaml` (bind-mounted, no rebuild needed)
2. Restart the proxy: `docker compose restart litellm`

### Running tests
```bash
docker compose up -d
pip install pytest openai httpx
GATEMID_URL=http://localhost:4000 pytest tests/ -v
```

## File map quick reference

| Concern | Files |
|---------|-------|
| Startup & middleware registration | `proxy/entrypoint.py` |
| Model routing config | `litellm_config.yaml` |
| API key masking | `proxy/guardrails/api_key_masking.py` |
| Question capture | `proxy/capture_original.py` |
| Skill injection | `proxy/skill_injector.py`, `proxy/skills/registry.py` |
| Compression (Headroom) | Third-party, patched in `proxy/entrypoint.py` |
| Eval callback (enqueue) | `proxy/callback.py` |
| Eval scoring (worker) | `eval/worker.py`, `eval/worker_main.py` |
| Redis data layer | `eval/redis_store.py` |
| Gemini embeddings | `eval/gemini_embeddings.py` |
| CLI tools | `eval/cli.py`, `eval/score_view*.py`, `eval/headroom_view*.py` |
| Docker infra | `docker-compose.yml`, `proxy/Dockerfile`, `eval/Dockerfile` |
| Tests | `tests/` |

## Naming conventions

- **Middleware classes**: PascalCase with `Middleware` suffix → `SkillInjectorMiddleware`
- **Files**: `snake_case.py` for modules, `kebab-case.md` for docs
- **Redis keys**: `prefix:type:identifier` → `eval:call:{call_id}`, `headroom:day:{YYYY-MM-DD}`
- **Environment variables**: `UPPER_SNAKE_CASE` → `GATEMID_URL`, `RAGAS_EVAL_ENABLED`
- **Loggers**: `__name__` is used, producing names like `proxy.callback`, `eval.worker`

## Logging conventions

All custom loggers use a single `StreamHandler` to stdout with format:
```
HH:MM:SS  LEVEL     name  message
```

Setup is centralized in `entrypoint.py:_setup_logger()`. The ComplexityRouter logger has a custom filter to only show routing decisions. LiteLLM's verbose loggers are suppressed to WARNING level.
