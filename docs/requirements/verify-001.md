# Verifying Cache-Hot-Zone Stability in GateMid + Headroom

## Why this matters

GateMid routes requests through a complexity router, then through Headroom
(CacheAligner → ContentRouter → SmartCrusher/CodeCompressor/Kompress) before
hitting the upstream provider. Two independent things can silently kill
Anthropic/OpenAI prompt caching:

1. **Cross-model routing** — the complexity router sends turn N to a
   different model than turn N-1. Provider KV caches are model-scoped, so
   this is an unavoidable cache miss by design, not a bug. Not what this doc
   verifies.
2. **Non-deterministic prefix mutation** — anything upstream of, inside, or
   downstream of Headroom re-serializes or edits the "hot zone" (system
   prompt, tool defs, older turns) differently between calls, even when the
   *logical* content is identical. This is a bug, and it's what this doc is
   for.

Per Headroom's architecture, only the "live zone" (latest user message +
latest tool result) should ever be mutated. System prompt, tool definitions,
and older turns should be byte-identical across calls with the same logical
content. This doc gives you a way to prove that's actually true in your
deployed pipeline, plus a checklist for an agent to grep the source for
places that could violate it.

---

## Part 1 — Empirical test (black-box, run against your live GateMid proxy)

Goal: send the "same" conversation through GateMid twice (or N times with
small live-zone variations) and diff the *exact bytes* that leave Headroom
and hit the provider. The hot zone must diff as empty.

### 1. Get a byte-level capture point

You need visibility into the outbound request Headroom/GateMid actually
sends upstream — not what you sent into GateMid. Options, in order of
preference:

- **Best**: point GateMid/Headroom's upstream `base_url` at a local mock
  server (e.g. a tiny FastAPI/Flask app) instead of the real provider. Log
  the full raw request body + headers for every call, then return a canned
  response. This captures the true post-pipeline payload.
- **OK**: if Headroom runs as a standalone proxy (`headroom proxy --port
  8787`), check whether it exposes request logging/tracing (env var or
  `--log-requests` type flag) that dumps the final payload before it forwards
  it.
- **Fallback**: enable verbose/debug logging on whatever HTTP client
  LiteLLM/GateMid uses to talk to the provider (e.g. `httpx` logging), and
  capture the request body from logs.

### 2. Build the test harness

```python
# verify_cache_stability.py
import hashlib
import json
import difflib
from pathlib import Path

CAPTURED_DIR = Path("./captures")
CAPTURED_DIR.mkdir(exist_ok=True)

def hash_prefix(body: dict, prefix_chars: int | None = None) -> str:
    """Hash the serialized request body, or a leading slice of it."""
    raw = json.dumps(body, sort_keys=False)  # preserve actual key order sent
    if prefix_chars:
        raw = raw[:prefix_chars]
    return hashlib.sha256(raw.encode()).hexdigest()

def split_hot_and_live(body: dict) -> tuple[dict, dict]:
    """
    Split a captured request into 'hot zone' (system prompt, tools,
    all messages except the last user turn / last tool result) and
    'live zone' (the newest blocks Headroom is allowed to touch).

    Adjust the message-index logic to match your actual payload shape
    once you've captured a real example.
    """
    hot = {
        "system": body.get("system"),
        "tools": body.get("tools"),
        "messages": body.get("messages", [])[:-1],  # all but last turn
    }
    live = {
        "messages": body.get("messages", [])[-1:],  # last turn only
    }
    return hot, live

def diff_report(name: str, body_a: dict, body_b: dict):
    hot_a, live_a = split_hot_and_live(body_a)
    hot_b, live_b = split_hot_and_live(body_b)

    hot_a_s = json.dumps(hot_a, sort_keys=False, indent=2)
    hot_b_s = json.dumps(hot_b, sort_keys=False, indent=2)

    identical = hot_a_s == hot_b_s
    print(f"[{name}] hot zone byte-identical: {identical}")

    if not identical:
        print(f"[{name}] HOT ZONE DIFF (this should be empty!):")
        diff = difflib.unified_diff(
            hot_a_s.splitlines(), hot_b_s.splitlines(),
            lineterm="", fromfile="call_a_hot", tofile="call_b_hot"
        )
        print("\n".join(diff))

    print(f"[{name}] hot zone hash A: {hash_prefix(hot_a)}")
    print(f"[{name}] hot zone hash B: {hash_prefix(hot_b)}")
    print(f"[{name}] live zone differs (expected: True): "
          f"{json.dumps(live_a) != json.dumps(live_b)}")
```

### 3. Test cases to run

Run each of these as a pair (or triple) of calls through the real GateMid
pipeline, capture the outbound payload for each, then run `diff_report`.

