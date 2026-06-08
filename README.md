# GateMid — AI Gateway Middleware

Local-dev AI gateway combining [Headroom](https://github.com/chopratejas/headroom) context compression (ASGI middleware) with [LiteLLM](https://github.com/BerriAI/litellm) auto-routing.

**What it does:**
- Compresses prompts before they reach the LLM (60-95% token savings)
- Automatically routes queries to the right model by complexity
- Drop-in proxy — works with Claude Code, Open Code, and any OpenAI-compatible SDK

---

## Architecture

```
Claude Code / Application
      │  HTTP :4000
      ▼
┌─────────────────────────────────────────────┐
│  LiteLLM Proxy (Python process)             │
│                                             │
│  ASGI Middleware Stack (inbound):           │
│    1. Headroom CompressionMiddleware        │
│       ├─ SmartCrusher  (JSON)               │
│       ├─ CodeCompressor (AST)               │
│       └─ CacheAligner  (KV cache prefix)    │
│    2. ComplexityRouter (auto-tier)          │
│    3. Provider dispatch                     │
└─────────────────────────────────────────────┘
      │  HTTPS (outbound to provider)
      ▼
Gemini / DeepSeek API
```

Headroom now runs as **ASGI middleware** (`proxy/startup.py`) instead of the old callback approach — more reliable, fires at HTTP level on every request.

---

## Quick Start

### 1. Clone and set API keys

```bash
git clone <repo-url> llm-mid && cd llm-mid
cp .env.example .env
# Edit .env with your actual GEMINI_API_KEY and DEEPSEEK_API_KEY
```

### 2. Start the gateway

```bash
docker compose up -d
```

Verify it's running:

```bash
curl -s http://localhost:4000/health -H "Authorization: Bearer sk-local-dev-key" | head -c 200
```

### 3. Connect your tools (pick your setup below)

---

## Claude Code Setup

Configure Claude Code to route through GateMid. All prompts get compressed and auto-routed to the best model.

### Step 1: Configure environment

Add to your shell profile (`~/.zshrc`, `~/.bashrc`, etc.):

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_API_KEY="sk-local-dev-key"
export ANTHROPIC_MODEL="team-smart-router"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="gemini-flash"
export ANTHROPIC_DEFAULT_SONNET_MODEL="gemini-pro"
```

Then reload:

```bash
source ~/.zshrc  # or ~/.bashrc
```

### Step 2: Run Claude Code

```bash
claude
```

Claude Code now sends all requests through GateMid. The complexity router classifies each prompt and picks the right model. Headroom compresses large contexts automatically.

### How it works

```
claude (CLI)
  │  Anthropic-format request
  │  ANTHROPIC_BASE_URL → GateMid (:4000)
  ▼
GateMid (LiteLLM Proxy)
  │  1. Headroom ASGI middleware compresses context
  │  2. ComplexityRouter classifies prompt
  │  3. LiteLLM translates Anthropic → Gemini/Deepseek format
  │  4. Routes to resolved model
  ▼
Gemini / Deepseek API
```

> **Note:** GateMid translates between Anthropic and OpenAI/Gemini/Deepseek formats automatically via LiteLLM's provider abstraction. Claude Code's tool use, streaming, and system prompts all work.

### Per-project model overrides

Create `~/.claude/settings.json` to pin specific models per project or override the router:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:4000",
    "ANTHROPIC_API_KEY": "sk-local-dev-key",
    "ANTHROPIC_MODEL": "team-smart-router",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "gemini-flash",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "gemini-pro"
  }
}
```

### Bypassing the router

To use a specific model directly (no auto-routing):

```bash
export ANTHROPIC_MODEL="deepseek-pro"
claude
```

Available models: `gemini-flash`, `deepseek-flash`, `gemini-pro`, `deepseek-pro`, `team-smart-router`

---

## Open Code Setup

Open Code supports OpenAI-compatible backends natively.

### Step 1: Configure environment

Add to your shell profile:

```bash
export OPENAI_BASE_URL="http://localhost:4000/v1"
export OPENAI_API_KEY="sk-local-dev-key"
```

### Step 2: Create Open Code config

Create `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "gatemid": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "GateMid",
      "options": {
        "baseURL": "{env:OPENAI_BASE_URL}",
        "apiKey": "{env:OPENAI_API_KEY}"
      },
      "models": {
        "team-smart-router": { "name": "team-smart-router" },
        "gemini-flash": { "name": "gemini-flash" },
        "gemini-pro": { "name": "gemini-pro" },
        "deepseek-flash": { "name": "deepseek-flash" },
        "deepseek-pro": { "name": "deepseek-pro" }
      }
    }
  }
}
```

### Step 3: Run Open Code

```bash
opencode
```

Use `/connect` in the Open Code CLI and select the **GateMid** provider, or set the default model to `team-smart-router` for automatic routing.

### Direct model selection

To bypass the router and pick a model directly in Open Code:

```bash
opencode --model gemini-pro
```

---

## Manual Model Selection

You can also call models directly from any OpenAI-compatible SDK:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000/v1",
    api_key="sk-local-dev-key",
)

