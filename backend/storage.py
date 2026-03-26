"""Brain storage — read/write brain.json with atomic writes and safety nets."""
import json
import math
import os
import re
import shutil
from pathlib import Path
from datetime import datetime
from typing import Optional

BRAIN_FILE = "data/brain.json"

REQUIRED_KEYS = ("nodes", "links", "sessions", "pending_questions", "meta")

EMPTY_BRAIN = {
    "nodes": [],
    "links": [],
    "sessions": [],
    "pending_questions": [],
    "meta": {"created_at": None, "last_updated": None, "version": "1.0"}
}

# ── MULTI-BRAIN PATH RESOLUTION ─────────────────────────────────

BRAINS_ROOT = Path("data/brains")
REGISTRY_FILE = BRAINS_ROOT / "registry.json"

_active_brain_id = None  # lazy-loaded from registry on first access


def get_active_brain_id() -> str:
    global _active_brain_id
    if _active_brain_id is None:
        _active_brain_id = _load_active_from_registry()
    return _active_brain_id


def set_active_brain(brain_id: str) -> None:
    global _active_brain_id
    registry = load_registry()
    if not any(b["id"] == brain_id for b in registry.get("brains", [])):
        raise ValueError(f"Brain '{brain_id}' not found in registry")
    _active_brain_id = brain_id
    registry["active_brain_id"] = brain_id
    save_registry(registry)


def _load_active_from_registry() -> str:
    registry = load_registry()
    return registry.get("active_brain_id", "career")


def load_registry() -> dict:
    if not REGISTRY_FILE.exists():
        return {"brains": [], "active_brain_id": "career"}
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"brains": [], "active_brain_id": "career"}


def save_registry(registry: dict) -> None:
    BRAINS_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2)
    tmp.rename(REGISTRY_FILE)


def get_brain_dir(brain_id: Optional[str] = None) -> Path:
    bid = brain_id or get_active_brain_id()
    d = BRAINS_ROOT / bid
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_brain_path(brain_id: Optional[str] = None) -> Path:
    return get_brain_dir(brain_id) / "brain.json"


def get_embeddings_path(brain_id: Optional[str] = None) -> Path:
    return get_brain_dir(brain_id) / "embeddings.npy"


def get_embeddings_index_path(brain_id: Optional[str] = None) -> Path:
    return get_brain_dir(brain_id) / "embeddings_index.json"


def get_evolution_log_path(brain_id: Optional[str] = None) -> Path:
    return get_brain_dir(brain_id) / "evolution_log.jsonl"


def get_snapshots_dir(brain_id: Optional[str] = None) -> Path:
    d = get_brain_dir(brain_id) / "snapshots"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_documents_dir(brain_id: Optional[str] = None) -> Path:
    d = get_brain_dir(brain_id) / "documents"
    d.mkdir(parents=True, exist_ok=True)
    return d


def create_brain(brain_id: str, label: str, description: str = "") -> dict:
    """Create a new empty brain and register it."""
    registry = load_registry()
    if any(b["id"] == brain_id for b in registry.get("brains", [])):
        raise ValueError(f"Brain '{brain_id}' already exists")
    brain_dir = get_brain_dir(brain_id)
    brain_dir.mkdir(parents=True, exist_ok=True)
    empty = _empty_copy()
    n = now_iso()
    empty["meta"]["created_at"] = n
    with open(brain_dir / "brain.json", "w", encoding="utf-8") as f:
        json.dump(empty, f, indent=2)
    entry = {"id": brain_id, "label": label, "created_at": n, "description": description}
    registry.setdefault("brains", []).append(entry)
    save_registry(registry)
    return entry


def _get_path():
    if BRAINS_ROOT.exists() and REGISTRY_FILE.exists():
        return get_brain_path()
    # Backward compat: un-migrated install
    return Path(os.environ.get("BRAIN_FILE", BRAIN_FILE))


def _empty_copy() -> dict:
    return {k: (v.copy() if isinstance(v, (list, dict)) else v) for k, v in EMPTY_BRAIN.items()}