| # | Test | What varies between calls | Hot zone should | Live zone should |
|---|------|---------------------------|------------------|-------------------|
| A | Baseline repeat | Nothing (exact same conversation, same model forced) | be byte-identical | be byte-identical |
| B | New user turn appended | Add one new user message | be byte-identical | differ |
| C | Large tool output injected | Latest tool result is a big JSON blob (triggers SmartCrusher) | be byte-identical | differ, and be shorter than raw |
| D | Timestamp in system prompt | System prompt template includes `Today is {date}` rendered per-call, same day both times | be byte-identical (CacheAligner should extract it) | n/a |
| E | Timestamp, different day | Same as D but run on two different days | **may legitimately differ** — confirm the diff is *isolated* to the extracted dynamic field, not a full reflow | n/a |
| F | Complexity router forces same model twice | Two calls scored into the same routing tier | be byte-identical | differ per content |
| G | Complexity router picks different models | Two calls scored into different tiers | N/A — expected cache miss, this is Part 1's "not a bug" case; just confirm hot zone content is *logically* equivalent even if provider-side cache can't be shared | differ |
| H | Session ID header present vs absent | Same conversation, with/without `x-headroom-session-id` | should NOT affect serialized body, only affects Headroom's internal session-key bookkeeping | n/a |

For each test, also compute and log:
```python
hash_prefix(hot_zone_body)   # full hot-zone hash
hash_prefix(hot_zone_body, prefix_chars=500)  # first-500-char hash, cheap sanity check
```//

### 4. Pass/fail criteria

- **Tests A, B, C, D, F, H**: hot zone hash must match exactly across calls.
  Any diff here is a bug — something is mutating content that should be
  frozen.
- **Test E**: diff is allowed but must be *localized* to the known dynamic
  field (e.g. only the date substring changes, not surrounding structure,
  not key ordering, not whitespace).
- **Test G**: hot zone bytes may differ if you're literally calling a
  different provider/model with a different tokenizer/system-prompt
  template — but if you're using the *same* system prompt text template
  across models, the logical content should still match; only note this as
  a routing-caused cache miss, not a Headroom bug.
