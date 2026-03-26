# Brain Project — Claude Code Guide
## Last updated: 2026-03-26 | Audit: 231 facts from 27 files

---

## What this is

A living, self-organizing knowledge graph that learns from documents.
Nodes: Feature, Surface, Outcome, Decision, Person, Company, Concept.
Edges: causal (red) and structural (white) with Hebbian weights.
Stack: FastAPI + Python 3.11 backend · Vanilla JS + D3 v7.8.5 frontend.
No build step. No TypeScript. No React.

---

## Run the server

```bash
source venv/bin/activate && python backend/main.py
# Open: http://localhost:8000
```

---

## CRITICAL: Read this before touching anything

### 1. Cache version — bump on EVERY frontend change
All static files use `?v=N` in index.html. Current: **v=57**.
After any change to ui.js, graph.js, or styles.css:
- Find ALL occurrences of `?v=57` in index.html
- Increment ALL of them to `?v=58` (or next number)
- Restart server + hard refresh browser (Cmd+Shift+R)
Miss this and changes will not appear. This is the #1 source of confusion.

### 2. call_claude() returns a TUPLE, not a string
```python
# CORRECT
text, usage = call_claude(prompt, model=MODEL_FAST, touchpoint="my_feature")

# WRONG — will crash
text = call_claude(prompt)
```
call_claude_json() also returns a tuple: `(dict, usage)`

### 3. Never save an empty brain
save_brain() has a guard: if nodes=[] and existing brain has nodes, it blocks.
Never call save_brain() with an empty dict or unloaded brain.

### 4. touch_node() after every node mutation
```python
node["desc"] = "new description"
touch_node(node)  # sets updated_at, queues for embedding flush
```
Skipping this means the node won't get re-embedded and updated_at stays stale.

### 5. Node IDs are permanent
Never change a node's `id` after creation. All links reference IDs.
Changing an ID silently orphans every edge connected to that node.

### 6. save_brain() strips D3 coords automatically
x, y, vx, vy, fx, fy, index are stripped from every node on save.
Never worry about storing them — but also never rely on them persisting.

### 7. save_brain() is expensive — don't call in tight loops
It computes the full BHS health score on every save (5 dimension passes
over all nodes/links). Call once at the end of an operation, not per-node.

---

## Multi-brain file structure

```
data/
  api_cost_log.jsonl          ← GLOBAL, shared across all brains
  brains/
    registry.json             ← brain registry + metadata
    career/                   ← default brain (your career)
      brain.json              ← nodes, links, questions, history
      brain.backup.json
      embeddings.npy          ← 384-dim float32 matrix
      embeddings_index.json   ← {node_ids: [...]}
      evolution_log.jsonl
      documents/              ← per-document records
      snapshots/              ← timestamped rollback points
    {other_brain_id}/         ← same structure
```

**Path resolution** — always use these functions from storage.py:
```python
get_brain_path()          # data/brains/{active_id}/brain.json
get_embeddings_path()     # data/brains/{active_id}/embeddings.npy
get_evolution_log_path()  # data/brains/{active_id}/evolution_log.jsonl
get_documents_dir()       # data/brains/{active_id}/documents/
get_snapshots_dir()       # data/brains/{active_id}/snapshots/
# All accept optional brain_id param; default = get_active_brain_id()
```

**Never hardcode** `data/brain.json` — this path no longer exists.

---

## Key files

```
backend/
  main.py             55 endpoints, ~1500 lines
  storage.py          Brain persistence, multi-brain paths, BHS, Hebbian
  brain_engine.py     Claude extraction, Q&A, embedding dedup
  consolidation.py    8-agent cleanup system
  evolution_engine.py Autonomous evolution, link prediction, Hebbian decay
  embeddings.py       sentence-transformers, cosine similarity, semantic search
  brain_query.py      NL query → BFS traversal → Claude synthesis
  chat_commander.py   Intent classification → query/command/plan dispatch
  node_summarizer.py  Structured node summaries via Claude
  cost_tracker.py     API cost logging and session stats
  document_parser.py  PDF/DOCX/MD/CSV/TXT parsing
  document_store.py   Document vault per-brain
  web_enricher.py     Web search enrichment pipeline
  config.py           Env config: ANTHROPIC_API_KEY, MODEL_*, MAX_DOC_CHARS

frontend/
  index.html          SPA shell, 4 tabs, CDN scripts
  ui.js               All UI logic, ~1851 lines
  graph.js            D3 force graph + Louvain community detection
  styles.css          Dark design system, ~518 lines
```

---

## Stack & dependencies

**Python 3.11.15** — no other version tested.

