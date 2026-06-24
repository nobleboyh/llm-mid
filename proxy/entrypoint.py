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
    "proxy.skill_injector",
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
    result = _original_compress(
        messages=messages, model=model, model_limit=model_limit,
        optimize=optimize, hooks=hooks, config=config, **kwargs,
    )

    # ── Store compression result in Redis (fire-and-forget) ──────────────
    if result and result.tokens_saved > 0:
        try:
            import datetime
            import json
            import uuid
            from eval.redis_store import (
                HEADROOM_CALL_PREFIX,
                store_headroom_result,
            )
            call_id = str(uuid.uuid4())
            store_headroom_result(
                call_id=call_id,
                timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                tokens_before=result.tokens_before,
                tokens_after=result.tokens_after,
                tokens_saved=result.tokens_saved,
                compression_ratio=result.compression_ratio,
                model=model,
                transforms_applied=result.transforms_applied,
                prompt_before=json.dumps(messages, ensure_ascii=False),
                prompt_after=json.dumps(result.messages, ensure_ascii=False),
            )

            # ── Enrich Redis hash with skill analytics (if skills were injected) ──
            from proxy.skill_injector import skill_info_var
            skill_info = skill_info_var.get()
            if skill_info:
                from eval.redis_store import r as redis_client
                skill_names = skill_info.get("skill_names", [])
                mapping = {
                    "skill_name": ", ".join(skill_names) if skill_names else "",
                    "skill_tokens_pre_compression": str(
                        skill_info.get("skill_tokens_pre_compression", 0),
                    ),
                }
                redis_client.hset(
                    f"{HEADROOM_CALL_PREFIX}{call_id}",
                    mapping=mapping,
                )
        except Exception:
            pass  # Redis storage is best-effort; never block compression

    return result


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

# ── Load skill files into registry ──────────────────────────────────────
# Skills are .md files in proxy/skills/ that can be activated by $trigger
# tokens in user messages. Must be loaded before middleware registration
# so SkillInjectorMiddleware has access to them.
from proxy.skills.registry import load_skills as _load_skills
_load_skills()

# Middleware registration order (last registered = outermost, runs first):
#
#   Inbound:
#     ApiKeyMasking → CaptureOriginal → SkillInjector → Compression → LiteLLM
#   Outbound:
#     LiteLLM → Compression → SkillInjector → CaptureOriginal → ApiKeyMasking
#
# ApiKeyMasking is outermost so API keys are sanitized before any other
# middleware reads or logs the body. CaptureOriginal runs second so it
# captures the raw question before Headroom compresses messages.
# SkillInjector runs third so trigger detection and skill injection happen
# before compression — the skill text is compressed with the rest of the payload.

# 1a. Register Headroom compression middleware (innermost — runs last inbound)
from headroom.integrations.asgi import CompressionMiddleware

app.add_middleware(
    CompressionMiddleware,
    min_tokens=300,
    # api_key not set → local mode (compresses in-process)
)

logger.info("Headroom CompressionMiddleware registered on LiteLLM proxy "
            "(compress_user_messages=True, local mode)")

# 1b. Register Skill Injector middleware (second-innermost — runs third inbound)
from proxy.skill_injector import SkillInjectorMiddleware

app.add_middleware(SkillInjectorMiddleware)

logger.info("SkillInjectorMiddleware registered — detecting $trigger tokens "
            "and injecting skills before compression")

# 1c. Register Original Question capture middleware (second — runs second inbound)
from proxy.capture_original import CaptureOriginalQuestionMiddleware

app.add_middleware(CaptureOriginalQuestionMiddleware)

logger.info("CaptureOriginalQuestionMiddleware registered — capturing raw "
            "question before compression")

# 1d. Register API Key masking guardrail (outermost — runs first inbound)
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
