"""Background brain evolution — manual trigger only. Priority scoring, link prediction, enrichment, merge detection."""
import json
import time
import copy
import math
import random
import hashlib
import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator

from storage import load_brain, save_brain, find_node
from brain_engine import call_claude, call_claude_json, MODEL_FAST
from cost_tracker import record_call

def _snapshots_dir():
    from storage import get_snapshots_dir
    return get_snapshots_dir()


def _evolution_log():
    from storage import get_evolution_log_path
    return get_evolution_log_path()

_evolution_running = False
_evolution_lock = None
_consecutive_empty_cycles = 0
_STALE_THRESHOLD = 3


def reset_stale_counter():
    global _consecutive_empty_cycles
    _consecutive_empty_cycles = 0


def description_quality_score(node):
    """0.0=good, 1.0=needs enrichment. Quality-based, not just length."""
    import re as _re
    desc = (node.get("desc") or "").strip()
    label = node.get("label", "").lower()
    if not desc: return 1.0
    score = 0.0
    if len(desc) < 80: score += 0.4
    if desc.lower().startswith(label[:15]) or label[:15] in desc.lower()[:30]: score += 0.3
    GENERIC = ["feature on ", "surface for ", "component of ", "part of the ", "related to ",
               "used for ", "this feature", "this surface", "this outcome", "no description"]
    if any(p in desc.lower() for p in GENERIC): score += 0.4
    if node.get("type") in ("Outcome", "Feature"):
        if not _re.search(r'\d+', desc) and "%" not in desc and not node.get("metrics"): score += 0.2
    ACTION = ["enables", "allows", "provides", "creates", "drives", "manages", "tracks",
              "generates", "reduces", "increases", "built", "designed", "launched", "shipped"]
    if not any(v in desc.lower() for v in ACTION): score += 0.15
    return min(score, 1.0)


def create_snapshot(brain):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    content = json.dumps(brain, sort_keys=True)
    h = hashlib.sha256(content.encode()).hexdigest()[:12]
    snap_id = f"{ts}_{h}"
    (_snapshots_dir() / f"{snap_id}.json").write_text(content)
    snaps = sorted(_snapshots_dir().glob("*.json"))
    for old in snaps[:-20]:
        old.unlink()
    return snap_id


def compute_health_delta(before, after):
    def quick_score(b):
        nodes, links = b.get("nodes", []), b.get("links", [])
        if not nodes:
            return 0
        deg = {}
        for l in links:
            deg[l.get("source", "")] = deg.get(l.get("source", ""), 0) + 1
            deg[l.get("target", "")] = deg.get(l.get("target", ""), 0) + 1
        connected = sum(1 for n in nodes if deg.get(n["id"], 0) >= 2)
        has_desc = sum(1 for n in nodes if len(n.get("desc", "")) > 40)
        return (connected / len(nodes) + has_desc / len(nodes)) / 2
    bs, as_ = quick_score(before), quick_score(after)
    return ((as_ - bs) / bs * 100) if bs else 0


def compute_priority_scores(brain):
    nodes, links = brain.get("nodes", []), brain.get("links", [])
    deg = {}
    for l in links:
        deg[l.get("source", "")] = deg.get(l.get("source", ""), 0) + 1
        deg[l.get("target", "")] = deg.get(l.get("target", ""), 0) + 1
    scored = []
    for n in nodes:
        nid, reasons, score = n["id"], [], 0.0
        conf = n.get("confidence_score", 0.3)
        if conf < 0.4:
            score += (0.4 - conf) * 2; reasons.append("low confidence")
        qual = description_quality_score(n)
        if qual >= 0.6:
            score += qual * 0.5; reasons.append("low_quality_desc")
        elif qual >= 0.3:
            score += qual * 0.2; reasons.append("mediocre_desc")
        d = deg.get(nid, 0)
        if d == 0:
            score += 0.8; reasons.append("isolated")
        elif d == 1:
            score += 0.4; reasons.append("low connectivity")
        if n.get("type") in ("Outcome", "Feature") and not n.get("metrics"):
            score += 0.3; reasons.append("no metrics")
        scored.append({"node_id": nid, "label": n["label"], "type": n.get("type", ""),
                        "score": round(score, 3), "reasons": reasons, "degree": d, "confidence": conf})
    return sorted(scored, key=lambda x: x["score"], reverse=True)