- **All tests**: live zone must differ whenever the input differed (sanity
  check that you're not accidentally over-caching / stripping real changes).

### 5. Optional: verify against the real provider's cache signal

If using Anthropic, the API response includes usage fields
(`cache_creation_input_tokens`, `cache_read_input_tokens`). Log these across
repeated calls in test A/B/C/F:

- Call 1 (cold): expect `cache_creation_input_tokens` > 0
- Call 2+ (same hot zone): expect `cache_read_input_tokens` > 0 and
  `cache_creation_input_tokens` ≈ 0 for the shared prefix

This is the ground-truth confirmation — byte-diffing tells you the payload
*should* cache; the usage fields tell you the provider *actually* cached it.
If bytes match but `cache_read_input_tokens` is still 0, look for:
- Missing/misplaced `cache_control` breakpoints in the request
- Cache TTL expiry (idle >5 min between calls resets it)
- A `cache_control` breakpoint placed *after* content that's actually
  dynamic (breakpoint too early relative to the true prefix boundary)

---

## Part 2 — Source code audit checklist (for an agent to run against the repo)

Target repo: `github.com/nobleboyh/llm-mid` (GateMid) plus vendored/installed
`headroom` package if inspecting its internals is in scope.

Give this checklist to an agent (or run manually) with instructions to grep
for each pattern and report file:line + a judgment of risk.

### 2.1 — Find every place the outbound payload is constructed or re-serialized

```bash
# Anywhere a request body/messages list is built or rebuilt
grep -rn "json.dumps\|orjson.dumps\|model_dump\|dict(\|asdict(" --include="*.py" .
grep -rn "messages\s*=\s*\[" --include="*.py" .
grep -rn "system_prompt\|system=" --include="*.py" .
```
For each hit, check:
- [ ] Is dict/JSON key ordering stable (Python 3.7+ dicts preserve insertion
  order, but confirm nothing does `dict(sorted(...))` inconsistently, or
  builds dicts from a `set()` or unordered source anywhere in the chain)?
- [ ] Is this code in the hot-zone path (system prompt, tools, history) or
  live-zone path (latest turn)? Anything touching hot-zone content here is
  suspect.

### 2.2 — Find non-deterministic content sources

```bash
grep -rn "datetime.now\|time.time()\|uuid.uuid4\|uuid4()\|random\." --include="*.py" .
```
For each hit:
- [ ] Is the non-deterministic value inserted into the system prompt or any
  message that's supposed to be part of the stable hot zone?
- [ ] If yes: is it happening *before* CacheAligner runs (bad — CacheAligner
  can't fix what it never sees) or is CacheAligner's `dynamic_patterns`
  config actually matching this specific format? Check the regex list
  against the actual generated string format.
- [ ] Specifically check any code that builds the system prompt template —
  search for `f"Today is` / `strftime` / `.isoformat()` patterns feeding
  into a system prompt string, and confirm they match one of
  CacheAligner's `dynamic_patterns` (default includes `Today is \w+ \d+,
  \d{4}` and `Current time: .*` — a custom template using a different
  wording, e.g. `"Current date: {d}"`, will NOT be caught by default).

### 2.3 — Find where the complexity router's decision touches the prompt

```bash
grep -rn "def route\|complexity_router\|route_request\|select_model" --include="*.py" .
```
For each match:
- [ ] Does the routing logic inject *any* metadata into the system prompt or
  message history (e.g. a routing tier label, a debug comment, a model name
  string) before forwarding to Headroom? Even a single added token in the
  hot zone breaks the prefix match.
- [ ] Is routing decided *before* or *after* Headroom compression runs? If
  routing happens after compression, confirm compression doesn't need to
  know the target model (tokenizer differences could matter for token
  counting but shouldn't matter for byte content).
- [ ] Confirm the router reads request content read-only and does not
  mutate the request object in place (Python passes dicts by reference —
  check for in-place `.update()`, `.pop()`, key reordering via
  reconstruction, etc.)

### 2.4 — Verify Headroom's hot-zone boundary is actually being respected

```bash
grep -rn "live_zone\|hot_zone\|frozen_prefix\|ContentRouter\|CacheAligner" --include="*.py" .
```
- [ ] If GateMid vendors or wraps Headroom rather than calling it as an
  external library/proxy unmodified, confirm no custom code overrides which
  messages are considered "live" vs "hot." Check for any subclassing or
  monkeypatching of Headroom's pipeline classes.
- [ ] Confirm GateMid is on a Headroom version where `IntelligentContext`
  and `RollingWindow` (position/score-based context managers) are actually
  retired — those were removed because they could reorder/drop messages
  from the hot zone. Check `requirements.txt` / `pyproject.toml` pinned
  version and changelog vs the PR that retired them.
- [ ] Search for any config that sets `min_tokens_to_crush` or
  `max_items_after_crush` in a way that could apply SmartCrusher to
  something in conversation history rather than only the live zone —
  confirm these configs are scoped correctly and not globally applied to
  the full message list.

### 2.5 — Session key / cache_control breakpoint placement

```bash
grep -rn "cache_control\|x-headroom-session-id\|session_id" --include="*.py" .
```
- [ ] Confirm `cache_control` breakpoints (if GateMid sets these explicitly
  for Anthropic calls, rather than relying on Headroom/provider defaults)
  are placed at the actual true boundary between hot and live zones — a
  breakpoint placed one message too early or late either caches dynamic
  content (wasted cache writes, never hits) or fails to cache stable
  content (missed savings).
- [ ] Confirm the session key derivation (`md5(model + system_prompt[:500])`
  per Headroom's own scheme, or GateMid's own if reimplemented) doesn't
  change when only the *live zone* changes — it should only change when
  `model` or the first 500 chars of `system_prompt` change.
- [ ] If the complexity router changes `model` per turn, trace what
  downstream systems key off `model` as part of a cache/session key
  (SkillInjectorMiddleware, Redis observability keys) and confirm that's an
  intentional, understood side effect rather than an accidental cache split.

### 2.6 — Report format for the agent

For each finding, report:
```
FILE: path/to/file.py:LINE
CATEGORY: [2.1 serialization | 2.2 nondeterminism | 2.3 router injection |
           2.4 hot-zone boundary | 2.5 cache_control/session key]
RISK: [confirmed bug | likely bug | needs empirical test | false positive]
NOTE: <one line: what's happening and why it matters for cache stability>
```

---

## Summary of expected outcome

If GateMid is correctly using Headroom's current live-zone-only compression
architecture, and nothing custom in GateMid mutates the hot zone:

- Part 1 tests A/B/C/D/F/H should all show byte-identical hot zones.
- Part 2 audit should turn up zero hits in 2.1–2.4 that touch hot-zone
  content, and any `cache_control` placement in 2.5 should align exactly
  with the hot/live boundary.

If either part turns up a violation, the fix is almost always one of:
1. Move the non-deterministic content generation to run through
   CacheAligner (or extend its `dynamic_patterns`) before it reaches the
   hot zone.
2. Move router-injected metadata out of the prompt entirely (use HTTP
   headers or a side-channel instead of embedding it in `system` or
   `messages`).
3. Fix `cache_control` breakpoint placement to match the true prefix
   boundary.