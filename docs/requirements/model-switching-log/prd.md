# PRD — Router Cache-Miss Impact Measurement (Phase 0)

**Status**: Draft
**Owner**: Hoang
**Component**: GateMid proxy (`github.com/nobleboyh/llm-mid`)
**Depends on**: Cache Stability Audit (verified — Headroom hot/live zone boundary confirmed clean)
**Precedes**: PRD — Cache-Value-Adjusted Routing (#2)

---

## 1. Problem statement

The complexity router picks a model per-request based on complexity score,
independent of which model previously served the same conversation. If a
session's turns get routed to different models, each switch is a guaranteed
cold-cache prefill on that session's shared prefix (system prompt, tools,
history) — because provider KV caches are model-scoped and switching models
means talking to a cache that has never seen this prefix.

**What we don't know yet**: whether this is actually costing meaningful
money/latency in practice, or whether it's a theoretical problem that
doesn't matter because:

- Provider cache TTL is short (Anthropic: ~5 min idle) — if real sessions
  already have multi-minute gaps between turns, the cache would have
  expired anyway regardless of routing, and fixing routing wouldn't help.
- The router might already be stable enough in practice (e.g. complexity
  scores rarely flip near tier boundaries) that cross-model switches within
  a session are rare.
- Session prefixes might be small enough (short system prompt, few tools)
  that the absolute cost of a cache miss is negligible either way.

**Goal of this phase**: measure the real thing before building anything to
fix it. Output is a report with hard numbers, which becomes the go/no-go
gate for PRD #2 (Cache-Value-Adjusted Routing).

---

## 2. Why this requires a session-identification method

GateMid's proxy is stateless per-request. There is no reliable guarantee
that the client sends a stable session identifier — depending on the
calling tool (Claude Code, Codex CLI, custom scripts hitting the proxy
directly), a session ID header may or may not be present or consistent.
Headroom's own session-key scheme
(`md5(model + system_prompt[:500])`) is a reasonable fallback precedent
but is keyed partly by `model` — which is exactly the thing that changes
when the router switches, so it can't be used as-is to detect "this is the
same session, but the router flipped the model" (that's definitionally a
key change under Headroom's scheme).

We need to answer: **"is this incoming request a continuation of a
conversation we've already seen, possibly with a different model?"**
without relying on the model field, and without assuming any client-side
cooperation.

### 2.1 Candidate methods (evaluate in this order — cheapest/most-reliable first)

