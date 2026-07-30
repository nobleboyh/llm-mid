"""LocalProviderFailureLogger — LiteLLM failure callback for local AI providers.

Detects 404 errors from local inference servers (Ollama, llama.cpp, LM Studio, oMLX)
and logs actionable diagnostic messages pointing the user to the correct model name.

Behavior is purely diagnostic — never blocks or modifies the request/response.
"""

from __future__ import annotations

import logging

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
        # Port-based heuristic matching
        port_map = {
            "11434": 0,  # Ollama
            "8080":  1,  # llama.cpp
            "1234":  2,  # LM Studio
            "8000":  3,  # oMLX
        }
        for port, idx in port_map.items():
            if f":{port}" in ab:
                return _LOCAL_PROVIDERS[idx]
        return None

    def log_failure_event(self, kwargs, response_obj, start_time, end_time):
        """LiteLLM failure callback — runs on every failed LLM call."""
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
            "→ Or update %s in .env and restart GateMid.",
            provider["label"],
            model_short,
            provider["hint"],
            provider["config_var"],
        )


local_provider_failure_logger = LocalProviderFailureLogger()