def predict_links(brain, top_k=10):
    nodes, links = brain.get("nodes", []), brain.get("links", [])
    neighbors = {n["id"]: set() for n in nodes}
    for l in links:
        s, t = l.get("source", ""), l.get("target", "")
        if s in neighbors: neighbors[s].add(t)
        if t in neighbors: neighbors[t].add(s)
    existing = {(l.get("source"), l.get("target")) for l in links}
    existing |= {(l.get("target"), l.get("source")) for l in links}
    nm = {n["id"]: n for n in nodes}
    COMPAT = {("Person","Feature"),("Person","Outcome"),("Feature","Surface"),
              ("Feature","Outcome"),("Company","Person"),("Company","Feature"),
              ("Decision","Feature"),("Outcome","Decision")}
    ids = [n["id"] for n in nodes]
    sample = random.sample(ids, min(len(ids), 120))
    cands = []
    for i, u in enumerate(sample):
        for v in sample[i+1:]:
            if (u, v) in existing: continue
            nu, nv = nm.get(u), nm.get(v)
            if not nu or not nv: continue
            pair = (nu.get("type",""), nv.get("type",""))
            if pair not in COMPAT and tuple(reversed(pair)) not in COMPAT: continue
            shared = neighbors.get(u, set()) & neighbors.get(v, set())
            if not shared: continue
            aa = sum(1/math.log(max(len(neighbors.get(w, set())), 2)) for w in shared)
            if aa > 0.3:
                cands.append({"source_id": u, "target_id": v, "source_label": nu["label"],
                              "target_label": nv["label"], "source_type": nu.get("type",""),
                              "target_type": nv.get("type",""), "aa_score": round(aa, 4),
                              "shared": [nm[w]["label"] for w in list(shared)[:3]],
                              "confidence": min(0.5 + aa * 0.3, 0.90)})
    return sorted(cands, key=lambda x: x["aa_score"], reverse=True)[:top_k]


async def task_enrich_nodes(brain, priority):
    nm = {n["id"]: n for n in brain.get("nodes", [])}
    links = brain.get("links", [])
    thin = [p for p in priority if "low_quality_desc" in p["reasons"] or "mediocre_desc" in p["reasons"] or "thin description" in p["reasons"]]
    # Prioritize episodic nodes — they need enrichment more than semantic ones
    thin.sort(key=lambda p: (0 if nm.get(p["node_id"], {}).get("memory_type") == "episodic" else 1, -p.get("score", 0)))
    thin = thin[:6]
    if not thin: return []
    contexts = []
    for p in thin:
        n = nm.get(p["node_id"])
        if not n: continue
        cl = []
        for l in links:
            if l.get("source") == n["id"]:
                o = nm.get(l.get("target", ""))
                if o: cl.append(f"{l.get('rel','related')}:{o['label']}")
            elif l.get("target") == n["id"]:
                o = nm.get(l.get("source", ""))
                if o: cl.append(f"{o['label']}:{l.get('rel','related')}")
        contexts.append({"id": n["id"], "label": n["label"], "type": n.get("type",""),
                          "desc": n.get("desc","") or "(none)", "conns": cl[:5]})
    prompt = (f"Improve descriptions for these knowledge graph nodes using their connections.\n\n"
              f"{json.dumps(contexts, indent=1)}\n\n"
              f"Return JSON array: [{{'id':'node_id','new_desc':'better 1-2 sentence desc','confidence':0.0-1.0}}]")
    try:
        result = call_claude_json(prompt, model=MODEL_FAST)
        if not isinstance(result, list): return []
        return [{"op": "enrich_description", "node_id": r["id"], "new_desc": r["new_desc"],
                  "confidence": r.get("confidence", 0.75), "safe": True, "auto_apply": True}
                 for r in result if r.get("new_desc") and r.get("confidence", 0) >= 0.70]
    except Exception:
        return []


