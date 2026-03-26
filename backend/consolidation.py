"""Brain Health — 6-agent architecture with shared working memory.
Agents: Cartographer, Skeptic, Synthesizer, Detective, Archivist, Questioner.
All communicate through a shared memo dict. Only Synthesizer modifies brain."""
import json
import re
import time
from datetime import datetime
from brain_engine import call_claude, call_claude_json
from config import MODEL_FAST
from storage import find_node


def find_semantic_duplicates(brain: dict, threshold: float = 0.90) -> list:
    """Find node pairs that are semantically near-identical but have different IDs."""
    try:
        from embeddings import load_embedding_store
        import numpy as np
    except ImportError:
        return []

    matrix, node_ids = load_embedding_store()
    if matrix is None or len(node_ids) < 10:
        return []

    nodes_map = {n["id"]: n for n in brain.get("nodes", [])}
    candidates = []
    seen = set()

    # Build degree map once
    degree = {}
    for l in brain.get("links", []):
        s, t = l.get("source", ""), l.get("target", "")
        degree[s] = degree.get(s, 0) + 1
        degree[t] = degree.get(t, 0) + 1

    # Bucket by type — only compare same-type nodes
    type_buckets = {}
    for i, nid in enumerate(node_ids):
        nd = nodes_map.get(nid)
        if not nd:
            continue
        t = nd.get("type", "unknown")
        type_buckets.setdefault(t, []).append(i)

    for ntype, indices in type_buckets.items():
        if len(indices) < 2:
            continue
        sub_matrix = matrix[indices]
        sims = sub_matrix @ sub_matrix.T

        for i in range(len(indices)):
            for j in range(i + 1, len(indices)):
                sim = float(sims[i, j])
                if sim < threshold:
                    continue
                id_a = node_ids[indices[i]]
                id_b = node_ids[indices[j]]
                pair_key = tuple(sorted([id_a, id_b]))
                if pair_key in seen:
                    continue
                seen.add(pair_key)

                na = nodes_map.get(id_a, {})
                nb = nodes_map.get(id_b, {})
                la = na.get("label", "").lower()
                lb = nb.get("label", "").lower()
                exact_label = (la == lb)
                confidence = 0.99 if exact_label else sim

                deg_a = degree.get(id_a, 0)
                deg_b = degree.get(id_b, 0)
                keep_id = id_a if deg_a >= deg_b else id_b
                absorb_id = id_b if deg_a >= deg_b else id_a

                candidates.append({
                    "keep_id": keep_id,
                    "keep_label": nodes_map.get(keep_id, {}).get("label", ""),
                    "absorb_id": absorb_id,
                    "absorb_label": nodes_map.get(absorb_id, {}).get("label", ""),
                    "similarity": round(sim, 3),
                    "confidence": round(confidence, 3),
                    "type": ntype,
                    "exact_label": exact_label,
                })

    candidates.sort(key=lambda x: (not x["exact_label"], -x["similarity"]))
    return candidates


def _parse_json_lenient(raw: str):
    """Parse JSON from Claude response, tolerating extra text after the JSON."""
    clean = re.sub(r'^```[a-z]*\n?', '', raw.strip())
    clean = re.sub(r'\n?```$', '', clean).strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    # Try extracting array
    match = re.search(r'\[.*?\]', clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    # Try extracting object
    match = re.search(r'\{.*\}', clean, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


# ── SHARED WORKING MEMORY ──────────────────────────────────────────

def make_memo(brain: dict) -> dict:
    return {
        "node_count": len(brain.get("nodes", [])),
        "link_count": len(brain.get("links", [])),
        "clusters": [],
        "singletons": [],
        "high_degree_nodes": [],
        "merge_candidates": [],
        "singleton_verdicts": [],
        "weak_nodes": [],
        "merges_executed": 0,
        "noise_deleted": 0,
        "descriptions_rewritten": 0,
        "contradictions": [],
        "broken_chains": [],
        "confidence_scores": {},
        "nodes_enriched": 0,
        "gap_questions": [],
        "events": [],
    }


def emit(memo, agent, text, etype="info"):
    """Add a micro-event to the shared memo. Types: find, action, warn, info."""
    memo["events"].append({"agent": agent, "text": text, "type": etype})


# ── AGENT 1: CARTOGRAPHER ─────────────────────────────────────────

def agent_cartographer(brain: dict, memo: dict) -> None:
    print("[cartographer] mapping territory...")
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])

    adjacency = {n["id"]: [] for n in nodes}
    for l in links:
        s = l.get("source", "")
        t = l.get("target", "")
        if s in adjacency:
            adjacency[s].append(t)
        if t in adjacency:
            adjacency[t].append(s)

    memo["singletons"] = [
        {"id": n["id"], "label": n.get("label", ""), "type": n.get("type", "")}
        for n in nodes
        if len(adjacency.get(n["id"], [])) <= 1
    ]

    degree_sorted = sorted(nodes, key=lambda n: len(adjacency.get(n["id"], [])), reverse=True)
    memo["high_degree_nodes"] = [
        {"id": n["id"], "label": n.get("label", ""), "connections": len(adjacency.get(n["id"], []))}
        for n in degree_sorted[:10]
    ]

    node_list = "\n".join([
        f"{n['id']} | {n.get('label','')} | {n.get('type','')} | {n.get('where','')} | {(n.get('desc','') or '')[:50]}"
        for n in nodes[:60]
    ])

    prompt = f"""You are mapping a knowledge graph to find semantic clusters.
Group these nodes into clusters of related concepts.
A cluster is a group of nodes that are about the same feature,
product area, or concept — even if named differently.

Nodes (id | label | type | surface | description):
{node_list}

Return JSON:
{{
  "clusters": [
    {{
      "cluster_name": "descriptive name for this group",
      "node_ids": ["id1", "id2", "id3"],
      "cluster_type": "feature_area|product|person_group|outcome_area",
      "notes": "what these have in common"
    }}
  ]
}}

Only group nodes you are confident belong together.
Minimum cluster size: 2 nodes."""

    try:
        result = call_claude_json(prompt, model=MODEL_FAST)
        memo["clusters"] = result.get("clusters", [])
        emit(memo, "cartographer", f"Found {len(memo['clusters'])} topic clusters", "find")
        if memo["clusters"]:
            biggest = max(memo["clusters"], key=lambda c: len(c.get("node_ids", [])))
            emit(memo, "cartographer", f"Largest cluster: '{biggest.get('cluster_name','')}' ({len(biggest.get('node_ids',[]))} nodes)", "find")
        n_sing = len(memo["singletons"])
        if n_sing:
            emit(memo, "cartographer", f"{n_sing} isolated nodes — not connected to anything", "warn" if n_sing > 5 else "info")
        for hub in memo["high_degree_nodes"][:3]:
            emit(memo, "cartographer", f"Hub node: {hub['label']} ({hub['connections']} connections)", "find")
    except Exception as e:
        emit(memo, "cartographer", f"Clustering failed: {e}", "warn")
        memo["clusters"] = []


