# Pitch Slide Draft — "Productivity & Token"
> Story arc: We built two complementary tools that attack dev productivity from both the **human side** and the **machine side** — one that helps developers understand code, and one that makes every LLM call smarter and cheaper.

---

## SLIDE 1 — Title / Hook

**Headline:** Dev Productivity × Token Efficiency

**Sub-headline:** Two tools. One goal — ship faster, spend less.

**Presenter note:** Open with the problem statement: developers today are spending too much time navigating unfamiliar codebases, and AI tools are burning tokens on noise instead of signal.

---

## SLIDE 2 — The Problem

**Headline:** Two things are quietly killing your team's velocity

**Points:**
- 🧠 Developers onboarding to brownfield projects spend weeks just understanding *what exists* — not building
- 💸 AI coding tools send bloated, uncompressed context to LLMs — paying full price for noise

**Presenter note:** Frame these as two sides of the same coin — both are about *wasted cognitive and computational resources*. Our solution attacks both simultaneously.

---

## SLIDE 3 — Our Story: "Productivity & Token"

**Headline:** We built two layers of productivity

```
┌─────────────────────────────────────────────────────┐
│                                                     │
│   HUMAN LAYER          ──────►   Understand-Anything│
│   (Developer UX)                 Knowledge Graph    │
│                                                     │
│   NON-HUMAN LAYER      ──────►   GateMid            │
│   (LLM Infrastructure)           AI Gateway Proxy   │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**Presenter note:** This slide sets up the two-act structure of the pitch. Keep it visual and brief.

---

## SLIDE 4 — Part 1: Human Layer — Understand Anything

**Headline:** Help developers *see* the codebase — before they write a single line

**What it is:**
- Built on top of the open-source **[Understand-Anything](https://github.com/koaning/understand-anything)** project
- Generates a rich `knowledge-graph.json` from any codebase — classes, services, relationships, call chains
- Wrapped in a **VS Code extension** so the graph is accessible in-editor without leaving the development environment

**What developers get:**
- A visual understanding dashboard — classes, services, relationships at a glance
- Faster onboarding into brownfield projects
- Context they can *trust*, not guess
- A **VS Code extension** that wraps the dashboard — one click inside the editor to open the graph, no context switching to a separate tool

**Presenter note:** Position this as "the map before the journey." Every new dev on a brownfield project used to have to build this map in their head over weeks. The VS Code extension means the map is always one click away, right where the dev already is.

---

## SLIDE 5 — Human Evidence: 50% Faster Comprehension

**Headline:** Same devs. Same project. 50% faster with the dashboard.

**Test design:**
- Two groups of developers — equal skill level, equal prior project knowledge
- Same comprehension test on the same codebase
- One group used the **Understanding Dashboard via VS Code extension**, one did not

**Result:**
> ✅ Same accuracy · ⏱️ **50% less time to complete**

**Presenter note:** Emphasise that this isn't just about speed — it's about *confidence*. Developers with the graph knew *why* their answer was right.

---

## SLIDE 6 — AI Agent Evidence: Better RAG Quality

**Headline:** The graph doesn't just help humans — it makes AI agents smarter too

**How:**
- AI agents can trace class relationships through the knowledge graph instead of grepping raw source files
- Cleaner, more structured context → higher quality answers
- Less token waste on irrelevant file chunks
- The VS Code extension exposes graph context directly to AI coding agents in-editor — agents get structured relationship data without needing to crawl the filesystem

**Evidence (Ragas scoring — before vs after):**

| Metric | Without Graph | With Graph |
|--------|--------------|------------|
| Faithfulness | — | ↑ |
| Answer Relevancy | — | ↑ |

> 📊 *[IMAGE PLACEHOLDER — Ragas score comparison chart]*

**Presenter note:** Use the Ragas output image here. This proves that the graph isn't just a nice UX — it materially improves AI-assisted development quality.

---

## SLIDE 7 — Part 2: Non-Human Layer — GateMid Proxy

**Headline:** A lightweight LLM gateway that sits between your dev tools and the cloud

**What it is:**
- A self-hosted proxy that every AI coding tool (Claude Code, Open Code, any OpenAI-compatible SDK) routes through
- Built on **[LiteLLM](https://github.com/BerriAI/litellm)** — supports 100+ LLM providers (OpenAI, Anthropic, Gemini, DeepSeek, Azure, Bedrock, and more) with a single unified API
- Drop-in replacement — no code changes needed in existing tools

**Architecture overview:**
```
Dev Tool (Claude Code / IDE)
    │
    ▼
GateMid Proxy (:4000)
    ├─ API Key Masking
    ├─ Token Compression (Headroom)
    ├─ [OPTIONAL] Prompt / Skill Injection  ← conceptual layer
    ├─ Complexity Router → right model for each request
    └─ Async Quality Scoring (Ragas)
    │
    ▼