async def task_link_prediction(brain):
    from storage import is_rejected_edge
    cands = predict_links(brain, top_k=6)
    cands = [c for c in cands if not is_rejected_edge(brain, c["source_id"], c["target_id"])]
    if not cands: return []
    prompt = (f"Evaluate these proposed edges for a knowledge graph.\n\n"
              f"{json.dumps([{'u':c['source_label'],'u_type':c['source_type'],'v':c['target_label'],'v_type':c['target_type'],'shared':c['shared']} for c in cands], indent=1)}\n\n"
              f"Return JSON array: [{{'should_connect':bool,'relationship':'str','confidence':0.0-1.0,'reason':'str'}}]")
    try:
        result = call_claude_json(prompt, model=MODEL_FAST)
        if not isinstance(result, list): return []
        ops = []
        for i, v in enumerate(result[:len(cands)]):
            if not v.get("should_connect"): continue
            c = cands[i]
            conf = v.get("confidence", c["confidence"])
            ops.append({"op": "add_edge", "source_id": c["source_id"], "target_id": c["target_id"],
                          "source_label": c["source_label"], "target_label": c["target_label"],
                          "rel": v.get("relationship", "related"), "confidence": conf,
                          "reasoning": v.get("reason", ""),
                          "safe": conf >= 0.80, "auto_apply": conf >= 0.80})
        return ops
    except Exception:
        return []


async def task_connect_isolated_nodes(brain):
    """Use Claude to semantically connect isolated nodes to hubs (AA can't help these)."""
    nodes, links = brain.get("nodes", []), brain.get("links", [])
    degree = {}
    for l in links:
        degree[l.get("source", "")] = degree.get(l.get("source", ""), 0) + 1
        degree[l.get("target", "")] = degree.get(l.get("target", ""), 0) + 1
    isolated = [n for n in nodes if degree.get(n["id"], 0) <= 1
                and n.get("type") not in ("Person", "Company") and n.get("desc")]
    if not isolated: return []
    hubs = sorted([n for n in nodes if degree.get(n["id"], 0) >= 5],
                  key=lambda x: -degree.get(x["id"], 0))[:15]
    if not hubs: return []
    iso_text = json.dumps([{"id": n["id"], "label": n["label"], "type": n.get("type", ""),
                             "desc": (n.get("desc", "") or "")[:100]} for n in isolated[:8]], indent=1)
    hub_text = json.dumps([{"id": n["id"], "label": n["label"], "type": n.get("type", ""),
                             "desc": (n.get("desc", "") or "")[:80]} for n in hubs], indent=1)
    prompt = (f"Connect isolated nodes to hub nodes based on semantic meaning.\n\n"
              f"ISOLATED (0-1 connections):\n{iso_text}\n\nHUB NODES:\n{hub_text}\n\n"
              f"Return JSON: [{{'source_id':'isolated','target_id':'hub','rel':'relationship','confidence':0.0-1.0,'reasoning':'why'}}]\n"
              f"Be conservative. Max 2 per isolated node.")
    try:
        result = call_claude_json(prompt, model=MODEL_FAST)
        if not isinstance(result, list): return []
        ops = []
        for item in result:
            conf = item.get("confidence", 0)
            if conf < 0.75: continue
            ops.append({"op": "add_edge", "source_id": item.get("source_id", ""),
                          "target_id": item.get("target_id", ""),
                          "source_label": next((n["label"] for n in isolated if n["id"] == item.get("source_id")), "?"),
                          "target_label": next((n["label"] for n in hubs if n["id"] == item.get("target_id")), "?"),
                          "rel": item.get("rel", "related"), "confidence": conf,
                          "reasoning": item.get("reasoning", ""),
                          "safe": conf >= 0.80, "auto_apply": conf >= 0.80})
        from storage import is_rejected_edge as _ire
        ops = [o for o in ops if not _ire(brain, o.get("source_id", ""), o.get("target_id", ""))]
        return ops
    except Exception:
        return []