# ── AGENT 2: SKEPTIC ──────────────────────────────────────────────

def agent_skeptic(brain: dict, memo: dict) -> None:
    print("[skeptic] challenging nodes...")
    nodes = brain.get("nodes", [])
    node_map = {n["id"]: n for n in nodes}
    merge_candidates = []

    for cluster in memo.get("clusters", []):
        cluster_nodes = [node_map[nid] for nid in cluster.get("node_ids", []) if nid in node_map]
        if len(cluster_nodes) < 2:
            continue

        node_details = "\n".join([
            f"ID: {n['id']}\n  Label: {n.get('label','')}\n  Type: {n.get('type','')}\n"
            f"  Where: {n.get('where', 'unknown')}\n  Desc: {(n.get('desc','') or 'none')[:80]}\n"
            f"  Metrics: {list(n.get('metrics', {}).keys())}\n  Source: {n.get('source_doc', 'unknown')}"
            for n in cluster_nodes
        ])

        prompt = f"""You are a ruthless skeptic reviewing a knowledge graph cluster.
Cluster: {cluster.get('cluster_name', '')}

These nodes were grouped together because they seem related:
{node_details}

For EACH pair that represents THE SAME real-world thing:
- Same feature with different names from different documents
- Same person with nickname vs full name
- Same outcome measured differently
- Same surface referred to differently

Return JSON array of merge decisions:
[
  {{
    "keep": "id_of_most_complete_node",
    "absorb": ["id1_to_delete", "id2_to_delete"],
    "reason": "specific reason they are the same",
    "confidence": "high|medium",
    "synthesized_label": "best label for merged node"
  }}
]

Return [] if nodes are genuinely distinct.
When in doubt about medium-confidence merges, still include them."""

        try:
            raw = call_claude(prompt, model=MODEL_FAST, max_tokens=600)
            decisions = _parse_json_lenient(raw)
            if isinstance(decisions, list):
                merge_candidates.extend(decisions)
        except Exception as e:
            print(f"[skeptic] cluster '{cluster.get('cluster_name','')}' failed: {e}")

    # Challenge singletons
    singletons = memo.get("singletons", [])
    if singletons:
        singleton_list = "\n".join([f"{s['id']} | {s['label']} | {s['type']}" for s in singletons[:20]])
        prompt2 = f"""These knowledge graph nodes have only 0-1 connections.
They may be: orphaned fragments, extraction noise, or genuinely isolated concepts.

Nodes:
{singleton_list}

For each node, decide:
- "keep": genuinely important, just not yet connected
- "likely_noise": probably an extraction artifact, should be deleted
- "needs_connection": real concept that should connect to other nodes

Return JSON array:
[{{"id": "node_id", "verdict": "keep|likely_noise|needs_connection", "reason": "why"}}]"""

        try:
            raw = call_claude(prompt2, model=MODEL_FAST, max_tokens=600)
            verdicts = _parse_json_lenient(raw)
            if isinstance(verdicts, list):
                memo["singleton_verdicts"] = verdicts
                noise_count = sum(1 for v in verdicts if v.get("verdict") == "likely_noise")
                print(f"[skeptic] {noise_count} singletons flagged as noise")
        except Exception as e:
            print(f"[skeptic] singleton check failed: {e}")

    memo["merge_candidates"] = merge_candidates
    high_conf = sum(1 for m in merge_candidates if m.get("confidence") == "high")
    for c in merge_candidates[:5]:
        absorb_label = c.get("absorb", [""])[0] if c.get("absorb") else ""
        emit(memo, "skeptic", f"Possible duplicate: '{c.get('keep','')}' \u2194 '{absorb_label}' — {c.get('reason','')[:60]}", "find")
    if merge_candidates:
        emit(memo, "skeptic", f"{len(merge_candidates)} merge candidates ({high_conf} high confidence)", "find")
    else:
        emit(memo, "skeptic", "No duplicates found — graph looks clean", "info")
    noise_count = sum(1 for v in memo.get("singleton_verdicts", []) if v.get("verdict") == "likely_noise")
    if noise_count:
        emit(memo, "skeptic", f"{noise_count} singletons flagged as noise", "warn")


# ── AGENT 3: SYNTHESIZER ──────────────────────────────────────────

