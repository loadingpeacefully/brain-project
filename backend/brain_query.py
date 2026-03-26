"""Brain Query Layer — ask the brain questions, get cited answers from the graph."""
import json
import re
from brain_engine import call_claude, call_claude_json
from config import MODEL_EXTRACTION, MODEL_FAST


def find_entry_nodes(brain: dict, query: str) -> list:
    """Find most relevant nodes — semantic search first, keyword fallback."""
    nodes = brain.get("nodes", [])
    if not nodes:
        return []

    # Try semantic search first
    try:
        from embeddings import semantic_search, load_embedding_store
        matrix, node_ids = load_embedding_store()
        if matrix is not None and len(node_ids) > 0:
            sem_results = semantic_search(query, brain, top_k=5, threshold=0.25)
            if sem_results:
                nodes_map = {n["id"]: n for n in nodes}
                entry = [nodes_map[r["node_id"]] for r in sem_results if r["node_id"] in nodes_map]
                if entry:
                    print(f"[query] Semantic entry: {[n['label'] for n in entry[:3]]}")
                    return entry
    except Exception as e:
        print(f"[query] Semantic fallback to keyword: {e}")

    # Keyword fallback
    query_lower = query.lower()
    query_words = set(query_lower.split())
    scored = []
    for n in nodes:
        score = 0
        label_lower = n.get("label", "").lower()
        desc_lower = (n.get("desc", "") or "").lower()
        if query_lower in label_lower or label_lower in query_lower: score += 10
        score += len(query_words & set(label_lower.split())) * 3
        if any(w in desc_lower for w in query_words if len(w) > 2): score += 2
        if score > 0: scored.append((score, n))
    candidates = [n for _, n in sorted(scored, key=lambda x: -x[0])[:10]]
    return candidates[:5] if candidates else []


def traverse_subgraph(brain: dict, entry_node_ids: list, max_nodes: int = 25) -> dict:
    """BFS traversal from entry nodes. Prioritises causal edges."""
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])
    node_map = {n["id"]: n for n in nodes}

    adjacency = {}
    for l in links:
        s, t = l.get("source", ""), l.get("target", "")
        adjacency.setdefault(s, []).append((t, l))
        adjacency.setdefault(t, []).append((s, l))

    visited = set(entry_node_ids)
    collected_nodes = []
    queue = list(entry_node_ids)
    type_priority = {"Outcome": 5, "Feature": 4, "Decision": 4, "Company": 3, "Surface": 2, "Person": 2}

    while queue and len(collected_nodes) < max_nodes:
        cid = queue.pop(0)
        node = node_map.get(cid)
        if not node:
            continue
        collected_nodes.append(node)

        neighbors = adjacency.get(cid, [])
        causal = [(nid, l) for nid, l in neighbors if l.get("causal")]
        other = [(nid, l) for nid, l in neighbors if not l.get("causal")]
        other.sort(key=lambda item: type_priority.get(node_map.get(item[0], {}).get("type", ""), 1), reverse=True)

        for nid, link in causal + other:
            if nid not in visited:
                visited.add(nid)
                queue.append(nid)

    collected_ids = {n["id"] for n in collected_nodes}
    relevant_links = [l for l in links if l.get("source") in collected_ids and l.get("target") in collected_ids]

    return {"nodes": collected_nodes, "links": relevant_links, "entry_nodes": entry_node_ids}


def serialise_subgraph(subgraph: dict) -> str:
    """Convert subgraph to dense readable format for Claude."""
    nodes = subgraph.get("nodes", [])
    links = subgraph.get("links", [])
    entry_ids = set(subgraph.get("entry_nodes", []))
    node_labels = {n["id"]: n["label"] for n in nodes}
    lines = ["NODES IN RELEVANT SUBGRAPH:"]

    for n in nodes:
        marker = "* " if n["id"] in entry_ids else "  "
        line = f"{marker}[{n['type']}] {n['label']}"
        if n.get("where"):
            line += f" (on {n['where']})"
        if n.get("company") and n["company"] != "unknown":
            line += f" @ {n['company']}"
        if n.get("desc"):
            line += f"\n     -> {n['desc']}"
        if n.get("metrics"):
            line += f"\n     -> Metrics: {', '.join(f'{k}: {v}' for k, v in n['metrics'].items())}"
        lines.append(line)

    causal = [l for l in links if l.get("causal")]
    if causal:
        lines.append("\nCAUSAL CHAINS (most important):")
        for l in causal:
            lines.append(f"  {node_labels.get(l.get('source',''),'?')} --[{l.get('rel','caused')}]--> {node_labels.get(l.get('target',''),'?')}")

    structural = [l for l in links if not l.get("causal")]
    if structural:
        lines.append("\nSTRUCTURAL CONNECTIONS:")
        for l in structural[:15]:
            lines.append(f"  {node_labels.get(l.get('source',''),'?')} -> [{l.get('rel','')}] -> {node_labels.get(l.get('target',''),'?')}")

    return "\n".join(lines)