| # | Method | How | Reliability | Cost | Notes |
|---|--------|-----|-------------|------|-------|
| A | **Explicit client session header** | Check for `x-headroom-session-id` or any client-supplied session/conversation ID already present in headers | Exact, free | Zero | **Check this first.** If GateMid's actual client population (Claude Code, internal tools) already sends this, most of the problem is solved for free. Audit real traffic before building anything else. |
| B | **Structural fingerprint** | Hash `(system_prompt[:500] + first_user_message[:500] + tool_names_sorted)` — stable across turns of the same conversation, independent of `model` and independent of the *latest* turn's content | Exact match, near-zero false positives | Cheap (one hash per request) | Directly analogous to Headroom's own scheme, minus the `model` component that breaks cross-model detection. This should be the primary method. |
| C | **Prefix superset check** | Treat request B as a continuation of request A if B's message list = A's message list + N new trailing messages (i.e. B's history is a strict prefix-extension of A's) | Exact, but requires keeping recent request bodies in memory/Redis to compare against | Moderate — O(sessions in TTL window) comparisons per request | Useful as a secondary confirmation on top of B, or as the primary method if system prompts are highly repetitive across *different* sessions (making B's fingerprint collide) |
| D | **Fuzzy prefix similarity** | Token-level longest-common-prefix ratio between incoming request's message history and recent sessions' last-seen history; treat as same session above a similarity threshold (e.g. ≥90% of tokens match in the same order) | Approximate — tunable false-positive/negative rate | Higher (token-level diffing, not just hashing) | Last resort — only needed if B/C prove insufficient in practice (e.g. because Headroom's SmartCrusher live-zone compression changes recent-turn content enough that superset/fingerprint checks miss real continuations) |

### 2.2 Recommended approach for this phase

Layered, cheapest-first:

1. **Audit real traffic first** (Method A) — before writing any detection
   logic, log what session-identifying headers actually arrive on
   production/dev traffic today. If a usable header is already present and
   consistent across turns for the clients you actually care about
   (internal team usage, Claude Code sessions), skip straight to using it
   and Methods B–D become unnecessary for this phase.
2. **If A is absent or unreliable**, implement Method B (structural
   fingerprint) as the default. It is cheap, deterministic, reuses a
   pattern already proven in Headroom, and doesn't require any new
   storage beyond a short-TTL Redis key (which GateMid already has for
   observability).
3. **Do not build Method C or D in this phase.** They add real complexity
   and are only justified if B's false-negative rate turns out to matter —
   which this measurement phase should surface empirically, not assume in
   advance. If B's fingerprinting shows a meaningfully high rate of
   "session lost track" events during analysis, that becomes a documented
   finding and a candidate follow-up, not a Phase 0 deliverable.

This keeps Phase 0 scoped to measurement, not to building a production-
grade session tracker.

---

## 3. Scope

### In scope
- Add lightweight session-fingerprinting (Method A with B fallback) to the
  proxy, write-through to Redis, non-blocking, observability-only (no
  effect on routing behavior).
- Log, per request: session fingerprint, chosen model, previous model for
  that fingerprint (if known), time since last request for that
  fingerprint, estimated hot-zone token count, provider-reported
  `cache_read_input_tokens` / `cache_creation_input_tokens` (Anthropic) or
  equivalent.
- Maintain a **running per-session switch record** — not just the
  immediately-previous model, but the full sequence of models used across
  the session, so we can report switch counts and which specific model
  pairs are involved (see §3.1 below).
- Build an offline analysis script/notebook that answers the questions in
  §4 from the collected data.
- Produce a short report with the findings, to gate the #2 PRD.

### 3.1 Per-session switch detail (required)

Aggregate switch counts alone ("X% of turns switched") aren't enough to
size or prioritize the fix — we need to know *how many times* a given
session flips and *between which models*, since a session that ping-pongs
5 times between two cheap models is a very different problem (and fix)
than one that switches once from a cheap to an expensive model.

For each session fingerprint, track and persist (append-only, alongside
the per-request log in §5.2):

```
{
  "session_key": "...",
  "model_sequence": ["deepseek-chat", "deepseek-chat", "claude-sonnet",
                      "deepseek-chat", "claude-opus", "claude-opus"],
  "switch_count": 3,
  "switch_pairs": [
    {"from": "deepseek-chat", "to": "claude-sonnet", "at_turn": 3,
     "seconds_since_last_request": 42, "within_cache_ttl": true},
    {"from": "claude-sonnet", "to": "deepseek-chat", "at_turn": 4,
     "seconds_since_last_request": 18, "within_cache_ttl": true},
    {"from": "deepseek-chat", "to": "claude-opus", "at_turn": 5,
     "seconds_since_last_request": 30, "within_cache_ttl": true}
  ],
  "total_turns": 6
}
```

- `switch_count` = number of turns where `model_chosen != previous_model`
  for that session — the primary per-session metric for §4 Q1.
- `switch_pairs` = the specific `(from, to)` model pairs involved, each
  tagged with whether that switch happened within the provider's cache TTL
  (i.e. whether it was a switch that actually cost a warm cache, vs. one
  that happened after the cache had already expired and was therefore
  free). This directly feeds §4 Q4's cost calculation — you can't compute
  the lost-cache cost without knowing *which* model pair was involved,
  since `cache_write_price` differs per destination model.
- This also surfaces routing *patterns* worth knowing regardless of the
  cache question — e.g. if one specific pair (say, cheap-tier ↔
  mid-tier) accounts for the vast majority of switches, that's useful
  context for tuning complexity-tier boundaries later, independent of
  whether PRD #2 gets built.

### Out of scope
- Any change to routing behavior.
- Any change to Headroom's pipeline.
- Fuzzy/prefix-similarity session detection (Methods C/D) — only revisit if
  Method B's limitations are empirically shown to matter.
- Building a persistent, production-grade session store — this phase's
  Redis usage is disposable/short-TTL, for measurement only.

---

## 4. Questions this measurement phase must answer

1. **How often does the router switch models within the same session, and
   between which specific models?** Report both the aggregate rate (% of
   turns that are a model-switch from the previous turn) and the
   per-session distribution of `switch_count` (p50/p90/max — a single
   outlier session flipping 15 times matters differently than every
   session averaging 1 switch). Break down `switch_pairs` by frequency to
   identify which specific model-pair transitions dominate (e.g. is it
   mostly cheap-tier ↔ mid-tier flapping near a threshold, or occasional
   jumps to the top tier for a single hard turn then back down?).
2. **What is the actual time gap between turns in real sessions?**
   Distribution (p50/p90/p99) of inter-turn gaps. Compare against the
   provider's cache TTL (~5 min for Anthropic) — if most gaps already
   exceed TTL, routing-caused cache loss is moot; the cache would have
   expired anyway.
3. **When a same-model, same-session turn occurs within the TTL window, do
   we actually observe a cache hit?** (sanity-check against
   `cache_read_input_tokens` > 0) — this validates that Headroom's hot
   zone is stable enough in practice for the provider to recognize it,
   consistent with the earlier cache-stability audit, but now observed
   under real multi-turn sessions rather than synthetic test pairs.
4. **When a model-switch occurs within the TTL window (i.e. the cache
   *would* have been warm on the old model), what is the estimated cost of
   that lost cache read?** Compute as
   `hot_zone_tokens × (cache_write_price - cache_read_price)` for the
   provider/model in question, summed across all such events in the
   measurement window.
5. **What fraction of total token spend does that lost-cache cost
   represent?** This is the number that actually matters for prioritization
   — if it's <1% of spend, #2 is not worth building yet regardless of how
   elegant the fix is.
6. **Does Method B's session fingerprint ever appear to "lose" a
   continuing session** (i.e. a request that a human reviewer can tell is
   a continuation, but the fingerprint doesn't match)? Spot-check a sample.
   This determines whether Methods C/D need to be revisited.

---

## 5. Implementation plan

### 5.1 Session fingerprint module
- New module, e.g. `proxy/session_fingerprint.py`.
- Input: incoming request body + headers.
- Logic:
  1. If a usable session/conversation ID header is present (confirm the
     actual header name from the traffic audit in §2.2 step 1) → use it
     directly as the session key.
  2. Else, compute `sha256(system_prompt[:500] + first_user_message[:500]
     + sorted(tool_names))[:16]` as the fingerprint.
- Output: session key string. Attach to request context for downstream
  logging (does not touch the request body — read-only, consistent with
  the cache-stability audit's finding that GateMid's existing middleware
  never mutates hot-zone content; this module must follow the same rule).

### 5.2 Logging
- On every request, write a Redis entry (short TTL, e.g. 30 min — long
  enough to observe patterns, short enough to stay disposable) keyed by
  session fingerprint:
  ```
  {
    "session_key": "...",
    "timestamp": ...,
    "model_chosen": "...",
    "previous_model": "..." | null,
    "seconds_since_last_request": ... | null,
    "hot_zone_token_estimate": ...,
    "cache_read_input_tokens": ... | null,
    "cache_creation_input_tokens": ... | null,
    "provider": "..."
  }
  ```
- Append-only log (list or stream), not overwrite — need the full sequence
  per session for the analysis in §4, not just the latest state.

### 5.3 Offline analysis
- Script (`scripts/analyze_router_cache_impact.py`) that reads the Redis
  logs (or a periodic dump to a flat file/SQLite for durability beyond
  Redis TTL) and computes the metrics in §4.
- Output: a markdown or notebook report with the distributions, a
  breakdown table of switch frequency by model pair (from → to, count,
  % within cache TTL, estimated cost where applicable), and the headline
  number from §4.5 (lost-cache cost as % of total spend).

### 5.4 Duration
- Run for a minimum of 1–2 weeks of real usage, or until a statistically
  reasonable number of multi-turn sessions have been captured (exact
  threshold TBD based on actual traffic volume — check session count after
  a few days and extend if too sparse).

---

## 6. Success criteria / exit condition

This phase is complete when the report in §5.3 exists and gives a clear
answer to:

> "What percentage of GateMid's total token spend is attributable to
> router-caused cache misses (model switches within the provider's cache
> TTL window)?"

- **If the number is small** (e.g. low single-digit % or less) →
  document the finding, do not build PRD #2 now; revisit if usage patterns
  change (e.g. much larger system prompts, much higher turn frequency).
- **If the number is meaningful** → proceed to PRD #2 with this data as
  the quantitative justification, and use the same fingerprinting/logging
  infrastructure as the foundation for #2's routing decisions (no need to
  rebuild session identification — #2 consumes what this phase built).

---

## 7. Risks / open questions

- **Method B fingerprint collisions across genuinely different sessions**
  with similar system prompts (e.g. two different users, same team,
  identical system prompt template) — low risk given the fingerprint also
  includes the first user message, but worth spot-checking in §4.6.
- **Redis TTL vs. actual session length** — if real sessions run longer
  than the chosen TTL (e.g. a long debugging session spanning hours with
  gaps), the fingerprint log could expire mid-session and undercount
  switches. Validate the TTL choice against the p90 session duration once
  data exists, and extend if needed.
- **Provider cache usage fields availability** — confirm
  `cache_read_input_tokens` / `cache_creation_input_tokens` (or provider
  equivalents) are actually present in the response payloads GateMid
  receives today; if LiteLLM strips or doesn't surface them, this needs a
  small fix before logging can capture them.