```bash
# Install all dependencies
pip install fastapi uvicorn anthropic pypdf python-docx \
            python-multipart python-dotenv pydantic numpy \
            apscheduler sentence-transformers \
            --break-system-packages
```

**NOTE: requirements.txt is stale.** It's missing:
- numpy
- apscheduler
- sentence-transformers

Always install manually with the command above.

**CDN (loaded in index.html, no npm):**
- D3.js 7.8.5 — `cdnjs.cloudflare.com`
- Graphology 0.26.0 — `cdnjs.cloudflare.com`
- Graphology Library 0.8.0 — `cdn.jsdelivr.net`

---

## Config (backend/config.py)

```python
ANTHROPIC_API_KEY    # from .env — required
MODEL_EXTRACTION     # default: "claude-sonnet-4-6"
MODEL_FAST           # default: "claude-haiku-4-5-20251001"
MAX_DOC_CHARS        # default: 18000
PORT                 # default: 8000
```

Model pricing (per 1M tokens):
- Sonnet: $3.00 in / $15.00 out
- Haiku: $1.00 in / $5.00 out

---

## 4 UI tabs

| Tab | What it does |
|-----|-------------|
| FEED | Upload documents, answer questions from extractions |
| THINK | Brain Cleanup (8 agents) + Evolution engine + Node summaries |
| ACT | Triage inbox — all pending questions, 3-tier cards |
| CHAT | Natural language query / command / plan dispatcher |

---

## 8-agent cleanup system (consolidation.py)

Agents run in this exact order, sharing a `memo` dict:

| # | Agent | What it does |
|---|-------|-------------|
| 1 | Cartographer | Map clusters, find singletons, identify hubs |
| 2 | Skeptic | Challenge nodes, find merge candidates |
| 3 | Synthesizer | Execute merges, semantic dedup via embeddings |
| 4 | Detective | Find contradictions, backwards causal chains |
| 5 | Archivist | Score confidence, enrich thin nodes |
| 6 | Questioner | Generate gap questions (max 8 total, max 3/category) |
| 7 | Compressor | Find absorbable nodes, propose synthetic nodes |
| 8 | Conceptualizer | Detect stable clusters → propose Concept nodes |

**Agent pattern:**
```python
memo = make_memo(brain)
emit(memo, "agent_name", "found X", "info")
# event types: "info" | "find" | "action" | "warn"
```

**Never add a 9th agent without understanding the memo structure.**

---

## Node schema (all fields)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| id | str | required | Snake_case. **PERMANENT. Never change.** |
| label | str | required | Display name |
| type | str | required | Feature\|Surface\|Outcome\|Decision\|Person\|Company\|Concept |
| where | str | "" | Surface location (Feature nodes) |
| desc | str | "" | One-sentence description |
| company | str | "" | Company context |
| metrics | dict | {} | Measurable outcomes key-value |
| confidence | str | "medium" | Legacy string — ignored by most code |
| confidence_score | float | 0.3 | **This is the real one.** 0.0–1.0 |
| memory_type | str | "unset" | "episodic"\|"semantic"\|"unset" |
| source_doc | str | "" | Primary source filename |
| source_docs | list | [] | All source filenames |
| depth_score | float | computed | Set by save_brain() automatically |
| created_at | str | auto | ISO timestamp |
| updated_at | str | auto | Set by touch_node() |
| synthesized | bool | false | Created by merge/synthesis |
| synthesized_from | list | [] | Source node IDs if merged |
| career | list | [] | Person only: [{company,role,from,to,is_current}] |
| concept_metadata | dict | null | Concept only: {cluster_id,member_ids,...} |
| _summary | dict | null | Claude-generated structured summary |
| contradiction_resolved | bool | false | Set when contradiction answered |

**Two confidence fields exist** — `confidence` (legacy string) and
`confidence_score` (float). Always use `confidence_score`.

---

## Edge/link schema (all fields)

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| source | str | required | Source node ID |
| target | str | required | Target node ID |
| rel | str | required | Relationship label |
| causal | bool | false | Red edge — only when explicitly confirmed |
| weight | float | 1.0 | Hebbian: 0.05–3.0 |
| access_count | int | 0 | Times traversed in queries |
| memory_type | str | "episodic" | "episodic"\|"semantic" |
| created_at | str | auto | ISO timestamp |
| last_accessed | str | auto | Updated on query traversal |
| confirmed | bool | false | User-confirmed (very slow decay) |
| valid_from | str | "" | When relationship started |
| valid_to | str | "" | "present" = current |

**Dedup rule:** (source, target, rel) triple must be unique.

