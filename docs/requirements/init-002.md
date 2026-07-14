# PRD: LiteLLM + Headroom Token Compression Gateway

**Status:** Draft  
**Decision:** Topology 1 — LiteLLM Proxy first, Headroom as ASGI middleware (same host)  
**Audience:** Engineering Lead / DevOps  

---

## 1. Problem Statement

Claude Code and production healthcare application services send raw, uncompressed
messages to LLM providers. The primary token cost drivers are:

- **CLI tool output** — `git`, `mvn test`, `grep`, `find`, `docker` dumped verbatim into context
- **FHIR/HL7 tool outputs** — raw JSON Bundles, DiagnosticReport resources, Observation arrays (10k–50k tokens each)
- **Model prose responses** — verbose assistant turns in long-running agent sessions
- **Conversation history** — full message history re-sent on every turn

Current state: no compression, no routing intelligence, direct provider calls. Cost and
latency scale linearly with payload size.

---

## 2. Goals

| Goal | Metric |
|------|--------|
| Reduce input token cost | 60–85% reduction on FHIR/HL7 payloads |
| Reduce CLI context bloat | 60–99% on bash tool outputs |
| Reduce output token cost | 65% reduction on dev-session prose (Caveman) |
| Centralise routing & auth | Single proxy, one master key, all providers |
| Minimise latency overhead | Net latency negative (compression saves more at provider than it costs) |
| Single host deployment | No extra network hops between compression and routing |

---

## 3. Architecture Decision

### Chosen: Topology 1 — LiteLLM first, Headroom as ASGI middleware

```
┌─────────────────────────────────────────────────────────────────────────┐
│  SAME HOST (Docker Compose, single machine)                             │
│                                                                         │
│  Claude Code / Application                                              │
│       │                                                                 │
│       │  HTTP  :4000                                                    │
│       ▼                                                                 │
│  ┌─────────────────────────────────────────────┐                        │
│  │  LiteLLM Proxy (Python process)             │                        │
│  │                                             │                        │
│  │  ASGI Middleware Stack (inbound):            │                        │
│  │    1. Headroom CompressionMiddleware         │                        │
│  │       ├─ SmartCrusher  (JSON/FHIR/HL7)      │                        │
│  │       ├─ CodeCompressor (Java/Spring AST)   │                        │
│  │       └─ CacheAligner  (KV cache prefix)    │                        │
│  │    2. LiteLLM routing / auth / fallback      │                        │
│  │    3. Provider dispatch                      │                        │
│  └─────────────────────────────────────────────┘                        │
│       │                                                                 │
│       │  HTTPS (outbound to provider)                                   │
│       ▼                                                                 │
│  OpenAI / Anthropic / AWS Bedrock / Azure OpenAI                        │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

### Why Topology 1 over Topology 2

| Concern | Topology 1 (chosen) | Topology 2 (rejected) |
|---------|--------------------|-----------------------|
| Network hops | 1 (app → proxy) | 2 (app → headroom → litellm) |
| Compression reliability | ASGI HTTP-level, immune to callback bugs | Same |
| Cost observability | LiteLLM sees **real** pre-compression token counts | Sees compressed counts — dashboards underreport |
| Retry on fallback | Compresses once, retries with same payload | Fine but extra hop |
| Operational complexity | 1 proxy process to manage | 2 proxy processes |
| KV cache hit rate | CacheAligner fires before provider call | Same |

### Why ASGI middleware over HeadroomCallback

| Concern | ASGI Middleware (chosen) | HeadroomCallback (rejected) |
|---------|--------------------------|-----------------------------|
| Reliability | HTTP-level, always fires | Router `acompletion` known to miss CustomLogger hooks intermittently |
| Async path | Native ASGI, clean async | LiteLLM hook dispatch overhead |
| Coverage | All routes including streaming | Callback only |

---

## 4. Compression Layer Map

Three compression layers operate at different points in the request lifecycle.
All three run simultaneously — they target completely different content.

```
Layer 1 — RTK (Rust Token Killer)
  Where:   Developer machine, PreToolUse bash hook in Claude Code
  What:    CLI output: git, mvn, pytest, find, grep, docker, kubectl
  Saves:   60–99% of bash tool output tokens
  Latency: <10ms (Rust binary, zero deps)
  Config:  rtk init --global  (writes .claude/settings.json hook)

