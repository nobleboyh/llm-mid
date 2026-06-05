# GateMid — AI Gateway Middleware

Local-dev AI gateway combining [Headroom](https://github.com/chopratejas/headroom) context compression with [LiteLLM](https://github.com/BerriAI/litellm) auto-routing.

**What it does:**
- Compresses prompts before they reach the LLM (60-95% token savings)
- Automatically routes queries to the right model by complexity
- Drop-in OpenAI-compatible API — no application code changes needed

## Quick Start

### 1. Set API Keys

```bash
cp .env.example .env
# Edit .env with your actual GEMINI_API_KEY and DEEPSEEK_API_KEY
```

### 2. Start the Gateway

```bash
docker compose up -d
```

### 3. Use It

Point any OpenAI-compatible SDK at the gateway:

```bash
export OPENAI_BASE_URL=http://localhost:4000/v1
export OPENAI_API_KEY=sk-local-dev-key
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:4000/v1",
    api_key="sk-local-dev-key",
)

response = client.chat.completions.create(
    model="team-smart-router",  # Auto-routing + compression
    messages=[{"role": "user", "content": "Write a Rust async function"}],
)
```

## How It Works

```
Client → GateMid (:4000)
           │
           ├─ ComplexityRouter: classifies prompt into SIMPLE/MEDIUM/COMPLEX/REASONING
           ├─ HeadroomCallback: compresses messages before forwarding
           └─ Routes to best-fit model:
                SIMPLE    → Gemini 2.5 Flash
                MEDIUM    → Deepseek V4 Flash (default)
                COMPLEX   → Gemini 2.5 Pro
                REASONING → Deepseek V4 Pro
```

## Manual Model Selection

You can also bypass the router and call models directly:

```python
# Use specific models
response = client.chat.completions.create(
    model="gemini-flash",    # or: deepseek-flash, gemini-pro, deepseek-pro
    messages=[...],
)
```

## Running Tests

```bash
# With the gateway running:
pip install pytest openai
GATEMID_URL=http://localhost:4000 pytest tests/ -v
```

## Configuration

Edit `litellm_config.yaml` to adjust:
- Model tier assignments
- Router classification thresholds
- Compression settings

See [LiteLLM Proxy docs](https://docs.litellm.ai/docs/proxy/configs) and [Headroom docs](https://headroom-docs.vercel.app/) for all options.

## Architecture

This project uses LiteLLM's native callback system. Zero custom Python application code — the integration is pure configuration:

- **ComplexityRouter** (`litellm.router_strategy.complexity_router`): Rule-based prompt classification
- **HeadroomCallback** (`headroom.integrations.litellm_callback`): Context compression hook
- **LiteLLM Proxy**: API server, auth, provider abstraction