def _validate_schema(data: dict) -> dict:
    """Ensure all required top-level keys exist with correct default types."""
    for k in REQUIRED_KEYS:
        if k not in data:
            default = EMPTY_BRAIN[k]
            data[k] = default.copy() if isinstance(default, (list, dict)) else default
    if not isinstance(data.get("meta"), dict):
        data["meta"] = {"created_at": None, "last_updated": None, "version": "1.0"}
    if not isinstance(data.get("nodes"), list):
        data["nodes"] = []
    if not isinstance(data.get("links"), list):
        data["links"] = []
    if not isinstance(data.get("sessions"), list):
        data["sessions"] = []
    if not isinstance(data.get("pending_questions"), list):
        data["pending_questions"] = []
    return data


def load_brain() -> dict:
    path = _get_path()
    if not path.exists():
        return _empty_copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            data = _validate_schema(data)

            # Clean up orphan links whose nodes no longer exist
            node_ids = {n['id'] for n in data.get('nodes', [])}
            before = len(data.get('links', []))
            data['links'] = [
                l for l in data.get('links', [])
                if l.get('source') in node_ids and l.get('target') in node_ids
            ]
            after = len(data['links'])
            if before != after:
                print(f"[storage] cleaned {before - after} orphan links on load")

            return data
    except Exception as e:
        print(f"[storage] WARNING: load_brain failed ({e}), returning empty brain")
        return _empty_copy()


def load_brain_safe() -> dict:
    """Safe wrapper — catches ALL exceptions, always returns a valid brain dict.
    Use in read-only contexts (GET endpoints, stats, search)."""
    try:
        return load_brain()
    except Exception:
        return _empty_copy()


def save_brain(brain: dict) -> None:
    path = _get_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Integrity check — warn if required keys missing but still save
    for k in REQUIRED_KEYS:
        if k not in brain:
            print(f"[storage] WARNING: save_brain called without required key '{k}' — adding default")
            default = EMPTY_BRAIN[k]
            brain[k] = default.copy() if isinstance(default, (list, dict)) else default

    # Guard: never overwrite a populated brain with an empty one
    if len(brain.get("nodes", [])) == 0 and path.exists():
        try:
            existing = json.load(open(path, "r", encoding="utf-8"))
            existing_nodes = len(existing.get("nodes", []))
            if existing_nodes > 0:
                print(f"[storage] BLOCKED empty brain save — existing brain has {existing_nodes} nodes")
                return
        except Exception:
            pass

    # Backup — if brain.json exists and is > 1KB, copy to brain.backup.json
    if path.exists() and path.stat().st_size > 1024:
        backup = path.with_name("brain.backup.json")
        try:
            shutil.copy2(path, backup)
        except Exception as e:
            print(f"[storage] WARNING: backup failed: {e}")

    # Ensure qa_history exists
    brain.setdefault("qa_history", [])

    # Timestamps
    brain.setdefault("meta", {})
    now = datetime.utcnow().isoformat() + "Z"
    brain["meta"]["last_updated"] = now
    if not brain["meta"].get("created_at"):
        brain["meta"]["created_at"] = now

    # Strip D3 simulation coords + compute depth_score per node
    links = brain.get("links", [])
    deg = {}
    for l in links:
        s, t = l.get("source", ""), l.get("target", "")
        deg[s] = deg.get(s, 0) + 1
        deg[t] = deg.get(t, 0) + 1
    for node in brain.get("nodes", []):
        for coord_key in ("x", "y", "vx", "vy", "fx", "fy", "index"):
            node.pop(coord_key, None)
        # Depth score
        nid = node["id"]
        d = deg.get(nid, 0)
        mc = len(node.get("metrics", {}))
        dl = len(node.get("desc", "") or "")
        qa_conf = 0.05 if node.get("confidence") in ("qa", "high") else 0
        synth = 0.1 if node.get("synthesized") else 0
        node["depth_score"] = round(min(d * 0.15 + mc * 0.1 + min(dl / 200, 0.2) + qa_conf + synth, 1.0), 3)

    # Compute and persist health score
    score_data = compute_health_score(brain)
    brain.setdefault("health", {})
    prev = brain["health"].get("last_health_score", score_data["score"])
    brain["health"]["last_health_score"] = prev
    score_data["delta"] = round(score_data["score"] - prev, 1)
    brain["health"]["score"] = score_data
    brain["health"]["last_health_score"] = score_data["score"]

    # Atomic write
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(brain, f, indent=2, ensure_ascii=False)
    tmp.rename(path)


