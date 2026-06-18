# PRD: AI Memory Layer (mem0) — Cross-Session Context & Adaptive Routing

**Version:** 1.0.0 — Draft
**Date:** June 2026
**Status:** Proposed
**Supersedes:** None — new capability
**Depends on:** init-003 (Ragas Eval Layer — baseline quality measurement)

---

## 1. Executive Summary

GateMid today is a stateless gateway: every request is compressed (Headroom), routed (ComplexityRouter), and scored (Ragas) in isolation. No information persists between sessions, between users, or even between consecutive turns in the same session beyond what the LLM provider's KV cache holds.

This document proposes integrating [mem0](https://mem0.ai) — an open-source memory layer for LLM applications — into the GateMid middleware stack. mem0 stores, retrieves, and manages user preferences, project context, learned routing patterns, and interaction history as vector-embedded memories, then injects relevant context into future requests before compression.

The result: **cross-session memory for every connected tool with zero client changes**, plus a feedback loop from Ragas quality scores into memory importance weighting.

### Why Now

| Precondition | Status | Notes |
|---|---|---|
| Ragas eval baseline (init-003) | ✅ **Deployed** | Scoring pipeline proven; Redis data flowing |
| Compression layer (init-002) | ✅ **Deployed** | Headroom ASGI middleware stable |
| Smart routing (init-001) | ✅ **Deployed** | ComplexityRouter tuned for Claude Code |
| Quality signal to feed memory | ✅ **Available** | Every call gets composite_score, per-dimension scores |
| **Missing: cross-session persistence** | ❌ **Gap** | No memory between sessions = repeated context, cold routing, missed optimisation |

---

## 2. What mem0 Does

mem0 is a memory management system that:

1. **Stores** conversations, facts, user preferences, and learned patterns as vector + text pairs in a storage backend
2. **Retrieves** the most relevant memories for a given input using semantic (embedding) search
3. **Updates** existing memories when new information contradicts or refines old information
4. **Scores** each memory by recency, importance, and user-defined relevance

For GateMid, the critical interfaces are:

```python
# Store a memory
mem0.add("User prefers JSON responses for FHIR queries", user_id="ito", metadata={"category": "preference"})

# Retrieve relevant memories for a new query
memories = mem0.search("How do I map FHIR Observation resources?", user_id="ito")
# Returns: [
#   {"text": "User prefers JSON responses for FHIR queries", "score": 0.92, ...},
#   {"text": "Last session discussed Observation.code.coding mapping", "score": 0.87, ...},
# ]

# Update memory importance from feedback
mem0.update(memory_id="xxx", metadata={"ragas_score": 0.95, "importance": 0.9})
```

---

## 3. Integration Architecture

### 3.1 Layer Placement

mem0 integrates as **two middleware hooks** on the existing ASGI stack — one on the request path (retrieve + inject), one on the response path (store + learn):

```
Current stack:
  ApiKeyMasking → CaptureOriginal → Headroom Compression → ComplexityRouter → LLM
                                                                                 ↓
                                                                           RagasLogger → Redis (scoring)

With mem0:
  ApiKeyMasking → CaptureOriginal → [mem0 Retrieve + Inject] → Headroom Compression → ComplexityRouter → LLM
                                                                                                            ↓
                                                                                                      RagasLogger → [mem0 Store] → Redis (scoring + memory)
```

### 3.2 Sidecar Service

Following the existing pattern (Ragas eval-worker is a separate container), mem0 runs as its own service with its own storage:

```yaml
# docker-compose.yml — new service
mem0:
  image: mem0ai/mem0:latest    # or custom build with qdrant/embedding support
  container_name: gatemid-memory
  restart: unless-stopped
  environment:
    MEM0_API_KEY: ${MEM0_API_KEY:-}
    OPENAI_API_KEY: ${OPENAI_API_KEY}        # or use Gemini for embeddings
    MEM0_CONFIG_PATH: /app/mem0_config.yaml
  volumes:
    - mem0_data:/app/data
    - ./mem0_config.yaml:/app/mem0_config.yaml:ro
  depends_on:
    redis:
      condition: service_healthy
  ports:
    - "8060:8060"    # mem0 REST API
```

The **proxy** connects to this sidecar via lightweight HTTP calls. The **eval-worker** also connects to write score-weighted memories.

### 3.3 Proxy-Side: mem0RetrieveMiddleware (new ASGI middleware)

A new middleware class registered between `CaptureOriginalQuestionMiddleware` and Headroom's `CompressionMiddleware`:

| Aspect | Detail |
|--------|--------|
| **Position** | 2nd middleware (after capture_original, before compression) |
| **What it does** | Extracts user_id and last user message → queries mem0.similar() → injects top-5 memories as system message enrichment |
| **Latency budget** | ≤50ms (non-blocking HTTP call to local mem0 sidecar) |
| **Maximum injection** | 3 memories, 200 tokens each, 600 tokens max total injection |
| **Fallback** | If mem0 is unreachable or empty → pass through unmodified |
| **Privacy** | Injects AFTER ApiKeyMasking but BEFORE Redis storage → keys are already masked |

```python
class Mem0RetrieveMiddleware:
    """Injects relevant memories before Headroom compression."""

    MEM0_API = "http://mem0:8060"

    async def __call__(self, scope, receive, send):
        # ... buffer body same as CaptureOriginal pattern ...
        # Retrieve user_id from request metadata (or x-user-id header)
        user_id = self._extract_user(data)
        question = self._extract_last_user_message(data)

        if user_id and question:
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{self.MEM0_API}/v1/search/",
                        json={"query": question, "user_id": user_id, "limit": 3},
                        timeout=0.2,
                    )
                    memories = resp.json().get("results", [])
                    if memories:
                        memory_texts = [m["text"] for m in memories if m["score"] > 0.7]
                        if memory_texts:
                            # Inject as a system message
                            system_msg = {
                                "role": "system",
                                "content": f"[Memory context]\n" + "\n".join(memory_texts)
                            }
                            messages.insert(0, system_msg)
                            data["messages"] = messages
                            # Flag so RagasLogger can instrument this
                            data["metadata"]["mem0_injected_memories"] = len(memory_texts)
            except Exception:
                pass  # best-effort — never fail the request
```

### 3.4 Proxy-Side: mem0StoreHook (LiteLLM callback extension)

Extended from the existing `RagasLogger.log_success_event()` — after enqueuing the Ragas scoring record, also push a memory record to mem0:

```python
class RagasLogger(CustomLogger):
    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        # ... existing logic (loop prevention, extraction, enqueue) ...

        # NEW: Store memory (async, best-effort)
        if question.strip() and answer.strip():
            try:
                store_memory(
                    text=f"Q: {question}\nA: {answer}",
                    user_id=meta.get("user_id", "default"),
                    metadata={
                        "request_category": meta.get("request_category", "general"),
                        "model": kwargs.get("model", ""),
                        "call_id": call_id,
                    }
                )
            except Exception:
                pass
```

### 3.5 Eval-Side: Score-Weighted Memory Update

In `eval/worker.py`, after `score_record()` writes to Redis, also update the corresponding mem0 memory with its quality score:

```python
def score_record(record, llm=None, embeddings=None):
    # ... existing scoring logic ...

    # NEW: Update memory importance from quality score
    try:
        _update_memory_importance(record["call_id"], record["composite_score"],
                                   record["scores"])
    except Exception:
        pass

def _update_memory_importance(call_id: str, composite: float, scores: dict):
    """Push quality feedback to mem0 so high-scoring interactions persist longer."""
    httpx.post(
        f"{MEM0_API}/v1/memories/{call_id}/feedback",
        json={
            "score": composite,
            "metrics": scores,
            "action": "promote" if composite > 0.85 else "demote" if composite < 0.3 else "neutral",
        },
        timeout=0.2,
    )
```

---

## 4. Memory Types & Lifecycle

| Memory Type | Source | Stored When | Retrieved When | TTL |
|---|---|---|---|---|
| **User Preference** | User message content, system prompt overrides | Every request | Every request from same user | 90d (decays without reinforcement) |
| **Project Context** | File snapshots, CLAUDE.md patterns | On high-quality responses (composite > 0.85) | Queries matching project tags | 30d |
| **Routing Pattern** | ComplexityRouter decision + Ragas score | After scoring | Router initialisation (daily refresh) | 7d (recalculated from aggregate) |
| **Interaction History** | Q&A pairs | Every request (truncated to 500 chars) | Semantic similarity to current query | 7d |
| **Failure Pattern** | Low-scoring responses (composite < 0.3) | When score is below threshold | Never (stored for analysis only) | 14d |

### Memory Decay Strategy

- Each memory has a `last_accessed` timestamp
- Retrieval score is multiplied by `decay_factor = 1 / (1 + days_since_access * 0.1)`
- High Ragas scores (>0.85) reset the decay clock (memory was useful → keep it fresh)
- Low scores (<0.3) accelerate decay (memory wasn't useful → get rid of it faster)

---

## 5. Evaluation Methodology

This section defines how to **measure** whether mem0 integration delivers value. Every claim must be testable with an A/B experiment using the existing Ragas infrastructure.

### 5.1 Hypotheses & Test Design

| # | Hypothesis | Null Hypothesis (H₀) | Test Design |
|---|---|---|---|
| H1 | mem0 memory injection reduces input token count by ≥10% beyond Headroom alone | mem0 injection saves <10% additional tokens | A/B: every 2nd request gets mem0 injection; compare Headroom-reported `tokens_before/tokens_after` |
| H2 | mem0 improves composite Ragas score by ≥0.05 | Score difference (mem0 − no-mem0) < 0.05 | A/B: tag requests with `mem0: true` in metadata; filter leaderboard by tag |
| H3 | mem0 reduces repeated context in multi-turn sessions | Same project context re-sent unchanged across turns | Measure duplicate tokens in consecutive requests from same user |
| H4 | mem0 improves routing tier accuracy (fewer "Too complex" misclassifications to flash) | Routing accuracy unchanged | Compare ComplexityRouter tier vs Ragas score per tier — mem0-injected context should shift borderline queries to correct tier |
| H5 | High-scoring memories persist longer (score-weighted decay) | Memory importance score is uncorrelated with Ragas composite | Track memory last_accessed timestamp vs Ragas score; Pearson correlation |

### 5.2 A/B Experiment Framework

GateMid already has everything needed to run A/B experiments without any external tooling:

#### Assignment

```python
# In Mem0RetrieveMiddleware — deterministic assignment by call_id hash
import hashlib

def _is_mem0_group(call_id: str) -> bool:
    """Assign 50% of calls to mem0 group deterministically."""
    return int(hashlib.md5(call_id.encode()).hexdigest(), 16) % 2 == 0
```

#### Tagging

The middleware sets `metadata.mem0_enabled: true|false` on every request. This propagates to:
- The RagasLogger callback → stored in Redis `eval:call:{call_id}` hash
- The ComplexityRouter log line → visible in proxy logs
- Headroom compression stats → attributed correctly

#### Comparison Queries

```bash
# Mean composite with mem0
python -m eval.score_view --prompt-id "mem0_enabled" --json \
  | jq '[.best[], .worst[]] | map(.composite_score) | add / length'

# Mean composite without mem0
python -m eval.score_view --prompt-id "mem0_disabled" --json \
  | jq '[.best[], .worst[]] | map(.composite_score) | add / length'

# Per-category breakdown
for cat in fhir_query hl7_transform code_qa general; do
    echo "=== $cat ==="
    for group in mem0_enabled mem0_disabled; do
        python -m eval.score_view --category "$cat" --prompt-id "$group" --json \
          | jq -r "[.best[], .worst[]] | \"\(.composite_score)\"" \
          | awk '{s+=$1; n++} END {printf "  %s: avg=%.4f (n=%d)\n", "'$group'", s/n, n}'
    done
done
```

### 5.3 Metrics Suite

#### Primary Metrics (from existing Ragas pipeline)

| Metric | Source | Frequency |
|---|---|---|
| `composite_score` (weighted) | `eval.worker.compute_composite()` | Every scored call |
| `faithfulness` | Ragas LLM-as-judge | Calls with context |
| `answer_relevancy` | Ragas + Gemini embeddings | Every call |
| `context_precision` | Ragas LLM-as-judge | Calls with context |

#### Secondary Metrics (new instrumentation)

| Metric | How Measured | Why |
|---|---|---|
| **Token injection overhead** | Count tokens in injected memory system message | Track cost of mem0 itself |
| **Token savings from memory** | Count repeated-token overlap in consecutive same-user requests | Measure dedup benefit |
| **Memories retrieved per request** | mem0 sidecar response count | Understand retrieval density |
| **Memory hit rate** | % of mem0 searches that return ≥1 result with score > 0.7 | Measure memory fill quality |
| **Injection latency** | Timer around `POST /v1/search/` call | Track overhead (budget: <50ms) |
| **Memory store latency** | Timer around memory write from callback | Track overhead (should be 0 — async, best-effort) |
| **Routing tier shift rate** | % of requests where mem0 injection changed ComplexityRouter tier | Measure routing impact |
| **Cross-session continuity** | % of session starts where mem0 injected memories from a different session | Core value metric |

### 5.4 Instrumentation Points

#### New middleware (Mem0RetrieveMiddleware)

```
Before mem0 call:    record query length, user_id presence
After mem0 call:     record latency, count of memories retrieved, max similarity score
After injection:     record injected token count (tiktoken estimate), new total body size
After compression:   compare Headroom-reported savings (mem0-injected payloads should still compress well)
After routing:       record ComplexityRouter tier — compare with tier the same query would have gotten without memory
```

#### Extended RagasLogger

Add two metadata fields to every scored record:

| New Field | Values | Purpose |
|---|---|---|
| `mem0_enabled` | `true`, `false` | A/B group assignment |
| `mem0_memories_injected` | 0–3 | How many memories were injected (0 = no memory hit) |
| `mem0_injection_tokens` | integer | Token count of injected memory context |

These are stored in the Redis hash alongside existing fields and queryable via `--prompt-id` and the score view.

### 5.5 Success Criteria

| Criterion | Threshold | How Measured |
|---|---|---|
| Composite score improvement (mem0 vs control) | ≥ +0.04 mean composite | Rolling 7-day window, t-test (p < 0.05) |
| Token savings beyond Headroom | ≥ 10% additional reduction | Headroom `tokens_saved` header, grouped by A/B group |
| Memory retrieval latency (p50/p95) | ≤ 50ms / ≤ 200ms | Timer in Mem0RetrieveMiddleware |
| Memory store latency (callback) | ≤ 5ms (async, guaranteed by design) | Timer in RagasLogger |
| Cross-session memory hit rate | ≥ 20% of sessions get ≥ 1 injected memory | `mem0_memories_injected > 0` count / total requests |
| Routing tier shift (beneficial) | ≥ 5% of requests shifted to a *more appropriate* tier (confirmed by Ragas score) | Compare tier before/after mem0 injection, filter to shifts that improved composite |
| Memory store error rate | < 1% of requests | httpx exception count in store hook |

### 5.6 Experiment Phases

#### Phase 0: Baseline Capture (duration: 7 days)

Before deploying mem0, gather 7 days of baseline data using only the existing system:

```bash
# Recording: enable mem0_tags=false on all requests to establish baseline with tagging
python -m eval.score_view --prompt-id "mem0_disabled" --json > baseline_7d.json
```

This establishes:
- Baseline composite score distribution per category
- Baseline token consumption per request (from Headroom headers)
- Baseline routing tier distribution
- Baseline latency distribution

#### Phase 1: Retrieve-Only (duration: 7 days, 50/50 A/B)

Deploy `Mem0RetrieveMiddleware` in read-only mode — store nothing new, only retrieve from a pre-seeded memory set.

**Seeding**: Inject 50 hand-curated memories about:
- System prompt templates (10 memories)
- Category-specific routing patterns (10 memories)
- Common FHIR/HL7 conventions (15 memories)
- Claude Code project context patterns (15 memories)

This phase validates that:
- The retrieval path is stable (no request failures)
- Retrieval latency is within budget
- Injected memories actually score higher than no-memory control (H2)

**Go/No-Go**: Composite score improvement ≥ 0.03 for any single category.

#### Phase 2: Store + Retrieve (duration: 14 days, 50/50 A/B)

Enable the memory store hook in `RagasLogger`. All requests in the `mem0_enabled` group now both read and write memories.

This phase validates:
- Full feedback loop (store → retrieve → score → importance update)
- Memory quality improves over time (composite score trend positive)
- Cross-session continuity emerges (memories from day 1 appear in day 7 sessions)

**Go/No-Go**: Cross-session hit rate ≥ 20% by day 10.

#### Phase 3: Score-Weighted Memory (duration: 7 days)

Deploy the eval-worker extension that updates memory importance from Ragas scores.

This phase validates:
- High-scoring memories persist (not evicted by volume)
- Low-scoring memories decay faster
- The "good memories last longer" effect measurably improves retrieval quality over Phase 2

#### Phase 4: Full Rollout (ongoing)

If all Go/No-Go criteria are met:
- Enable mem0 for 100% of traffic
- Remove A/B tagging overhead
- Publish weekly "Memory quality report" using the score view
- Begin adaptive routing (Section 6)

### 5.7 Statistical Rigour

| Concern | Mitigation |
|---|---|
| Day-of-week bias | Run each phase for full 7-day cycles (includes Monday and Sunday) |
| User population skew | A/B split is deterministic per call_id (not per user) — both groups see the same user mix |
| Model change contamination | Log model name; filter experiments to single-model comparisons; if a model change happens mid-phase, restart |
| Cold start bias | Exclude first 24 hours of Phase 2 from analysis (memory pool is empty) |
| Simpson's paradox | Report scores per-category *and* overall; check direction is consistent |
| Multiple comparison correction | 4 hypothesis tests across categories → Bonferroni threshold: p < 0.0125 |

### 5.8 Reporting Template

After each phase, run:

```bash
# Generate full comparison report
cat << 'REPORT' | bash
echo "=== Mem0 A/B Report ==="
echo "Phase: $PHASE"
echo "Date range: $(date -v-7d '+%Y-%m-%d') to $(date '+%Y-%m-%d')"
echo ""

# Overall
for group in mem0_enabled mem0_disabled; do
    COMPOSITES=$(python -m eval.score_view --prompt-id "$group" --json \
        | jq '[.best[], .worst[]] | map(.composite_score)')
    COUNT=$(echo "$COMPOSITES" | jq 'length')
    AVG=$(echo "$COMPOSITES" | jq 'add / length')
    echo "Group: $group | Calls: $COUNT | Mean composite: $AVG"
done

echo ""
echo "--- Per-Category ---"
for cat in fhir_query hl7_transform code_qa general; do
    echo "Category: $cat"
    for group in mem0_enabled mem0_disabled; do
        python -m eval.score_view --category "$cat" --prompt-id "$group" --json 2>/dev/null \
            | jq -r '[.best[], .worst[]] | if length > 0 then "\(length) calls, \(add/length)" else "0 calls" end' \
            || echo "  $group: N/A"
    done
done
REPORT
```

---

## 6. Adaptive Routing (Future — Phase 5)

Once the memory store has accumulated enough routing examples, the eval-worker can periodically recalculate ComplexityRouter weights:

```python
# In eval-worker (cron job, runs daily at 2am)
def recalculate_routing_weights():
    """Analyze last 7 days of scored calls → tune ComplexityRouter weights."""
    all_scores = get_scored_calls(days=7)

    for category in ["fhir_query", "hl7_transform", "code_qa", "general"]:
        cat_scores = [s for s in all_scores if s["category"] == category]

        # For each tier, find which model actually scored best
        for tier in ["SIMPLE", "MEDIUM", "COMPLEX", "REASONING"]:
            tier_calls = [s for s in cat_scores if s["router_tier"] == tier]

            # If deepseek-pro scores consistently higher on MEDIUM FHIR queries,
            # the router's MEDIUM→deepseek-flash mapping is wrong for this category.
            if tier_calls:
                best_model = max(
                    set(s["model"] for s in tier_calls),
                    key=lambda m: sum(s["composite"] for s in tier_calls if s["model"] == m) / 
                                  len([s for s in tier_calls if s["model"] == m])
                )
                # ... emit suggested config change ...
```

This is deferred until Phase 5 to let the memory store accumulate sufficient data per category × tier combination.

---

## 7. Project Structure (Additions)

```
llm-mid/
├── docker-compose.yml              # +mem0 service
├── proxy/
│   ├── entrypoint.py               # +Mem0RetrieveMiddleware registration
│   ├── callback.py                 # +memory store hook (extended RagasLogger)
│   └── memory/                     # NEW — mem0 integration
│       ├── __init__.py
│       ├── retrieve_middleware.py   # Mem0RetrieveMiddleware ASGI class
│       ├── store_hook.py           # Memory store logic (called by callback.py)
│       └── config.py               # mem0 client config, embedding model selection
├── eval/
│   └── worker.py                   # +score-weighted memory update
├── mem0_config.yaml                # NEW — mem0 sidecar config
├── experiments/                    # NEW — experiment tooling
│   ├── ab_report.sh                # A/B comparison report (as shown in §5.8)
│   ├── seed_memories.py            # Seed initial memory pool (Phase 1)
│   └── routing_analysis.py         # Analyze routing tier shifts (Phase 4)
└── tests/
    ├── test_mem0_retrieve.py       # NEW
    └── test_mem0_store.py          # NEW
```

---

## 8. Risks & Mitigations

| Risk | Severity | Likelihood | Mitigation |
|---|---|---|---|
| mem0 retrieval latency blows budget | Medium | Low | Hard timeout at 200ms; circuit-breaker after 3 consecutive failures; fall through silently |
| Stale or contradictory memories injected | Medium | Medium | Score threshold (min 0.7); maximum 3 memories; injection as system message (not overwriting user intent) |
| Memory store fills with low-value noise | Low | High | Ragas score-weighted persistence; automatic decay; max memory count per user (configurable) |
| PII/PHI stored in memory | High | Low | Store hook runs AFTER ApiKeyMasking; subject line truncation at 500 chars; periodic audit query |
| mem0 depends on OpenAI embeddings → vendor lock-in | Medium | Low | Configurable embedding provider (OpenAI/Gemini/local); Gemini already available in stack |
| Cross-session memory feels "creepy" to users | Medium | Medium | Opt-out header (`X-Mem0-Disabled: true`); clear CLI command to purge own memories (`mem0 forget --all`) |
| Headroom and mem0 compete (mem0 injects context, Headroom compresses it) | Low | Medium | Measure both directions; if mem0 injection is 100 tokens but Headroom saves 40 tokens from it, net is still +60 tokens — but the quality improvement may justify it |

---

## 9. Open Questions

| # | Question | Owner | Resolution Needed By |
|---|---|---|---|
| 1 | Should mem0 use OpenAI or Gemini for embeddings? Gemini is already available in the eval-worker (gemini-embedding-001). OpenAI ada-002 has higher recall on technical code content. | Tech Lead | Phase 0 |
| 2 | What is the storage backend? mem0 supports Qdrant, Pinecone, Chroma, and PostgreSQL. Qdrant runs well as a sidecar but adds a new DB. Redis could work for MVP but lacks vector search. | Infra | Phase 0 |
| 3 | Should memory be per-user (user_id) or per-project (project hash)? For Claude Code, user ≈ machine. For the proxy, requests may not have a user_id header at all. | Product | Phase 1 |
| 4 | How do memories survive container restarts? mem0 data volume must be mounted externally (bind mount or named volume). Qdrant also needs its own volume. | Infra | Phase 0 |
| 5 | Should the eval-worker write score updates synchronously or enqueue them? Synchronous is simpler but adds latency to the eval loop. Enqueuing adds complexity. | Engineering | Phase 3 |
| 6 | What memory size limits per user? 100 memories? 1000? Affects retrieval latency and storage cost. Start with 200 per user, tune after Phase 2. | Engineering | Phase 1 |
| 7 | Does mem0 support filtering by metadata fields (e.g. `request_category`)? This is needed for category-specific memory retrieval. | Engineering | Phase 1 |

---

## 10. Success Metrics Summary

| Metric | Current (baseline) | Target (with mem0) | Measurement |
|---|---|---|---|
| Mean composite score | X.XXXX (measured in Phase 0) | X.XXXX + 0.04 | A/B experiment §5.2 |
| Token savings beyond Headroom | 60-95% (Headroom alone) | 60-97% (additional 10-40%) | Headroom headers, grouped by A/B group |
| Cross-session memory hit rate | 0% (no cross-session memory) | ≥ 20% of sessions | mem0_memories_injected > 0 |
| Requests with memory retrieval | 0% (no memory layer) | ≥ 60% of requests | mem0 sidecar query count |
| Memory retrieval p95 latency | N/A | ≤ 200ms | Middleware timer |
| Routing tier accuracy (high-score calls → correct tier) | Baseline from Phase 0 | +5% improvement | Tier vs composite correlation |

---

## Appendix A: Detailed Experiment Configuration

### Phase 1 Configuration

```yaml
# mem0_config.yaml (Phase 1 — retrieve only, pre-seeded)
version: "v1.0"
embedding:
  provider: "openai"
  model: "text-embedding-3-small"    # 1536d, $0.02/M tokens
storage:
  type: "qdrant"
  config:
    collection_name: "gatemid_memories"
    path: "/app/data/qdrant"
vector_dim: 1536
memory:
  min_score_threshold: 0.7
  max_memories_per_request: 3
  max_tokens_per_memory: 200
  enable_updates: false              # Phase 1: read-only
```

### Phase 2 Configuration

```yaml
# mem0_config.yaml (Phase 2 — full read/write)
enable_updates: true
memory:
  ttl_days: 90                       # user preferences
  project_context_ttl_days: 30
  interaction_ttl_days: 7
  max_memories_per_user: 200
  decay_enabled: true
  decay_rate: 0.1                    # per day since last access
```

### Phase 3 Configuration — Score-Weighted Feedback

```yaml
# mem0_config.yaml (Phase 3 — quality feedback)
memory:
  score_feedback_enabled: true
  score_reinforcement_threshold: 0.85    # boost importance
  score_decay_threshold: 0.3             # accelerate decay
  importance_boost_factor: 1.5           # multiplier on importance score
  importance_decay_factor: 0.5           # multiplier on importance score
```

---

## Appendix B: Comparison Dashboard (Existing Score View Integration)

The existing `score_view.py` is extended with a `--compare` flag:

```bash
# Compare mem0 vs control across all categories
python -m eval.score_view --compare mem0_enabled mem0_disabled

# Output:
# ┌────────────────────┬─────────────────────┬─────────────────────┬──────────┐
# │ Category           │ mem0_enabled        │ mem0_disabled       │ Δ        │
# ├────────────────────┼─────────────────────┼─────────────────────┼──────────┤
# │ fhir_query         │ 0.8423 (n=342)      │ 0.8156 (n=318)      │ +0.0267  │
# │ hl7_transform      │ 0.7912 (n=156)      │ 0.7789 (n=144)      │ +0.0123  │
# │ code_qa            │ 0.8934 (n=521)      │ 0.8712 (n=507)      │ +0.0222  │
# │ general            │ 0.7654 (n=278)      │ 0.7543 (n=291)      │ +0.0111  │
# │ OVERALL            │ 0.8284 (n=1297)     │ 0.8102 (n=1260)     │ +0.0182* │
# └────────────────────┴─────────────────────┴─────────────────────┴──────────┘
# * p < 0.05 (Welch's t-test, two-tailed)
```

---

## Appendix C: Memory Seed Templates (Phase 1)

These 50 seed memories establish a baseline knowledge base before the system learns from live traffic.

```python
# experiments/seed_memories.py
SEED_MEMORIES = [
    # System prompt templates (10)
    {"text": "System prompt v2: 'You are a helpful healthcare FHIR integration assistant...'",
     "user_id": "__system__", "metadata": {"type": "system_prompt", "version": "v2"}},

    # Category routing patterns (10)
    {"text": "FHIR query: user typically needs resource structure, cardinality constraints, search parameters",
     "user_id": "__system__", "metadata": {"type": "routing_hint", "category": "fhir_query"}},

    # FHIR/HL7 conventions (15)
    {"text": "FHIR R4 Observation resources use code '8867-4' for heart rate, '8480-6' for systolic BP",
     "user_id": "__system__", "metadata": {"type": "domain_knowledge", "domain": "fhir"}},

    # Claude Code patterns (15)
    {"text": "Claude Code sessions typically include project context from CLAUDE.md and file tree dumps",
     "user_id": "__system__", "metadata": {"type": "session_pattern", "source": "claude_code"}},
]
```

---

## Appendix D: Implementation Checklist

### Phase 0: Baseline (Week 1)
- [ ] Enable `prompt_id` tagging on all requests (even without mem0) — `mem0_disabled`
- [ ] Record 7-day baseline: composite scores, token savings, routing distribution
- [ ] Publish baseline report via `eval.score_view`
- [ ] Choose embedding provider (OpenAI vs Gemini vs local)
- [ ] Choose storage backend (Qdrant vs Chroma vs PGVector)
- [ ] Set up mem0 sidecar in docker-compose (pre-seeded, retrieve only)
- [ ] Write integration test: mem0 sidecar health check from proxy container

### Phase 1: Retrieve-Only (Week 2)
- [ ] Implement `Mem0RetrieveMiddleware` in `proxy/memory/retrieve_middleware.py`
- [ ] Implement memory store hook in `proxy/callback.py` (disabled by flag)
- [ ] Register middleware in `proxy/entrypoint.py`
- [ ] Seed 50 hand-curated memories
- [ ] Run 50/50 A/B for 7 days
- [ ] Analyse Phase 1 results → Go/No-Go

### Phase 2: Full Loop (Weeks 3-4)
- [ ] Enable memory store hook in callback
- [ ] Add `mem0_memories_injected` and `mem0_injection_tokens` to Redis record
- [ ] Implement memory decay (TTL-based)
- [ ] Run 50/50 A/B for 14 days
- [ ] Analyse Phase 2 results → Go/No-Go
- [ ] Build `experiments/ab_report.sh`

### Phase 3: Quality Feedback (Week 5)
- [ ] Implement score-weighted memory update in `eval/worker.py`
- [ ] Implement memory importance score propagation
- [ ] Run 50/50 A/B for 7 days
- [ ] Analyse Phase 3 results → Go/No-Go

### Phase 4: Rollout (Week 6)
- [ ] Remove A/B tagging overhead
- [ ] Enable mem0 for 100% of traffic
- [ ] Publish "Memory Quality Report" weekly
- [ ] Begin adaptive routing analysis

---

*End of document — AI Memory Layer (mem0) PRD v1.0.0*