def agent_synthesizer(brain: dict, memo: dict) -> None:
    print("[synthesizer] executing merges...")
    nodes = brain.get("nodes", [])
    node_map = {n["id"]: n for n in nodes}
    merges_done = 0
    absorbed_ids = set()

    candidates = sorted(
        memo.get("merge_candidates", []),
        key=lambda m: 0 if m.get("confidence") == "high" else 1
    )

    for merge in candidates:
        keep_id = merge.get("keep")
        absorb_ids = merge.get("absorb", [])
        if not keep_id or keep_id not in node_map or keep_id in absorbed_ids:
            continue
        absorb_ids = [aid for aid in absorb_ids if aid in node_map and aid not in absorbed_ids]
        if not absorb_ids:
            continue

        keep_node = node_map[keep_id]
        absorb_nodes = [node_map[aid] for aid in absorb_ids]

        if merge.get("synthesized_label"):
            keep_node["label"] = merge["synthesized_label"]

        for absorb_node in absorb_nodes:
            for k, v in absorb_node.get("metrics", {}).items():
                if k not in keep_node.setdefault("metrics", {}):
                    keep_node["metrics"][k] = v

        # Synthesize description from multiple sources
        all_descs = [d for d in [keep_node.get("desc", "")] + [n.get("desc", "") for n in absorb_nodes] if d and len(d) > 15]
        if len(all_descs) > 1:
            try:
                prompt = (
                    f"Synthesize these descriptions of the same node into one comprehensive sentence.\n"
                    f"Node: {keep_node.get('label','')} ({keep_node.get('type','')})\n\n"
                    f"Descriptions:\n" + "\n".join(f"- {d}" for d in all_descs) +
                    "\n\nReturn ONLY the synthesized one-sentence description."
                )
                new_desc = call_claude(prompt, model=MODEL_FAST, max_tokens=150).strip()
                if new_desc and len(new_desc) > 20:
                    keep_node["desc"] = new_desc
                    memo["descriptions_rewritten"] = memo.get("descriptions_rewritten", 0) + 1
            except Exception:
                pass

        for link in brain["links"]:
            if link.get("source") in absorb_ids:
                link["source"] = keep_id
            if link.get("target") in absorb_ids:
                link["target"] = keep_id

        # Remove self-loops and duplicate links
        brain["links"] = [l for l in brain["links"] if l.get("source") != l.get("target")]
        seen_links = set()
        unique_links = []
        for l in brain["links"]:
            key = (l.get("source"), l.get("target"), l.get("rel"))
            if key not in seen_links:
                seen_links.add(key)
                unique_links.append(l)
        brain["links"] = unique_links

        keep_node["synthesized"] = True
        keep_node["synthesized_from"] = absorb_ids

        for aid in absorb_ids:
            brain["nodes"] = [n for n in brain["nodes"] if n["id"] != aid]
            if aid in node_map:
                del node_map[aid]
            absorbed_ids.add(aid)

        merges_done += 1
        emit(memo, "synthesizer", f"Merged {', '.join(absorb_ids)} → {keep_id}", "action")
        print(f"[synthesizer] merged {absorb_ids} -> {keep_id}: {merge.get('reason', '')[:60]}")

    # Delete noise singletons
    noise_deleted = 0
    for verdict in memo.get("singleton_verdicts", []):
        if verdict.get("verdict") == "likely_noise":
            nid = verdict.get("id")
            if nid and nid not in absorbed_ids and find_node(brain, nid):
                brain["nodes"] = [n for n in brain["nodes"] if n["id"] != nid]
                brain["links"] = [l for l in brain["links"] if l.get("source") != nid and l.get("target") != nid]
                noise_deleted += 1
                print(f"[synthesizer] deleted noise node: {nid}")

    # Semantic duplicate detection via embeddings
    sem_dupes = find_semantic_duplicates(brain, threshold=0.90)
    auto_merged_sem = 0
    queued_sem = 0
    for candidate in sem_dupes:
        if candidate["exact_label"]:
            # Auto-merge exact label duplicates
            keep_id = candidate["keep_id"]
            absorb_id = candidate["absorb_id"]
            keep_node = find_node(brain, keep_id)
            absorb_node = find_node(brain, absorb_id)
            if not keep_node or not absorb_node:
                continue
            # Copy missing fields
            if not keep_node.get("desc") and absorb_node.get("desc"):
                keep_node["desc"] = absorb_node["desc"]
            if absorb_node.get("metrics"):
                keep_node.setdefault("metrics", {}).update(absorb_node.get("metrics", {}))
            keep_node["confidence_score"] = max(keep_node.get("confidence_score", 0), absorb_node.get("confidence_score", 0))
            # Re-point edges
            for link in brain["links"]:
                if link.get("source") == absorb_id:
                    link["source"] = keep_id
                if link.get("target") == absorb_id:
                    link["target"] = keep_id
            brain["links"] = [l for l in brain["links"] if l.get("source") != l.get("target")]
            seen_lk = set()
            brain["links"] = [l for l in brain["links"] if (l.get("source"), l.get("target")) not in seen_lk and not seen_lk.add((l.get("source"), l.get("target")))]
            brain["nodes"] = [n for n in brain["nodes"] if n["id"] != absorb_id]
            auto_merged_sem += 1
            emit(memo, "synthesizer", f"Auto-merged: '{candidate['absorb_label']}' \u2192 '{candidate['keep_label']}'", "action")
        elif candidate["similarity"] >= 0.93 and queued_sem < 5:
            from storage import add_question_safe, normalise_question
            q = {
                "id": f"sem_dupe_{candidate['absorb_id']}_{int(time.time())}",
                "type": "yesno",
                "question": f"Merge '{candidate['absorb_label']}' into '{candidate['keep_label']}'? They are {candidate['similarity']*100:.0f}% semantically similar.",
                "why": "Semantic duplicate detected by embeddings",
                "context": candidate["keep_id"],
                "priority": "high" if candidate["similarity"] > 0.95 else "medium",
                "category": "contradiction",
                "category_label": "DUPLICATE",
                "from_doc": "brain_cleanup",
                "proposed_action": {
                    "op": "merge_nodes",
                    "keep_id": candidate["keep_id"],
                    "absorb_id": candidate["absorb_id"],
                    "keep_label": candidate["keep_label"],
                    "absorb_label": candidate["absorb_label"],
                },
            }
            normalise_question(q)
            if add_question_safe(brain, q):
                queued_sem += 1
                emit(memo, "synthesizer", f"Queued merge: '{candidate['absorb_label']}' ({candidate['similarity']*100:.0f}% similar)", "find")

    memo["merges_executed"] = merges_done + auto_merged_sem
    memo["noise_deleted"] = noise_deleted
    if auto_merged_sem or queued_sem:
        emit(memo, "synthesizer", f"Semantic dedup: {auto_merged_sem} auto-merged, {queued_sem} queued", "action")
    if noise_deleted:
        emit(memo, "synthesizer", f"Removed {noise_deleted} noise nodes with no value", "action")
    if memo.get("descriptions_rewritten", 0):
        emit(memo, "synthesizer", f"Rewrote {memo['descriptions_rewritten']} thin descriptions using context", "action")
    if not merges_done and not noise_deleted and not auto_merged_sem and not queued_sem:
        emit(memo, "synthesizer", "Nothing to merge — brain is already clean", "info")


