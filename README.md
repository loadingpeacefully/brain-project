# Brain

**A living knowledge graph that turns documents into structured, evolving intelligence.**

> Most knowledge tools are storage. Brain thinks.

[![Python](https://img.shields.io/badge/Python-3.11-blue?style=flat-square)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green?style=flat-square)](https://fastapi.tiangolo.com)
[![D3.js](https://img.shields.io/badge/D3.js-7.8-orange?style=flat-square)](https://d3js.org)
[![License](https://img.shields.io/badge/License-MIT-purple?style=flat-square)](LICENSE)

---

## What is Brain?

Brain is a self-organizing knowledge graph for professionals. Upload documents from your career — PRDs, retrospectives, case studies, meeting notes — and Brain extracts entities, finds causal relationships, asks gap-filling questions, and evolves autonomously.

It doesn't just store what you know. It reasons about it.

**Upload a document ->** Brain extracts 15-30 nodes and their relationships
**Answer questions ->** Brain builds confidence, resolves contradictions, deepens understanding
**Run cleanup ->** 8 specialized agents audit, merge, enrich, and compress the graph
**Query in natural language ->** Brain traverses the graph and synthesizes grounded answers

The longer you use it, the smarter it gets.

---

## What makes Brain different

| Feature | Vector databases | Note-taking apps | Brain |
|---------|-----------------|-----------------|-------|
| Stores facts | Yes | Yes | Yes |
| Understands causality | No | No | Yes |
| Confidence tracking | No | No | Yes |
| Self-organizes | No | No | Yes |
| Detects contradictions | No | No | Yes |
| Evolves autonomously | No | No | Yes |
| Asks what it doesn't know | No | No | Yes |

**The core insight:** Most AI memory systems retrieve similar text. Brain maintains a structured causal graph — it knows that decision X caused outcome Y, that feature Z is owned by person A, that claim B contradicts claim C. It strengthens connections that get used and lets unused ones fade. It forms abstract concepts from stable patterns.

This is not retrieval. This is structured reasoning over accumulated knowledge.

---

## Architecture

```
Documents (PDF/DOCX/MD/TXT)
        |
   Extraction Pipeline
   Claude extracts nodes, edges, causal relationships
   Embedding dedup prevents duplicate nodes (0.88 similarity threshold)
        |
   Knowledge Graph (brain.json)
   466 nodes - 1,038 edges - 7 concept nodes
   Node types: Feature - Surface - Outcome - Decision - Person - Company - Concept
        |
   +-----------------------------------------------------+
   |           Intelligence Layer                         |
   |                                                      |
   |  8-Agent Cleanup System                              |
   |  Cartographer -> Skeptic -> Synthesizer ->           |
   |  Detective -> Archivist -> Questioner ->             |
   |  Compressor -> Conceptualizer                        |
   |                                                      |
   |  Hebbian Edge Dynamics                               |
   |  Edges strengthen on use - Decay when idle           |
   |  Biological retention curve - LTP effect             |
   |                                                      |
   |  Semantic Embeddings (all-MiniLM-L6-v2)             |
   |  384-dim - CPU-only - Semantic search                |
   |  Cross-cluster link prediction                       |
   |                                                      |
   |  Evolution Engine                                    |
   |  Autonomous enrichment - Link prediction             |
   |  Concept formation - Stale cycle detection           |
   +-----------------------------------------------------+
        |
   Brain Health Score (BHS)
   Geometric mean of 5 dimensions:
   Connectivity - Completeness - Confidence - Coherence - Coverage
        |
   Query Interface
   Natural language -> BFS traversal -> Claude synthesis
   Semantic search - Causal chain reasoning - Confidence-weighted answers
```

---

## The 8-Agent Cleanup System

Brain's most novel feature. Eight specialized agents run in sequence, sharing a working memory (`memo`), each responsible for a different dimension of graph intelligence:

| Agent | Role |
|-------|------|
| **Cartographer** | Maps clusters, identifies hubs and singletons |
| **Skeptic** | Challenges thin nodes, finds merge candidates |
| **Synthesizer** | Executes merges, semantic dedup via embeddings |
| **Detective** | Finds contradictions, broken causal chains |
| **Archivist** | Scores confidence, enriches thin descriptions |
| **Questioner** | Generates targeted gap questions (max 8, max 3/category) |
| **Compressor** | Finds absorbable nodes, proposes synthetic concepts |
| **Conceptualizer** | Detects stable clusters, proposes Concept nodes |

Contradictions are flagged, not ignored. Duplicates are merged semantically, not just by label. Concept nodes form only when a cluster is stable across 2+ consecutive cleanup runs (Jaccard >= 0.70) — earned, not auto-generated.

---

## Hebbian Edge Dynamics

Every edge in the graph has memory.

```
Strengthening (Oja's rule):
  delta = amount x (1 - weight / 3.0)
  Called on every query traversal

Decay (biological retention curve):
  Confirmed edges:   rate = 0.001  (barely decays)
  10+ accesses:      rate = 0.002  (long-term potentiation)
  Untouched:         rate = 0.020  (fades quickly)

Graduation:
  episodic -> semantic at 5+ accesses or user confirmation

Pruning:
  weight < 0.10 AND age > 14 days -> deleted
```

Connections the brain actually uses grow stronger. Connections it never uses fade away. The graph develops a point of view on what matters.

---

## Brain Health Score (BHS)

A single number that summarizes how well the brain knows what it knows.

```
BHS = geometric_mean(C, K, V, H, R) x 100

C -- Connectivity:  Are nodes well-connected?
K -- Completeness:  Do nodes have descriptions, owners, sources?
V -- Confidence:    How confirmed is the knowledge?
H -- Coherence:     Are there contradictions or duplicates?
R -- Coverage:      Are all knowledge types represented?
```

Grades: A+(>=90) - A(>=80) - B(>=70) - C(>=60) - D(>=50) - F(<50)

The geometric mean means you can't compensate for one weak dimension with a strong one. A brain with perfect connectivity but no confidence scores is still a C-grade brain.

---

## Multi-Brain Support

Brain supports multiple isolated knowledge graphs — one per domain, project, or context.

```
data/brains/
  career/          <- your career, decisions, outcomes
  project_x/       <- a specific thing you're building
  research/        <- a domain you're studying
  registry.json    <- brain metadata + active brain
```

Each brain has its own nodes, edges, embeddings, evolution log, and BHS. Switch between brains in one click. Query across brains with semantic search.

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/loadingpeacefully/brain-project
cd brain-project

# 2. Install dependencies
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install fastapi uvicorn anthropic pypdf python-docx \
            python-multipart python-dotenv pydantic numpy \
            apscheduler sentence-transformers

# 3. Set API key
echo "ANTHROPIC_API_KEY=your_key_here" > .env

# 4. Run
python backend/main.py

# 5. Open
open http://localhost:8000
```

**Requirements:** Python 3.11+ - Anthropic API key - 500MB disk (model download on first run)

---

## Usage

### Upload documents
Drop any PDF, DOCX, MD, or TXT file into the Feed tab. Brain extracts nodes and asks questions to fill gaps.

### Answer questions (Act tab)
Every question Brain asks is targeted — contradictions, missing causality, ownership gaps. Each answer strengthens the graph. Three confirmations graduates a node from episodic to semantic.

### Run Brain Cleanup (Think tab)
The 8-agent system audits the graph. Takes 60-90 seconds. Run after every 5-10 uploads.

### Query in natural language (Chat tab)
```
"What caused the enrollment drop in Q3?"
"What were my key decisions at WheelsEye?"
"What features does Suneet own?"
"Merge node A into node B"
```

### Evolution (Think tab)
Autonomous background learning. Finds missing connections, enriches thin nodes, proposes concept abstractions. Run for 2-5 minutes after uploading new documents.

---

## Stack

```
Backend:   Python 3.11 - FastAPI - Anthropic Claude API
           sentence-transformers (all-MiniLM-L6-v2, CPU-only)
           APScheduler - NumPy

Frontend:  Vanilla JS - D3.js 7.8 - Graphology (Louvain)
           No build step - No TypeScript - No React

Storage:   JSON (brain.json per brain) - .npy (embeddings)
           Designed for SQLite migration at scale
```

No database. No cloud required. Runs entirely on your machine.

---

## API

55 endpoints. Key ones:

```bash
POST /api/upload              # Upload and extract a document
POST /api/answer              # Answer a pending question
POST /api/brain/chat          # Natural language query/command
GET  /api/brain/health/stream # SSE: run 8-agent cleanup
GET  /api/evolve/stream       # SSE: run evolution cycles
GET  /api/brain/stats         # BHS + node/edge counts
POST /api/brain/search/semantic  # Semantic search
GET  /api/brains              # List all brains
POST /api/brains              # Create new brain
```

Full API documentation in [CLAUDE.md](CLAUDE.md).

---

## Project Structure

```
brain-project/
├── backend/
│   ├── main.py              # 55 FastAPI endpoints
│   ├── storage.py           # Graph persistence, BHS, Hebbian dynamics
│   ├── brain_engine.py      # Claude extraction + Q&A interpretation
│   ├── consolidation.py     # 8-agent cleanup system
│   ├── evolution_engine.py  # Autonomous evolution
│   ├── embeddings.py        # Semantic embeddings + search
│   ├── brain_query.py       # NL query -> BFS traversal -> synthesis
│   ├── chat_commander.py    # Intent classification (query/command/plan)
│   └── ...
├── frontend/
│   ├── index.html           # SPA shell, 5 tabs
│   ├── ui.js                # All UI logic (~1,851 lines)
│   ├── graph.js             # D3 force graph + Louvain communities
│   └── styles.css           # Dark design system
├── data/brains/             # Brain storage (gitignored)
├── CLAUDE.md                # Comprehensive developer guide
└── README.md
```

---

## Roadmap

- [ ] MCP server — expose Brain as memory infrastructure for any AI agent
- [ ] SQLite migration — replace JSON storage for scale
- [ ] Causal chain queries — trace full causal paths through the graph
- [ ] Portfolio generator — export brain as structured career narrative
- [ ] Interview prep mode — generate STAR answers from graph data
- [ ] Multi-user brains — shared institutional memory

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and PRs welcome.

The most valuable contributions right now:
- Bug reports with reproduction steps
- New document parser formats
- BHS formula improvements
- Alternative graph visualization layouts

---

## License

MIT — see [LICENSE](LICENSE)

---

*Built with Claude API - Runs on your machine - Your data stays yours*
