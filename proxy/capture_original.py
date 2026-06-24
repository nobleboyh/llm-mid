"""Middleware — captures the original user question before Headroom compression.

Runs as ASGI middleware (same layer as ApiKeyMasking), reading the request
body before Headroom compresses messages. Injects the original question into
LiteLLM metadata so the Ragas callback uses it instead of compressed text.

Registration order (last = outermost, runs first inbound):
  1. ApiKeyMasking         (outermost — masks keys first)
  2. CaptureOriginal        (middle — captures raw question)
  3. CompressionMiddleware  (innermost — Headroom compression)

Execution order:
  Inbound:  ApiKeyMasking → CaptureOriginal → Compression → LiteLLM
  Outbound: Litellm → Compression → ApiKeyMasking
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("proxy.capture_original")

# Side-channel for passthrough endpoints (/v1/messages) where LiteLLM never
# calls add_litellm_data_to_request — the request-body metadata never reaches
# the callback's model_call_details, so we write it here for the callback to
# pick up directly.
original_question_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "original_question", default="",
)

# Paths that carry user messages
_CHAT_PATHS = (
    "/v1/messages",
    "/v1/chat/completions",
    "/chat/completions",
)


class CaptureOriginalQuestionMiddleware:
    """Capture the last user message before Headroom transforms it.

    Injects ``metadata.original_question`` into the LiteLLM request body.
    If no question is found (e.g. empty user message) the key is not set —
    the callback will skip scoring, which is the desired behaviour.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # LOG IMMEDIATELY to confirm dispatch
        logger.info(
            "CaptureOriginal — scope type=%s method=%s path=%s",
            scope.get("type"), scope.get("method"), scope.get("path"),
        )

        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope.get("method") != "POST" or not any(
            path.endswith(p) or path == p for p in _CHAT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        logger.debug("CaptureOriginal — intercepting %s", path)

        # ── Buffer the full request body ───────────────────────────────────
        chunks: list[bytes] = []
        while True:
            message: MutableMapping[str, Any] = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                if body:
                    chunks.append(body)
                if not message.get("more_body", False):
                    break

        full_body = b"".join(chunks)
        logger.debug("CaptureOriginal — read %d bytes body", len(full_body))

        # ── Extract and inject original question ───────────────────────────
        try:
            data = json.loads(full_body)
            messages = data.get("messages", [])

            original_question = ""
            for msg in reversed(messages):
                if isinstance(msg, dict) and msg.get("role") == "user":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        original_question = " ".join(
                            p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text"
                        )
                    else:
                        original_question = str(content)
                    break

            if original_question.strip():
                original_question_var.set(original_question)
                logger.debug(
                    "CaptureOriginal — stored original_question=%r path=%s",
                    original_question, path,
                )
            else:
                logger.debug(
                    "CaptureOriginal — no user message in %d messages path=%s",
                    len(messages), path,
                )

        except json.JSONDecodeError:
            logger.debug("CaptureOriginal — non-JSON body")
        except Exception:
            logger.exception("CaptureOriginal — failed to capture")

        # ── Forward request (possibly modified) ────────────────────────────
        body_sent = False

        async def modified_receive() -> MutableMapping[str, Any]:
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": full_body,
                    "more_body": False,
                }
            # Forward to real receive so LiteLLM can detect actual client
            # disconnects during streaming, rather than a fake disconnect
            # that would abort the response before it completes.
            return await receive()

        await self.app(scope, modified_receive, send)