# ── AGENT 4: DETECTIVE ─────────────────────────────────────────────

def agent_detective(brain: dict, memo: dict) -> None:
    print("[detective] investigating contradictions...")
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])
    node_map = {n["id"]: n for n in nodes}
    contradictions = []
    broken_chains = []

    # Multiple owners for same feature
    ownership = {}
    for l in links:
        if l.get("rel") in ("owns", "product owner", "leads", "built"):
            ownership.setdefault(l.get("target"), []).append(l.get("source"))

    for feature_id, owner_ids in ownership.items():
        if len(set(owner_ids)) > 1:
            feature = node_map.get(feature_id, {})
            owners = [node_map.get(oid, {}).get("label", oid) for oid in set(owner_ids)]
            contradictions.append({
                "node": feature.get("label", feature_id),
                "node_id": feature_id,
                "type": "multiple_owners",
                "issue": f"Owned by: {', '.join(owners)}",
                "evidence": "Multiple 'owns' edges pointing to this node",
                "suggestion": f"Confirm single owner — likely {owners[0]}"
            })

    # Backwards causal chains
    for l in links:
        if l.get("causal"):
            source = node_map.get(l.get("source"))
            target = node_map.get(l.get("target"))
            if source and target and source.get("type") == "Outcome" and target.get("type") == "Feature":
                broken_chains.append({
                    "from": source.get("label"),
                    "to": target.get("label"),
                    "issue": "Causal arrow goes Outcome -> Feature (backwards)"
                })

    # Unclaimed outcomes
    outcome_ids_with_source = {l.get("target") for l in links if l.get("causal")}
    for n in nodes:
        if n.get("type") == "Outcome" and n["id"] not in outcome_ids_with_source:
            contradictions.append({
                "node": n.get("label"),
                "node_id": n["id"],
                "type": "unclaimed_outcome",
                "issue": "No feature claims responsibility for this outcome",
                "evidence": "No causal edge pointing to this Outcome node",
                "suggestion": "Which feature drove this result?"
            })

    memo["contradictions"] = contradictions
    memo["broken_chains"] = broken_chains
    for c in contradictions[:5]:
        emit(memo, "detective", f"Contradiction: '{c['node']}' — {c['issue'][:70]}", "warn")
    for b in broken_chains[:3]:
        emit(memo, "detective", f"Broken causal chain: {b['from']} → {b['to']} ({b['issue'][:40]})", "warn")
    if not contradictions and not broken_chains:
        emit(memo, "detective", "No contradictions found — logic is consistent", "info")


# ── AGENT 5: ARCHIVIST ─────────────────────────────────────────────

