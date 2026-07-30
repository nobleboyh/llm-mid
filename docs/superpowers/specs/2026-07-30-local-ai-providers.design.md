# Local AI Provider Support for GateMid

**Date:** 2026-07-30
**Status:** Draft
**Author:** AI-assisted design

## Problem

GateMid currently supports cloud-only LLM providers (Gemini, DeepSeek, Anthropic, OpenAI, GitHub Copilot/Models). Users running local inference via Ollama, llama.cpp, LM Studio, or oMLX cannot route through GateMid, losing compression, skill injection, complexity routing, and eval scoring for their local models.

## Goals

1. Add Ollama, llama.cpp, LM Studio, and oMLX as first-class providers in `quick-setup.sh`
2. User provides free-text model name at setup time (matching whatever their local server serves)
3. Local model failures (e.g. 404 "model not found") log clear, actionable diagnostics
4. Follow existing provider pattern — minimal new code, maximal reuse

## Non-goals

- Auto-discovery of running local instances (too fragile, port conflicts)
- Full offline mode (can be added later)
- Hot-swapping model names at runtime (user restarts config)

## Design

### A. Provider Registry

Add to `PROVIDER_REGISTRY` in `quick-setup.sh`. Each local provider has **one** model alias and requires no API key env var:

```bash
PROVIDER_REGISTRY+=(
  "ollama|Ollama (local)||ollama"
  "llamacpp|llama.cpp (local)||llamacpp"
  "lmstudio|LM Studio (local)||lmstudio"
  "omlx|oMLX (Apple Silicon)||omlx"
)
```

### B. New setup step: Local Endpoint Configuration

After `assign_fallbacks` and before `write_env`, a new step `configure_local_endpoints()` runs. For each enabled local provider, it asks:

```
  ── Local Provider Configuration ──

  Ollama endpoint URL [http://localhost:11434]:
  Model name (what your Ollama serves) [llama3]:

  llama.cpp endpoint URL [http://localhost:8080]:
  Model name (what your llama.cpp serves) [llama-3.2-3b]:

  LM Studio endpoint URL [http://localhost:1234]:
  Model name (what your LM Studio serves) [model]:

  oMLX endpoint URL [http://localhost:8000]:
  Model name (what your oMLX serves) [llama]:
```

Default endpoints:
- Ollama: `http://localhost:11434`
- llama.cpp: `http://localhost:8080`
- LM Studio: `http://localhost:1234`
- oMLX: `http://localhost:8000`

Model name defaults:
- Ollama: `llama3`
- llama.cpp: `llama-3.2-3b`
- LM Studio: `model`
- oMLX: `llama`

Values stored in `.env`:
```
OLLAMA_API_BASE=http://localhost:11434
OLLAMA_MODEL=llama3
LLAMACPP_API_BASE=http://localhost:8080
LLAMACPP_MODEL=llama-3.2-3b
LMSTUDIO_API_BASE=http://localhost:1234
LMSTUDIO_MODEL=model
OMLX_API_BASE=http://localhost:8000
OMLX_MODEL=llama
```

### C. model_backend() mapping

Free-text model name substituted at config generation time:

```bash
ollama)      echo "ollama/${OLLAMA_MODEL:-llama3}" ;;
llamacpp)    echo "openai/${LLAMACPP_MODEL:-llama-3.2}" ;;  # uses custom api_base + /v1
lmstudio)    echo "lm_studio/${LMSTUDIO_MODEL:-model}" ;;
omlx)        echo "openai/${OMLX_MODEL:-llama}" ;;          # uses custom api_base + /v1
```

### D. Litellm config generation

For each local model, the YAML includes api_base + dummy api_key. Models using the `openai/` prefix (llama.cpp, oMLX) need api_base ending in `/v1`:

```yaml
  # Ollama (native LiteLLM provider)
  - model_name: ollama
    litellm_params:
      model: ollama/llama3
      api_base: http://localhost:11434
      api_key: "ollama"

  # llama.cpp (OpenAI-compatible API)
  - model_name: llamacpp
    litellm_params:
      model: openai/llama-3.2-3b
      api_base: http://localhost:8080/v1
      api_key: "sk-no-key"

  # LM Studio (native LiteLLM provider)
  - model_name: lmstudio
    litellm_params:
      model: lm_studio/model
      api_base: http://localhost:1234
      api_key: "sk-no-key"

  # oMLX (OpenAI-compatible API)
  - model_name: omlx
    litellm_params:
      model: openai/llama
      api_base: http://localhost:8000/v1
      api_key: "sk-no-key"
```

### E. Local-provider failure callback

New file `proxy/callbacks/local_provider_failure.py` — a LiteLLM failure callback that detects local-provider errors (404s from any known local provider) and logs user-friendly diagnostics.

Detection logic:
1. Error contains status code 404
2. Model name starts with `ollama/`, `lm_studio/`, or api_base matches known local endpoints (localhost:11434, :8080, :1234, :8000)
3. Log: `ERROR [LocalModel] model 'xyz' not found on $PROVIDER. Run \`ollama list\` to see available models, or update OLLAMA_MODEL in .env`

Registered in `litellm_settings`:
```yaml
litellm_settings:
  failure_callbacks: ['proxy.callbacks.local_provider_failure']
```

### F. Error logging example output

```
ERROR [LocalModel] 404 — Ollama model 'mistral-v2' not found.
→ Run `ollama list` to see models available on that server.
→ Or update OLLAMA_MODEL in .env and restart GateMid.

ERROR [LocalModel] 404 — llama.cpp model 'codellama-34b' not found.
→ Check which model your llama.cpp server is serving (see terminal output).
→ Or update LLAMACPP_MODEL in .env and restart GateMid.

ERROR [LocalModel] 404 — LM Studio model 'deepseek-r1' not found.
→ Check which model is loaded in LM Studio's UI.
→ Or update LMSTUDIO_MODEL in .env and restart GateMid.

ERROR [LocalModel] 404 — oMLX model 'qwen2.5-32b' not found.
→ Check available models in ~/.omlx/models/.
→ Or update OMLX_MODEL in .env and restart GateMid.
```

### G. Docker networking note

Local providers run on the **host machine**, not inside Docker containers. The GateMid proxy (Docker container) needs to reach them via the host's network:

- **macOS**: use `host.docker.internal` (e.g. `http://host.docker.internal:11434`)
- **Linux**: use `--network=host` or `http://172.17.0.1:11434`
- **Windows**: use `host.docker.internal`

The `configure_local_endpoints()` step should **not** auto-rewrite — just log a warning if the URL starts with `localhost`, suggesting `host.docker.internal` instead.

### H. README updates

- Add local providers to "Available models" list: `ollama`, `llamacpp`, `lmstudio`, `omlx`
- Add local-provider examples to Claude Code / Open Code config sections
- Add troubleshooting section for local provider connection checks + model name mismatches
- Note: eval scoring requires Gemini embeddings — unavailable if running purely local
- Document Docker networking caveat for local providers

## Future considerations

- Dynamic model listing via `ollama list`, llama.cpp `/v1/models`, oMLX file-system scan (nice-to-have for setup wizard)
- Full offline mode: skip Gemini embedding dependency entirely
- Containerized local inference: running Ollama/llama.cpp/oMLX as sibling Docker containers on the same Docker network

## Files changed

| File | Change |
|------|--------|
| `quick-setup.sh` | Provider registry, `configure_local_endpoints()`, config gen |
| `proxy/callbacks/local_provider_failure.py` | New — failure callback |
| `litellm_config.example.yaml` | Local provider entries |
| `README.md` | Local provider docs |
