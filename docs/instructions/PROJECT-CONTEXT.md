# Project Context — GateMid

## What is GateMid?

GateMid is a **local-dev AI gateway proxy** that combines:

1. **Headroom context compression** — 60-95% token savings via SmartCrusher (JSON), CodeCompressor (AST), and CacheAligner (KV-cache alignment)
2. **LiteLLM auto-routing** — rule-based complexity classifier routes each prompt to the right model
3. **Skill injection** — `$trigger` tokens (e.g. `$ponytail`, `$caveman`) inject behavior-constraining system prompts
4. **Async Ragas quality scoring** — every response is scored for faithfulness, relevancy, and precision

It runs as a Docker-based proxy on `localhost:4000`, consuming Anthropic-format or OpenAI-format requests from AI coding tools, transforming them, routing them to the appropriate provider, and returning responses.

## Why it exists

The project solves several problems simultaneously:

- **Token costs** — Claude Code's large system prompts (~500+ tokens) waste money on every call. Headroom compresses them before they reach the LLM.
- **Model selection fatigue** — users shouldn't have to think about which model to use for which query.
- **Quality observability** — without scoring, you can't know if model changes or prompt experiments are actually improving output quality.
- **Behavioral control** — skill directives let users dial output style (minimalism, conciseness) without editing tool config.

## Architecture overview

```
┌──────────────────────────────────────────────────────────────┐
│                     AI Coding Tool                            │
│              (Claude Code / Open Code / SDK)                  │
│         Sends Anthropic or OpenAI-format requests             │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTP POST to localhost:4000
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                   GateMid Proxy (:4000)                       │
│                                                              │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  ASGI Middleware Stack (inbound order)                │    │
│  │  1. ApiKeyMaskingMiddleware     — mask API keys      │    │
│  │  2. CaptureOriginalMiddleware   — snapshot question   │    │
│  │  3. SkillInjectorMiddleware     — $trigger → skill   │    │
│  │  4. Headroom CompressionMiddleware — compress payload │    │
│  └──────────────────────────────────────────────────────┘    │
│                           │                                  │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  LiteLLM Proxy Engine                                 │    │
│  │  - ComplexityRouter classifies prompt complexity     │    │
│  │  - Routes to resolved model (Gemini/DeepSeek)        │    │
│  │  - Translates between provider formats               │    │
│  └──────────────────────────────────────────────────────┘    │
│                           │                                  │
│  ┌──────────────────────────────────────────────────────┐    │
│  │  RagasLogger Callback (fire-and-forget)              │    │
│  │  - Extracts {question, answer, contexts}             │    │
│  │  - RPUSH to Redis eval:pending queue                 │    │
│  └──────────────────────────────────────────────────────┘    │
└──────────────────────────┬───────────────────────────────────┘
                           │ HTTPS to provider APIs
                           ▼
                   Gemini / DeepSeek API


┌──────────────────────────────────────────────────────────────┐
│                 Eval Worker (separate container)              │
│                                                              │
│  Loop:                                                        │
│  1. BLPOP from Redis eval:pending                            │
│  2. Ragas scoring (DeepSeek-as-judge + Gemini embeddings)    │
│  3. Write scored record → Redis hash + sorted sets           │
│  4. Update leaderboards                                      │
└──────────────────────────────────────────────────────────────┘
```

## Key architectural decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Middleware approach | ASGI middleware (not LiteLLM callbacks) | Fires at HTTP level on every request; covers all routes including streaming |
| Entrypoint | Custom `entrypoint.py` (not `--startup_file`) | Full control over Headroom patches, middleware order, and logger config |
| Routing timing | Route before compress | ComplexityRouter sees original prompt for correct classification |
| Compression model | SmartCrusher only (Kompress disabled) | Target is JSON/FHIR/HL7 — ONNX model adds latency and ~500MB download |
| Eval judge model | Direct DeepSeek via LiteLLM proxy | Uses `ragas-eval` model alias; callback skips self-scoring |
| Skill injection timing | Before compression | Skill text gets compressed with payload — ~150-250 net tokens |
| Loop prevention | Two-tier (model prefix + metadata flag) | Prevents Ragas from scoring its own evaluation calls |

## Service topology

```
┌──────────────┐     ┌──────────────┐     ┌────────────────┐
│ gatemid-     │────▶│ gatemid-     │◀────│ gatemid-eval-  │
│ headroom     │     │ redis        │     │ worker         │
│ (proxy)      │     │ (:6379)      │     │                │
│ (:4000)      │     │              │     │                │
└──────┬───────┘     └──────────────┘     └────────────────┘
       │                                       │
       │ HTTPS to providers                    │ HTTPS to LiteLLM
       ▼                                       ▼
  Gemini / DeepSeek API              http://litellm:4000/v1
```

**Communication paths:**
- Proxy → Redis: LPUSH eval records, HINCRBY headroom stats
- Eval worker → Redis: BLPOP queue, HSET scored records
- Eval worker → Proxy: OpenAI-compatible calls for Ragas LLM-as-judge (model=`ragas-eval`)
- Proxy → Providers: HTTPS (Gemini / DeepSeek APIs)
- AI tools → Proxy: HTTP POST (`/v1/messages`, `/v1/chat/completions`)

## Configuration philosophy

- **Zero-config for basic use** — `docker compose up -d` after setting API keys
- **Edit `litellm_config.yaml`** for routing changes (bind-mounted, no rebuild)
- **Drop `.md` files into `proxy/skills/`** to add skills (restart proxy to load)
- **Environment variables** in `.env` for secrets, `docker-compose.yml` for infrastructure
- **No database migrations** — Redis is schema-less, data is self-describing

## Project lifecycle

The project started from init docs (`init-001.md` through `init-005-skill-injection.md`) that track the iterative design process. These are historical artifacts, not active documentation. The canonical reference is the `docs/` directory.

## Related documentation

- `AGENTS.md` — instructions for AI agents working on this codebase
- `TECH-STACK.md` — dependency inventory and environment details
- `CODING-STANDARDS.md` — code conventions and patterns
- `DATA-MODELS.md` — Redis schema and data flow
- `API-CONTRACTS.md` — request/response formats and provider translation
- `UI-UX-GUIDELINES.md` — TUI component patterns
- `middleware/` — detailed docs for each middleware layer