def get_stats(brain: dict) -> dict:
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])
    by_type = {}
    for n in nodes:
        t = n.get("type", "Unknown")
        by_type[t] = by_type.get(t, 0) + 1
    return {
        "total_nodes": len(nodes),
        "total_links": len(links),
        "causal_links": sum(1 for l in links if l.get("causal")),
        "by_type": by_type,
        "docs_processed": len(brain.get("sessions", [])),
        "pending_questions": len(brain.get("pending_questions", [])),
        "last_updated": brain.get("meta", {}).get("last_updated"),
    }


def find_node(brain: dict, node_id: str):
    for n in brain.get("nodes", []):
        if n["id"] == node_id:
            return n
    return None


def find_node_by_label(brain: dict, label: str):
    label_lower = label.lower()
    for n in brain.get("nodes", []):
        if n.get("label", "").lower() == label_lower:
            return n
    return None


def now_iso():
    return datetime.utcnow().isoformat() + "Z"


def touch_node(node):
    node["updated_at"] = now_iso()


def add_node(brain: dict, node: dict) -> str:
    existing = find_node(brain, node["id"])
    if existing:
        for k, v in node.items():
            if v and k != "id":
                if k == "memory_type" and existing.get("memory_type") == "semantic":
                    continue  # Never downgrade semantic back to episodic
                elif k == "metrics" and isinstance(v, dict):
                    existing.setdefault("metrics", {}).update(v)
                elif k == "career" and isinstance(v, list):
                    # Merge career entries, don't overwrite
                    existing.setdefault("career", [])
                    existing_companies = {e.get("company", "").lower() for e in existing["career"]}
                    for entry in v:
                        if entry.get("company", "").lower() not in existing_companies:
                            existing["career"].append(entry)
                else:
                    existing[k] = v
        touch_node(existing)
        return "updated"
    # Initialize Person career
    if node.get("type") == "Person" and "career" not in node:
        company = node.get("company", "")
        if company:
            node["career"] = [{"company": company, "role": node.get("where", ""),
                                "from": "", "to": "present", "is_current": True}]
        else:
            node["career"] = []
    node.setdefault("memory_type", "episodic")
    touch_node(node)
    if "created_at" not in node:
        node["created_at"] = node["updated_at"]
    brain["nodes"].append(node)
    return "added"


EDGE_PRUNE_THRESHOLD = 0.10
EDGE_PRUNE_MIN_AGE_DAYS = 14


def is_rejected_edge(brain, source_id, target_id):
    pair_key = "||".join(sorted([source_id, target_id]))
    return pair_key in brain.get("rejected_edges", [])


def reject_edge(brain, source_id, target_id):
    rejected = brain.setdefault("rejected_edges", [])
    pair_key = "||".join(sorted([source_id, target_id]))
    if pair_key not in rejected:
        rejected.append(pair_key)


def add_link(brain: dict, link: dict) -> str:
    for l in brain.get("links", []):
        if (l["source"] == link["source"]
                and l["target"] == link["target"]
                and l["rel"] == link["rel"]):
            return "exists"
    # Temporal + Hebbian metadata
    n = now_iso()
    if "is_current" not in link:
        link["is_current"] = (not link.get("valid_to") or link.get("valid_to") == "present")
    link.setdefault("weight", 1.0)
    link.setdefault("access_count", 0)
    link.setdefault("created_at", n)
    link.setdefault("last_accessed", n)
    link.setdefault("confirmed", False)
    link.setdefault("memory_type", "semantic" if link.get("causal") else "episodic")
    brain["links"].append(link)
    return "added"


def strengthen_edge(link, amount=0.05):
    """Hebbian: fire together → wire together. Oja's rule prevents unbounded growth."""
    max_w = 3.0
    cur = link.get("weight", 1.0)
    delta = amount * (1.0 - cur / max_w)
    link["weight"] = round(min(cur + delta, max_w), 4)
    link["access_count"] = link.get("access_count", 0) + 1
    link["last_accessed"] = now_iso()
    if link.get("access_count", 0) >= 5 and link.get("memory_type") == "episodic":
        link["memory_type"] = "semantic"


