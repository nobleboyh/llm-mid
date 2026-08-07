"""LocalProviderFailureLogger — LiteLLM failure callback for local AI providers.

Detects 404 errors from local inference servers (Ollama, llama.cpp, LM Studio, oMLX)
and logs actionable diagnostic messages pointing the user to the correct model name.

Behavior is purely diagnostic — never blocks or modifies the request/response.
"""

from __future__ import annotations

import logging
import re

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("proxy.callbacks.local_provider")

# Known local provider patterns for matching in error messages and model names.
# Each entry: (provider_label, model_name_prefixes, diagnostic_hint)
_LOCAL_PROVIDERS = [
    {
        "label": "Ollama",
        "model_prefixes": ("ollama/",),
        "hint": "Run `ollama list` to see models available on that server.",
        "config_var": "OLLAMA_MODEL",
    },
    {
        "label": "llama.cpp",
        "model_prefixes": (),
        "hint": "Check which model your llama.cpp server is serving (see terminal output).",
        "config_var": "LLAMACPP_MODEL",
    },
    {
        "label": "LM Studio",
        "model_prefixes": ("lm_studio/",),
        "hint": "Check which model is loaded in LM Studio's UI.",
        "config_var": "LMSTUDIO_MODEL",
    },
    {
        "label": "oMLX",
        "model_prefixes": (),
        "hint": "Check available models in ~/.omlx/models/.",
        "config_var": "OMLX_MODEL",
    },
]


class LocalProviderFailureLogger(CustomLogger):
    """Logs friendly diagnostics when a local provider returns 404."""

    def _get_provider(self, model: str) -> dict | None:
        """Return the matching local-provider config, or None."""
        model_lower = model.lower()
        for prov in _LOCAL_PROVIDERS:
            for prefix in prov["model_prefixes"]:
                if model_lower.startswith(prefix):
                    return prov
        # Fallback: heuristic based on api_base URL patterns
        # Some providers (llama.cpp, oMLX) use the generic "openai/" prefix,
        # so we can't identify them from the model name alone.
        # We rely on api_base matching instead — captured in kwargs.
        return None

    def _api_base_is_local(self, api_base: str | None) -> dict | None:
        """Check if api_base points to a known local provider port."""
        if not api_base:
            return None
        ab = api_base.lower()
        # Port-based heuristic matching — anchored so `:8080` doesn't match
        # inside `:18080` or `:114341`.
        port_map = [
            (r"11434", 0),  # Ollama
            (r"8080",  1),  # llama.cpp
            (r"1234",  2),  # LM Studio
            (r"8000",  3),  # oMLX
        ]
        for port, idx in port_map:
            if re.search(rf":{port}(?:[/:]|$)", ab):
                return _LOCAL_PROVIDERS[idx]
        return None

    def _log_failure(self, kwargs) -> None:
        """Log a diagnostic when a local provider returns a 404.

        Shared by the sync and async failure hooks so behavior is identical
        regardless of which dispatch LiteLLM uses.
        """
        model = kwargs.get("model", "")
        exception = kwargs.get("exception", "")

        # Only act on 404 — other errors (auth, network) aren't about model names
        exception_str = str(exception).lower()
        if "404" not in exception_str and "not found" not in exception_str:
            return

        # Try to identify the local provider
        provider = self._get_provider(model)
        if provider is None:
            # Check api_base from litellm_params (for openai/ generic prefix)
            litellm_params = kwargs.get("litellm_params") or {}
            provider = self._api_base_is_local(
                litellm_params.get("api_base")
            )

        if provider is None:
            return  # Not a local provider — no diagnostic to add

        # Strip the LiteLLM provider prefix for a cleaner log message
        model_short = model.split("/", 1)[-1] if "/" in model else model

        logger.error(
            "ERROR [LocalModel] 404 — %s model '%s' not found.\n"
            "→ %s\n"
            "→ Fix %s — re-run ./quick-setup.sh or edit litellm_config.yaml, then restart.",
            provider["label"],
            model_short,
            provider["hint"],
            provider["config_var"],
        )

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """Sync failure hook — used when LiteLLM dispatches failures synchronously."""
        self._log_failure(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """Async failure hook — the path the LiteLLM proxy actually dispatches."""
        self._log_failure(kwargs)


local_provider_failure_logger = LocalProviderFailureLogger()
