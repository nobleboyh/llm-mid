"""RagasLogger — LiteLLM custom callback for async quality scoring.

Captures every successful LiteLLM call and pushes a structured record to a
Redis list (eval:pending). The eval-worker container BLPOPs from this list,
runs Ragas scoring, and writes results back to Redis.

Zero impact on response latency — the callback only does a Redis RPUSH.
"""

import datetime
import logging
import os
import uuid

from litellm.integrations.custom_logger import CustomLogger

logger = logging.getLogger("proxy.callback")

# ── Loop prevention ────────────────────────────────────────────────────────────
# Calls made by the eval worker itself go through LiteLLM. We must NOT log those
# calls back to the eval queue — that would create an infinite Ragas scoring loop.
# Two independent checks:
#   1. The model name starts with "ragas-eval" (configured in litellm_config.yaml).
#   2. A special metadata flag _ragas_eval_call is set.
_EVAL_MODEL_PREFIX = "ragas-eval"


class RagasLogger(CustomLogger):
    """Fire-and-forget callback. RPUSHes to Redis; eval worker does the scoring."""

    def _should_skip(self, kwargs: dict) -> bool:
        """Return True if this call was made by the eval worker itself."""
        model = kwargs.get("model", "")
        if model.startswith(_EVAL_MODEL_PREFIX):
            return True

        meta = kwargs.get("metadata") or {}
        if meta.get("_ragas_eval_call"):
            return True

        return False

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        if self._should_skip(kwargs):
            return

        meta = kwargs.get("metadata", {})

        # Use the original pre-compression question captured by the
        # CaptureOriginalQuestionMiddleware. If absent, skip scoring
        # entirely — scoring against compressed/transformed text would
        # produce meaningless Ragas metrics.
        question = meta.get("original_question", "")

        def _extract_content(content) -> str:
            """Normalize LiteLLM Message.content to a string.

            LiteLLM 1.82+ types content as Union[str, List[...]] and returns
            an empty list [] instead of "" when there's no response text.
            """
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content
                         if isinstance(p, dict) and p.get("type") == "text"]
                return " ".join(parts) if parts else ""
            if isinstance(content, str):
                return content
            if content is None:
                return ""
            return str(content)

        def _get_choices(response) -> list | None:
            """Get the choices list from either a ModelResponse or raw dict."""
            if hasattr(response, "choices"):
                return response.choices
            if isinstance(response, dict):
                return response.get("choices") or response.get("content", [])
            return None

        answer = ""
        choices = _get_choices(response_obj) if response_obj else None
        if choices:
            choice = choices[0]
            if isinstance(choice, dict):
                # Dict response — can be one of:
                #   A) OpenAI format:    {"message": {"content": "..."}, ...}
                #   B) Anthropic format: {"type": "text", "text": "...", ...}
                #   C) Anthropic format: {"type": "thinking", "thinking": "...", ...}
                #
                # For case B/C the choices *list* is the content-blocks list
                # from /v1/messages. DeepSeek models emit a "thinking" block
                # before the "text" block, so choices[0] may not be the answer.
                msg = choice.get("message", {})
                if isinstance(msg, dict) and bool(msg.get("content")):
                    # Case A — standard OpenAI message wrapper
                    answer = _extract_content(msg["content"])
                else:
                    # Case B/C — Anthropic content blocks; find the first
                    # block with type="text" across the whole list.
                    for block in choices:
                        if isinstance(block, dict) and block.get("type") == "text":
                            answer = _extract_content(block.get("text", ""))
                            break
                    # Fallback: extract whatever the first block has
                    if not answer:
                        answer = _extract_content(
                            choice.get("text", "") or choice.get("content", "")
                        )
            elif hasattr(choice, "message") and choice.message:
                answer = _extract_content(choice.message.content)
            elif hasattr(choice, "delta") and choice.delta:
                answer = _extract_content(choice.delta.content)

        def _get_usage(response) -> dict:
            """Extract usage stats from either a ModelResponse or raw dict."""
            if response is None:
                return {}
            if hasattr(response, "usage"):
                u = response.usage
                return {"prompt_tokens": u.prompt_tokens or 0,
                        "completion_tokens": u.completion_tokens or 0} if u else {}
            if isinstance(response, dict):
                u = response.get("usage", {})
                return {"prompt_tokens": u.get("prompt_tokens", 0) if isinstance(u, dict) else 0,
                        "completion_tokens": u.get("completion_tokens", 0) if isinstance(u, dict) else 0}
            return {}

        # Skip records with no original question — Headroom may have stripped
        # the user message, or this is a non-conversational call that shouldn't
        # pollute Ragas scoring. Never fall back to compressed messages.
        if not question.strip():
            model = kwargs.get("model", "unknown")
            logger.warning(
                "Skipping — absent or empty original_question in metadata. "
                "model=%s", model,
            )
            return

        usage = _get_usage(response_obj)
        record = {
            "call_id":          str(uuid.uuid4()),
            "timestamp":        datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "question":         question,
            "answer":           answer,
            "contexts":         meta.get("retrieved_context", []),
            "ground_truth":     meta.get("ground_truth", ""),
            "request_category": meta.get("request_category", "general"),
            "prompt_id":        meta.get("prompt_id", "default"),
            "model":            kwargs.get("model", ""),
            "tokens_in":        usage.get("prompt_tokens", 0),
            "tokens_out":       usage.get("completion_tokens", 0),
        }

        # Push to Redis list — non-blocking, best-effort.
        # If Redis is down, the record is silently dropped.
        try:
            from eval.redis_store import enqueue_call_record
            enqueue_call_record(record)
        except Exception:
            logger.exception("Failed to enqueue call record")

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        """Async proxy hook — delegates to the shared sync logic."""
        self.log_success_event(data, response, None, None)


ragas_callback = RagasLogger()
