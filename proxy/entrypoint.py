"""Custom entrypoint — registers Headroom ASGI middleware, then starts LiteLLM.

LiteLLM doesn't have a --startup_file flag, so we do middleware registration
in-process before handing off to the normal server launch.
"""
import logging
import sys

# ── Logging setup ────────────────────────────────────────────────────────────
# Set up a uniform handler shared by all loggers we care about.
_handler = logging.StreamHandler(sys.stdout)
_handler.setLevel(logging.INFO)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
))


def _setup_logger(name: str, level: int = logging.INFO):
    """Ensure *name* logger writes to stdout at *level*."""
    l = logging.getLogger(name)
    l.setLevel(level)
    l.handlers.clear()
    l.addHandler(_handler)
    l.propagate = False


# Headroom compression stats — middleware logs at INFO but the default
# effective level is WARNING.
for name in (
    "headroom",
    "headroom.integrations",
    "headroom.integrations.asgi",
    "proxy.guardrails",
    "guardrails",
    "proxy.callback",
    "eval.redis_store",
):
    _setup_logger(name)

logger = logging.getLogger("headroom.startup")

# -- Patch 1: Headroom CompressConfig defaults compress_user_messages=False.  --
# -- The middleware (v0.23.0) doesn't expose a config parameter, so we patch  --
# -- compress() directly to inject compress_user_messages=True.               --
from headroom.compress import compress as _original_compress, CompressConfig


# -- Patch 2: Disable Kompress ML model. We target JSON/FHIR/HL7 payloads    --
# -- (SmartCrusher). The ONNX model download adds 100-200ms latency and       --
# -- pulls ~500MB from Hugging Face on first request. Not needed for JSON.    --
# -- Dataclass field defaults can't be changed after definition, so we patch  --
# -- ContentRouter.__init__ to force enable_kompress=False on its config.     --
from headroom.transforms.content_router import ContentRouter as _ContentRouter

_original_cr_init = _ContentRouter.__init__


def _patched_cr_init(self, config=None, observer=None):
    _original_cr_init(self, config=config, observer=observer)
    self.config.enable_kompress = True
    self.config.skip_user_messages = False  # SmartCrusher needs to see user payloads


_ContentRouter.__init__ = _patched_cr_init


def _patched_compress(messages, model="claude-sonnet-4-5-20250929",
                      model_limit=200000, optimize=True, hooks=None,
                      config=None, **kwargs):
    if config is None:
        config = CompressConfig(compress_user_messages=True,
                                min_tokens_to_compress=250)
    else:
        config.compress_user_messages = True
    return _original_compress(
        messages=messages, model=model, model_limit=model_limit,
        optimize=optimize, hooks=hooks, config=config, **kwargs,
    )


# headroom/__init__.py re-exports compress as a function, so
# `import headroom.compress` gives the function, not the module.
# Patch the real module in sys.modules so the middleware's lazy
# `from headroom.compress import compress` picks up our wrapper.
sys.modules["headroom.compress"].compress = _patched_compress

# 1. Register Headroom ASGI middleware on the LiteLLM FastAPI app
from litellm.proxy.proxy_server import app

# ── LiteLLM logging (set after litellm is imported) ─────────────────────
# We need only the complexity router's routing decision per request, not the
# noisy "LiteLLM completion() model=..." or "ageneric_api_call_with_fallbacks"
# lines from other LiteLLM internals.
import litellm._logging as _llm_logging


class _ComplexityRouterFilter(logging.Filter):
    """Only passes lines with the complexity router's routing decision."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "ComplexityRouter:" in record.getMessage()


_llm_logging.verbose_router_logger.addFilter(_ComplexityRouterFilter())
_llm_logging.verbose_router_logger.setLevel(logging.INFO)
_llm_logging.verbose_router_logger.handlers.clear()
_llm_logging.verbose_router_logger.addHandler(_handler)
_llm_logging.verbose_router_logger.propagate = False

# Suppress generic litellm logger ("completion()" lines) and proxy logger
# ("SESSION REUSE" noise).
_llm_logging.verbose_logger.setLevel(logging.WARNING)
_llm_logging.verbose_proxy_logger.setLevel(logging.WARNING)

logger.info("LiteLLM verbose loggers reconfigured for stdout")

# Middleware registration order (last registered = outermost, runs first):
#
#   Inbound:  ApiKeyMasking → CaptureOriginal → Compression → LiteLLM
#   Outbound: LiteLLM → Compression → CaptureOriginal → ApiKeyMasking
#
# ApiKeyMasking is outermost so API keys are sanitized before any other
# middleware reads or logs the body. CaptureOriginal runs second so it
# captures the raw question before Headroom compresses messages.

# 1a. Register Headroom compression middleware (innermost — runs last inbound)
from headroom.integrations.asgi import CompressionMiddleware

app.add_middleware(
    CompressionMiddleware,
    min_tokens=300,
    # api_key not set → local mode (compresses in-process)
)

logger.info("Headroom CompressionMiddleware registered on LiteLLM proxy "
            "(compress_user_messages=True, local mode)")

# 1b. Register Original Question capture middleware (middle — runs second inbound)
from proxy.capture_original import CaptureOriginalQuestionMiddleware

app.add_middleware(CaptureOriginalQuestionMiddleware)

logger.info("CaptureOriginalQuestionMiddleware registered — capturing raw "
            "question before compression")

# 1c. Register API Key masking guardrail (outermost — runs first inbound)
from guardrails.api_key_masking import ApiKeyMaskingMiddleware

app.add_middleware(ApiKeyMaskingMiddleware)

logger.info("ApiKeyMaskingMiddleware registered — masking API keys in "
            "request/response bodies")

# 2. Start the LiteLLM proxy server normally via its Click CLI
from litellm.proxy.proxy_cli import run_server

if __name__ == "__main__":
    run_server(
        ["--config", "/app/litellm_config.yaml", "--port", "4000"],
        standalone_mode=False,
    )
