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

        # ComplexityRouter resolves ragas-eval → deepseek-v4-flash.
        # The original model survives in litellm_params.proxy_server_request.body.model.
        # ponytail: .get(key, default) returns None when key exists with None value.
        _lp = kwargs.get("litellm_params") or {}
        if isinstance(_lp, dict):
            if ((_lp.get("proxy_server_request") or {}).get("body") or {}).get("model", "").startswith(_EVAL_MODEL_PREFIX):
                return True
            # ponytail: router-resolved calls (eval worker) have no
            # proxy_server_request — skip to avoid noisy warnings.
            if not _lp.get("proxy_server_request") and kwargs.get("model", "").startswith("deepseek-v4"):
                return True

        return False

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        if self._should_skip(kwargs):
            return

        # LiteLLM uses "litellm_metadata" for /v1/messages (Anthropic API)
        # and nests user-supplied metadata under "requester_metadata".
        # For /v1/chat/completions it uses the plain "metadata" key.
        user_meta = kwargs.get("metadata") or kwargs.get("litellm_metadata") or {}
        rm = user_meta.get("requester_metadata") or {}

        # Context variable set by CaptureOriginalQuestionMiddleware.
        from proxy.capture_original import original_question_var
        question = original_question_var.get()

        # ponytail: proxy_server_request.body has the original request body
        # (pre-resolution, pre-compression) — read the user question from there
        # when ContextVar is empty (router-resolved calls on different thread).
        if not question.strip():
            _psr = (kwargs.get("litellm_params") or {}).get("proxy_server_request") or {}
            if isinstance(_psr, dict):
                for msg in (_psr.get("body") or {}).get("messages", []):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        c = msg.get("content", "")
                        if isinstance(c, str):
                            question = c
                        elif isinstance(c, list):
                            parts = [b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text"]
                            question = " ".join(parts)
                        break

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
            "contexts":         user_meta.get("retrieved_context", []) or rm.get("retrieved_context", []),
            "ground_truth":     user_meta.get("ground_truth", "") or rm.get("ground_truth", ""),
            "request_category": user_meta.get("request_category", "general") or rm.get("request_category", "general"),
            "prompt_id":        user_meta.get("prompt_id", "default") or rm.get("prompt_id", "default"),
            "model":            kwargs.get("model", ""),
            "tokens_in":        usage.get("prompt_tokens", 0),
            "tokens_out":       usage.get("completion_tokens", 0),
        }

        # Attach skill injection info (if any) so it propagates through to the
        # scored eval record for monitoring in score_view et al.
        # Supports multiple skill names (comma-separated for storage).
        try:
            from proxy.skill_injector import skill_info_var
            skill_info = skill_info_var.get()
            if skill_info:
                skill_names = skill_info.get("skill_names", [])
                record["skill_name"] = ", ".join(skill_names) if skill_names else ""
                record["skill_tokens_pre_compression"] = skill_info.get(
                    "skill_tokens_pre_compression", 0,
                )
        except Exception:
            pass  # Best-effort — never block the callback

        # Push to Redis list — non-blocking, best-effort.
        # If Redis is down, the record is silently dropped.
        try:
            from eval.redis_store import enqueue_call_record
            enqueue_call_record(record)
        except Exception:
            logger.exception("Failed to enqueue call record")

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        """Async hook for all requests (streaming + non-streaming).
        Delegates to the shared sync logic."""
        self.log_success_event(kwargs, response_obj, start_time, end_time)


ragas_callback = RagasLogger()