def agent_archivist(brain: dict, memo: dict) -> None:
    print("[archivist] scoring and enriching nodes...")
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])

    degree = {n["id"]: 0 for n in nodes}
    for l in links:
        if l.get("source") in degree:
            degree[l["source"]] += 1
        if l.get("target") in degree:
            degree[l["target"]] += 1

    confidence_scores = {}
    nodes_to_enrich = []

    for n in nodes:
        score = 0.0
        conn_count = degree.get(n["id"], 0)
        score += min(conn_count / 10.0, 0.4)
        if n.get("desc") and len(n.get("desc", "")) > 20:
            score += 0.15
        if n.get("metrics") and len(n.get("metrics", {})) > 0:
            score += 0.15
        if n.get("type") == "Feature" and n.get("where"):
            score += 0.1
        if n.get("company") and n["company"] != "unknown":
            score += 0.1
        if n.get("synthesized"):
            score += 0.1
        confidence_scores[n["id"]] = round(min(score, 1.0), 2)
        if score < 0.4 and conn_count >= 2:
            nodes_to_enrich.append(n)

    # Temporal decay — stale nodes lose confidence
    now = datetime.utcnow()
    for node in nodes:
        last_confirmed = node.get("last_confirmed_at")
        if last_confirmed:
            try:
                dt = datetime.fromisoformat(last_confirmed.replace("Z", ""))
                days_stale = (now - dt).days
                if days_stale > 30:
                    decay = min((days_stale / 30) * 0.02, 0.15)
                    nid = node["id"]
                    confidence_scores[nid] = max(confidence_scores.get(nid, 0.5) - decay, 0.1)
                    if decay > 0.05:
                        emit(memo, "archivist", f"Confidence decay: {node.get('label','')} not confirmed in {days_stale} days", "warn")
            except Exception:
                pass

    memo["confidence_scores"] = confidence_scores

    # Enrich thin nodes in batches of 4
    enriched_count = 0
    node_map = {n["id"]: n for n in nodes}

    for i in range(0, min(len(nodes_to_enrich), 8), 4):
        batch = nodes_to_enrich[i:i+4]
        batch_context = []
        for n in batch:
            connected_labels = []
            for l in links:
                if l.get("source") == n["id"]:
                    t = node_map.get(l.get("target", ""))
                    if t:
                        connected_labels.append(f"{l['rel']} -> {t['label']}")
                elif l.get("target") == n["id"]:
                    s = node_map.get(l.get("source", ""))
                    if s:
                        connected_labels.append(f"{s['label']} -> {l['rel']}")
            batch_context.append(
                f"Node: {n.get('label','')} ({n.get('type','')})\n"
                f"Current desc: {n.get('desc', 'none')}\n"
                f"Connections: {', '.join(connected_labels[:5])}"
            )

        prompt = (
            "Improve the descriptions of these knowledge graph nodes.\n"
            "Use their connections as context.\n\n"
            + "\n\n".join(batch_context) +
            "\n\nReturn a JSON array, one entry per node:\n"
            '[{"improved_desc": "better one-sentence description"}]\n'
            "If already good, return unchanged."
        )

        try:
            raw = call_claude(prompt, model=MODEL_FAST, max_tokens=400)
            results = _parse_json_lenient(raw)
            if isinstance(results, list):
                for j, r in enumerate(results):
                    if j < len(batch) and isinstance(r, dict):
                        new_desc = r.get("improved_desc", "")
                        if new_desc and len(new_desc) > 20:
                            batch[j]["desc"] = new_desc
                            enriched_count += 1
        except Exception as e:
            print(f"[archivist] enrichment batch failed: {e}")

    for n in nodes:
        n["confidence_score"] = confidence_scores.get(n["id"], 0.5)

    memo["nodes_enriched"] = enriched_count
    avg_conf = sum(confidence_scores.values()) / max(len(confidence_scores), 1)
    low_conf_count = sum(1 for s in confidence_scores.values() if s < 0.4)
    high_conf_count = sum(1 for s in confidence_scores.values() if s >= 0.7)
    emit(memo, "archivist", f"Avg confidence: {avg_conf:.0%} — {high_conf_count} high, {low_conf_count} need more info", "find")
    if enriched_count:
        emit(memo, "archivist", f"Enriched {enriched_count} thin node descriptions using graph context", "action")

    # Batch graduation: episodic → semantic for eligible nodes
    from storage import maybe_graduate_node
    graduated = sum(1 for n in nodes if maybe_graduate_node(n))
    if graduated:
        emit(memo, "archivist", f"Graduated {graduated} nodes from episodic to semantic", "action")


# ── AGENT 6: QUESTIONER ────────────────────────────────────────────

def agent_questioner(brain: dict, memo: dict) -> None:
    """Generate specific, categorized audit questions from agent findings. No Claude calls."""
    print("[questioner] generating targeted audit questions...")
    nodes = brain.get("nodes", [])
    node_map = {n["id"]: n for n in nodes}
    gap_questions = []
    ts = int(time.time())

    # Build answered set to avoid re-asking
    _answered_ctxs = set(a.get("context", "") for a in brain.get("qa_history", []) if a.get("context"))
    _answered_pfxs = set(a.get("question", "").strip().lower()[:80] for a in brain.get("qa_history", []) if a.get("question"))
    def _already_answered(ctx=None, text=None):
        if ctx and ctx in _answered_ctxs: return True
        if text and text.strip().lower()[:80] in _answered_pfxs: return True
        return False

    # From Detective: contradictions → specific resolution questions
    for c in memo.get("contradictions", []):
        if _already_answered(ctx=c.get("node_id")): continue
        if c.get("type") == "multiple_owners":
            gap_questions.append({
                "id": f"audit_owner_{c.get('node_id','x')}_{ts}",
                "type": "freetext",
                "question": f"Who is the single owner of '{c.get('node', 'this feature')}'? Currently shows: {c.get('issue', 'multiple owners')}",
                "why": "Resolves ownership contradiction — will remove duplicate edges",
                "context": c.get("node_id", ""),
                "priority": "high",
                "category": "contradiction",
                "category_label": "CONTRADICTION",
                "from_doc": "brain_cleanup",
            })
        elif c.get("type") == "unclaimed_outcome":
            outcome_node = node_map.get(c.get("node_id", ""))
            metrics_str = ""
            if outcome_node and outcome_node.get("metrics"):
                metrics_str = " (metrics: " + ", ".join(f"{k}: {v}" for k, v in list(outcome_node["metrics"].items())[:2]) + ")"
            gap_questions.append({
                "id": f"audit_outcome_{c.get('node_id','x')}_{ts}",
                "type": "freetext",
                "question": f"What caused '{c.get('node', 'this outcome')}'{metrics_str}? Which feature, decision, or initiative drove this?",
                "why": "Creates a causal edge — the most valuable link in the brain",
                "context": c.get("node_id", ""),
                "priority": "high",
                "category": "no_source",
                "category_label": "NO SOURCE",
                "from_doc": "brain_cleanup",
            })
        elif c.get("type") == "multiple_surfaces":
            gap_questions.append({
                "id": f"audit_surface_{c.get('node_id','x')}_{ts}",
                "type": "freetext",
                "question": f"What is the PRIMARY surface for '{c.get('node', 'this feature')}'? Currently shows: {c.get('issue', 'multiple surfaces')}",
                "why": "Resolves surface conflict",
                "context": c.get("node_id", ""),
                "priority": "medium",
                "category": "contradiction",
                "category_label": "CONTRADICTION",
                "from_doc": "brain_cleanup",
            })

    # From Archivist: low confidence Feature nodes without surface
    low_conf_nodes = [
        node_map[nid] for nid, score in memo.get("confidence_scores", {}).items()
        if score < 0.3 and nid in node_map
    ][:4]
    for n in low_conf_nodes:
        if _already_answered(ctx=n["id"]): continue
        if n.get("type") == "Feature" and not n.get("where"):
            gap_questions.append({
                "id": f"audit_surface_{n['id']}_{ts}",
                "type": "freetext",
                "question": f"What surface or platform does '{n['label']}' live on?",
                "why": "Places this feature correctly in the product architecture",
                "context": n["id"],
                "priority": "medium",
                "category": "no_owner",
                "category_label": "NO SURFACE",
                "from_doc": "brain_cleanup",
            })

    # From Cartographer: singletons that need connection
    for v in memo.get("singleton_verdicts", []):
        if v.get("verdict") == "needs_connection":
            n = node_map.get(v.get("id", ""))
            if n and not _already_answered(ctx=n["id"]):
                gap_questions.append({
                    "id": f"audit_isolated_{n['id']}_{ts}",
                    "type": "freetext",
                    "question": f"'{n['label']}' is isolated with only 1 connection. What does it connect to or what project is it part of?",
                    "why": "Integrates an isolated node into the knowledge graph",
                    "context": n["id"],
                    "priority": "medium",
                    "category": "isolated",
                    "category_label": "ISOLATED",
                    "from_doc": "brain_cleanup",
                })

    # Cap: sort by priority, max 8 total, max 3 per category
    pri_order = {"high": 0, "medium": 1, "low": 2}
    gap_questions.sort(key=lambda q: pri_order.get(q.get("priority", "low"), 2))
    from collections import defaultdict as _dd
    cat_count = _dd(int)
    capped = []
    for q in gap_questions:
        c = q.get("category", "")
        if cat_count[c] < 3 and len(capped) < 8:
            capped.append(q)
            cat_count[c] += 1
    gap_questions = capped

    memo["gap_questions"] = gap_questions
    for q in gap_questions[:5]:
        emit(memo, "questioner", f"Gap question: {q['question'][:70]}...", "find")
    if not gap_questions:
        emit(memo, "questioner", "Brain is well-covered — no critical gaps found", "info")


