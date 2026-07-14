# FEAT-skill-injection — Skill Injection via Mention Trigger

**Status:** Draft  
**Author:** Hoang  
**Created:** 2026-06-23  
**Stack:** llm-mid / LiteLLM + Headroom ASGI

---

## 1. Problem Statement

Developers on the team often send verbose, over-engineered prompts or receive bloated LLM responses that introduce unnecessary abstractions, dependencies, or complexity. There is no lightweight, friction-free mechanism to activate behavioral guardrails (e.g. YAGNI, minimalism, "code you never write is code you never debug") at the proxy layer without modifying the client tool or the system prompt in the IDE.

The `llm-mid` gateway already intercepts every request before it reaches LiteLLM and before Headroom compresses the payload. This is the ideal injection point to enrich the system prompt with a curated skill — a short markdown document that constrains the LLM's behavior for the duration of that request.

---

## 2. Goal

Allow any developer to activate a named skill by mentioning its trigger keyword (e.g. `$ponytail`) anywhere in their message. The gateway detects the trigger, strips it from the forwarded message, injects the skill's markdown content into the system prompt, and lets Headroom compress the enriched payload — all transparently, with no client-side changes required.

---

## 3. Scope

**In scope**

- Trigger detection via `$<skill-name>` mention in user message content
- System prompt injection of matched skill markdown
- Trigger token stripping from the forwarded message
- `$ponytail` as the first supported skill
- Skill file loading from a local `proxy/skills/` directory at startup
- Response header `X-GateMid-Skill-Applied: <skill-name>` for observability
- Token overhead logging to the existing Redis analytics key

**Out of scope (future FEATs)**

- Auto-trigger based on ComplexityRouter tier
- Per-team default skill assignment in `litellm_config.yaml`
- Dynamic skill reload without restart
- A skill management UI or API endpoint
- Multi-skill activation in a single request (e.g. `$ponytail $fhir-guard`)

---

## 4. User Story

> As a developer on a squad, I want to type `$ponytail` anywhere in my chat message and have the gateway automatically apply the minimalism skill to the LLM call — so I get a tighter, more YAGNI-compliant response without touching my IDE config or system prompt.

**Acceptance criteria**

