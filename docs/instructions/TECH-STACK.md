# Tech Stack — GateMid

## Runtime

| Component | Technology | Version |
|-----------|-----------|---------|
| Language | Python | 3.11+ |
| Package manager | pip | latest |
| Container runtime | Docker + Docker Compose | latest |

## Core dependencies

### Proxy container (`proxy/requirements.txt`)

| Package | Version | Purpose |
|---------|---------|---------|
| `litellm[proxy]` | ≥1.40.0 | LLM proxy server, provider abstraction, complexity router |
| `headroom-ai[proxy]` | ≥0.8.0 | Context compression (ASGI middleware) |
| `redis` | ≥5.0.0 | Redis client for eval queue and storage |
| `uvicorn` | ≥0.29.0 | ASGI server (used internally by LiteLLM) |

### Eval worker container (`requirements-eval.txt`)

| Package | Version | Purpose |
|---------|---------|---------|
| `ragas` | ≥0.1.0 | LLM response quality evaluation metrics |
| `datasets` | ≥2.14.0 | Hugging Face datasets (Ragas dependency) |
| `redis` | ≥5.0.0 | Redis client |
| `openai` | ≥1.0.0 | OpenAI-compatible client for LLM-as-judge calls |
| `pandas` | ≥1.5.0 | Data manipulation (Ragas dependency) |
| `rich` | ≥13.0.0 | Terminal UI rendering for interactive score/headroom boards |

### Key transitive dependencies (implicit)

| Package | Why it matters |
|---------|---------------|
| `httpx` | HTTP client used by Gemini embeddings, OpenAI client, and tests |
| `tiktoken` | Token counting for skill injection (optional, falls back to char/4 heuristic) |
| `fastapi` | Web framework (LiteLLM is built on FastAPI) |
| `starlette` | ASGI toolkit — middleware base classes |

## Infrastructure

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Proxy server | LiteLLM Proxy (FastAPI + Uvicorn) | LLM request handling and routing |
| Cache / queue | Redis 7 (Alpine) | Eval queue, scored records, leaderboards, compression stats |
| Compression | Headroom.ai | SmartCrusher (JSON), CodeCompressor (AST), CacheAligner (KV-cache) |
| Routing | LiteLLM ComplexityRouter | Rule-based 7-dimension prompt classifier |
| Eval framework | Ragas 0.4.x | Faithfulness, answer relevancy, context precision/recall |
| Embeddings | Google Gemini (`gemini-embedding-001`) | REST API via httpx (no PyTorch) |
| LLM-as-judge | DeepSeek Flash (via LiteLLM proxy) | OpenAI-compatible endpoint for Ragas scoring |

## Provider models (configured in `litellm_config.yaml`)

| Alias | Provider Model | Used for |
|-------|---------------|---------|
| `gemini-flash` | `gemini/gemini-2.5-flash` | Fast model via Gemini API |
| `gemini-pro` | `gemini/gemini-2.5-pro` | Capable model via Gemini API |
| `deepseek-flash` | `deepseek/deepseek-v4-flash` | Fast model via DeepSeek API |
| `deepseek-pro` | `deepseek/deepseek-v4-pro` | Capable model via DeepSeek API |
| `ragas-eval` | `deepseek/deepseek-v4-flash` | LLM-as-judge for Ragas scoring |
| `team-smart-router` | `auto_router/complexity_router` | Auto-classifies and routes to tier models |

## Docker images

| Service | Base image | Key additions |
|---------|-----------|---------------|
| `litellm` | `python:3.11-slim` | curl, litellm, headroom, redis client |
| `redis` | `redis:7-alpine` | AOF persistence (`--save 60 1`) |
| `eval-worker` | `python:3.11-slim` | gcc (for numpy/pandas), ragas, datasets, openai, rich |

## Development dependencies (test suite)

Install with: `pip install pytest httpx openai`
Plus: `proxy/requirements.txt` and `requirements-eval.txt`

## Environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `GEMINI_API_KEY` | Yes (for eval) | — | Gemini API key for provider + embeddings |
| `DEEPSEEK_API_KEY` | Yes | — | DeepSeek API key for provider + LLM-as-judge |
| `ANTHROPIC_API_KEY` | No | — | Anthropic API key (if using Claude models) |
| `OPENAI_API_KEY` | No | — | OpenAI API key (if using GPT models) |
| `GITHUB_API_KEY` | No | — | GitHub token (Copilot/Copilot-codex models) |
| `GATEWAY_MASTER_KEY` | No | `sk-local-dev-key` | LiteLLM proxy master key |
| `HF_TOKEN` | No | — | Hugging Face token (sped up model downloads) |
| `REDIS_URL` | No | `redis://redis:6379` (container) / `redis://localhost:6379` (host) | Redis connection |
| `LITELLM_URL` | No | `http://litellm:4000` | Proxy URL (used by eval worker) |
| `RAGAS_EVAL_ENABLED` | No | `true` | Set to `true` to activate eval worker |
| `EVAL_RECORD_TTL_DAYS` | No | `30` | TTL for scored records and headroom data |
| `GATEMID_URL` | No | `http://localhost:4000` | Gateway URL (used by test suite) |

## CI/CD

GitHub Actions (`.github/workflows/ci.yml`):
- Python 3.11 on Ubuntu
- Unit tests (ignore Docker-dependent tests)
- Docker-dependent tests run separately with `continue-on-error: true`

## Version compatibility notes

- **Ragas 0.4.x**: Uses `BaseRagasEmbedding` base class. Earlier versions used `LangchainEmbeddingsWrapper` (deprecated).
- **LiteLLM 1.82+**: `Message.content` typed as `Union[str, List[...]]` — content extraction must handle both.
- **Headroom 0.23.0**: CompressConfig defaults `compress_user_messages=False` — patched in `entrypoint.py`.
- **redis-py 8.x**: Default `socket_timeout=5` — must be set to `None` for blocking `BRPOP`.
