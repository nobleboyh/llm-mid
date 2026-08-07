# FEAT-healthcheck-leak — Eliminate Idle LLM Usage From `/health` Probes

**Status:** Implemented  
**Author:** Ito  
**Created:** 2026-08-07  
**Stack:** llm-mid / LiteLLM proxy on :4000

---

## 1. Problem Statement

GateMid bills DeepSeek usage **even when no client (Claude Code, Open Code) is making requests**. The idle usage appears in the provider's platform as real, charged completions, yet is invisible in GateMid's own observability:

- `eval.cli headroom` shows nothing (no compression stats recorded)
- The eval worker logs nothing
- The proxy's access log shows only `/health` requests

Investigation traced the leak to **LiteLLM's `/health` endpoint**, which executes a **live LLM probe against every deployment** on each call. The Docker `healthcheck` fires `curl /health` every 30 seconds, unconditionally, 24/7. Each probe is a genuine provider completion (message like `"test from litellm"`), so it is billed on every configured provider — including DeepSeek.

## 2. Evidence

LiteLLM 1.92.0 source (`litellm/proxy/health_endpoints/_health_endpoints.py:913-921`):

```
To run health checks in the background, add this to config.yaml:
    general_settings:
        background_health_checks: True
else, the health checks will be run on models when /health is called.
```

GateMid's `litellm_config.yaml` does **not** set `background_health_checks`. Therefore every `/health` request takes the live-probe path (`perform_health_check`), and for **each deployment** in the model list, LiteLLM issues a real completion (`litellm/proxy/health_check.py:205-222`):

```python
litellm_params["messages"] = _get_random_llm_message()
return await run_with_timeout(
    litellm.ahealth_check(litellm_params, prompt="test from litellm", ...),
)
```

### Why it hits DeepSeek

The probe runs against **every deployment**, including DeepSeek primary entries and DeepSeek fallbacks:

| Deployment | Primary | DeepSeek fallback |
|---|---|---|
| `gemini-flash` | `gemini-2.5-flash` | `deepseek-v4-flash` (order 2) |
| `gemini-pro` | `gemini-2.5-pro` | `deepseek-v4-flash` (order 2) |
| `deepseek-flash` | — | `deepseek-v4-flash` |
| `deepseek-pro` | — | `deepseek-v4-pro` + `deepseek-v4-flash` |
| `ragas-eval` | — | `deepseek-v4-flash` (order 1) |

Observed in proxy logs — 2 completions per health check, correlated 1:1 with the Docker health interval (30s), with the panic callback firing (`Skipping — absent or empty original_question ... model=`):

```
10:19:29  WARNING  proxy.callback  Skipping... model=gemini-2.5-flash   ← live probe completion #1
10:19:29  WARNING  proxy.callback  Skipping... model=gemini-2.5-flash   ← live probe completion #2
10:19:29  INFO  "GET /health HTTP/1.1" 200 OK                          ← Docker healthcheck response
```

### Why GateMid's own observability doesn't surface it

- **Not in `eval.cli headroom`** — probe prompts are ~7 tokens (`"test from litellm"`), well below `min_tokens_to_compress=250`; `tokens_saved = 0`, and `_patched_compress` only stores results when `result.tokens_saved > 0` (`proxy/entrypoint.py:116`).
- **Not logged by the eval worker** — the callback's `log_success_event` finds no real user question and returns early with the `Skipping — absent or empty original_question` warning, so nothing is enqueued for Ragas.
- **Not the Ragas eval loop** — the eval worker scored nothing when idle; the leak is LiteLLM's own health subsystem.

### Scale

- `GET /health` every 30s (Docker healthcheck, `docker-compose.yml:39`) = 2,880 `/health` calls/day.
- Each fires completions against ~5 DeepSeek deployments (2 fallbacks + 2 primary + ragas-eval) = **roughly 14,000 DeepSeek completions/day** of pure overhead.

---

## 3. Goal

Stop `/health` from triggering live, billed LLM probes. Prefer cached health state so the endpoint still reports model health without paying a provider charge on every call.

## 4. Scope

### In scope

- Configure LiteLLM to run **background health checks** and serve `/health` from the cached result
- Verify `/health` no longer produces LLM completions
- Optionally scope health probes so they don't cover unused / fallback deployments

### Out of scope (future FEATs)

- Removing/adding provider probes entirely for cost reasons
- Custom probe cadence / per-deployment health-check enablement toggles

---

## 5. User Story

> As a developer running GateMid with DeepSeek, I expected no provider charges when no client uses the gateway. Currently LiteLLM's every-30s Docker healthcheck triggers live, billed LLM probes on all deployments — including DeepSeek fallbacks — so I'm charged ~14k completions/day even when idle.

**Acceptance criteria**

1. `GET /health` returns healthy/unhealthy status **without** making live provider completions.
2. No new DeepSeek/Gemini completions appear in the provider dashboard purely from health checks.
3. `integration status` (`docker compose ps`)
 the proxy remains reported healthy.
4. Any configuration change is bind-mounted (`litellm_config.yaml`) — applied via `docker compose restart litellm`, no rebuild.

---

## 6. Proposed Solution

Two settings in `litellm_config.yaml`, both required for **zero** idle probes:

```yaml
general_settings:
  master_key: "os.environ/GATEWAY_MASTER_KEY"
  background_health_checks: true

model_list:
  - model_name: gemini-flash
    litellm_params:
      model: gemini/gemini-2.5-flash
      api_key: "os.environ/GEMINI_API_KEY"
    model_info:
      disable_background_health_check: true
  # ... every deployment carries the same model_info flag
```

- `background_health_checks: true` → `use_background_health_checks = True`. LiteLLM starts `_run_background_health_check()` (refresh cycle per `health_check_interval`), and `/health` serves the cached `health_check_results` instead of calling `perform_health_check` synchronously.
- `model_info.disable_background_health_check: true` on **every** deployment removes those deployments from the background loop's probe set (`model_count_enabled` → 0). The loop still runs and repopulates the cache with empty healthy/unhealthy lists, so `/health` stays 200 — but no provider is ever called by health checks.
- Together, the two settings yield **zero** idle probes: neither `/health` nor the background loop issues a billed provider completion.
- Routing no longer gets proactive health state; it relies on LiteLLM's failure-cooldown mechanism (`allowed_fails`, `cooldown_time`) plus the existing cross-model `router_settings.fallbacks`, which is how GateMid already routes in practice.
- Restart: `docker compose restart litellm` (config is bind-mounted; no image rebuild).
