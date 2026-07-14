# PRD — Cache-Value-Adjusted Complexity Routing

**Status**: Draft — **gated on PRD #4 (Router Cache-Miss Impact
Measurement)**. Do not begin implementation until that report shows a
meaningful cost impact (see PRD #4 §6, exit criteria).
**Owner**: Hoang
**Component**: GateMid proxy — complexity router
**Depends on**: PRD #4 (session fingerprinting infra, real cache-miss cost
data)

---

## 1. Problem statement

Recap from PRD #4: the complexity router selects a model per-request based
on complexity score alone. This is blind to a second cost axis — whether
switching away from the model that currently holds a warm provider cache
for this session is worth the routing gain. Every cross-model switch
within the cache's TTL window is a guaranteed cold prefill on the shared
prefix, and that cost is currently invisible to the routing decision.

This PRD proposes making the router **cache-aware**: it computes an
adjusted cost per candidate model that includes the estimated value of
cache continuity, rather than treating model price as the only signal.

This is explicitly **not** sticky/session-affinity routing. Sticky routing
assumes staying is always worth it and hard-codes that assumption; this
approach computes whether it's worth it per-decision and lets the router
choose accordingly — including choosing to switch when the complexity gap
justifies it. This preserves the router's core design principle (route on
merit, per request) while correcting a blind spot in its cost model,
rather than overriding it with a fixed policy.

---

## 2. Design

### 2.1 Cost model

For each candidate model `m` being considered for a request in session `s`:

```
effective_cost(m, s) = base_cost(m) + cache_penalty(m, s)
```

Where:

- `base_cost(m)` = the router's existing cost signal (current per-token
  pricing × estimated tokens for this request) — unchanged from today.

- `cache_penalty(m, s)`:
  ```
  if m == last_model_used(s) AND time_since_last_request(s) < provider_cache_ttl(m):
      cache_penalty = 0   # cache is warm, no penalty, this model gets its
                           # normal cache-read discount naturally
  elif last_model_used(s) is not None AND time_since_last_request(s) < provider_cache_ttl(last_model_used(s)):
      # switching away from a model with a still-warm cache — this is the
      # case that costs real money
      cache_penalty = hot_zone_tokens(s) × (cache_write_price(m) - cache_read_price(last_model_used(s)))
      # i.e. what we forfeit: the read-discount we would have gotten by
      # staying, replaced by paying full/write price on the new model
  else:
      cache_penalty = 0   # cache already expired or no prior session data —
                           # switching is free from a caching perspective
  ```

- `hot_zone_tokens(s)` — estimated token count of the frozen hot zone
  (system prompt + tools + history up to the live-zone boundary) for this
  session. Can be tracked incrementally per session (updates each turn)
  rather than recomputed from scratch.

- `provider_cache_ttl(m)` — per-provider constant (~5 min for Anthropic;
  confirm actual current values per provider GateMid routes to, since
  these are provider-controlled and may differ or change).

### 2.2 Routing decision

The router's existing complexity-tier logic still determines the
*candidate* model(s) as it does today. The change is in the final
selection step: instead of picking the cheapest/best-fit candidate on
`base_cost` alone, pick on `effective_cost`. This means:

- A request that's genuinely complex enough to need a stronger model still
  gets routed there — `cache_penalty` doesn't override a real complexity
  requirement, it only breaks ties/near-ties in favor of cache continuity.
- A trivial follow-up that would otherwise bounce to a cheaper model, but
  where the switch cost exceeds the savings, stays on the current model.
- Once the cache has actually expired (gap exceeds TTL), `cache_penalty`
  is zero and the router behaves exactly as it does today — no unnecessary
  stickiness once there's nothing left to lose.

This directly reuses PRD #4's session-fingerprinting infrastructure
(`last_model_used(s)`, `time_since_last_request(s)`, `hot_zone_tokens(s)`
are all populated by that logging layer — no new session-identification
work needed here).

### 2.3 Configuration

- `cache_penalty` weight should be a tunable multiplier initially (e.g.
  `cache_penalty_weight` config, default 1.0), not hard-baked, so it can be
  dialed down/off quickly if the estimate proves inaccurate in practice —
  same operational caution as any new scoring dimension added to a live
  router.
- Provider cache pricing (`cache_write_price`, `cache_read_price`) should
  be config-driven per model/provider, not hardcoded, since providers
  change these.

---

## 3. Scope

### In scope
- Extend the router's scoring step to compute `effective_cost` per
  candidate using the formula in §2.1.
- Consume (not rebuild) PRD #4's session fingerprint, Redis session state,
  and hot-zone token tracking.
- Add config for `cache_penalty_weight` and per-provider cache pricing
  constants.
- Add logging of the *counterfactual*: for every routing decision, log
  both what was chosen and what would have been chosen under `base_cost`
  alone, so the actual savings from this change can be measured after
  launch (mirrors how PRD #4 measured the problem — measure the fix the
  same way).
- Update AI4PM pitch materials to describe this as the router's
  "cache-aware cost model," positioned alongside the Ragas-routing
  feedback loop as the two data-driven refinements to routing decisions —
  a unified narrative rather than two unrelated patches.

### Out of scope
- Sticky/session-affinity routing as a separate mechanism — superseded by
  this approach.
- Cross-provider cache sharing (not possible — out of GateMid's control).
- Any change to Headroom's compression pipeline.
- Predictive/lookahead routing (e.g. anticipating future turn complexity)
  — this PRD only adjusts cost for the *current* request.

---

## 4. Validation plan

1. **Shadow mode first**: compute `effective_cost` and log the
   counterfactual routing decision without actually changing routing
   behavior, for a period long enough to accumulate meaningful session
   volume (same order of magnitude as PRD #4's measurement window).
2. **Compare** shadow-mode counterfactual choices against actual PRD #4
   baseline data — confirm the adjusted model would have avoided the
   specific cache-miss events PRD #4 identified as costly, and quantify
   the estimated savings.
3. **Enable for real, gated behind `cache_penalty_weight`** starting at a
   conservative value, monitor `cache_read_input_tokens` /
   `cache_creation_input_tokens` trends and actual spend before/after,
   increase weight if results track the estimate.
4. **Guardrail**: track whether `effective_cost` ever causes a session to
   stay on a materially worse-fit model for many consecutive turns (i.e.
   confirm this isn't accidentally reintroducing sticky-routing's
   downside) — add an escape hatch (e.g. cap: cache_penalty cannot
   override a complexity-tier mismatch beyond N tiers) if this occurs.

---

## 5. Success criteria

- Effective-cost routing measurably reduces `cache_creation_input_tokens`
  (cache misses) for same-session, within-TTL requests, without
  increasing average model cost per request beyond what the complexity
  router would have chosen anyway for tasks that genuinely need a
  different model.
- The gap between shadow-mode predicted savings and actually-realized
  savings post-launch is small enough to trust the cost model going
  forward (if it's large, the pricing constants or `hot_zone_tokens`
  estimation need revisiting before wider rollout).

---

## 6. Open questions

- Do all providers GateMid routes to expose cache usage fields in a
  consistent way, or does this need per-provider normalization in
  LiteLLM's response handling?
- Is `hot_zone_tokens(s)` best tracked via GateMid's own token counting, or
  can it be read directly from provider `usage` fields on prior responses
  for that session (likely more accurate, avoids reimplementing a
  tokenizer)?
- Should `cache_penalty_weight` eventually be learned/tuned automatically
  (e.g. folded into the same Ragas-feedback-loop mechanism already planned
  for routing quality) rather than a static config value? Flag as a
  natural convergence point with that existing roadmap item, not something
  to build in this PRD.