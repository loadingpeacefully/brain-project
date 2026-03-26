"""Structured node summary generator."""
import json
import time as _time
from datetime import datetime, timezone
from brain_engine import call_claude, MODEL_FAST
from storage import find_node


def build_node_context(node, brain):
    links = brain.get("links", [])
    nm = {n["id"]: n for n in brain.get("nodes", [])}
    conns = []
    for l in links:
        src, tgt = l.get("source", ""), l.get("target", "")
        if src == node["id"] and nm.get(tgt):
            o = nm[tgt]
            conns.append({"dir": "\u2192", "rel": l.get("rel", "related"), "label": o["label"],
                          "type": o.get("type", ""), "desc": (o.get("desc", "") or "")[:60],
                          "causal": l.get("causal", False)})
        elif tgt == node["id"] and nm.get(src):
            o = nm[src]
            conns.append({"dir": "\u2190", "rel": l.get("rel", "related"), "label": o["label"],
                          "type": o.get("type", ""), "desc": (o.get("desc", "") or "")[:60],
                          "causal": l.get("causal", False)})
    return conns


def generate_node_summary(node, brain, model=MODEL_FAST):
    conns = build_node_context(node, brain)
    conn_text = ""
    for c in conns[:15]:
        conn_text += f"\n  {c['dir']} {c['rel']}: {c['label']} ({c['type']})" + (" [CAUSAL]" if c["causal"] else "")
        if c["desc"]:
            conn_text += f"\n    \u2514 {c['desc']}"
    if not conn_text:
        conn_text = "\n  (no connections)"

    prompt = (f"Generate a structured summary for this knowledge graph node.\n\n"
              f"NODE: {node.get('label','')} ({node.get('type','')})\n"
              f"WHERE: {node.get('where','unknown')}\n"
              f"COMPANY: {node.get('company','unknown')}\n"
              f"DESCRIPTION: {node.get('desc','') or '(none)'}\n"
              f"METRICS: {json.dumps(node.get('metrics',{})) if node.get('metrics') else 'none'}\n"
              f"CONFIDENCE: {node.get('confidence_score',0.3):.0%}\n"
              f"CONNECTIONS ({len(conns)} total):{conn_text}\n\n"
              f"Return JSON:\n"
              f'{{"role":"what this node IS (1 sentence, specific)",'
              f'"context":"where it fits in product/company (1 sentence)",'
              f'"ownership":"who owns/built this (person+company if known)",'
              f'"impact":"measurable outcome or \'impact not documented\'",'
              f'"connections_narrative":"2-3 sentences about what edges reveal",'
              f'"open_gaps":["1-3 specific missing facts"],'
              f'"confidence_note":"why confidence is high/medium/low"}}\n'
              f"Be specific \u2014 use actual names, products, numbers.")

    try:
        import re
        raw = call_claude(prompt, model=model, max_tokens=600, touchpoint="node_summary_gen")
        clean = re.sub(r'^```[a-z]*\n?', '', raw.strip())
        clean = re.sub(r'\n?```$', '', clean).strip()
        summary = json.loads(clean)
        existing = node.get("_summary", {})
        summary["generated_at"] = datetime.now(timezone.utc).isoformat()
        summary["version"] = existing.get("version", 0) + 1
        summary["node_id"] = node["id"]
        summary["node_label"] = node["label"]
        return summary
    except Exception as e:
        return {
            "role": node.get("desc", "") or "No description available",
            "context": f"{node.get('type','')} in {node.get('company','unknown')}",
            "ownership": "Unknown", "impact": "Not documented",
            "connections_narrative": f"Connected to {len(conns)} nodes.",
            "open_gaps": ["Needs enrichment"], "confidence_note": f"{node.get('confidence_score',0.3):.0%}",
            "generated_at": datetime.now(timezone.utc).isoformat(), "version": 1,
            "node_id": node["id"], "node_label": node["label"], "error": str(e)
        }
