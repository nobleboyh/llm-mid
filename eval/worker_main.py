"""Entrypoint for the eval-worker Docker container.

Sets up logging, configures the Ragas LLM-as-judge through LiteLLM proxy
(using the ragas-eval model alias), and starts the eval worker loop.

Environment variables:
    REDIS_URL            — Redis connection string (default: redis://redis:6379)
    GATEWAY_MASTER_KEY   — LiteLLM proxy API key
    LITELLM_URL          — LiteLLM proxy URL (default: http://litellm:4000)
    RAGAS_EVAL_ENABLED   — Set to "true" to activate eval (requires Gemini key)
"""

import logging
import os
import sys
import types  # used by _install_ragas_compat_stubs

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("eval.worker_main")

# ── Graceful skip when Gemini key is missing ──────────────────────────────────
if os.environ.get("RAGAS_EVAL_ENABLED", "").lower() != "true":
    logger.info(
        "RAGAS_EVAL_ENABLED is not 'true' — eval worker is disabled. "
        "Set GEMINI_API_KEY and run quick-setup.sh to enable."
    )
    sys.exit(0)

# ── Ragas VertexAI compat shim ────────────────────────────────────────────────
# Ragas 0.4.3 unconditionally imports ChatVertexAI / VertexAI from
# langchain_community at module load time.  Register lightweight stubs so
# those imports succeed without pulling in the Google Cloud SDK.

for _mod_name, _cls_name in [
    ("langchain_community.chat_models.vertexai", "ChatVertexAI"),
    ("langchain_community.llms.vertexai", "VertexAI"),
]:
    if _mod_name not in sys.modules:
        _mod = types.ModuleType(_mod_name)
        setattr(_mod, _cls_name, type(_cls_name, (), {}))
        sys.modules[_mod_name] = _mod

# ── Configure Ragas LLM-as-judge (through LiteLLM proxy) ──────────────────────
# The eval worker calls LiteLLM with model="ragas-eval". The RagasLogger
# callback skips requests with model_name starting with "ragas-eval", so
# there is no infinite eval loop.

litellm_url = os.getenv("LITELLM_URL", "http://litellm:4000").rstrip("/")
litellm_key = os.getenv("GATEWAY_MASTER_KEY", "sk-local-dev-key")

logger.info(
    "Ragas LLM-as-judge configured — routing through LiteLLM at %s",
    litellm_url,
)


# ── Start the worker ──────────────────────────────────────────────────────────
def main():
    # Create an OpenAI-compatible client pointed at LiteLLM proxy
    from openai import OpenAI
    from ragas.llms import llm_factory

    client = OpenAI(
        base_url=f"{litellm_url}/v1",
        api_key=litellm_key,
    )
    ragas_llm = llm_factory(
        "ragas-eval",              # ← uses the ragas-eval model alias in litellm_config.yaml
        client=client,
        temperature=0.1,
        max_tokens=2048,
    )

    # Create Gemini-based embeddings for metrics that need them
    # (answer_relevancy, context_recall). Uses the Gemini REST API
    # via lightweight httpx — no PyTorch/sentence-transformers needed.
    # GeminiEmbeddings extends ragas BaseRagasEmbedding directly so no
    # deprecated LangchainEmbeddingsWrapper is required.
    from eval.gemini_embeddings import GeminiEmbeddings

    ragas_embeddings = GeminiEmbeddings(api_key=os.environ.get("GEMINI_API_KEY"))

    from eval.worker import eval_worker

    logger.info(
        "Starting eval worker (judge=ragas-eval via LiteLLM, embeddings=%s)",
        "GeminiEmbeddings(gemini-embedding-001)",
    )
    eval_worker(llm=ragas_llm, embeddings=ragas_embeddings)


if __name__ == "__main__":
    main()