def detect_person_duplicates(brain):
    """Find Person nodes that represent the same real individual using name matching."""
    import re as _re
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])
    persons = [n for n in nodes if n.get("type") == "Person" and len(n.get("label", "")) < 60]
    degree = {}
    for l in links:
        degree[l.get("source", "")] = degree.get(l.get("source", ""), 0) + 1
        degree[l.get("target", "")] = degree.get(l.get("target", ""), 0) + 1
    candidates = []
    for i, p1 in enumerate(persons):
        for p2 in persons[i + 1:]:
            clean1 = _re.sub(r'[@(].*', '', p1["label"].lower()).strip()
            clean2 = _re.sub(r'[@(].*', '', p2["label"].lower()).strip()
            words1 = set(w for w in clean1.split() if len(w) >= 3)
            words2 = set(w for w in clean2.split() if len(w) >= 3)
            shared = words1 & words2
            if shared:
                d1 = degree.get(p1["id"], 0)
                d2 = degree.get(p2["id"], 0)
                keep = p1 if d1 >= d2 else p2
                absorb = p2 if d1 >= d2 else p1
                candidates.append({
                    "keep_id": keep["id"], "keep_label": keep["label"],
                    "absorb_id": absorb["id"], "absorb_label": absorb["label"],
                    "shared": list(shared),
                    "confidence": 0.90 if len(shared) >= 2 else 0.75
                })
    return candidates


async def task_find_merges(brain):
    nodes = brain.get("nodes", [])
    # Person-specific merge detection first
    person_merges = detect_person_duplicates(brain)
    person_ops = [{"op": "merge_nodes", "keep_id": c["keep_id"], "source_label": c["keep_label"],
                    "target_label": c["absorb_label"], "absorb_ids": [c["absorb_id"]],
                    "confidence": c["confidence"], "reasoning": f"Same person: shared name words {c['shared']}",
                    "safe": False, "auto_apply": False} for c in person_merges]

    # General Jaccard merge detection
    labels = [(n["id"], n["label"].lower(), n.get("type", "")) for n in nodes]
    cands = []
    for i, (id1, l1, t1) in enumerate(labels):
        for id2, l2, t2 in labels[i+1:]:
            if t1 != t2: continue
            w1, w2 = set(l1.split()), set(l2.split())
            if not w1 or not w2: continue
            j = len(w1 & w2) / len(w1 | w2)
            if j > 0.5: cands.append((id1, id2, j))
    if not cands: return []
    nm = {n["id"]: n for n in nodes}
    top = sorted(cands, key=lambda x: x[2], reverse=True)[:5]
    pairs = [{"id1": a, "label1": nm[a]["label"], "id2": b, "label2": nm[b]["label"], "sim": round(s, 2)}
             for a, b, s in top if nm.get(a) and nm.get(b)]
    prompt = (f"Are these node pairs the same entity?\n\n{json.dumps(pairs, indent=1)}\n\n"
              f"Return JSON: [{{'id1':'','id2':'','are_same':bool,'keep_id':'id','confidence':0.0-1.0,'reason':'str'}}]")
    try:
        result = call_claude_json(prompt, model=MODEL_FAST)
        if not isinstance(result, list): return []
        general_ops = [{"op": "merge_nodes", "keep_id": r.get("keep_id"), "source_label": r.get("id1",""),
                  "target_label": r.get("id2",""),
                  "absorb_ids": [r["id1"] if r.get("keep_id") == r["id2"] else r["id2"]],
                  "confidence": r.get("confidence", 0.8), "reasoning": r.get("reason", ""),
                  "safe": False, "auto_apply": False}
                 for r in result if r.get("are_same") and r.get("confidence", 0) >= 0.75]
        return person_ops + general_ops
    except Exception:
        return person_ops


async def task_web_enrich(brain, priority):
    """Enrich high-priority nodes from web during evolution. Cost-gated."""
    from web_enricher import enrich_brain_from_web
    from storage import add_question_safe

    nm = {n["id"]: n for n in brain.get("nodes", [])}

    # Pick nodes that benefit most from web context
    candidates = []
    for p in priority[:30]:
        node = nm.get(p["node_id"])
        if not node:
            continue
        ntype = node.get("type", "")
        conf = node.get("confidence_score", 0)

        if ntype == "Person" and conf < 0.7:
            label = node.get("label", "")
            company = node.get("company", "")
            candidates.append({"node": node, "query": f"{label} {company} role background"})
        elif ntype == "Company" and conf < 0.7:
            label = node.get("label", "")
            candidates.append({"node": node, "query": f"{label} company product funding"})
        elif ntype == "Outcome" and node.get("metrics"):
            label = node.get("label", "")
            company = node.get("company", "")
            candidates.append({"node": node, "query": f"{company} {label}"})

        if len(candidates) >= 3:  # cost gate: ~$0.60/cycle max
            break

    if not candidates:
        return []

    questions_added = 0
    for c in candidates:
        try:
            result = enrich_brain_from_web(c["query"], brain)
            for q in result.get("questions", []):
                if add_question_safe(brain, q, "evolution_web"):
                    questions_added += 1
        except Exception as e:
            print(f"[evolution/web] Failed for '{c['query']}': {e}")

    if questions_added > 0:
        return [{"op": "web_enrich_summary", "questions_added": questions_added,
                 "nodes_searched": len(candidates), "safe": False, "auto_apply": False}]
    return []