---

## brain.json top-level keys

| Key | What it holds |
|-----|--------------|
| nodes | All graph nodes |
| links | All graph edges |
| sessions | Document processing log |
| pending_questions | Awaiting user answer in Act |
| qa_history | Answered questions with updates |
| meta | {created_at, last_updated, version} |
| health | {score, grade, delta, dimensions, breakdown} |
| concept_nodes | Concept node registry |
| cluster_history | Louvain snapshots (max 5, for stability detection) |
| rejected_edges | "src_id\|\|tgt_id" sorted pairs — never re-proposed |
| query_history | Natural language query log |

---

## Hebbian memory system

```
Strengthening (Oja's rule):
  delta = amount * (1 - weight / 3.0)
  weight = min(weight + delta, 3.0)
  Called by strengthen_edge() on every query traversal

Graduation:
  episodic → semantic at 5+ accesses
  Also graduates on user confirmation (confirmed=True)

Decay (runs every 6 hours via APScheduler):
  confirmed edges:  rate = 0.001  (barely decays)
  10+ accesses:     rate = 0.002  (LTP — 10x slower)
  3-9 accesses:     rate = 0.01
  untouched:        rate = 0.02
  Consolidation window (0-3 days): rate × 0.1
  Power-law after 30 days

Pruning:
  weight < 0.10 AND age > 14 days AND not causal AND not confirmed
  → deleted from brain.links
```

---

## Embeddings (sentence-transformers)

**Model:** all-MiniLM-L6-v2 · 384 dimensions · CPU-only · ~80MB
**Stored:** per-brain as embeddings.npy + embeddings_index.json

```python
# Rebuild all embeddings for active brain
POST /api/brain/embeddings/rebuild

# Semantic search
POST /api/brain/search/semantic
body: {"query": "...", "top_k": 10, "threshold": 0.3}

# Find duplicate at extraction time (threshold 0.88)
find_existing_node(brain, new_node, threshold=0.88)
# → returns existing node if found, else None
```

**node_to_text() format:** `"label (type) | where | desc[:200] | metrics"`

Embeddings are NOT auto-rebuilt on save. If you add many nodes manually,
run `POST /api/brain/embeddings/rebuild` afterward.

---

## Brain Health Score (BHS)

5 dimensions, each 0.0–1.0, combined as geometric mean × 100:

| Dim | Formula |
|-----|---------|
| C Connectivity | 50% nodes≥2edges + 30% avg_degree/4 + 20% causal_ratio |
| K Completeness | 25% desc>40chars + 30% surfaced + 25% owned + 20% sourced |
| V Confidence | 45% QA-confirmed + 35% avg_score + 20% QA density |
| H Coherence | 1.0 − contradiction_penalty − orphan_penalty − dup_penalty |
| R Coverage | 40% node_coverage + 35% edge_density + 25% type_coverage |

**Grades:** A+(≥90) A(≥80) B(≥70) C(≥60) D(≥50) F(<50)

**What moves each dimension:**
- V: Answer questions in Act (every Yes → +0.08–0.20 confidence)
- H: Run Brain Cleanup (merges semantic dupes, resolves contradictions)
- K: Add owner edges, surface connections, source outcomes causally
- C: Evolution engine adds edges; answer Yes to edge proposals

---

## Confidence feedback loop (Loop 2)

```python
# Called after every /api/answer
update_confidence_from_answer(brain, question, answer)

# Yes → boost (varies by category)
# contradiction: +0.20
# no_source:     +0.15
# no_owner:      +0.12
# evolution:     +0.08
# default:       +0.10

# No → penalty: -0.08

# 3+ Yes answers on same node → memory_type = "semantic"

# Freetext on contradiction → desc updated + +0.25 boost + semantic
```

---

## Question structure (Act tab)

Every pending question requires these fields:

```json
{
  "id": "category_timestamp",
  "type": "yesno | choice | freetext",
  "question": "text — max 180 chars",
  "why": "why this matters",
  "context": "node_id",
  "priority": "high | medium | low",
  "category": "gap|contradiction|no_source|no_owner|isolated|web_conflict|web_enrich|web_new|compress|evolution|concept_proposal|plan",
  "category_label": "HUMAN-READABLE",
  "from_doc": "source filename",
  "proposed_action": {"op": "...", "details": {...}}
}
```

Always add via `add_question_safe(brain, q)` — never direct append.
`is_duplicate_question()` runs 6 checks before allowing the add.

---

## Proposed action handling (/api/answer)