Layer 2 — Caveman Skill
  Where:   CLAUDE.md system prompt (Claude Code sessions only)
  What:    Model prose output — assistant turn verbosity
  Saves:   65% of output tokens in dev sessions
  Latency: 0ms (prompt-side instruction, no middleware)
  Config:  npx caveman install  +  CLAUDE.md injection
  ⚠️  NOT injected at proxy level — unsafe for clinical/production calls

Layer 3 — Headroom ASGI Middleware
  Where:   Inside LiteLLM proxy process (ASGI middleware stack)
  What:    FHIR Bundles, HL7 JSON, RAG chunks, conversation history
  Saves:   60–85% of structured JSON payloads
  Latency: 15–50ms (SmartCrusher only, ML disabled for JSON-heavy stack)
  Config:  app.add_middleware(CompressionMiddleware, disable_ml=True)
```

---

## 5. File Structure

```
litellm-headroom/
├── docker-compose.yml            # single service — litellm only
├── .env                          # secrets — never commit
├── litellm_config.yaml           # model routing config
├── proxy/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── startup.py                # ASGI middleware registration
└── claude-code/
    ├── CLAUDE.md                 # caveman skill injection
    └── settings.json             # RTK PreToolUse hook
```

---

## 6. Docker Compose

```yaml
# docker-compose.yml

services:

  # ── LiteLLM Proxy + Headroom ASGI middleware (same process) ──────────────
  litellm:
    build:
      context: ./proxy
      dockerfile: Dockerfile
    container_name: litellm-headroom
    restart: unless-stopped
    ports:
      - "4000:4000"
    environment:
      # Provider keys — only populate what you use locally
      OPENAI_API_KEY:        ${OPENAI_API_KEY:-""}
      ANTHROPIC_API_KEY:     ${ANTHROPIC_API_KEY:-""}
      AZURE_API_KEY:         ${AZURE_API_KEY:-""}
      AZURE_API_BASE:        ${AZURE_API_BASE:-""}
      AWS_ACCESS_KEY_ID:     ${AWS_ACCESS_KEY_ID:-""}
      AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY:-""}
      AWS_REGION_NAME:       ${AWS_REGION_NAME:-"us-east-1"}
      # LiteLLM
      LITELLM_MASTER_KEY:    ${LITELLM_MASTER_KEY:-"sk-local-dev"}
      # Headroom (optional — only needed for Headroom Cloud features)
      HEADROOM_API_KEY:      ${HEADROOM_API_KEY:-""}
    volumes:
      - ./litellm_config.yaml:/app/litellm_config.yaml:ro
      - ./proxy/startup.py:/app/startup.py:ro
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:4000/health"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
```

---

## 7. Proxy Dockerfile

```dockerfile
# proxy/Dockerfile
FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# LiteLLM config and startup hook are bind-mounted at runtime
# so they can be edited without rebuilding the image

EXPOSE 4000

# startup.py registers Headroom ASGI middleware before litellm starts
CMD ["python", "-m", "litellm", \
     "--config", "/app/litellm_config.yaml", \
     "--port", "4000", \
     "--startup_file", "/app/startup.py"]
```

```text
# proxy/requirements.txt
litellm[proxy]>=1.40.0
headroom-ai[proxy]>=0.8.0
uvicorn>=0.29.0
```

---

## 8. Headroom ASGI Middleware Registration

```python
# proxy/startup.py
# This file is executed by LiteLLM's --startup_file hook before the server starts.
# It registers Headroom as ASGI middleware on the LiteLLM FastAPI app.

from litellm.proxy.proxy_server import app
from headroom.integrations.asgi import CompressionMiddleware
import logging

logger = logging.getLogger("headroom.startup")

app.add_middleware(
    CompressionMiddleware,

    # Skip compression on small payloads — saves CPU for short queries
    # that wouldn't benefit much anyway
    min_tokens=300,

    # FHIR/HL7 payloads are JSON — SmartCrusher gives 70-85% reduction.
    # Disabling the ML model (Kompress-base) cuts compression latency
    # from ~100-200ms to ~15-50ms. Re-enable if you have prose-heavy RAG.
    disable_ml=True,

    # CacheAligner stabilises message prefixes to improve KV cache hit rate.
    # Free latency saving — a KV cache hit cuts TTFT from ~3s to ~0.3s on Claude.
    enable_cache_aligner=True,

    # Routes that should NEVER be compressed:
    # - /health, /metrics — not LLM calls
    # - /v1/embeddings — embeddings don't benefit from message compression
    excluded_paths=[
        "/health",
        "/metrics",
        "/v1/embeddings",
        "/v1/moderations",
    ],
)