# Auto-routing (recommended)
response = client.chat.completions.create(
    model="team-smart-router",
    messages=[{"role": "user", "content": "Write a Rust async function"}],
)

# Direct model selection
response = client.chat.completions.create(
    model="deepseek-pro",
    messages=[{"role": "user", "content": "Explain quantum computing"}],
)
```

---

## How Routing Works

```
Incoming prompt
      │
      ▼
Headroom ASGI Middleware (~20ms)
      │  SmartCrusher (JSON), CodeCompressor (AST), CacheAligner (KV cache)
      ▼
ComplexityRouter (sub-millisecond, local)
      │
      ├─ SIMPLE    → gemini-flash     (greetings, definitions, yes/no)
      ├─ MEDIUM    → deepseek-flash   (general queries — default fallback)
      ├─ COMPLEX   → gemini-pro       (code, architecture, technical)
      └─ REASONING → deepseek-pro     (step-by-step, analysis, debugging)
      │
      ▼
Compressed prompt → Provider API
```

---

## Running Tests

```bash
# With the gateway running:
pip install pytest openai httpx
GATEMID_URL=http://localhost:4000 pytest tests/ -v
```

---

## Configuration

| File | Purpose |
|------|---------|
| `litellm_config.yaml` | Model routing, complexity router, provider config |
| `proxy/startup.py` | Headroom ASGI middleware registration |
| `docker-compose.yml` | Single-service Docker deployment |
| `.env` | Provider API keys (never commit) |

See [LiteLLM Proxy docs](https://docs.litellm.ai/docs/proxy/configs) and [Headroom docs](https://headroom-docs.vercel.app/) for all options.

---

## Dashboard

LiteLLM ships a built-in admin UI for monitoring spend, viewing logs, managing keys, and checking model health.

### Access the dashboard

```bash
open http://localhost:4000/ui/
```

Login with your master key: `sk-local-dev-key` (or whatever you set `GATEWAY_MASTER_KEY` to).

### What you can monitor

| Feature | Description |
|---|---|
| **Usage & Spend** | Track token usage and cost per model, per API key |
| **Logs** | View every request/response passing through the gateway |
| **Model Health** | See which models are healthy, latency, error rates |
| **API Keys** | Create and manage keys for team members |
| **Rate Limits** | Set RPM/TPM limits per key or model |

---

## Troubleshooting

### Gateway fails to start

Check the logs:

```bash
docker compose logs litellm
```

Common issues:
- **Missing API keys**: Ensure `.env` has valid `GEMINI_API_KEY` and `DEEPSEEK_API_KEY`
- **Port conflict**: Port 4000 already in use? Change `docker-compose.yml` ports mapping
- **Docker build fails**: Ensure Docker Desktop is running and you have internet access for pip

### Claude Code can't connect

```bash
# Verify the gateway is reachable
curl -s http://localhost:4000/health -H "Authorization: Bearer sk-local-dev-key"

# Check your env vars are set
echo $ANTHROPIC_BASE_URL
echo $ANTHROPIC_API_KEY
```

### Open Code can't connect

```bash
# Verify the OpenAI-compatible endpoint
curl -s http://localhost:4000/v1/models -H "Authorization: Bearer sk-local-dev-key"
```
