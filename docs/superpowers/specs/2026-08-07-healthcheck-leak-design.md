# Eliminate Idle LLM Usage From `/health` Probes — Design

**Date:** 2026-08-07
**Status:** Approved
**Author:** Ito
**Spec source:** `docs/requirements/init-007-healthcheck-leak.md`

## Problem

GateMid bills provider usage even when no client (Claude Code, Open Code) is
making requests. The Docker `healthcheck` fires `curl /health` every 30 seconds,
24/7. LiteLLM's `/health` endpoint performs a **live LLM probe** against every
deployment in `model_list` — a real, billed completion per deployment per call.
With ~5 DeepSeek deployments in the config, that is ~14,000 billed DeepSeek
completions/day of pure overhead, invisible in GateMid's own observability
(probe prompts are ~7 tokens, below `min_tokens_to_compress`; the eval callback
skips them as having no real user question).

Verified against installed `litellm 1.92.0` source:
- `/health` live-probe path: `litellm/proxy/health_endpoints/_health_endpoints.py` —
  `if use_background_health_checks: return health_check_results` else
  `_perform_health_check_and_save(...)`.
- Background loop: `litellm/proxy/proxy_server.py:3206-3405` — filters
  `model_info.disable_background_health_check` at line 3264.

## Goal

`GET /health` returns healthy/unhealthy status **without** triggering live,
billed provider completions. Acceptance criterion: *no new DeepSeek/Gemini
completions purely from health checks*.

**Decision (approved): zero probes.** Both the on-demand `/health` probes and the
periodic background-loop probes are disabled. This fully satisfies the acceptance
criteria at the cost of losing proactive health state; routing falls back to
LiteLLM's failure-cooldown mechanism (`allowed_fails`, `cooldown_time`) plus the
existing cross-model `router_settings.fallbacks`, which is how GateMid already
routes in practice.

## Solution

Two settings, both required:

1. `general_settings.background_health_checks: true` — makes `GET /health`
   serve cached `health_check_results` instead of live-probing every deployment.
2. `model_info.disable_background_health_check: true` on **every** deployment —
   removes those deployments from the background loop's probe set
   (`model_count_enabled` → 0).

The Docker `healthcheck` command in `docker-compose.yml` is unchanged — `/health`
stays 200, so the proxy container remains reported healthy.

### Files changed

| File | Change |
|------|--------|
| `litellm_config.example.yaml` | `background_health_checks: true` in `general_settings`; `model_info.disable_background_health_check: true` on all 10 deployments |
| `litellm_config.test.yaml` | Same — all 8 deployments |
| `quick-setup.sh` | `write_litellm_config()`: add `background_health_checks: true` to the `general_settings` heredoc; emit `model_info.disable_background_health_check: true` at every deployment-writing site (primary loop, order-2 fallback, ragas-eval primary, ragas-eval fallback). Merge with existing `mode: responses` model_info for `copilot-codex` — a YAML map cannot have two `model_info` keys |
| `litellm_config.yaml` (live, gitignored, bind-mounted) | Apply the same settings directly so the fix is live now without re-running `quick-setup.sh` |
| `tests/test_healthcheck_config.py` (new) | Unit test (no Docker): load `litellm_config.example.yaml` and `litellm_config.test.yaml`, assert `general_settings.background_health_checks` is true and every deployment carries `model_info.disable_background_health_check: true` |
| `docs/instructions/TECH-STACK.md` | Note the new `general_settings` options |
| `docs/requirements/init-007-healthcheck-leak.md` | Flip status to implemented once verified |

### Generator details (`quick-setup.sh`)

Deployment-writing sites in `write_litellm_config()`:

- **Primary model loop** (~line 702): after the existing `mode: responses`
  model_info block (line 720), append the disable flag. If `mode: responses`
  is present, emit both keys under the same `model_info:` block.
- **Order-2 fallback** (~line 737): emit a standalone `model_info` block after
  `order: 2`.
- **Ragas-eval primary** (~line 786): emit a standalone `model_info` block.
- **Ragas-eval fallback** (~line 812): emit a standalone `model_info` block.
- **Team Smart Router** (~line 848): standalone `model_info` block in the
  `team-smart-router` heredoc (complexity router deployment).

## Verification

1. `docker compose restart litellm` — config is bind-mounted, no image rebuild
   (acceptance criterion #4).
2. `docker compose ps` — proxy remains `healthy` (criterion #3).
3. Watch proxy logs for 60–70s (longer than `health_check_interval` default of
   300s would be ideal, but the absence of probe warnings right after a restart
   plus `model_count_enabled=0` in the background-cycle debug log confirms):
   - no `Skipping... model=` probe warnings after `/health` hits,
   - background loop logs show `model_count_enabled=0`.
4. `curl /health` returns 200 quickly from cached results.
5. `pytest tests/test_healthcheck_config.py -v` passes.

## Trade-offs (accepted)

- **No proactive health state.** The router no longer knows a deployment is down
  until a real request fails; it then relies on `allowed_fails: 3` /
  `cooldown_time: 60` cooldown plus `router_settings.fallbacks`. First request to
  a down provider may fail before cooldown kicks in. This is LiteLLM's default
  behavior without health checks and is consistent with how GateMid already routes.
- **Per-deployment verbosity.** Each deployment gains a 3-line `model_info` block
  (10 in the example config, 8 in the test config). Enforced by the new config
  test so future edits can't silently reintroduce probes.

## Out of scope

- Provider-probe removal for cost reasons beyond health checks.
- Custom probe cadence / per-deployment health-check toggles (beyond the global
  disable already applied).
- Changing the Docker `healthcheck` cadence or command.