logger.info("Headroom CompressionMiddleware registered on LiteLLM proxy")
```

---

## 9. LiteLLM Routing Config

```yaml
# litellm_config.yaml

model_list:

  # ── Anthropic (direct) ────────────────────────────────────────────────────
  - model_name: claude-sonnet
    litellm_params:
      model: anthropic/claude-sonnet-4-20250514
      api_key: os.environ/ANTHROPIC_API_KEY
      max_retries: 2

  - model_name: claude-opus
    litellm_params:
      model: anthropic/claude-opus-4-20250514
      api_key: os.environ/ANTHROPIC_API_KEY
      max_retries: 2

  # ── OpenAI ────────────────────────────────────────────────────────────────
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
      max_retries: 2

  # ── AWS Bedrock (fallback for Anthropic) ──────────────────────────────────
  - model_name: claude-sonnet
    litellm_params:
      model: bedrock/anthropic.claude-sonnet-4-20250514-v1:0
      aws_access_key_id:     os.environ/AWS_ACCESS_KEY_ID
      aws_secret_access_key: os.environ/AWS_SECRET_ACCESS_KEY
      aws_region_name:       os.environ/AWS_REGION_NAME

  # ── Azure OpenAI (fallback for OpenAI) ───────────────────────────────────
  - model_name: gpt-4o
    litellm_params:
      model: azure/gpt-4o
      api_key:  os.environ/AZURE_API_KEY
      api_base: os.environ/AZURE_API_BASE

router_settings:
  # Load balance + fallback: tries first healthy endpoint, falls back on failure
  routing_strategy: least-busy
  num_retries: 2
  retry_after: 5
  allowed_fails: 2            # mark endpoint unhealthy after 2 consecutive failures
  cooldown_time: 60           # seconds before retrying a failed endpoint

litellm_settings:
  # Verbose off — Headroom logs compression stats separately
  set_verbose: false

  # Drop unknown params rather than erroring (useful during provider upgrades)
  drop_params: true

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

---

## 10. Environment File

```bash
# .env — DO NOT COMMIT

# Provider keys — only populate what you use locally
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
AZURE_API_KEY=
AZURE_API_BASE=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION_NAME=us-east-1

# LiteLLM
LITELLM_MASTER_KEY=sk-local-dev    # or: openssl rand -hex 32

# Headroom (optional — required only for Headroom Cloud features)
HEADROOM_API_KEY=
```

---

## 11. Claude Code Integration

### RTK (Layer 1 — shell output)

```bash
# Install RTK
brew install rtk-ai/rtk/rtk       # macOS
# Windows: download from https://github.com/rtk-ai/rtk/releases

# Wire PreToolUse hook into Claude Code globally
rtk init --global

# Verify
rtk gain
```

This writes to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "rtk rewrite"
          }
        ]
      }
    ]
  }
}
```

### Point Claude Code at the LiteLLM proxy

```bash
# In shell profile or .env
export ANTHROPIC_BASE_URL=http://localhost:4000
export ANTHROPIC_API_KEY=sk-litellm-...   # your LITELLM_MASTER_KEY
```

Or in Claude Code settings:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4000",
    "ANTHROPIC_API_KEY": "sk-litellm-YOUR_MASTER_KEY"
  }
}
```

### Caveman (Layer 2 — output tokens, dev sessions only)

```bash
# Install caveman skill
npx caveman install
```

Add to project `CLAUDE.md` — only for Claude Code / dev sessions.
**Do not inject into production healthcare application system prompts.**

```markdown
<!-- caveman-begin -->
RESPOND CAVEMAN STYLE.
- Remove: filler, pleasantries, hedge words, transition phrases, apologies
- Keep: ALL technical content, code, file paths, error messages, line numbers, numbers
- Use: fragments over full sentences where meaning is fully preserved
- Level: FULL
<!-- caveman-end -->
```

