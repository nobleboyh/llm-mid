"""Entrypoint for the eval-worker Docker container.

Sets up logging, configures the Ragas LLM-as-judge to call DeepSeek Flash
directly (bypassing LiteLLM), and starts the eval worker loop.

Environment variables:
    REDIS_URL            — Redis connection string (default: redis://redis:6379)
    DEEPSEEK_API_KEY     — DeepSeek API key for LLM-as-judge
    RAGAS_EVAL_ENABLED   — Set to "true" to activate eval (requires Gemini key)
    RAGAS_EVAL_MODEL     — Model for Ragas LLM-as-judge (default: deepseek-chat)
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

# ── Configure Ragas LLM-as-judge (direct to DeepSeek) ─────────────────────────
# Bypass LiteLLM entirely so eval traffic doesn't compete with user requests
# and avoids needing embedding models in the proxy.

deepseek_key = os.environ["DEEPSEEK_API_KEY"]
eval_model = os.getenv("RAGAS_EVAL_MODEL", "deepseek-chat")

logger.info(
    "Ragas LLM-as-judge configured — model=%s, provider=deepseek (direct)",
    eval_model,
)


# ── Start the worker ──────────────────────────────────────────────────────────
def main():
    # Create an OpenAI-compatible client pointed directly at DeepSeek
    from openai import OpenAI
    from ragas.llms import llm_factory

    client = OpenAI(
        base_url="https://api.deepseek.com/v1",
        api_key=deepseek_key,
    )
    ragas_llm = llm_factory(
        eval_model,
        client=client,
        temperature=0.1,     # Lower temp → more deterministic JSON output
        max_tokens=2048,     # Room for the question-generation prompt response
    )

    # Create Gemini-based embeddings for metrics that need them
    # (answer_relevancy, context_recall). Uses the Gemini REST API
    # via lightweight httpx — no PyTorch/sentence-transformers needed.
    from ragas.embeddings.base import LangchainEmbeddingsWrapper
    from eval.gemini_embeddings import GeminiEmbeddings

    raw_embeddings = GeminiEmbeddings(api_key=os.environ.get("GEMINI_API_KEY"))
    ragas_embeddings = LangchainEmbeddingsWrapper(raw_embeddings)

    from eval.worker import eval_worker

    logger.info(
        "Starting eval worker (model=%s, embeddings=%s)",
        eval_model,
        "GeminiEmbeddings(gemini-embedding-001)",
    )
    eval_worker(llm=ragas_llm, embeddings=ragas_embeddings)


if __name__ == "__main__":
    main()