def decay_edge(link, days_since_access):
    """Biological retention: fast decay early, slow decay later. LTP for well-used edges."""
    ac = link.get("access_count", 0)
    cur = link.get("weight", 1.0)
    if link.get("confirmed"): rate = 0.001
    elif ac >= 10: rate = 0.002
    elif ac >= 3: rate = 0.01
    else: rate = 0.02
    if days_since_access < 3: eff = rate * 0.1
    elif days_since_access < 30: eff = rate * days_since_access
    else: eff = rate * (30 + (days_since_access - 30) ** 0.5)
    return round(max(0.05, cur - eff), 4)


def update_person_career(brain, person_id, company, role="", from_date="", to_date="present"):
    """Add or update a career entry for a Person node."""
    node = find_node(brain, person_id)
    if not node or node.get("type") != "Person":
        return
    career = node.setdefault("career", [])
    for entry in career:
        if entry.get("company", "").lower() == company.lower():
            if role: entry["role"] = role
            if from_date: entry["from"] = from_date
            if to_date: entry["to"] = to_date
            entry["is_current"] = (to_date == "present" or not to_date)
            touch_node(node)
            return
    if to_date == "present":
        for entry in career:
            if entry.get("is_current"):
                entry["is_current"] = False
    career.append({"company": company, "role": role, "from": from_date,
                    "to": to_date, "is_current": (to_date == "present" or not to_date)})
    touch_node(node)


def remove_node(brain: dict, node_id: str) -> None:
    brain["nodes"] = [n for n in brain["nodes"] if n["id"] != node_id]
    brain["links"] = [
        l for l in brain["links"]
        if l["source"] != node_id and l["target"] != node_id
    ]


def log_session(brain: dict, filename: str, nodes_added: int,
                links_added: int, questions_asked: int, questions_answered: int):
    brain.setdefault("sessions", []).append({
        "doc": filename,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "nodes_added": nodes_added,
        "links_added": links_added,
        "questions_asked": questions_asked,
        "questions_answered": questions_answered,
    })


def is_duplicate_question(brain: dict, new_q: dict) -> bool:
    """Check if a question is a duplicate — checks pending, answered, AND rejected edges."""
    pending = brain.get("pending_questions", [])
    answered = brain.get("qa_history", [])
    rejected = set(brain.get("rejected_edges", []))
    new_cat = new_q.get("category", "")
    new_ctx = new_q.get("context", "")
    new_prefix = new_q.get("question", "").strip().lower()[:80]

    # ── Edge proposal dedup (evolution, web, etc.) ──
    proposed = new_q.get("proposed_action", {})
    if proposed and proposed.get("op") in ("add_edge", "add_link"):
        det = proposed.get("details", proposed)
        src = det.get("source_id", "") or det.get("source", "")
        tgt = det.get("target_id", "") or det.get("target", "")
        if src and tgt:
            pair_key = "||".join(sorted([src, tgt]))
            if pair_key in rejected:
                return True
            for q in pending:
                p = q.get("proposed_action", {})
                if p and p.get("op") in ("add_edge", "add_link"):
                    pd = p.get("details", p)
                    ek = "||".join(sorted([pd.get("source_id", "") or pd.get("source", ""),
                                            pd.get("target_id", "") or pd.get("target", "")]))
                    if ek == pair_key:
                        return True
            for a in answered:
                p = a.get("proposed_action", {})
                if p and p.get("op") in ("add_edge", "add_link"):
                    pd = p.get("details", p)
                    ek = "||".join(sorted([pd.get("source_id", "") or pd.get("source", ""),
                                            pd.get("target_id", "") or pd.get("target", "")]))
                    if ek == pair_key:
                        return True
            for l in brain.get("links", []):
                if "||".join(sorted([l.get("source", ""), l.get("target", "")])) == pair_key:
                    return True

    # ── Standard dedup ──
    if new_cat and new_ctx:
        for q in pending:
            if q.get("category") == new_cat and q.get("context") == new_ctx:
                return True

    if new_prefix:
        for q in pending:
            if q.get("question", "").strip().lower()[:80] == new_prefix:
                return True

    if new_prefix:
        for a in answered:
            if a.get("question", "").strip().lower()[:80] == new_prefix:
                return True

    if new_ctx and new_cat in ("no_source", "no_owner", "isolated", "contradiction", "evolution"):
        for a in answered:
            if a.get("context", "") == new_ctx:
                return True

    return False