Compress the `CLAUDE.md` file itself to save input tokens on every session start:

```bash
npx caveman compress CLAUDE.md   # saves backup as CLAUDE.md.original
```

---

## 12. Production Application Integration

For your Spring Boot / Java services calling through the proxy:

```java
// application.yml
anthropic:
  base-url: http://localhost:4000   # or http://litellm:4000 inside Docker
  api-key:  ${LITELLM_MASTER_KEY}

// Never inject Caveman system prompts here.
// Headroom (Layer 3) is transparent — no code changes needed.
// SmartCrusher handles FHIR Bundle compression automatically.
```

---

## 13. Request Lifecycle (Full Flow)

```
Claude Code issues: mvn test
  │
  ├─ RTK PreToolUse hook intercepts (Layer 1, ~5ms)
  │   mvn test → compressed: "BUILD SUCCESS. Tests: 142 passed."
  │   [was 8,000 tokens → 40 tokens]
  │
  ▼
POST http://localhost:4000/v1/messages
  │
  ├─ Headroom ASGI middleware fires (Layer 3, ~20ms)
  │   SmartCrusher compresses FHIR JSON tool outputs in messages[]
  │   CacheAligner stabilises prefix for KV cache
  │   [FHIR Bundle: 25,000 tokens → 4,000 tokens]
  │
  ├─ LiteLLM routing: selects claude-sonnet, tries Anthropic direct first
  │   Falls back to Bedrock if Anthropic returns 529
  │
  ▼
Anthropic API receives compressed payload
  │
  ▼
Response from Claude
  │
  ├─ Caveman CLAUDE.md instruction shapes output (Layer 2, 0ms overhead)
  │   "NullPointerException line 42. Root: repo null. Fix: add null check."
  │   [was 180 tokens → 55 tokens]
  │
  ▼
Claude Code receives compressed response
```

---

## 14. Token Savings Estimate (Healthcare Agent Session)

| Content type | Volume / session | Without compression | With all 3 layers |
|---|---|---|---|
| `mvn test` / `git` CLI output | ~50 bash calls | ~400k tokens | ~8k tokens |
| FHIR Bundle tool outputs | ~20 calls × 25k tokens | ~500k tokens | ~80k tokens |
| Model prose responses | ~100 turns | ~50k tokens | ~17k tokens |
| CLAUDE.md system prompt | every turn | ~2k tokens | ~1.1k tokens |
| **Total** | | **~952k tokens** | **~106k tokens** |
| **Saving** | | — | **~89%** |

Costs vary by model and provider. At Claude Sonnet input pricing (~$3/M tokens),
a 952k → 106k reduction saves approximately **$2.50 per agent session**.

---

## 15. Operational Notes

### Health checks

```bash
# Proxy health
curl http://localhost:4000/health

# RTK savings dashboard
rtk gain

# Headroom response headers (check compression is firing)
curl -s -I -X POST http://localhost:4000/v1/messages \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet","messages":[{"role":"user","content":"hi"}],"max_tokens":10}' \
  | grep -i headroom
# Expected: x-headroom-compressed: true
#           x-headroom-tokens-saved: N
```

### When to disable Headroom per-call

For calls where you explicitly need the raw payload delivered (e.g. structured extraction
prompts where exact field names matter), pass a header:

```http
X-Headroom-Skip: true
```

### Scaling beyond local dev

When promoting to production or a shared team environment, add these incrementally:

1. Add PostgreSQL for spend tracking and virtual key management
2. Add Redis for rate limiting and prompt caching
3. Both are drop-in additions to `docker-compose.yml` — no changes to `startup.py` or `litellm_config.yaml`
4. At that point, scale the LiteLLM + Headroom container horizontally behind a load balancer
5. Headroom is stateless — each container runs middleware independently

---

## 16. Not In Scope

- Caveman injection at proxy level — explicitly excluded (patient safety risk on clinical content)
- Topology 2 (Headroom first) — rejected due to extra network hop and broken cost observability
- Separate Headroom proxy container — rejected, adds process boundary overhead for zero benefit
- ML-based prose compression (Kompress-base) — disabled for FHIR/HL7 workloads; re-evaluate if RAG prose becomes significant