# ── AGENT 7: COMPRESSOR ────────────────────────────────────────────

def agent_compressor(brain: dict, memo: dict) -> None:
    """Find thin nodes to absorb and synthetic nodes to create."""
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])
    degree = {n["id"]: 0 for n in nodes}
    for l in links:
        s, t = l.get("source", ""), l.get("target", "")
        if s in degree: degree[s] += 1
        if t in degree: degree[t] += 1
    node_map = {n["id"]: n for n in nodes}

    # Absorption candidates: low-degree, no metrics, not key types
    keep_types = {"Person", "Company", "Outcome"}
    absorption = [
        {"id": n["id"], "label": n["label"], "type": n.get("type", ""), "degree": degree.get(n["id"], 0)}
        for n in nodes
        if n.get("type") not in keep_types and degree.get(n["id"], 0) <= 1
        and not n.get("metrics") and n.get("confidence") not in ("qa", "high")
    ]
    emit(memo, "compressor", f"Found {len(absorption)} thin nodes (degree ≤1, no metrics)", "find")

    # Synthetic node opportunities from clusters
    clusters = memo.get("clusters", [])
    synthetic_opps = []
    for cluster in clusters[:8]:
        cids = cluster.get("node_ids", [])
        if len(cids) < 3:
            continue
        cset = set(cids)
        internal = sum(1 for l in links if l.get("source") in cset and l.get("target") in cset)
        if internal < len(cids) * 0.4:
            detail = [{"id": nid, "label": node_map[nid]["label"], "type": node_map[nid].get("type", "")}
                      for nid in cids[:6] if nid in node_map]
            synthetic_opps.append({"cluster_name": cluster.get("cluster_name", ""), "nodes": detail,
                                   "internal_links": internal, "nodes_count": len(cids)})

    if synthetic_opps:
        opp_text = json.dumps(synthetic_opps[:3], indent=1)
        prompt = (f"Analyze these knowledge graph clusters to find 'synthetic nodes' —\n"
                  f"concepts that don't exist yet but would connect multiple nodes.\n\n"
                  f"CLUSTERS NEEDING CONNECTORS:\n{opp_text}\n\n"
                  f"Return JSON array:\n"
                  f'[{{"cluster_name":"name","synthetic_node_label":"concept","synthetic_node_type":"Feature|Surface|Outcome|Decision",'
                  f'"would_connect":["id1","id2"],"rationale":"why","confidence":"high|medium"}}]\n'
                  f"Return [] if none. Be conservative.")
        try:
            result = call_claude_json(prompt, model=MODEL_FAST)
            if isinstance(result, list):
                for opp in result[:3]:
                    emit(memo, "compressor",
                         f"Synthetic: '{opp.get('synthetic_node_label','')}' would connect "
                         f"{len(opp.get('would_connect',[]))} nodes in '{opp.get('cluster_name','')}'", "find")
                memo["synthetic_opportunities"] = result
        except Exception as e:
            print(f"[compressor] synthetic analysis failed: {e}")

    # Build compression questions
    ts = int(time.time())
    comp_qs = []
    for cand in absorption[:5]:
        comp_qs.append({
            "id": f"compress_absorb_{cand['id']}_{ts}", "type": "yesno",
            "question": f"'{cand['label']}' has {cand['degree']} connection(s) and no metrics. Remove or absorb?",
            "why": "Removing thin nodes compresses the brain", "context": cand["id"],
            "priority": "low", "category": "compress", "category_label": "COMPRESS", "from_doc": "brain_cleanup",
        })
    for opp in memo.get("synthetic_opportunities", [])[:3]:
        nid = opp.get("synthetic_node_label", "").lower().replace(" ", "_")[:25]
        comp_qs.append({
            "id": f"compress_synthetic_{ts}", "type": "yesno",
            "question": f"Create '{opp.get('synthetic_node_label','')}' ({opp.get('synthetic_node_type','Feature')}) to connect: "
                       f"{', '.join(opp.get('would_connect',[])[:3])}?",
            "why": opp.get("rationale", ""), "context": "", "priority": "medium",
            "category": "compress", "category_label": "COMPRESS", "from_doc": "brain_cleanup",
            "proposed_action": {"op": "add_node", "details": {
                "id": nid, "label": opp.get("synthetic_node_label", ""), "type": opp.get("synthetic_node_type", "Feature"),
                "desc": f"Synthetic connector: {opp.get('rationale','')}", "company": "unknown"
            }},
        })
    memo["compression_questions"] = comp_qs
    emit(memo, "compressor", f"Generated {len(comp_qs)} compression proposals", "action")