def normalise_question(q: dict, source_doc: str = "") -> dict:
    """Ensure every question has required fields with sensible defaults."""
    import time as _t
    if not q.get("priority"):
        cat = q.get("category", "")
        q["priority"] = "high" if cat in ("contradiction", "no_owner", "web_conflict") else "medium"
    if not q.get("category") or q["category"] == "unknown":
        q["category"] = "gap"
        q["category_label"] = "GAP"
    LABELS = {"contradiction": "CONTRADICTION", "no_source": "NO SOURCE", "no_owner": "NO OWNER",
              "isolated": "ISOLATED", "web_conflict": "WEB CONFLICT", "web_enrich": "WEB ENRICH",
              "web_new": "WEB NEW", "compress": "COMPRESS", "plan": "PLAN", "evolution": "EVOLVED", "gap": "GAP"}
    if not q.get("category_label"):
        q["category_label"] = LABELS.get(q["category"], q["category"].upper())
    if not q.get("from_doc") and source_doc:
        q["from_doc"] = source_doc
    if not q.get("id"):
        q["id"] = q.get("category", "q") + "_" + str(int(_t.time() * 1000))
    if len(q.get("question", "")) > 180:
        q["question"] = q["question"][:177] + "\u2026"
    return q


def add_question_safe(brain: dict, question: dict, source_doc: str = "") -> bool:
    """Normalise, dedup, then add question. Returns True if added."""
    q = normalise_question(question, source_doc)
    if is_duplicate_question(brain, q):
        return False
    brain.setdefault("pending_questions", []).append(q)
    return True


def queue_question(brain: dict, question: dict, source_doc: str):
    """Legacy wrapper — now uses add_question_safe."""
    add_question_safe(brain, question, source_doc)


def log_answered_question(brain: dict, question: dict, answer: str, updates: list):
    """Persist a Q&A exchange permanently in the brain."""
    brain.setdefault("qa_history", []).append({
        "question": question.get("question", ""),
        "answer": answer,
        "updates": updates,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source_doc": question.get("from_doc", ""),
    })


def update_confidence_from_answer(brain: dict, question: dict, answer: str) -> list:
    """Every Q&A answer updates node confidence. Yes=boost, No=reduce, 3 confirmations=semantic."""
    answer_lower = answer.strip().lower()
    context_id = question.get("context", "")
    category = question.get("category", "")
    updates = []

    if not context_id:
        return updates

    node = find_node(brain, context_id)
    if not node:
        return updates

    current = node.get("confidence_score", 0.3)

    if answer_lower in ("yes", "y"):
        boost = {"contradiction": 0.20, "no_source": 0.15, "no_owner": 0.12, "evolution": 0.08}.get(category, 0.10)
        new_conf = min(1.0, current + boost)
        node["confidence_score"] = round(new_conf, 3)

        # Count total confirmations for this node
        confirmations = sum(
            1 for a in brain.get("qa_history", [])
            if a.get("context") == context_id and str(a.get("answer", "")).lower() in ("yes", "y")
        )
        if confirmations >= 3 and node.get("memory_type") != "semantic":
            node["memory_type"] = "semantic"
            updates.append(f"Graduated to semantic: {node['label']}")

        touch_node(node)
        updates.append(f"Confidence +{boost:.2f}: {node['label']} {current:.2f}\u2192{new_conf:.2f}")

    elif answer_lower in ("no", "n"):
        penalty = 0.08
        new_conf = max(0.05, current - penalty)
        node["confidence_score"] = round(new_conf, 3)
        touch_node(node)
        updates.append(f"Confidence -{penalty:.2f}: {node['label']} {current:.2f}\u2192{new_conf:.2f}")

    # Contradiction resolution via freetext
    if category == "contradiction" and answer_lower not in ("yes", "y", "no", "n", "skip"):
        if len(answer) > 10:
            node["desc"] = answer
            node["confidence_score"] = min(1.0, node.get("confidence_score", 0.3) + 0.25)
            node["contradiction_resolved"] = True
            node["memory_type"] = "semantic"
            touch_node(node)
            updates.append(f"Contradiction resolved: {node['label']}")

    # Check graduation after any confidence change
    if maybe_graduate_node(node):
        updates.append(f"Graduated to semantic: {node['label']}")

    return updates


