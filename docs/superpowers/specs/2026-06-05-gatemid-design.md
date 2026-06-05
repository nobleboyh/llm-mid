# GateMid: AI Gateway Middleware — Design Spec

## Context

Team of 5-10 developers needs a local-dev middleware that combines **Headroom** (context compression) and **LiteLLM** (auto-routing) into a single gateway. The goal is to reduce token costs via compression and automatically route queries to the most appropriate model by complexity, with zero application-code changes for the team.

## Architecture

**Pattern**: LiteLLM Proxy + Callback Hook integration.

Both `ComplexityRouter` and `HeadroomCallback` are drop-in components from their respective packages. The project is overwhelmingly configuration — zero custom Python application code for MVP.

### Request Flow

```
Client SDK (OpenAI-compat)
  │ POST /v1/chat/completions  { model: "team-smart-router", messages: [...] }
  ▼
LiteLLM Proxy (FastAPI on :4000)
  │ 1. Auth (master key)
  │ 2. ComplexityRouter.async_pre_routing_hook()
  │    → Classifies prompt into SIMPLE/MEDIUM/COMPLEX/REASONING
  │    → Resolves to concrete model (e.g., gemini-flash)
  │ 3. HeadroomCallback.async_pre_call_hook()
  │    → headroom.compress() — SmartCrusher + CodeCompressor + Kompress-Base
  │    → Replaces messages with compressed payload
  │ 4. Forward to provider API (Gemini or Deepseek)
  ▼
Provider Response → Client
```

Route-before-compress order is guaranteed by LiteLLM's hook execution order — `async_pre_routing_hook` fires before `async_pre_call_hook`.

### Components

| Component | Source | Role |
|---|---|---|
| LiteLLM Proxy | `litellm` (1.87.1) | API server, auth, provider abstraction |
| ComplexityRouter | `litellm.router_strategy.complexity_router` | Rule-based prompt classification (<1ms) |
| HeadroomCallback | `headroom.integrations.litellm_callback` | Context compression in `pre_call_hook` |
| Config | Our `litellm_config.yaml` | Wires everything together |

## Model Routing

### Tier Mapping

| Tier | Model | Context | Purpose |
|---|---|---|---|
| SIMPLE | `gemini/gemini-2.5-flash` | 1M | Greetings, definitions, yes/no |
| MEDIUM | `deepseek/deepseek-v4-flash` | 1M | General queries (default) |
| COMPLEX | `gemini/gemini-2.5-pro` | 1M | Code, technical content, architecture |
| REASONING | `deepseek/deepseek-v4-pro` | 1M | Multi-step reasoning, analysis |

### Classification Dimensions

The ComplexityRouter scores prompts across seven dimensions with configurable weights:
- Token count (10%): <15 tokens → simple, >400 → complex
- Code presence (30%): function, class, import, async, etc.
- Reasoning markers (25%): "step by step", "analyze this", "break down"
- Technical terms (25%): architecture, distributed, encryption, etc.
- Simple indicators (5%): "what is", "define", greetings — negative weight
- Multi-step patterns (3%): numbered steps, "first...then"
- Question complexity (2%): multiple question marks

## Compression

Headroom's `compress()` runs locally in-process with three specialized compressors:
- **SmartCrusher**: JSON arrays, nested schemas, DB results — preserves anomalies and schema
- **CodeCompressor**: AST-based (Tree-Sitter) for Python, JS, Go, Rust, Java, C++ — preserves signatures
- **Kompress-Base**: ModernBERT-based prose compression for linguistic redundancies

Failure mode: if compression crashes, the original messages pass through unchanged (logged warning).

## Project Structure

```
llm-mid/
├── docker-compose.yml
├── Dockerfile
├── litellm_config.yaml
├── requirements.txt
├── .env.example
├── tests/
│   ├── test_routing.py
│   └── test_compression.py
└── README.md
```

## Key Configuration

### litellm_config.yaml

Three sections:
1. **model_list**: Four provider models + one virtual router model
2. **litellm_settings**: HeadroomCallback registration via `callbacks`
3. **general_settings**: Master key for auth

### Docker

- Base: `python:3.13-slim` (headroom's PyO3 requires ≤3.13)
- Build env: `PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1` (required for headroom Rust build)
- Build deps: maturin, puccinialin (headroom Rust build chain)
- Entry: `litellm --config /app/litellm_config.yaml --port 4000`
- Env vars: `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, `GATEWAY_MASTER_KEY`

### Team Onboarding

1. Set `GEMINI_API_KEY` and `DEEPSEEK_API_KEY` in `.env`
2. `docker compose up -d`
3. Point any OpenAI-compatible SDK at `http://localhost:4000/v1` with key `sk-local-dev-key`
4. Use model name `team-smart-router` — routing and compression are transparent

## Error Handling

All error handling is inherited from the underlying components:
- Headroom compression failure → original messages pass through
- Router classification failure → falls back to `default_model` (deepseek-flash)
- Provider API errors → LiteLLM's built-in retry/fallback
- Unrecognized model names (deepseek-v4-*) → LiteLLM pass-through to provider

## Testing

### test_routing.py
Black-box integration test: sends prompts of varying complexity to `/v1/chat/completions` and verifies the router selects the correct tier model by inspecting response metadata.

### test_compression.py
Sends a prompt with JSON blobs and code blocks through the gateway, verifies:
1. Token count is lower than uncompressed baseline
2. Response content is semantically equivalent to a non-compressed baseline

## Out of Scope (MVP)

- Redis for distributed state (single-instance only)
- CCR (Compress-Cache-Retrieve) — no retrieval of original compressed data
- `headroom learn` — no failure mining
- Streaming response handling
- MCP server integration
- Production deployment (no ALB, ECS, multi-AZ)