# ── AGENT 8: CONCEPTUALIZER ───────────────────────────────────────

def agent_conceptualizer(brain: dict, memo: dict) -> None:
    """Detect stable clusters, propose concept nodes, update existing ones."""
    from storage import find_stable_clusters, cluster_fingerprint, add_question_safe, normalise_question
    from datetime import datetime

    emit(memo, "conceptualizer", "Detecting stable knowledge clusters", "info")
    current_clusters = memo.get("clusters", [])
    if not current_clusters:
        emit(memo, "conceptualizer", "No cluster data from Cartographer", "info")
        memo["concept_proposals"] = []
        return

    # Save current clusters to history
    history_entry = {
        "run_id": f"cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "clusters": [{"cluster_id": cluster_fingerprint(c.get("node_ids", [])),
                       "member_ids": c.get("node_ids", []), "member_count": len(c.get("node_ids", [])),
                       "cluster_name": c.get("cluster_name", "")}
                      for c in current_clusters if len(c.get("node_ids", [])) >= 4]
    }
    brain.setdefault("cluster_history", []).append(history_entry)
    brain["cluster_history"] = brain["cluster_history"][-5:]
    emit(memo, "conceptualizer", f"Saved {len(history_entry['clusters'])} clusters to history", "info")

    # Update existing concept node summaries
    node_map = {n["id"]: n for n in brain.get("nodes", [])}
    for cn in brain.get("concept_nodes", []):
        concept_node = node_map.get(cn["node_id"])
        if not concept_node: continue
        members = [node_map[mid] for mid in cn.get("member_ids", []) if mid in node_map]
        if len(members) < 2: continue
        mt = "\n".join(f"- {m['label']} ({m.get('type','')}): {m.get('desc','')[:60]}" for m in members[:10])
        try:
            raw = call_claude(f"Update summary for concept node '{concept_node['label']}' with members:\n{mt}\n\nReturn 2-3 sentence description only.",
                              model=MODEL_FAST, max_tokens=150, touchpoint="concept_summary_update")
            if raw.strip() and len(raw.strip()) > 20:
                concept_node["desc"] = raw.strip()
                emit(memo, "conceptualizer", f"Updated summary: {concept_node['label']}", "action")
        except Exception:
            pass

    # Find stable clusters
    stable = find_stable_clusters(brain, min_runs=2, min_jaccard=0.70, min_members=4)
    if not stable:
        emit(memo, "conceptualizer", "No new stable clusters (need 2+ consistent runs)", "info")
        memo["concept_proposals"] = []
        return

    emit(memo, "conceptualizer", f"Found {len(stable)} stable clusters for concept nodes", "find")

    # Generate names for top 3 proposals
    proposals = []
    for cluster in stable[:3]:
        members = [node_map[mid] for mid in cluster["member_ids"] if mid in node_map]
        if not members: continue
        mt = "\n".join(f"- {m['label']} ({m.get('type','')}): {m.get('desc','')[:60]}" for m in members[:8])
        try:
            result = call_claude_json(
                f"Name this stable knowledge cluster.\nMEMBERS:\n{mt}\n\nReturn JSON: {{\"name\":\"3-5 word concept name\",\"description\":\"2 sentence description\"}}",
                model=MODEL_FAST)
            if result.get("name") and result.get("description"):
                proposals.append({"cluster_id": cluster["cluster_id"], "proposed_name": result["name"],
                                   "description": result["description"], "member_ids": cluster["member_ids"],
                                   "member_count": len(members), "stability": cluster["jaccard_stability"]})
                emit(memo, "conceptualizer", f"Proposed: '{result['name']}' ({len(members)} members, {cluster['jaccard_stability']:.0%} stable)", "find")
        except Exception:
            pass

    memo["concept_proposals"] = proposals

    # Queue proposals to Act
    ts = int(time.time())
    for p in proposals:
        q = {"id": f"concept_{p['cluster_id']}_{ts}", "type": "yesno",
             "question": f"Create concept node '{p['proposed_name']}'? Represents {p['member_count']} stable nodes: "
                        f"{', '.join([node_map[m]['label'] for m in p['member_ids'][:3] if m in node_map])}...",
             "why": p["description"], "context": p["cluster_id"], "priority": "medium",
             "category": "concept_proposal", "category_label": "CONCEPT", "from_doc": "brain_cleanup",
             "proposed_action": {"op": "create_concept_node", "details": p}}
        normalise_question(q)
        add_question_safe(brain, q)
    if proposals:
        emit(memo, "conceptualizer", f"Queued {len(proposals)} concept proposals in Act", "action")