def maybe_graduate_node(node: dict) -> bool:
    """Check if node qualifies for episodic -> semantic. One-way only."""
    if node.get("memory_type") == "semantic":
        return False
    graduated = False
    # Trigger 1: confidence >= 0.65
    if node.get("confidence_score", 0) >= 0.65:
        graduated = True
    # Trigger 2: 3+ source documents (with fallback for singular source_doc)
    source_count = len(node.get("source_docs", []))
    if source_count == 0 and node.get("source_doc"):
        source_count = 1
    if source_count >= 3:
        graduated = True
    # Trigger 3: contradiction resolved
    if node.get("contradiction_resolved"):
        graduated = True
    if graduated:
        node["memory_type"] = "semantic"
        node["graduated_at"] = now_iso()
        touch_node(node)
    return graduated


def compute_health_score(brain: dict) -> dict:
    """Compute Brain Health Score across 5 dimensions. No Claude calls — pure math."""
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])
    qa_history = brain.get("qa_history", [])
    sessions = brain.get("sessions", [])
    health_meta = brain.get("health", {})

    n = len(nodes)
    e = len(links)

    if n == 0:
        return {"score": 0, "grade": "F", "delta": 0,
                "dimensions": {"connectivity": 0, "completeness": 0, "confidence": 0, "coherence": 0, "coverage": 0},
                "breakdown": {}}

    node_map = {nd["id"]: nd for nd in nodes}

    # Build adjacency + degree
    degree = {nd["id"]: 0 for nd in nodes}
    causal_count = 0
    orphan_links = 0
    for l in links:
        s, t = l.get("source", ""), l.get("target", "")
        if s in node_map and t in node_map:
            degree[s] = degree.get(s, 0) + 1
            degree[t] = degree.get(t, 0) + 1
            if l.get("causal"):
                causal_count += 1
        else:
            orphan_links += 1

    # Build ownership + surface maps
    owned_features = set()
    surfaced_features = set()
    outcome_has_causal_source = set()
    for l in links:
        rel = l.get("rel", "")
        s, t = l.get("source", ""), l.get("target", "")
        if rel in ("owns", "product owner", "leads", "built"):
            owned_features.add(t)
        if rel in ("lives on", "powers", "runs on", "renders on"):
            surfaced_features.add(s)
        if l.get("causal") and t in node_map and node_map[t].get("type") == "Outcome":
            outcome_has_causal_source.add(t)

    feature_nodes = [nd for nd in nodes if nd.get("type") == "Feature"]
    outcome_nodes = [nd for nd in nodes if nd.get("type") == "Outcome"]
    f_count = max(len(feature_nodes), 1)
    o_count = max(len(outcome_nodes), 1)

    # DIMENSION 1: CONNECTIVITY
    nodes_with_2plus = sum(1 for nd in nodes if degree.get(nd["id"], 0) >= 2)
    avg_degree = (2 * e) / n if n > 0 else 0
    C = 0.5 * (nodes_with_2plus / n) + 0.3 * min(avg_degree / 4.0, 1.0) + 0.2 * min(causal_count / max(n * 0.08, 1), 1.0)
    C = round(min(C, 1.0), 4)

    # DIMENSION 2: COMPLETENESS
    nodes_with_desc = sum(1 for nd in nodes if nd.get("desc") and len(nd.get("desc", "")) > 40)
    features_surfaced = sum(1 for nd in feature_nodes if nd.get("where") or nd["id"] in surfaced_features)
    features_owned = sum(1 for nd in feature_nodes if nd["id"] in owned_features)
    outcomes_sourced = sum(1 for nd in outcome_nodes if nd["id"] in outcome_has_causal_source)
    K = 0.25 * (nodes_with_desc / n) + 0.30 * (features_surfaced / f_count) + 0.25 * (features_owned / f_count) + 0.20 * (outcomes_sourced / o_count)
    K = round(min(K, 1.0), 4)

    # DIMENSION 3: CONFIDENCE
    qa_confirmed_ids = set()
    for h in qa_history:
        ctx = h.get("context", "")
        if ctx:
            qa_confirmed_ids.add(ctx)
        for upd in h.get("updates", []):
            if not isinstance(upd, dict):
                continue
            if upd.get("id"):
                qa_confirmed_ids.add(upd["id"])
            if upd.get("node"):
                qa_confirmed_ids.add(upd["node"])
    qa_confirmed_ratio = len(qa_confirmed_ids) / n
    conf_scores = [nd.get("confidence_score", 0.3) for nd in nodes]
    avg_conf = sum(conf_scores) / len(conf_scores)
    qa_density = min(len(qa_history) / max(n * 0.3, 1), 1.0)
    V = 0.45 * min(qa_confirmed_ratio, 1.0) + 0.35 * avg_conf + 0.20 * qa_density
    V = round(min(V, 1.0), 4)

    # DIMENSION 4: COHERENCE
    contradictions = health_meta.get("contradictions", [])
    contradiction_penalty = min(len(contradictions) / max(n * 0.15, 1), 0.5)
    orphan_penalty = min(orphan_links / max(e, 1), 0.3)
    label_counts = {}
    for nd in nodes:
        lbl = nd.get("label", "").lower().strip()
        label_counts[lbl] = label_counts.get(lbl, 0) + 1
    duplicate_node_count = sum(c - 1 for c in label_counts.values() if c > 1)
    duplicate_penalty = min(duplicate_node_count / max(n * 0.1, 1), 0.3)
    H = max(1.0 - contradiction_penalty - orphan_penalty - duplicate_penalty, 0.0)
    H = round(H, 4)

    # DIMENSION 5: COVERAGE
    docs_processed = max(len(sessions), 1)
    node_coverage = min(n / (docs_processed * 5), 1.0)
    edge_density = min(e / max(n * 1.5, 1), 1.0)
    types_present = {nd.get("type") for nd in nodes}
    required_types = {"Feature", "Surface", "Outcome", "Decision", "Person"}
    type_coverage = len(types_present & required_types) / len(required_types)
    R = 0.40 * node_coverage + 0.35 * edge_density + 0.25 * type_coverage
    R = round(min(R, 1.0), 4)

    # FINAL SCORE: Geometric mean
    dimensions = [C, K, V, H, R]
    epsilon = 0.01
    adjusted = [max(d, epsilon) for d in dimensions]
    geometric_mean = math.exp(sum(math.log(d) for d in adjusted) / len(adjusted))
    raw_score = round(geometric_mean * 100, 1)

    if raw_score >= 90:   grade = "A+"
    elif raw_score >= 80: grade = "A"
    elif raw_score >= 70: grade = "B"
    elif raw_score >= 60: grade = "C"
    elif raw_score >= 50: grade = "D"
    else:                 grade = "F"

    last_score = health_meta.get("last_health_score", raw_score)
    delta = round(raw_score - last_score, 1)

    return {
        "score": raw_score, "grade": grade, "delta": delta,
        "dimensions": {
            "connectivity": round(C * 100, 1), "completeness": round(K * 100, 1),
            "confidence": round(V * 100, 1), "coherence": round(H * 100, 1),
            "coverage": round(R * 100, 1),
        },
        "breakdown": {
            "nodes_connected_2plus": nodes_with_2plus, "avg_degree": round(avg_degree, 2),
            "causal_chains": causal_count, "features_with_surface": features_surfaced,
            "features_with_owner": features_owned, "outcomes_sourced": outcomes_sourced,
            "qa_confirmed_nodes": len(qa_confirmed_ids), "avg_confidence_score": round(avg_conf, 2),
            "qa_answers_total": len(qa_history), "contradictions": len(contradictions),
            "orphan_links": orphan_links, "duplicate_labels": duplicate_node_count,
            "docs_processed": docs_processed, "node_types_present": sorted(types_present),
        },
    }