def apply_safe_ops(brain, ops):
    from storage import touch_node
    changes = []
    for op in ops:
        if not op.get("auto_apply") or not op.get("safe"): continue
        if op["op"] == "enrich_description":
            node = find_node(brain, op["node_id"])
            if node:
                node["desc"] = op["new_desc"]
                node["desc_enriched_at"] = datetime.now(timezone.utc).isoformat()
                touch_node(node)
                changes.append(f"Enriched: {node['label']}")
        elif op["op"] == "add_edge":
            existing = {(l.get("source"), l.get("target")) for l in brain.get("links", [])}
            pair = (op["source_id"], op["target_id"])
            if pair not in existing and tuple(reversed(pair)) not in existing:
                brain.setdefault("links", []).append({"source": op["source_id"], "target": op["target_id"],
                                                       "rel": op.get("rel", "related"), "causal": False,
                                                       "confidence": op.get("confidence", 0.85), "added_by": "evolution"})
                changes.append(f"Edge: {op.get('source_label','?')} \u2192 {op.get('target_label','?')} ({op.get('rel','')})")
    return changes


def queue_risky_ops(brain, ops):
    from storage import add_question_safe
    queued = 0
    for op in ops:
        if op.get("auto_apply"): continue
        ot = op.get("op", "")
        if ot == "merge_nodes":
            q_text = f"Evolution: '{op.get('source_label','')}' and '{op.get('target_label','')}' may be duplicates ({op.get('confidence',0):.0%})"
        elif ot == "add_edge":
            q_text = f"Evolution: connect '{op.get('source_label','')}' \u2192 '{op.get('target_label','')}' ({op.get('rel','')}, {op.get('confidence',0):.0%})"
        else:
            continue
        q = {
            "id": f"evo_{ot}_{int(time.time())}_{queued}", "type": "yesno",
            "question": q_text, "why": op.get("reasoning", ""),
            "context": op.get("keep_id") or op.get("source_id", ""),
            "priority": "medium", "category": "evolution", "category_label": "EVOLVED",
            "from_doc": "evolution", "proposed_action": {"op": ot, "details": op},
            "confidence": op.get("confidence", 0.5)
        }
        if add_question_safe(brain, q, "evolution"):
            queued += 1
    return queued


async def run_evolution_cycle(intensity="medium") -> AsyncGenerator:
    global _evolution_running, _evolution_lock
    if _evolution_running:
        yield {"type": "skipped", "reason": "already running"}
        return
    import asyncio as _aio
    if _evolution_lock is None:
        _evolution_lock = _aio.Lock()
    async with _evolution_lock:
        _evolution_running = True
        try:
            async for event in _run_inner(intensity):
                yield event
        finally:
            _evolution_running = False


