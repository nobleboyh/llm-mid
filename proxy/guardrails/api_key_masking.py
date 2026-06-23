"""ASGI middleware that masks API keys in LLM request/response bodies.

Intercepts POST requests to LLM endpoints, recursively walks the JSON body
for any string values matching known API key patterns, and replaces the
sensitive portion with ``***MASKED***`` while preserving the key type prefix.

Registered in entrypoint.py before Headroom CompressionMiddleware so that
keys are scrubbed before the payload reaches the compressor.

Every masking event is logged at INFO level with the key type and JSON path.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# ── API key detection patterns ─────────────────────────────────────────────────
# ORDER MATTERS — most specific patterns first; generic catch-alls last.
# Each entry: (name, compiled_pattern)
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Gemini: AIzaSy... (27+ chars)
    ("gemini_key", re.compile(r"\b(AIzaSy[A-Za-z0-9_-]{26,})\b")),
    # Hugging Face: hf_...
    ("huggingface_token", re.compile(r"\b(hf_[A-Za-z0-9_-]{20,})\b")),
    # GitHub: ghp_..., ghs_..., gho_...
    ("github_token", re.compile(r"\b(gh[ops]_[a-zA-Z0-9]{36,})\b")),
    # AWS access key: AKIA...
    ("aws_access_key", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    # OpenAI / Anthropic / LiteLLM: sk-... (20+ chars, multi-segment)
    ("openai_key", re.compile(r"\b(sk-[a-zA-Z0-9_-]{20,})\b")),
    # Bearer token inline in text — must come BEFORE openai_key so the full
    # ``Bearer sk-proj-...`` value is captured in one go.
    ("bearer_token", re.compile(
        r"""\b(Bearer\s+[A-Za-z0-9._\-\/+=]{20,})\b""",
        re.IGNORECASE,
    )),
    # key-value: api_key = "..."
    ("api_key_value", re.compile(
        r"""\b(api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{20,}['\"]?)""",
        re.IGNORECASE,
    )),
    # Generic catch-all for unknown formats (36+ chars)
    ("generic_long_key", re.compile(r"\b([A-Za-z0-9_-]{36,})\b")),
]


def _mask_single_value(key_type: str, full_match: str, value: str) -> str:
    """Replace the matched sensitive part with a masked placeholder.

    Architecture:
    *   For tokens with ``-`` separators (e.g. ``sk-proj-XXXX``), preserves all
        segments except the last (the key body).
    *   For Bearer tokens, applies the same logic to the token portion.
    *   For known type prefixes without a ``-`` separator (``AIzaSy``, ``AKIA``,
        ``hf_``, ``ghp_``), preserves the prefix as-is followed by ``***MASKED***``.
    """
    # Bearer tokens — preserve the "Bearer " label then mask the token itself
    if full_match.lower().startswith("bearer "):
        m = re.match(r"^Bearer\s+(.+)", full_match, re.IGNORECASE)
        if m:
            inner = _mask_single_value("", m.group(1), value)
            return f"Bearer {inner}"
        return "Bearer ***MASKED***"

    # api_key=value pattern — preserve up to ``=`` or ``:``
    m = re.match(r'^(api[_-]?key\s*[:=]\s*[\'"]?).+', full_match, re.IGNORECASE)
    if m:
        return m.group(1) + "***MASKED***"

    # Hyphen-separated tokens — keep all but the last segment
    parts = full_match.split("-")
    if len(parts) >= 3:
        prefix = "-".join(parts[:-1]) + "-"
        return prefix + "***MASKED***"

    # Tokens with a known type prefix (no ``-`` separator)
    known_prefixes = ["AIzaSy", "AKIA", "hf_", "ghp_", "ghs_", "gho_"]
    for prefix in known_prefixes:
        if full_match.startswith(prefix):
            return prefix + "***MASKED***"

    # Two-segment tokens such as ``sk-XXXXX``
    if len(parts) == 2:
        return parts[0] + "-***MASKED***"

    # Fallback — no recognizable structure
    return "***MASKED***"