def export_to_markdown(brain: dict) -> str:
    lines = ["# Brain Export\n", f"*{len(brain.get('nodes',[]))} nodes · {len(brain.get('links',[]))} connections*\n"]
    by_type = {}
    for n in brain.get("nodes", []):
        by_type.setdefault(n.get("type", "Unknown"), []).append(n)
    for type_name, nodes in sorted(by_type.items()):
        lines.append(f"\n## {type_name}s\n")
        for n in nodes:
            lines.append(f"### {n['label']}")
            if n.get("where"):
                lines.append(f"*{n['where']}*")
            if n.get("desc"):
                lines.append(f"\n{n['desc']}")
            if n.get("metrics"):
                lines.append("\n**Metrics:**")
                for k, v in n["metrics"].items():
                    lines.append(f"- {k}: {v}")
            conns = [l for l in brain.get("links", [])
                     if l["source"] == n["id"] or l["target"] == n["id"]]
            if conns:
                lines.append("\n**Connections:**")
                for l in conns[:6]:
                    arrow = "→" if l["source"] == n["id"] else "←"
                    other_id = l["target"] if l["source"] == n["id"] else l["source"]
                    other = find_node(brain, other_id)
                    other_label = other["label"] if other else other_id
                    causal = " 🔴" if l.get("causal") else ""
                    lines.append(f"- {arrow} {other_label} ({l['rel']}){causal}")
            lines.append("")
    return "\n".join(lines)