async def _run_inner(intensity) -> AsyncGenerator:
    start = time.time()
    yield {"type": "start", "intensity": intensity}
    brain = load_brain()
    snap_id = create_snapshot(brain)
    brain_before = copy.deepcopy(brain)
    yield {"type": "snapshot", "snap_id": snap_id, "nodes": len(brain.get("nodes", [])), "edges": len(brain.get("links", []))}

    # Priority scoring
    yield {"type": "step", "step": "scoring", "label": "Scoring node priorities"}
    priority = compute_priority_scores(brain)
    high_pri = [p for p in priority if p["score"] > 0.5]
    yield {"type": "step_done", "step": "scoring", "found": len(high_pri), "auto_apply": 0, "needs_review": 0,
           "top3": [p["label"] for p in priority[:3]]}

    all_ops = []

    # Enrich
    if intensity in ("medium", "full"):
        yield {"type": "step", "step": "enrich", "label": "Enriching thin descriptions"}
        enrich_ops = await task_enrich_nodes(brain, priority)
        all_ops.extend(enrich_ops)
        yield {"type": "step_done", "step": "enrich", "found": len(enrich_ops),
               "auto_apply": sum(1 for o in enrich_ops if o.get("auto_apply")), "needs_review": 0}

    # Links
    if intensity in ("medium", "full"):
        yield {"type": "step", "step": "links", "label": "Predicting missing connections"}
        link_ops = await task_link_prediction(brain)
        all_ops.extend(link_ops)
        safe_l = sum(1 for o in link_ops if o.get("auto_apply"))
        yield {"type": "step_done", "step": "links", "found": len(link_ops),
               "auto_apply": safe_l, "needs_review": len(link_ops) - safe_l}

    # Isolated nodes — semantic connection via Claude
    if intensity in ("medium", "full"):
        yield {"type": "step", "step": "isolated", "label": "Connecting isolated nodes"}
        iso_ops = await task_connect_isolated_nodes(brain)
        all_ops.extend(iso_ops)
        safe_iso = sum(1 for o in iso_ops if o.get("auto_apply"))
        yield {"type": "step_done", "step": "isolated", "found": len(iso_ops),
               "auto_apply": safe_iso, "needs_review": len(iso_ops) - safe_iso}

    # Merges (full only)
    if intensity == "full":
        yield {"type": "step", "step": "merges", "label": "Detecting duplicate nodes"}
        merge_ops = await task_find_merges(brain)
        all_ops.extend(merge_ops)
        yield {"type": "step_done", "step": "merges", "found": len(merge_ops),
               "auto_apply": 0, "needs_review": len(merge_ops)}

    # Web enrichment (full only)
    if intensity == "full":
        yield {"type": "step", "step": "web_enrich", "label": "Searching web for node enrichment"}
        try:
            web_ops = await task_web_enrich(brain, priority)
            web_q = web_ops[0]["questions_added"] if web_ops else 0
            yield {"type": "step_done", "step": "web_enrich",
                   "found": web_q, "auto_apply": 0, "needs_review": web_q}
        except Exception as e:
            print(f"[evolution] web_enrich failed: {e}")
            yield {"type": "step_done", "step": "web_enrich",
                   "found": 0, "auto_apply": 0, "needs_review": 0}

    # Apply
    yield {"type": "step", "step": "apply", "label": "Applying safe changes"}
    changes = apply_safe_ops(brain, all_ops)
    queued = queue_risky_ops(brain, all_ops)
    delta = compute_health_delta(brain_before, brain)

    if delta < -5.0:
        yield {"type": "rollback", "delta": round(delta, 2)}
        restored = json.loads((_snapshots_dir() / f"{snap_id}.json").read_text())
        brain = restored
    save_brain(brain)

    # Stale cycle detection
    global _consecutive_empty_cycles
    if len(changes) == 0 and len(all_ops) == 0:
        _consecutive_empty_cycles += 1
    else:
        _consecutive_empty_cycles = 0

    elapsed = round(time.time() - start, 1)
    try:
        with open(_evolution_log(), "a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "intensity": intensity,
                                 "ops": len(all_ops), "applied": len(changes), "queued": queued,
                                 "delta": round(delta, 2), "elapsed": elapsed,
                                 "changes": changes}) + "\n")
    except Exception:
        pass

    yield {"type": "complete", "elapsed": elapsed, "changes_applied": len(changes),
           "queued_for_review": queued, "health_delta": round(delta, 2),
           "changes": changes, "snap_id": snap_id, "rolled_back": delta < -5.0}

    # Emit stale signal if threshold reached
    if _consecutive_empty_cycles >= _STALE_THRESHOLD:
        yield {"type": "stale", "consecutive_empty": _consecutive_empty_cycles,
               "message": f"Brain well-covered \u2014 {_STALE_THRESHOLD} empty cycles"}
