"""Monkey-patches LiteLLM Router to log and store clear FALLBACK events.

Two hooks:
  1. async_get_available_deployment  — logs when the Router selects a
     non-primary deployment (order > 1) because the primary is in cooldown
     or unhealthy. This handles the multi-deployment-within-model_name case.

  2. _get_fallback_model_group_from_fallbacks  — logs when the Router
     falls back to a different model group via router_settings.fallbacks
     (cross-model fallback).

Each event is also persisted to Redis for the TUI board.
"""

import logging
import uuid
from datetime import datetime, timezone

import litellm.router

logger = logging.getLogger("proxy.fallback")


def apply_patches() -> None:
    """Apply monkey-patches to LiteLLM Router. Safe to call multiple times."""

    # ── Patch 1: async_get_available_deployment ──────────────────────────
    # Fires on EVERY request. We log only when the selected deployment has
    # order > 1, meaning the primary (order=1) is in cooldown — a fallback.
    _orig_get_deployment = (
        litellm.router.Router.async_get_available_deployment
    )

    async def _patched_get_deployment(
        self, model, request_kwargs, messages=None,
        input=None, specific_deployment=False,
    ):
        deployment = await _orig_get_deployment(
            self, model, request_kwargs, messages,
            input, specific_deployment,
        )
        if isinstance(deployment, dict):
            lp = deployment.get("litellm_params", {}) or {}
            order = lp.get("order", 1) or 1
            deployed_model = lp.get("model", "unknown")
            model_name = deployment.get("model_name") or model
            if order > 1:
                logger.info(
                    "FALLBACK (order=%d): %s → %s",
                    order, model_name, deployed_model,
                )
                _store_fallback(
                    model_group=model_name,
                    deployment_model=deployed_model,
                    order=order,
                    fallback_type="order",
                )
        return deployment

    litellm.router.Router.async_get_available_deployment = (
        _patched_get_deployment
    )

    # ── Patch 2: cross-model group fallback ─────────────────────────────
    # Fires when the Router must switch to a different model group.
    _orig_get_fallback_group = (
        litellm.router.Router._get_fallback_model_group_from_fallbacks
    )

    def _patched_get_fallback_group(self, fallbacks, model_group):
        result = _orig_get_fallback_group(self, fallbacks, model_group)
        if result is not None:
            fbs = ", ".join(str(m) for m in result)
            logger.info(
                "FALLBACK (cross-model): %s → [%s]",
                model_group, fbs,
            )
            _store_fallback(
                model_group=model_group,
                deployment_model=fbs,
                order=0,
                fallback_type="cross-model",
            )
        return result

    litellm.router.Router._get_fallback_model_group_from_fallbacks = (
        _patched_get_fallback_group
    )

    logger.info("Fallback logger patches applied")


def _store_fallback(
    model_group: str,
    deployment_model: str,
    order: int,
    fallback_type: str = "order",
    original_exception: str | None = None,
) -> None:
    """Persist a fallback event to Redis. Fire-and-forget (silent on failure)."""
    try:
        from eval.redis_store import store_fallback_result

        store_fallback_result(
            call_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc).isoformat(),
            model_group=model_group,
            deployment_model=deployment_model,
            order=order,
            fallback_type=fallback_type,
            original_exception=original_exception,
        )
    except Exception as exc:
        logger.debug("Failed to store fallback result to Redis: %s", exc)