1. A message containing `$ponytail` results in the ponytail skill content being prepended to the system prompt before the request is forwarded.
2. The `$ponytail` token is stripped from the user message text before forwarding (does not appear in the LLM's context).
3. If no system prompt exists in the request, one is created from the skill content alone.
4. If a system prompt already exists, the skill content is appended after a `---` separator.
5. The response carries header `X-GateMid-Skill-Applied: ponytail`.
6. Token overhead of the injected skill is recorded in the Redis analytics hash for the call.
7. An unrecognised trigger (e.g. `$unknownskill`) passes through unchanged — no error, no injection.
8. The ponytail skill file is loaded once at startup; a missing file logs a warning and the trigger is silently ignored.

---

## 5. Architecture

### 5.1 Middleware Positioning

The `SkillInjectorMiddleware` is inserted between `CaptureOriginalQuestionMiddleware` and the Headroom `CompressionMiddleware`. This ordering is intentional:

```
Inbound HTTP Request
        │
        ▼
ApiKeyMaskingMiddleware          ← mask keys from logs
        │
        ▼
CaptureOriginalQuestionMiddleware ← store raw user question
        │
        ▼
SkillInjectorMiddleware          ← NEW — detect trigger, inject skill
        │
        ▼
Headroom CompressionMiddleware   ← compress enriched payload
        │
        ▼
LiteLLM Proxy → Provider
```

Injecting before Headroom means the skill's prose is subject to SmartCrusher / Kompress compression — you pay net token cost, not gross. For a ~500-token skill, net cost after compression is typically 150–250 tokens.

### 5.2 Component Overview

```
proxy/
├── middleware/
│   ├── api_key_masking.py          (existing)
│   ├── capture_question.py         (existing)
│   └── skill_injector.py           ← NEW
├── skills/
│   ├── __init__.py
│   ├── registry.py                 ← NEW — loads .md files at startup
│   └── ponytail.md                 ← NEW — skill content
└── main.py / proxy_server.py       ← register new middleware
```

### 5.3 SkillRegistry

Loaded once at startup. Reads every `.md` file in `proxy/skills/`, keys the content by stem filename.

```python
# proxy/skills/registry.py

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SKILLS_DIR = Path(__file__).parent
_registry: dict[str, str] = {}


def load_skills() -> None:
    """Call once at application startup."""
    for skill_file in _SKILLS_DIR.glob("*.md"):
        name = skill_file.stem.lower()
        content = skill_file.read_text(encoding="utf-8").strip()
        if content:
            _registry[name] = content
            logger.info(f"[SkillRegistry] Loaded skill '{name}' ({len(content)} chars)")
        else:
            logger.warning(f"[SkillRegistry] Skill file '{skill_file}' is empty — skipped")


def get(skill_name: str) -> str | None:
    return _registry.get(skill_name.lower())


def list_skills() -> list[str]:
    return list(_registry.keys())
```

### 5.4 SkillInjectorMiddleware

```python
# proxy/middleware/skill_injector.py

import json
import logging
import re
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from proxy.skills.registry import get as get_skill

logger = logging.getLogger(__name__)

TRIGGER_PATTERN = re.compile(r"\$([a-zA-Z][a-zA-Z0-9_-]*)")
SKILL_SEPARATOR = "\n\n---\n\n"


class SkillInjectorMiddleware(BaseHTTPMiddleware):

    async def dispatch(self, request: Request, call_next) -> Response:
        # Only process chat completion endpoints
        if not self._is_chat_endpoint(request):
            return await call_next(request)

        body_bytes = await request.body()

        try:
            payload = json.loads(body_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return await call_next(request)

        skill_name, payload = self._detect_and_inject(payload)

        if skill_name:
            body_bytes = json.dumps(payload).encode("utf-8")

        # Rebuild request with (possibly mutated) body
        async def receive():
            return {"type": "http.request", "body": body_bytes}

        request._receive = receive

        response = await call_next(request)

        if skill_name:
            response.headers["X-GateMid-Skill-Applied"] = skill_name

        return response

    # ------------------------------------------------------------------

    def _is_chat_endpoint(self, request: Request) -> bool:
        return (
            request.method == "POST"
            and any(
                request.url.path.endswith(ep)
                for ep in ("/chat/completions", "/v1/chat/completions")
            )
        )

    def _detect_and_inject(
        self, payload: dict
    ) -> tuple[str | None, dict]:
        """
        Scan messages for a $trigger. If found:
          - strip the trigger from the user message
          - inject skill content into the system prompt
        Returns (skill_name | None, mutated payload).
        """
        messages: list[dict] = payload.get("messages", [])
        skill_name = None
        skill_content = None

        for msg in messages:
            if msg.get("role") != "user":
                continue
            content = msg.get("content", "")
            if not isinstance(content, str):
                continue

            match = TRIGGER_PATTERN.search(content)
            if not match:
                continue

            candidate = match.group(1).lower()
            found = get_skill(candidate)
            if found is None:
                logger.debug(
                    f"[SkillInjector] Trigger '${candidate}' not in registry — ignoring"
                )
                continue

            skill_name = candidate
            skill_content = found
            # Strip the trigger token from the message
            msg["content"] = TRIGGER_PATTERN.sub("", content, count=1).strip()
            break  # first match wins

        if skill_name and skill_content:
            payload = self._inject_system_prompt(payload, skill_content)

        return skill_name, payload

    def _inject_system_prompt(self, payload: dict, skill_content: str) -> dict:
        messages: list[dict] = payload.get("messages", [])

        system_messages = [m for m in messages if m.get("role") == "system"]

        if system_messages:
            # Append to the last system message
            last_system = system_messages[-1]
            existing = last_system.get("content", "")
            last_system["content"] = existing + SKILL_SEPARATOR + skill_content
        else:
            # Prepend a new system message
            messages.insert(0, {"role": "system", "content": skill_content})
            payload["messages"] = messages

        return payload
```

### 5.5 Startup Registration

```python
# In main.py or proxy_server.py — add after existing middleware registrations

from proxy.skills.registry import load_skills
from proxy.middleware.skill_injector import SkillInjectorMiddleware

# Load skills once at startup
load_skills()

# Register middleware — order matters
app.add_middleware(CompressionMiddleware)       # Headroom — outermost compression
app.add_middleware(SkillInjectorMiddleware)     # inject before compression
app.add_middleware(CaptureOriginalQuestionMiddleware)
app.add_middleware(ApiKeyMaskingMiddleware)
```

> Note: Starlette/ASGI middleware is applied in reverse registration order (last-added = first-executed). Adjust registration order so `SkillInjectorMiddleware` fires before `CompressionMiddleware` at runtime.

---

## 6. Skill File: ponytail.md

Location: `proxy/skills/ponytail.md`

**Upstream source:** The ponytail skill content is derived from the open-source [Ponytail](https://github.com/DietrichGebert/ponytail) project by Dietrich Gebert. The `.md` file placed in `proxy/skills/` should be sourced or adapted directly from that repository. Check the upstream repo for the latest version before authoring the local copy — the content below is a reference summary, not a substitute for the canonical source.

```markdown
# Skill: Ponytail — The Minimalism Ladder

You are a senior developer who values the code you never had to write.
Before proposing any implementation, descend this ladder and stop at the
first rung that holds:

1. **Does this need to exist at all?** Question the requirement before touching the keyboard.
2. **Is there a config, flag, or existing feature that already does this?** Check before adding.
3. **Can a one-liner or stdlib solve it?** Prefer language builtins over libraries.
4. **Is the simplest working solution good enough?** Avoid speculative abstractions.
5. **Only if all above fail:** write the minimal new code required — no more.

Rules:
- Never introduce a new dependency when stdlib suffices.
- Never create a new abstraction layer for a single use case.
- Never add configuration for something that has one sensible default.
- Prefer deletion over addition. Shorter diffs are better diffs.
- If you must build something, build the smallest thing that could possibly work.

When you present code, lead with the rungs you considered and which one you stopped at.
```

---

## 7. Redis Analytics Integration

Extend the existing `headroom:call:{call_id}` Redis hash with two new fields when a skill is applied:

| Field | Type | Example |
|---|---|---|
| `skill_name` | string | `"ponytail"` |
| `skill_tokens_pre_compression` | integer | `487` |

This allows the compression dashboard to report: "Ponytail skill injected in 38 calls this week. Avg token overhead before compression: 487. Avg after: 183."

Token counting uses the same tiktoken estimator already in use by the Ragas quality hook.

---

## 8. Observability

| Signal | Where |
|---|---|
| `INFO [SkillRegistry] Loaded skill 'ponytail' (1842 chars)` | Startup log |
| `DEBUG [SkillInjector] Trigger '$ponytail' matched — injecting` | Per-call log |
| `DEBUG [SkillInjector] Trigger '$unknownskill' not in registry — ignoring` | Per-call log |
| `X-GateMid-Skill-Applied: ponytail` | Response header |
| `skill_name` + `skill_tokens_pre_compression` | Redis `headroom:call:{id}` hash |

---

## 9. Error Handling & Edge Cases

| Scenario | Behaviour |
|---|---|
| Trigger mentions unknown skill | Pass through unchanged. Log at DEBUG. No error. |
| Skill `.md` file is empty | Skipped at load time with a WARN. Trigger treated as unknown. |
| Request body is not valid JSON | Middleware no-ops. Passes through to LiteLLM unchanged. |
| User message content is a list (multipart) | Middleware skips injection for that message. Only `string` content is scanned. |
| Multiple triggers in one message | First match wins. Subsequent triggers are left in the message text. |
| Skill file updated at runtime | Requires restart. Dynamic reload is out of scope for this FEAT. |

---

## 10. Testing Plan

### Unit tests — `tests/test_skill_injector.py`

| Test | Assertion |
|---|---|
| `test_trigger_detected_and_stripped` | `$ponytail` removed from user message content |
| `test_skill_injected_into_new_system_prompt` | System message created when none existed |
| `test_skill_appended_to_existing_system_prompt` | Separator + skill appended after existing system content |
| `test_unknown_trigger_passthrough` | Payload unchanged when `$unknownskill` present |
| `test_non_chat_endpoint_passthrough` | Non-`/chat/completions` request passes through untouched |
| `test_response_header_set` | `X-GateMid-Skill-Applied` header present when skill applied |
| `test_invalid_json_body_passthrough` | Middleware does not crash on malformed body |

### Integration test — `test_e2e.py` extension

```python
def test_ponytail_skill_injection(client):
    response = client.post("/v1/chat/completions", json={
        "model": "test-model",
        "messages": [{"role": "user", "content": "Refactor this service $ponytail"}]
    })
    assert response.status_code == 200
    assert response.headers.get("X-GateMid-Skill-Applied") == "ponytail"
    # Verify trigger stripped from forwarded message via captured request log
```

---

## 11. Future Skill Candidates

| Trigger | Purpose |
|---|---|
| `$fhir-guard` | Enforce FHIR R4 resource patterns; avoid raw HL7 string manipulation |
| `$spring-check` | Prefer existing `@Service` beans from domain graph; no new stereotype without justification |
| `$secure` | Input validation at every boundary, no wildcard CORS, no suppressed auth warnings |
| `$test-first` | Propose test before implementation; red–green–refactor discipline |
| `$pg-safe` | Avoid N+1 queries, prefer indexed columns, flag missing `LIMIT` clauses |

---

## 12. Delivery

| Task | Estimate |
|---|---|
| `proxy/skills/registry.py` | 1h |
| `proxy/skills/ponytail.md` | 0.5h |
| `proxy/middleware/skill_injector.py` | 2h |
| Startup wiring + middleware order fix | 0.5h |
| Redis analytics extension | 1h |
| Unit tests | 1.5h |
| E2E test extension | 0.5h |
| **Total** | **~7h** |