# ── MAIN ORCHESTRATOR ──────────────────────────────────────────────

def run_brain_health(brain: dict):
    """Generator — yields progress events as dicts. Final yield is {"type": "complete", "result": {...}}."""
    start = time.time()
    print(f"\n[brain health] starting — {len(brain.get('nodes',[]))} nodes, {len(brain.get('links',[]))} links")

    memo = make_memo(brain)

    agents = [
        ("cartographer", "Mapping territory", agent_cartographer),
        ("skeptic", "Challenging every node", agent_skeptic),
        ("synthesizer", "Executing merges", agent_synthesizer),
        ("detective", "Finding contradictions", agent_detective),
        ("archivist", "Scoring and enriching", agent_archivist),
        ("questioner", "Generating gap questions", agent_questioner),
        ("compressor", "Compressing graph", agent_compressor),
        ("conceptualizer", "Forming concepts", agent_conceptualizer),
    ]

    for agent_id, description, agent_fn in agents:
        yield {"type": "agent_start", "agent": agent_id, "description": description}
        try:
            agent_fn(brain, memo)
        except Exception as e:
            print(f"[{agent_id}] error: {e}")
            emit(memo, agent_id, f"Error: {str(e)[:60]}", "warn")

        # Yield micro-events accumulated by this agent
        for ev in memo.get("events", []):
            if ev.get("agent") == agent_id:
                yield {"type": "event", "agent": agent_id, "text": ev["text"], "event_type": ev.get("type", "info")}
        memo["events"] = [e for e in memo["events"] if e["agent"] != agent_id]

        summaries = {
            "cartographer": f"{len(memo.get('clusters', []))} clusters, {len(memo.get('singletons', []))} singletons",
            "skeptic": f"{len(memo.get('merge_candidates', []))} merge candidates",
            "synthesizer": f"{memo.get('merges_executed', 0)} merged, {memo.get('noise_deleted', 0)} noise removed",
            "detective": f"{len(memo.get('contradictions', []))} contradictions, {len(memo.get('broken_chains', []))} broken chains",
            "archivist": f"{memo.get('nodes_enriched', 0)} enriched",
            "questioner": f"{len(memo.get('gap_questions', []))} gap questions",
            "compressor": f"{len(memo.get('compression_questions', []))} compression proposals",
        }
        yield {"type": "agent_done", "agent": agent_id, "summary": summaries.get(agent_id, "Done")}

    # Final cleanup
    node_ids = {n["id"] for n in brain.get("nodes", [])}
    before_links = len(brain.get("links", []))
    brain["links"] = [l for l in brain.get("links", []) if l.get("source") in node_ids and l.get("target") in node_ids]
    orphans_removed = before_links - len(brain["links"])

    # Store gap questions + compression questions (deduped + normalised)
    from storage import add_question_safe
    all_new_qs = memo.get("gap_questions", []) + memo.get("compression_questions", [])
    added = 0
    for q in all_new_qs:
        if add_question_safe(brain, q, "brain_cleanup"):
            added += 1

    # Store health metadata
    from storage import compute_health_score
    score_data = compute_health_score(brain)
    prev = brain.get("health", {}).get("last_health_score", score_data["score"])
    score_data["delta"] = round(score_data["score"] - prev, 1)
    brain.setdefault("health", {})
    brain["health"]["score"] = score_data
    brain["health"]["last_health_score"] = score_data["score"]
    brain["health"]["last_run"] = datetime.utcnow().isoformat() + "Z"
    brain["health"]["contradictions"] = memo.get("contradictions", [])
    brain["health"]["avg_confidence"] = round(
        sum(memo.get("confidence_scores", {}).values()) / max(len(memo.get("confidence_scores", {})), 1), 2
    )

    elapsed = round(time.time() - start, 1)
    changes_made = (
        memo.get("merges_executed", 0) > 0 or
        memo.get("noise_deleted", 0) > 0 or
        memo.get("nodes_enriched", 0) > 0 or
        orphans_removed > 0
    )

    result = {
        "elapsed_seconds": elapsed,
        "nodes_before": memo["node_count"],
        "nodes_after": len(brain.get("nodes", [])),
        "duplicates_merged": memo.get("merges_executed", 0),
        "noise_removed": memo.get("noise_deleted", 0),
        "synthesized": memo.get("descriptions_rewritten", 0),
        "orphan_links_removed": orphans_removed,
        "nodes_enriched": memo.get("nodes_enriched", 0),
        "avg_confidence": brain["health"]["avg_confidence"],
        "contradictions": memo.get("contradictions", []),
        "gap_questions_added": len(memo.get("gap_questions", [])),
        "health_score": score_data,
        "gaps": {
            "unowned_features": [{"id": c.get("node_id", ""), "label": c["node"]} for c in memo.get("contradictions", []) if c.get("type") == "multiple_owners"],
            "unclaimed_outcomes": [{"id": c.get("node_id", ""), "label": c["node"]} for c in memo.get("contradictions", []) if c.get("type") == "unclaimed_outcome"],
            "isolated_nodes": memo.get("singletons", [])[:10],
        },
        "profiles_updated": 0,
        "changes_made": changes_made,
        "summary": f"Merged {memo.get('merges_executed',0)}, enriched {memo.get('nodes_enriched',0)}, found {len(memo.get('contradictions',[]))} contradictions in {elapsed}s",
    }

    print(f"\n[brain health] complete in {elapsed}s — merged: {result['duplicates_merged']}, enriched: {result['nodes_enriched']}, contradictions: {len(result['contradictions'])}")

    yield {"type": "complete", "result": result}
