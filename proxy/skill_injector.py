"""ASGI middleware — detects ``$trigger`` tokens in user messages and injects
skill content into the system prompt.

Trigger detection via ``$<skill-name>`` mention in user message content.
The trigger token is stripped from the forwarded message. Skill content is
prepended to (or appended after) the system prompt. A response header
``X-GateMid-Skill-Applied`` signals which skill was activated.

Positioned between ``CaptureOriginalQuestionMiddleware`` and Headroom's
``CompressionMiddleware`` so the skill text is compressed with the rest of
the payload, minimising net token overhead.

Usage in ``entrypoint.py``:

    from proxy.skills.registry import load_skills
    from proxy.skill_injector import SkillInjectorMiddleware

    load_skills()                          # once at startup
    app.add_middleware(SkillInjectorMiddleware)  # before CompressionMiddleware
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from collections.abc import MutableMapping
from typing import Any

from starlette.types import ASGIApp, Receive, Scope, Send

from proxy.skills.registry import get as get_skill

logger = logging.getLogger(__name__)

# ── Context variable: skill info for the current request ────────────────────────
# Read by the patched compress() in entrypoint.py to enrich the Redis hash.
skill_info_var: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("skill_info", default=None)
)

# Regex for ``$skill-name`` triggers (alpha start, then alnum/underscore/hyphen)
TRIGGER_PATTERN = re.compile(r"\$([a-zA-Z][a-zA-Z0-9_-]*)")

# Paths that carry user messages (must match capture_original.py)
_CHAT_PATHS = (
    "/v1/messages",
    "/v1/chat/completions",
    "/chat/completions",
)

# ── Token estimator (lightweight) ───────────────────────────────────────────────
# Use a simple heuristic: ~4 chars per token.  If tiktoken is available we use it
# for accuracy; otherwise fall back to the heuristic.
try:
    import tiktoken

    _ENCODING = tiktoken.get_encoding("cl100k_base")

    def _count_tokens(text: str) -> int:
        return len(_ENCODING.encode(text))

except ImportError:

    def _count_tokens(text: str) -> int:
        return max(1, len(text) // 4)


class SkillInjectorMiddleware:
    """ASGI middleware that detects ``$trigger`` tokens and injects skill content.

    Registration order in entrypoint.py:
        1. ApiKeyMasking           (outermost — runs first inbound)
        2. CaptureOriginalQuestion (second — captures raw question)
        3. SkillInjector           ← NEW (third — injects skills before compression)
        4. CompressionMiddleware   (innermost — Headroom compression)

    At runtime: messages flow through ApiKeyMasking → CaptureOriginal →
    SkillInjector → Compression → LiteLLM.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope.get("method") != "POST" or not any(
            path.endswith(p) or path == p for p in _CHAT_PATHS
        ):
            await self.app(scope, receive, send)
            return

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
        if not full_body:
            await self.app(scope, receive, send)
            return

        # ── Attempt to parse JSON body ─────────────────────────────────────
        try:
            payload = json.loads(full_body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            await self.app(scope, receive, send)
            return

        # ── Prepare skill info (used for injection + analytics) ──────────
        skill_name, mutated = self._detect_and_inject(payload)

        if skill_name:
            skill_content = get_skill(skill_name)
            # Encode the mutated body (with stripped trigger, skill injected)
            full_body = json.dumps(
                mutated,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")

            # Set context var for the compress function to pick up
            skill_content = get_skill(skill_name)
            token_count = _count_tokens(skill_content) if skill_content else 0
            if skill_content:
                skill_info_var.set({
                    "skill_name": skill_name,
                    "skill_tokens_pre_compression": token_count,
                })

            logger.info(
                "[SkillInjector] path=%s — skill '%s' injected "
                "(%d tokens pre-compression)",
                path,
                skill_name,
                token_count,
            )

            skill_info_var.set({
                "skill_name": skill_name,
                "skill_tokens_pre_compression": token_count,
            })
        else:
            logger.info(
                "[SkillInjector] path=%s — scanned %d messages, "
                "no trigger found",
                path,
                len(payload.get("messages", [])),
            )

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
            # ponytail: forward to real receive so LiteLLM detects
            # actual client disconnects during streaming, instead of
            # getting a fake disconnect that aborts the response early.
            return await receive()

        # Wrap send to intercept response and add header
        header_sent = False

        async def skill_send(message: MutableMapping[str, Any]) -> None:
            nonlocal header_sent
            is_body = message.get("type") == "http.response.body"
            is_intermediate_chunk = is_body and message.get("more_body", False)
            log_level = logger.debug if is_intermediate_chunk else logger.info
            log_level(
                "[SKILL_SEND] path=%s msg_type=%s more_body=%s skill_name=%s header_sent=%s",
                path,
                message.get("type"),
                message.get("more_body", "N/A"),
                skill_name,
                header_sent,
            )
            if (
                skill_name
                and message["type"] == "http.response.start"
                and not header_sent
            ):
                header_sent = True
                headers = list(message.get("headers", []) or [])
                headers.append(
                    (b"x-gatemid-skill-applied", skill_name.encode("utf-8"))
                )
                # Strip content-length — it may have changed
                filtered_headers = [
                    (k, v) for k, v in headers
                    if k.lower() != b"content-length"
                ]
                await send({**message, "headers": filtered_headers})
            else:
                await send(message)

        await self.app(scope, modified_receive, skill_send)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_and_inject(
        payload: dict,
    ) -> tuple[str | None, dict]:
        """Scan messages for a ``$trigger``. If found:

        * strip the trigger from the user message
        * inject skill content into the system prompt

        Handles both string content (OpenAI) and list-of-blocks content
        (Anthropic /v1/messages — e.g. ``[{"type":"text","text":"..."}]``).

        Returns ``(skill_name | None, mutated payload)``.
        """
        messages: list[dict] = payload.get("messages", [])
        skill_name: str | None = None
        skill_content: str | None = None

        def _find_trigger(text: str) -> re.Match | None:
            return TRIGGER_PATTERN.search(text)

        def _strip_trigger(text: str) -> str:
            return TRIGGER_PATTERN.sub("", text, count=1).strip()

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")

            # ── String content (OpenAI /chat/completions) ─────────────
            if isinstance(content, str):
                match = _find_trigger(content)
                if not match:
                    continue

                candidate = match.group(1).lower()
                found = get_skill(candidate)
                if found is None:
                    logger.debug(
                        "[SkillInjector] Trigger '$%s' not in registry — ignoring",
                        candidate,
                    )
                    continue

                skill_name = candidate
                skill_content = found
                msg["content"] = _strip_trigger(content)
                logger.debug(
                    "[SkillInjector] Trigger '$%s' matched (string content) — injecting",
                    skill_name,
                )
                break

            # ── List-of-blocks content (Anthropic /v1/messages) ───────
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "text":
                        continue
                    text = block.get("text", "")
                    if not isinstance(text, str):
                        continue

                    match = _find_trigger(text)
                    if not match:
                        continue

                    candidate = match.group(1).lower()
                    found = get_skill(candidate)
                    if found is None:
                        logger.debug(
                            "[SkillInjector] Trigger '$%s' not in registry — ignoring",
                            candidate,
                        )
                        continue

                    skill_name = candidate
                    skill_content = found
                    block["text"] = _strip_trigger(text)
                    logger.debug(
                        "[SkillInjector] Trigger '$%s' matched (list block) — injecting",
                        skill_name,
                    )
                    break

                # Did we find and strip a trigger in a list block?
                if skill_name:
                    break

        if skill_name and skill_content:
            if SkillInjectorMiddleware._already_injected(payload, skill_content):
                # Duplicate invocation — skill content already present.
                # No mutation needed.
                logger.debug(
                    "[SkillInjector] skill '%s' already "
                    "present in system prompt — skipping duplicate injection",
                    skill_name,
                )
                return None, payload
            payload = SkillInjectorMiddleware._inject_system_prompt(
                payload, skill_content,
            )

        return skill_name, payload

    @staticmethod
    def _already_injected(payload: dict, skill_content: str) -> bool:
        """Return True if *skill_content* is already present in any system message.

        Compares the first non-empty line of *skill_content* against existing
        system messages. This prevents duplicate injection when the ASGI
        middleware fires multiple times for the same request.
        """
        # Extract a signature: the first meaningful line of the skill
        sig = ""
        for line in skill_content.split("\n"):
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                sig = stripped[:80]
                break
        if not sig:
            return False

        for msg in payload.get("messages", []):
            if msg.get("role") != "system":
                continue
            existing = str(msg.get("content", ""))
            if sig in existing:
                return True
        return False

    @staticmethod
    def _inject_system_prompt(payload: dict, skill_content: str) -> dict:
        """Append *skill_content* to the last system message, or create one."""
        messages: list[dict] = payload.get("messages", [])
        separator = "\n\n---\n\n"

        system_messages = [m for m in messages if m.get("role") == "system"]

        if system_messages:
            last_system = system_messages[-1]
            existing = last_system.get("content", "")
            last_system["content"] = existing + separator + skill_content
        else:
            messages.insert(0, {"role": "system", "content": skill_content})
            payload["messages"] = messages

        return payload