def synthesise_answer(query: str, subgraph: dict) -> dict:
    """Use Claude to answer the query from the subgraph."""
    if not subgraph.get("nodes"):
        return {"answer": "No relevant information found.", "cited_node_ids": [], "cited_node_labels": [], "confidence": "none", "subgraph_size": 0}

    subgraph_text = serialise_subgraph(subgraph)
    node_count = len(subgraph["nodes"])

    prompt = f"""You are Suneet's personal knowledge brain answering a question.
Answer directly and confidently. Take a position.

QUESTION: {query}

KNOWLEDGE AVAILABLE ({node_count} nodes):
{subgraph_text}

INSTRUCTIONS:
1. Answer the question directly in 2-3 sentences. Take a clear position based on the evidence. If one thing is clearly more central/connected/important, say so.
2. If knowledge is LIMITED, say it plainly: "The brain has limited [topic] context \u2014 only N nodes documented." Then still give the best answer you can.
3. End with ONE specific thing the user could add to improve: "To get a fuller answer, add: [specific document or info]" \u2014 only if confidence is low.
4. Never say "the graph does not explicitly..." \u2014 just say what you know and what you don't.
5. Cite specific node names, metrics, and relationships. Be concrete.

Confidence calibration:
- 10+ relevant nodes: answer confidently, no caveats
- 3-9 nodes: mild caveat about scope
- 1-2 nodes: lead with limitation, then best answer, then what to add

Return JSON:
{{
  "answer": "direct 2-3 sentence answer",
  "reasoning": "how you found this",
  "cited_node_ids": ["id1", "id2"],
  "cited_node_labels": ["Label 1", "Label 2"],
  "confidence": "high|medium|low",
  "missing_info": "one sentence only, or null if confidence is high/medium"
}}"""

    try:
        result = call_claude_json(prompt, model=MODEL_EXTRACTION)
        result["subgraph_size"] = node_count
        return result
    except Exception as e:
        print(f"[query] synthesis failed: {e}")
        return {"answer": f"Found {node_count} relevant nodes but couldn't synthesise an answer.", "cited_node_ids": [], "cited_node_labels": [], "confidence": "low", "subgraph_size": node_count}


def answer_brain_query(query: str, brain: dict) -> dict:
    """Full pipeline: query -> entry nodes -> subgraph -> answer."""
    print(f"[query] '{query}'")

    entry_nodes = find_entry_nodes(brain, query)
    if not entry_nodes:
        return {"query": query, "answer": "No relevant nodes found. The brain may not have information about this topic yet.", "cited_node_labels": [], "cited_node_ids": [], "confidence": "none", "subgraph_size": 0, "entry_nodes": []}

    entry_ids = [n["id"] for n in entry_nodes]
    print(f"[query] entry nodes: {[n['label'] for n in entry_nodes]}")

    subgraph = traverse_subgraph(brain, entry_ids, max_nodes=25)
    print(f"[query] subgraph: {len(subgraph['nodes'])} nodes, {len(subgraph['links'])} links")

    result = synthesise_answer(query, subgraph)

    # Hebbian: strengthen traversed edges
    try:
        from storage import load_brain as _lb, save_brain as _sb, strengthen_edge
        brain_rw = _lb()
        lm = {}
        for l in brain_rw.get("links", []):
            lm[(l.get("source", ""), l.get("target", ""))] = l
            lm[(l.get("target", ""), l.get("source", ""))] = l
        strengthened = 0
        for l in subgraph.get("links", []):
            src = l.get("source", "") if isinstance(l.get("source"), str) else l["source"].get("id", "")
            tgt = l.get("target", "") if isinstance(l.get("target"), str) else l["target"].get("id", "")
            edge = lm.get((src, tgt))
            if edge:
                strengthen_edge(edge, amount=0.05)
                strengthened += 1
        if strengthened > 0:
            _sb(brain_rw)
    except Exception:
        pass

    return {
        "query": query,
        "answer": result.get("answer", ""),
        "reasoning": result.get("reasoning", ""),
        "cited_node_labels": result.get("cited_node_labels", []),
        "cited_node_ids": result.get("cited_node_ids", []),
        "confidence": result.get("confidence", "low"),
        "missing_info": result.get("missing_info"),
        "subgraph_size": result.get("subgraph_size", 0),
        "entry_nodes": [n["label"] for n in entry_nodes],
    }