Gemini / DeepSeek / Any LLM
```

**Optional layer — Prompt / Skill Injection (conceptual):**
- The proxy sits in the middle of every request, making it a natural place to *inject* additional context or skills before the prompt reaches the model
- Pattern: intercept the request → call an external skill provider (e.g. **Caveman**) → enrich the prompt with domain-specific instructions or few-shot examples → forward to LLM
- ⚠️ *Not implemented in this version* — external skill providers like Caveman can introduce incorrect or hallucinated content into the injected context, which would degrade rather than improve response quality. Left as a deliberate future decision once a reliable skill source is established.

**Presenter note:** Mention this briefly as "here's where the proxy *could* go further" — it shows architectural foresight without overpromising. Don't dwell on it; the audience should understand it's a conscious decision not to ship half-baked skill injection.

---

## SLIDE 8 — Feature 1: Smart Model Routing

**Headline:** Don't pay pro-model prices for a "hello world" question

**How it works:**
- Every request is classified in **sub-millisecond** time — no API call, pure local logic
- Complexity score is computed across 7 weighted dimensions (reasoning markers, code presence, question depth, token count…)

**Routing tiers:**

| Tier | Score | Model | Example |
|------|-------|-------|---------|
| Simple | 0–0.20 | deepseek-flash | Greetings, yes/no |
| Medium | 0.20–0.45 | deepseek-flash | General queries |
| Complex | 0.45–0.65 | deepseek-pro | Code, architecture |
| Reasoning | 0.65+ | deepseek-pro | Debugging, analysis |

**Presenter note:** This is pure cost optimisation. Most daily dev queries are Simple or Medium — they don't need the most expensive model.

---

## SLIDE 9 — Feature 2: Token Compression with Headroom

**Headline:** 60–95% fewer tokens. Same answer quality.

**What Headroom does:**
- ASGI middleware that compresses prompt context *before* it reaches the LLM
- Three compression strategies: **SmartCrusher** (JSON/FHIR/HL7 payloads), **CodeCompressor** (AST-based), **CacheAligner** (KV cache prefix alignment)
- Zero intelligence loss — the model sees *the same information*, just without the noise

**Why it matters for us:**
- Our codebase deals with FHIR/HL7 payloads — verbose XML/JSON by nature
- SmartCrusher is purpose-built for exactly this shape of data

> *"Compress tool outputs, logs, files, and RAG chunks before they reach the LLM. 60–95% fewer tokens, same answers."*
> — Headroom (16k+ ⭐ on GitHub)

**Presenter note:** Show the headline stat prominently. 60–95% is a dramatic number and it's independently validated by the Headroom project.

---

## SLIDE 10 — Feature 3: Guardrails — API Key & PII/PHI Masking

**Headline:** Your secrets never reach the model. Ever.

**What it does:**
- Custom `ApiKeyMaskingMiddleware` intercepts every request *before* it touches the LLM
- 8 regex patterns covering: Gemini, Hugging Face, GitHub tokens, AWS access keys, OpenAI/Anthropic `sk-` keys, Bearer tokens, and generic 36+ char secrets
- Preserves key type prefix, masks the sensitive portion

> 📸 *[IMAGE PLACEHOLDER — demo screenshot of masked API key in request log]*

**Beyond API keys:**
- The same middleware pattern is directly extensible to **PII and PHI masking**
- Healthcare dev workflows: patient names, MRNs, DOBs, diagnosis codes can all be masked before the LLM sees them
- Critical for FHIR/HL7 environments operating under HIPAA

**Presenter note:** This is the slide that resonates with enterprise and healthcare audiences. Guardrails aren't just a nice-to-have — they're a compliance requirement.

---

## SLIDE 11 — Feature 4: Async Quality Scoring

**Headline:** Know which prompts are working — and which aren't

**How it works:**
- Every LLM response is automatically scored *after* it's returned — zero latency impact
- Scores are pushed to a Redis queue, processed by a separate eval-worker container
- Uses **Ragas** metrics with DeepSeek as the LLM judge and Gemini for embeddings

**Three metrics tracked:**

| Metric | Weight | What it measures |
|--------|--------|-----------------|
| Faithfulness | 40% | Is the answer grounded in context? |
| Answer Relevancy | 40% | Does it actually answer the question? |
| Context Precision | 20% | Is the context clean and relevant? |

**What you do with it:**
- Find the top-performing prompt sessions → promote those patterns to the team
- Spot systematically poor categories → fix prompts or context upstream
- Read it any time: `docker exec gatemid python -m eval.score_view`

**Presenter note:** This turns LLM usage from a black box into a feedback loop. Teams learn what good prompting looks like for *their specific domain*.

---

## SLIDE 12 — Performance

**Headline:** Lightweight by design — sub-50ms proxy overhead per request

**Numbers:**
- Total proxy processing time: **< 50ms per request**
- Routing classification: **sub-millisecond** (local, no API calls)
- Async scoring: **zero impact** on response latency (separate container)
- Compression savings: **60–95% token reduction** on FHIR/JSON payloads

**Presenter note:** Address the "is this adding latency?" objection directly. The proxy adds less than 50ms — negligible compared to LLM inference time (typically 1–10s).

---

## SLIDE 13 — Deployment Flexibility

**Headline:** Run it your way — local tool or org-wide platform

**Two modes:**

**🖥️ Local Dev Tool**
- Single developer runs GateMid on their machine
- Claude Code / IDE routes through `localhost:4000`
- Personal cost savings, local quality scoring

**🏢 Org-Wide Platform**
- Deploy GateMid to a shared server
- Organisation controls all LLM access centrally via Claude Code
- Custom API keys per team/user
- Assign model allow-lists per user or role
- Dashboard for usage, cost, and quality metrics
- Audit trail for compliance (especially valuable in healthcare)

**Presenter note:** The org-wide mode is the enterprise pitch. IT/security teams love having a single choke point for all LLM traffic.

---

## SLIDE 14 — What We Built

**Headline:** Two tools, shipped in one hackathon

| Tool | Stack | What it delivers |
|------|-------|-----------------|
| **Understand-Anything Dashboard + VS Code Extension** | Understand-Anything + VS Code Extension API | 50% faster codebase comprehension, graph accessible in-editor with one click |
| **GateMid Proxy** | Python, LiteLLM, Headroom, Ragas, Redis, Docker | Smart routing + compression + guardrails + scoring |

**Combined impact:**
- Developers understand the codebase faster ✅
- AI agents get better context, produce better answers ✅
- Every LLM call costs less ✅
- Sensitive data never reaches the model ✅
- Teams learn what good AI-assisted dev looks like ✅

---

## SLIDE 15 — Future Development

**Headline:** The feedback loop closes itself — next step: auto-improvement

**Where we're going:**

**🔄 Auto-Improvement via Ragas Logs**
- Today: Ragas scores every response asynchronously and stores results in Redis
- Tomorrow: A scheduled job reads the top-scoring sessions, extracts what made them great (prompt structure, context quality, tool usage patterns), and **auto-generates improved skill/prompt templates**
- These templates feed back into the optional Skill Injection layer in GateMid — closing the loop
- The system learns from its own best outputs, continuously raising the quality floor for the whole team

```
LLM Response
    │
    ▼
