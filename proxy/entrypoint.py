"""Custom entrypoint — registers Headroom ASGI middleware, then starts LiteLLM.

LiteLLM doesn't have a --startup_file flag, so we do middleware registration
in-process before handing off to the normal server launch.
"""
import logging
import sys

# Ensure Headroom compression stats appear in docker logs.
# The middleware logs at INFO but the default effective level is WARNING.
for name in ("headroom", "headroom.integrations", "headroom.integrations.asgi"):
    l = logging.getLogger(name)
    l.setLevel(logging.INFO)
    if not l.handlers:
        l.addHandler(logging.StreamHandler(sys.stdout))

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
from headroom.integrations.asgi import CompressionMiddleware

app.add_middleware(
    CompressionMiddleware,
    min_tokens=300,
    # api_key not set → local mode (compresses in-process)
)

logger.info("Headroom CompressionMiddleware registered on LiteLLM proxy "
            "(compress_user_messages=True, local mode)")

# 2. Start the LiteLLM proxy server normally via its Click CLI
from litellm.proxy.proxy_cli import run_server

if __name__ == "__main__":
    run_server(
        ["--config", "/app/litellm_config.yaml", "--port", "4000"],
        standalone_mode=False,
    )