def mask_api_keys_in_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Scans *text* for API key patterns and masks them.

    Returns ``(masked_text, events)`` where *events* is a list of dicts with
    ``key_type`` and ``count`` for each distinct pattern that fired.
    """
    if not isinstance(text, str) or not text:
        return text, []

    events: dict[str, int] = {}
    result = text

    for key_type, pattern in _PATTERNS:
        # Find all matches first to count them
        matches = list(pattern.finditer(result))
        if not matches:
            continue

        # Count and record
        events[key_type] = events.get(key_type, 0) + len(matches)

        # Replace — use a lambda that captures ``key_type`` to build the mask
        def _replacer(m: re.Match, kt: str = key_type) -> str:
            return _mask_single_value(kt, m.group(1), m.string)

        result = pattern.sub(_replacer, result)

    event_list = [{"key_type": k, "count": v} for k, v in sorted(events.items())]
    return result, event_list


def mask_api_keys_in_json(
    data: Any,
    path: str = "$",
) -> tuple[Any, list[dict[str, Any]]]:
    """Recursively walk a JSON-parsed structure and mask API keys in every string.

    *path* is the current JSON path (for logging). Returns ``(masked_data, events)``
    where *events* aggregates masking events across all string values.
    """
    all_events: list[dict[str, Any]] = []

    def _walk(node: Any, current_path: str) -> Any:
        nonlocal all_events
        if isinstance(node, str):
            masked, events = mask_api_keys_in_text(node)
            if events:
                for e in events:
                    all_events.append({**e, "path": current_path})
                return masked
            return node
        if isinstance(node, dict):
            return {
                k: _walk(v, f"{current_path}.{k}") for k, v in node.items()
            }
        if isinstance(node, list):
            return [
                _walk(item, f"{current_path}[{i}]")
                for i, item in enumerate(node)
            ]
        return node

    masked_data = _walk(data, path)
    return masked_data, all_events


# ── Content-only masking helpers ──────────────────────────────────────────────_

def mask_api_keys_in_request(body: dict) -> tuple[dict, list[dict[str, Any]]]:
    """Mask API keys only in content-bearing request fields.

    Scans ``system`` and ``messages[*].content`` (both string and
    Anthropic-style list-of-blocks form).  Everything else — tools,
    metadata, protocol fields — is left untouched.
    """
    all_events: list[dict[str, Any]] = []

    def _walk_text(text: str, path: str) -> str:
        nonlocal all_events
        masked, events = mask_api_keys_in_text(text)
        if events:
            for e in events:
                all_events.append({**e, "path": path})
        return masked

    # ── system (top-level, Anthropic /v1/messages) ────────────────────────
    if isinstance(body.get("system"), str):
        body["system"] = _walk_text(body["system"], "$.system")

    # ── messages[].content ────────────────────────────────────────────────
    messages = body.get("messages", [])
    if isinstance(messages, list):
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, str):
                messages[i]["content"] = _walk_text(
                    content, f"$.messages[{i}].content"
                )
            elif isinstance(content, list):
                for j, block in enumerate(content):
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if isinstance(text, str):
                            content[j] = {
                                **block,
                                "text": _walk_text(
                                    text, f"$.messages[{i}].content[{j}].text"
                                ),
                            }

    # ── user content at top level (Anthropic /v1/messages) ─────────────────
    if isinstance(body.get("content"), str):
        body["content"] = _walk_text(body["content"], "$.content")

    return body, all_events


def mask_api_keys_in_response(body: dict) -> tuple[dict, list[dict[str, Any]]]:
    """Mask API keys only in content-bearing response fields.

    Scans ``choices[*].message.content`` (OpenAI /chat/completions),
    ``choices[*].delta.content`` (streaming chunks), and
    ``content[*].text`` (Anthropic /v1/messages).
    """
    all_events: list[dict[str, Any]] = []

    def _walk_text(text: str, path: str) -> str:
        nonlocal all_events
        masked, events = mask_api_keys_in_text(text)
        if events:
            for e in events:
                all_events.append({**e, "path": path})
        return masked

    # ── choices[].message.content / choices[].delta.content ───────────────
    choices = body.get("choices")
    if isinstance(choices, list):
        for i, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            msg = choice.get("message")
            if isinstance(msg, dict):
                c = msg.get("content")
                if isinstance(c, str):
                    choices[i]["message"]["content"] = _walk_text(
                        c, f"$.choices[{i}].message.content"
                    )
            delta = choice.get("delta")
            if isinstance(delta, dict):
                c = delta.get("content")
                if isinstance(c, str):
                    choices[i]["delta"]["content"] = _walk_text(
                        c, f"$.choices[{i}].delta.content"
                    )

    # ── content[].text (Anthropic /v1/messages format) ────────────────────
    content_blocks = body.get("content")
    if isinstance(content_blocks, list):
        for i, block in enumerate(content_blocks):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str):
                    content_blocks[i] = {
                        **block,
                        "text": _walk_text(text, f"$.content[{i}].text"),
                    }

    return body, all_events


# ── ASGI Middleware ────────────────────────────────────────────────────────────

# Paths that contain LLM messages to inspect
_LLM_PATHS = (
    "/v1/messages",          # Anthropic
    "/v1/chat/completions",  # OpenAI
    "/v1/responses",         # OpenAI Responses API
    "/chat/completions",     # LiteLLM (without /v1 prefix)
)


class ApiKeyMaskingMiddleware:
    """ASGI middleware that masks API keys in LLM request/response bodies.

    Registered in proxy/entrypoint.py, placed before CompressionMiddleware so
    that key scrubbing happens before payload compression.

    Logs at INFO every time a key is masked, including the pattern that
    matched and the JSON path where it was found.
    """

    def __init__(
        self,
        app: ASGIApp,
    ) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # Only intercept POST to LLM endpoints
        if method != "POST" or not any(
            path.endswith(p) or path == p for p in _LLM_PATHS
        ):
            await self.app(scope, receive, send)
            return

        # ── Buffer request body ────────────────────────────────────────────
        body_chunks: list[bytes] = []

        async def buffering_receive() -> MutableMapping[str, Any]:
            message: MutableMapping[str, Any] = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                if chunk:
                    body_chunks.append(chunk)
            return message

        while True:
            msg = await buffering_receive()
            if msg.get("type") == "http.request":
                if not msg.get("more_body", False):
                    break

        full_body = b"".join(body_chunks)

        # ── Mask API keys in request body (content fields only) ───────────
        try:
            body_json = json.loads(full_body)
            masked_body, events = mask_api_keys_in_request(body_json)

            if events:
                full_body = json.dumps(masked_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                req_masked = True
                _log_events("request", path, events)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass  # non-JSON body — pass through unmodified

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
            return {"type": "http.disconnect"}

        # ── Buffer and optionally mask response body ────────────────────────
        # For non-streaming responses we buffer all chunks, mask, then forward.
        # SSE/streaming responses are forwarded as-is (each SSE event would need
        # individual parsing, adding complexity beyond the initial use case).
        resp_chunks: list[bytes] = []
        resp_headers_sent = False
        is_streaming = False

        async def buffering_send(message: MutableMapping[str, Any]) -> None:
            nonlocal resp_headers_sent, is_streaming
            if message["type"] == "http.response.start":
                resp_headers_sent = True
                # Detect streaming from content-type
                headers = dict(message.get("headers", []) or [])
                ct = headers.get(b"content-type", b"").decode().lower()
                if "text/event-stream" in ct or "text/plain" in ct:
                    is_streaming = True
                # Strip Content-Length — body size may change after masking
                filtered_headers = [
                    (k, v) for k, v in (message.get("headers", []) or [])
                    if k.lower() != b"content-length"
                ]
                await send({**message, "headers": filtered_headers})

            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                more = message.get("more_body", False)

                if is_streaming:
                    # Forward streaming chunks immediately
                    await send(message)
                else:
                    # Buffer non-streaming body
                    if chunk:
                        resp_chunks.append(chunk)
                    if not more:
                        # All chunks received — mask and forward
                        resp_body = b"".join(resp_chunks)
                        try:
                            resp_json = json.loads(resp_body)
                            masked_resp, resp_events = mask_api_keys_in_response(resp_json)
                            if resp_events:
                                _log_events("response", path, resp_events)
                                resp_body = json.dumps(masked_resp, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
                        except (json.JSONDecodeError, TypeError, ValueError):
                            pass
                        await send({
                            "type": "http.response.body",
                            "body": resp_body,
                            "more_body": False,
                        })

        await self.app(scope, modified_receive, buffering_send)


def _log_events(
    direction: str,
    path: str,
    events: list[dict[str, Any]],
) -> None:
    """Log masking events in a concise, structured format."""
    # Group by key_type for summary
    by_type: dict[str, int] = {}
    for e in events:
        kt = e.get("key_type", "unknown")
        by_type[kt] = by_type.get(kt, 0) + e.get("count", 1)

    detail = "; ".join(
        f"{k}×{v}" for k, v in sorted(by_type.items())
    )
    # Log the first few paths for context
    paths = {e.get("path", "$") for e in events}
    path_sample = ", ".join(sorted(paths)[:5])
    if len(paths) > 5:
        path_sample += f" (and {len(paths)-5} more)"

    logger.info(
        "ApiKeyMask %s %s — masked %s at %s",
        direction.upper(),
        path,
        detail,
        path_sample,
    )