# ── CLUSTER STABILITY + CONCEPT NODES ────────────────────────────

def cluster_fingerprint(member_ids):
    import hashlib as _hl
    return "clust_" + _hl.md5(",".join(sorted(member_ids)).encode()).hexdigest()[:10]


def find_stable_clusters(brain, min_runs=2, min_jaccard=0.70, min_members=4):
    history = brain.get("cluster_history", [])
    if len(history) < min_runs:
        return []
    recent = history[-2:]
    ca = {c["cluster_id"]: set(c["member_ids"]) for c in recent[0].get("clusters", [])}
    cb = {c["cluster_id"]: set(c["member_ids"]) for c in recent[1].get("clusters", [])}
    existing_members = set()
    for cn in brain.get("concept_nodes", []):
        existing_members.update(cn.get("member_ids", []))
    stable = []
    for cid_b, mb in cb.items():
        if len(mb) < min_members:
            continue
        best_j = 0
        for ma in ca.values():
            if not ma or not mb:
                continue
            j = len(ma & mb) / len(ma | mb)
            if j > best_j:
                best_j = j
        if best_j < min_jaccard:
            continue
        if len(set(mb) & existing_members) > len(mb) * 0.5:
            continue
        stable.append({"cluster_id": cid_b, "member_ids": list(mb),
                        "jaccard_stability": round(best_j, 3), "member_count": len(mb)})
    return stable


def add_concept_node(brain, concept):
    cid = concept.get("cluster_id", "")
    node_id = f"concept_{cid}"
    n = now_iso()
    node = {
        "id": node_id, "label": concept["label"], "type": "Concept",
        "desc": concept.get("description", ""), "company": "", "where": "abstract concept",
        "confidence_score": 0.9, "memory_type": "semantic", "created_at": n, "updated_at": n,
        "concept_metadata": {"cluster_id": cid, "member_ids": concept.get("member_ids", []),
                              "member_count": len(concept.get("member_ids", [])),
                              "generated_at": n, "last_summary_update": n}
    }
    nm = {nd["id"]: nd for nd in brain.get("nodes", [])}
    for mid in concept.get("member_ids", []):
        if mid in nm:
            add_link(brain, {"source": mid, "target": node_id, "rel": "instance_of",
                              "causal": False, "confirmed": True, "weight": 1.5, "memory_type": "semantic"})
    existing_ids = {nd["id"] for nd in brain.get("nodes", [])}
    if node_id not in existing_ids:
        brain.setdefault("nodes", []).append(node)
    brain.setdefault("concept_nodes", []).append({
        "node_id": node_id, "cluster_id": cid, "label": concept["label"],
        "member_ids": concept.get("member_ids", []), "created_at": n})
    return node