Three branches — never collapse them:
1. **Yes** (`answer in ("yes","y")`) → execute `proposed_action`
2. **No** (`answer in ("no","n")`) → record in `rejected_edges`, skip
3. **Freetext** → interpret with Claude → apply graph operations

Supported ops in proposed_action:
- `add_edge` — creates link between two nodes
- `merge_nodes` — absorbs one node into another
- `create_concept_node` — materializes a concept from a cluster
- `delete_node` — removes node and all its edges

---

## Act tab — 3-tier card system

Scored by `questionInterestScore()`:

| Tier | Score | Appearance | Behavior |
|------|-------|-----------|---------|
| Hot | ≥12 | Full card, red left border, expanded | Shown first, full answer UI visible |
| Normal | 6–11 | Compact row, collapsed | Click to expand → full question + answer UI |
| Low/Micro | <6 | Ultra-compact row | Y / N / — inline buttons |

Section headers: "Needs your attention" / "Worth reviewing" /
"▸ Quick reviews (N)" (collapsed by default)

Keyboard shortcuts (Act tab, no input focused):
- Y = Yes, N = No, S = Skip

---

## SSE streaming pattern

Used for: evolution, cleanup, summary generation.

```python
# Backend
from sse_starlette.sse import EventSourceResponse

@app.get("/api/evolve/stream")
async def evolve_stream():
    async def gen():
        async for event in run_evolution_cycle(intensity):
            yield {"data": json.dumps(event)}
    return EventSourceResponse(gen())

# Frontend
const es = new EventSource('/api/evolve/stream');
es.onmessage = (e) => {
    const event = JSON.parse(e.data);
    // handle event.type, event.label, etc.
};
```

---

## Evolution engine

3 tasks run per cycle in this order:
1. **Enrich** — Claude enriches thin descriptions (quality-based, not length)
2. **Link prediction** — Adamic-Adar + semantic similarity finds missing edges
3. **Connect isolated** — Claude semantically connects 0-1 degree nodes to hubs

**Auto-apply threshold:** confidence ≥ 0.80 → applies automatically
**Queued to Act:** confidence 0.50–0.79 → user decides

**Stale detection:** 3 consecutive empty cycles → stops, emits `{type:"stale"}`
**Rollback:** if health drops >5% → restores from snapshot

**Do not run cleanup and evolution simultaneously** — both write brain.json,
no mutual exclusion beyond `_evolution_running` flag.

---

## Design system (CSS variables)

```css
/* Backgrounds */
--bg:       #060810;
--panel:    #0b0e18;
--surface2: #0f1420;

/* Borders */
--border:   rgba(255,255,255,0.07);
--border2:  rgba(255,255,255,0.13);

/* Text */
--text:   #e0e4f0;
--muted:  rgba(224,228,240,0.5);
--dim:    rgba(224,228,240,0.22);
--hover:  rgba(255,255,255,0.04);

/* Node type colors */
--feature:   #4da6ff;   /* Feature — blue */
--surface-c: #a87fff;   /* Surface — purple */
--outcome:   #4ecb8d;   /* Outcome — green */
--decision:  #f0a040;   /* Decision — amber */
--person:    #ff8fab;   /* Person — pink */
--company:   #7ec8e3;   /* Company — cyan */

/* Semantic aliases */
--blue:   #4da6ff;
--green:  #4ecb8d;
--amber:  #f0a040;
--red:    #ff6b6b;
--purple: #a87fff;

/* Graph edges */
/* Causal:   rgba(255,100,100,0.65) */
/* Semantic: rgba(255,255,255,0.12) */
/* Default:  rgba(255,255,255,0.04) */
```

---

