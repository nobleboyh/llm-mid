# Middleware: Skill Injector

**File:** `proxy/skill_injector.py`
**Registry:** `proxy/skills/registry.py`
**Skills:** `proxy/skills/*.md`
**Position:** Middle (runs THIRD inbound)
**Order:** 3 of 4

## Purpose

Detects `$<skill-name>` trigger tokens in user messages (e.g. `$ponytail`, `$caveman`), strips them from the forwarded message, and injects the corresponding skill content into the system prompt. Supports multiple triggers in a single message.

## Architecture

```
Inbound:  ApiKeyMasking → CaptureOriginal → SkillInjector → Compression → LiteLLM
```

Positioned BEFORE compression so the injected skill text gets compressed with the rest of the payload. This minimizes net token overhead — a typical ~500-token skill becomes ~150-250 tokens after compression.

## Trigger detection

### Pattern
```
Regex: \$([a-zA-Z][a-zA-Z0-9_-]*)
```
Match `$` followed by an alpha-starting, alphanumeric/underscore/hyphen identifier. Only triggers that match a registered skill name are activated — unknown triggers are silently ignored.

### Content formats supported

**String content** (OpenAI `/v1/chat/completions`):
```json
{"role": "user", "content": "Refactor this $ponytail"}
```

**List-of-blocks content** (Anthropic `/v1/messages`):
```json
{"role": "user", "content": [{"type": "text", "text": "Refactor this $ponytail"}]}
```

### Multiple triggers

All `$trigger` tokens in a message are detected. Skills are injected in alphabetical order, each dedup-checked individually:

```
$caveman $ponytail summarise this
→ triggers: ["caveman", "ponytail"]
→ injected: caveman first (alphabetical), then ponytail
→ user sees: "summarise this"
```

## How it works

1. **Buffers the full request body** from the ASGI receive stream
2. **Parses JSON** — non-JSON bodies pass through unmodified
3. **Scans all user messages** for `$trigger` tokens using `TRIGGER_PATTERN`
4. **Validates each trigger** against the skill registry (loaded at startup)
5. **Strips ALL `$trigger` tokens** from user messages
6. **Injects matched skill contents** into system prompt (alphabetical order)
7. **Dedup-checks** each skill against existing system prompt content
8. **Sets context var** `skill_info_var` with skill names and token count
9. **Adds response header** `X-GateMid-Skill-Applied: skill1, skill2`
10. **Forwards modified request** via `modified_receive`

## Skill injection method

Skills are appended to the LAST system message with a separator:

```python
separator = "\n\n---\n\n"
existing_system_content + separator + skill_content
```

If no system message exists, a new one is prepended at `messages[0]`.

## Dedup prevention

The `_already_injected()` method prevents duplicate injection when the ASGI middleware fires multiple times for the same request. It compares the first non-empty, non-comment line of the skill content against existing system messages:

```python
sig = first_meaningful_line(skill_content)[:80]  # signature
if sig in any_existing_system_message:
    skip_this_skill
```

## Skill registry

`proxy/skills/registry.py` — loaded once at startup in `entrypoint.py`:

```python
load_skills()  # scans proxy/skills/*.md at startup
```

Registry API:
```python
get(name: str) -> str | None    # content by lowercase stem
list_skills() -> list[str]       # sorted list of registered names
```

Auto-discovery: every `.md` file in `proxy/skills/` is keyed by its lowercase stem. Empty files are skipped with WARNING.

## Context variable

```python
skill_info_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "skill_info", default=None
)
```

Set to `None` by default. When skills are injected, set to:
```python
{
    "skill_names": ["caveman", "ponytail"],
    "skill_tokens_pre_compression": 850
}
```

Read by:
- **`entrypoint.py` patched compress()** — enriches headroom Redis hash with skill analytics
- **`callback.py` RagasLogger** — attaches skill info to eval records

## Response header

```
X-GateMid-Skill-Applied: ponytail, caveman
```

Comma-separated, lowercase skill names. Only set when skills are activated. Content-Length is stripped from response headers because the body may have changed.

## Token estimation

`_count_tokens()` uses `tiktoken` (`cl100k_base` encoding) if available, otherwise falls back to `len(text) // 4`. Used to track pre-compression token count for analytics.

## Current skills

| Trigger | File | Effect |
|---------|------|--------|
| `$ponytail` | `ponytail.md` | Minimalism Ladder — 7-rung YAGNI, ~54% LOC reduction |
| `$caveman` | `caveman.md` | Ultra-compressed output — 65-75% token reduction |

## Adding a new skill

1. Create `proxy/skills/<name>.md` with markdown content
2. Restart proxy: `docker compose restart litellm`
3. Use `$<name>` in any message

No code changes, no config reload.

## Edge cases

| Scenario | Behaviour |
|----------|-----------|
| Unknown trigger (`$unknownskill`) | Ignored, passes through |
| Multiple triggers in one message | All matched, alphabetically injected |
| Multipart (list) message content | Scanned block-by-block |
| Missing/empty skill file | Skipped at load time with WARNING |
| Non-JSON request body | Middleware no-ops, passes through |
| Skill content already in system prompt | Skipped (dedup check) |
| Empty request body | Passed through unmodified |

## Logging

At INFO:
```
[SkillInjector] path=/v1/messages — skill(s) 'caveman, ponytail' injected (850 tokens pre-compression)
```

At INFO (no trigger):
```
[SkillInjector] path=/v1/messages — scanned 3 messages, no trigger found
```

At DEBUG:
- Per-trigger match details
- Dedup skip notifications
- Injection confirmation per skill

## Test coverage

`tests/test_skill_injector.py` — covers registry loading, trigger detection in both content formats, multiple triggers, stripping, injection placement, dedup, response headers, and edge cases.