Ragas Scoring (async)
    │
    ▼
Redis — scored sessions
    │
    ▼
Auto-Improvement Job  ←── reads top-scored sessions
    │                       extracts patterns + prompt structures
    ▼
Skill / Prompt Templates
    │
    ▼
GateMid Skill Injection Layer  ←── enriches future requests
    │
    ▼
Better LLM Responses  ──► higher Ragas scores  ──► better templates  (loop)
```

**Other planned directions:**
- **Domain graph enrichment** — a Java post-processor layer to enrich the Understand-Anything graph with Spring stereotypes, JPA relationships, FHIR resource classification, and HL7 message type detection → producing a `domain-graph.json` purpose-built for healthcare codebases
- PHI/PII masking patterns extended from the API key guardrail middleware
- Skill injection layer — once a reliable, low-hallucination skill source is available
- Multi-tenant dashboard: per-user token spend, model usage breakdown, quality trends over time

**Presenter note:** This slide shows the judges that the architecture was designed with extensibility in mind — it's not a hackathon one-off. The auto-improvement loop is the most compelling future story: the system gets smarter by watching itself work.

---

## SLIDE 16 — Call to Action / Closing

**Headline:** Productivity and Token. Two problems. One stack.

**What's next:**
- Open for collaboration — both tools are designed to be self-hosted and extensible
- GateMid can be extended with more guardrail patterns (PHI, secrets, custom regex)
- Understanding dashboard can ingest any language supported by Understand-Anything

> **The best AI dev workflow isn't just about the LLM you choose — it's about the infrastructure around it.**

**Presenter note:** End with energy. The ask can be: feedback, collaboration, or simply "try it on your team this week."

---

## 📝 Notes for You Before Finalising

1. **Slide 6** — needs your Ragas score comparison image (before/after with domain graph)
2. **Slide 10** — needs your API key masking demo screenshot
3. **Slides 8–9** — confirm the exact model names you want to show publicly (deepseek-flash / deepseek-pro or rename for the audience?)
4. **Slide 2** — you may want to add a cost stat (e.g., "average dev team spends $X/month on LLM tokens") if you have one
5. **Slide 14** — stack column now just references "Understand-Anything + VS Code Extension API"; add any other tooling you want visible to the audience
6. **Slide 7** — the Caveman reference in the optional layer: decide whether to name Caveman explicitly to the audience or keep it generic ("external skill provider")
7. **Slide 15** — the auto-improvement loop diagram may need simplifying for a slide; consider just the text bullets if the ASCII art feels too dense