## All 55 API endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve index.html |
| GET | `/api/health` | Health check + API key status |
| GET | `/api/brain` | Full brain state |
| GET | `/api/brain/stats` | Node/edge counts + BHS |
| DELETE | `/api/brain/reset` | Reset brain to empty |
| POST | `/api/upload` | Upload + extract single document |
| POST | `/api/upload-batch` | Batch upload (50MB max) |
| POST | `/api/brain/ask` | Natural language query |
| POST | `/api/brain/chat` | Smart chat dispatcher |
| POST | `/api/brain/chat/confirm` | Execute confirmed chat command |
| GET | `/api/brain/query-history` | Last 20 queries |
| POST | `/api/brain/web-enrich` | Web search enrichment |
| POST | `/api/brain/clarify` | Clarify ambiguous question |
| GET | `/api/nodes/{id}/summary` | Get/generate node summary |
| POST | `/api/brain/queue-question` | Add question to Act |
| POST | `/api/answer` | Process Q&A answer, update graph |
| GET | `/api/brain/health/stream` | SSE: 8-agent cleanup |
| POST | `/api/brain/health` | Non-streaming cleanup |
| GET | `/api/brain/questions` | Pending questions |
| POST | `/api/brain/merge-duplicate-persons` | Merge Person duplicates |
| POST | `/api/brain/merge-duplicate-labels` | Merge identical label nodes |
| POST | `/api/brain/backfill-person-careers` | Infer careers from Company edges |
| POST | `/api/brain/questions/deduplicate-evolution` | Dedup evolution questions |
| POST | `/api/brain/questions/deduplicate` | General question dedup |
| POST | `/api/brain/questions/cleanup` | Remove orphan questions |
| POST | `/api/rethink` | Re-extract from document |
| GET | `/api/documents` | List uploaded documents |
| GET | `/api/documents/{filename}` | Single document record |
| GET | `/api/nodes/{id}/sources` | Trace document sources |
| GET | `/api/search` | Keyword node search |
| GET | `/api/node/{id}` | Single node + connections |
| GET | `/api/export/markdown` | Export as markdown |
| GET | `/api/brain/history` | All sessions |
| GET | `/api/pending-questions` | Legacy pending questions |
| GET | `/api/nodes/{id}/summary/generate` | SSE: generate one summary |
| POST | `/api/nodes/{id}/summary/save` | Save generated summary |
| GET | `/api/brain/summaries/generate-all` | SSE: bulk generate |
| POST | `/api/brain/summaries/save-all` | Save all summaries |
| GET | `/api/brain/summaries/status` | Summary coverage |
| GET | `/api/brain/summaries/generate-stale` | SSE: regen stale |
| GET | `/api/evolve/stream` | SSE: evolution cycles |
| GET | `/api/evolve/status` | Is evolution running? |
| GET | `/api/evolve/history` | Last N cycle records |
| POST | `/api/brain/embeddings/rebuild` | Rebuild all embeddings |
| GET | `/api/brain/embeddings/stats` | Embedding coverage |
| POST | `/api/brain/search/semantic` | Semantic search |
| POST | `/api/brain/backfill-edge-weights` | Init Hebbian metadata |
| POST | `/api/brain/decay/run` | Manual edge decay |
| GET | `/api/brain/edge-stats` | Hebbian statistics |
| GET | `/api/cost/live` | Session cost stats |
| GET | `/api/cost/log` | Recent API call log |
| GET | `/api/brains` | List all brains |
| POST | `/api/brains` | Create new brain |
| POST | `/api/brains/{id}/activate` | Switch active brain |
| GET | `/api/brains/active` | Get current brain |

---

## Known gaps & sharp edges

1. **requirements.txt is stale** — always use the pip install command above
2. **Embeddings don't auto-rebuild** — run /api/brain/embeddings/rebuild
   after bulk node changes
3. **Edge decay only runs on active brain** — other brains' edges pause
4. **No brain deletion endpoint** — manually delete directory + edit registry.json
5. **Two confidence fields** — always use `confidence_score` (float), not
   `confidence` (legacy string)
6. **Concept nodes inflate BHS** — they start at confidence 0.9 + high degree;
   this is intentional but be aware
7. **No concurrent write protection** — don't run cleanup + evolution together
8. **Louvain is client-side only** — graph.js detects communities for visual
   halos; backend agents use their own detection independently
9. **Document records don't update after node merges** — source tracking
   is approximate after merges

---

## Never do

1. Never hardcode `data/brain.json` — use `get_brain_path()`
2. Never change a node's `id` after creation
3. Never call `save_brain()` in a loop — it's expensive
4. Never save x/y/vx/vy on nodes — stripped automatically
5. Never save an empty brain — guard blocks it
6. Never create duplicate edges — (source, target, rel) must be unique
7. Never add questions with direct append — use `add_question_safe()`
8. Never run cleanup and evolution simultaneously
9. Never forget to call `touch_node()` after mutating a node
10. Never forget to bump `?v=N` on frontend changes
11. Never generate more than 8 questions per cleanup run
12. Never auto-apply merge/delete operations — always queue to Act
13. Never use `MODEL_EXTRACTION` for fast/cheap tasks — use `MODEL_FAST`

---

## Starting a new session — checklist

Before writing any code, confirm:
- [ ] Which brain is active? (`GET /api/brains/active`)
- [ ] What is the current `?v=` version? (check index.html)
- [ ] What is the current BHS? (`GET /api/brain/stats`)
- [ ] Is evolution running? (`GET /api/evolve/status`)
- [ ] What files will this change? (backend only? frontend? both?)

Then begin.