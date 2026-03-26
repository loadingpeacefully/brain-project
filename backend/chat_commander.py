"""
Chat Commander — classifies chat messages as query/command/plan
and dispatches accordingly. Commands get preview→confirm flow.
"""
import json
import time
from brain_engine import call_claude, call_claude_json, MODEL_FAST, MODEL_EXTRACTION
from storage import find_node


def _find_node_fuzzy(brain, node_id_or_label):
    """Try exact ID, then case-insensitive ID, then label match."""
    exact = find_node(brain, node_id_or_label)
    if exact:
        return exact
    nid_lower = node_id_or_label.lower().replace(" ", "_")
    for n in brain.get("nodes", []):
        if n["id"].lower() == nid_lower:
            return n
    label_lower = node_id_or_label.lower()
    for n in brain.get("nodes", []):
        if n.get("label", "").lower() == label_lower:
            return n
    matches = [n for n in brain.get("nodes", [])
               if label_lower in n.get("label", "").lower() or label_lower in n["id"].lower()]
    if len(matches) == 1:
        return matches[0]
    return None


def classify_intent(message: str, brain: dict) -> dict:
    command_kw = ["merge", "combine", "delete", "remove", "rename", "add link",
                  "connect", "add edge", "update", "change", "fix", "set",
                  "make", "re-point", "absorb"]
    plan_kw = ["audit", "find gaps", "what questions", "review", "analyze",
               "generate questions", "deep dive", "what should i answer"]

    msg_lower = message.lower()
    hint = "query"
    for kw in command_kw:
        if kw in msg_lower:
            hint = "command"
            break
    if hint != "command":
        for kw in plan_kw:
            if kw in msg_lower:
                hint = "plan"
                break

    nodes_text = "\n".join([f'{n["id"]}: {n["label"]} ({n.get("type","")})' for n in brain.get("nodes", [])])

    prompt = f"""Classify this message to a knowledge graph assistant.

MESSAGE: "{message}"
HINT: {hint}
GRAPH NODES:
{nodes_text}

Classify as:
- "query": user wants information
- "command": user wants to modify the graph (merge, delete, add, rename, connect)
- "plan": user wants to generate audit questions for a topic
- "ambiguous": unclear

For command, extract operations as a JSON array. Each operation is an object with "op" key:
  {{"op":"merge_nodes","keep_id":"id","absorb_ids":["id"],"reason":"why"}}
  {{"op":"add_link","source_id":"id","target_id":"id","rel":"rel","causal":false}}
  {{"op":"update_field","node_id":"id","field":"desc|where|company|label","new_value":"val"}}
  {{"op":"delete_node","node_id":"id","reason":"why"}}
  {{"op":"rename_node","node_id":"id","new_label":"label"}}

Use exact node IDs from the list above.

Return JSON:
{{"intent":"query|command|plan|ambiguous","confidence":"high|medium|low","operations":[],"topic":"for plan","reasoning":"one sentence"}}"""

    try:
        raw = call_claude(prompt, model=MODEL_FAST, max_tokens=600)
        import re
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(clean)
        if result.get("confidence") == "low":
            result["intent"] = "ambiguous"
        return result
    except Exception as e:
        return {"intent": hint, "confidence": "low", "operations": [], "topic": "", "reasoning": str(e)}


def _normalize_op(op):
    """Normalize operation to {op, ...flat fields} format."""
    if op.get("op"):
        return op.get("op"), op
    for key in ("merge_nodes", "add_link", "delete_node", "rename_node", "update_field"):
        if key in op:
            d = op[key] if isinstance(op[key], dict) else op
            d["op"] = key
            return key, d
    return "", op


def preview_command(operations: list, brain: dict) -> dict:
    lines = []
    is_destructive = False
    affected_ids = []

    for raw_op in operations:
        ot, op = _normalize_op(raw_op)

        if ot == "merge_nodes":
            keep = _find_node_fuzzy(brain, op.get("keep_id", ""))
            absorb_ids = op.get("absorb_ids", [])
            absorb_nodes = [_find_node_fuzzy(brain, a) for a in absorb_ids]
            real_absorb_ids = [n["id"] for n in absorb_nodes if n]
            absorb_labels = [n["label"] for n in absorb_nodes if n]
            keep_label = keep["label"] if keep else op.get("keep_id", "?")
            edges = sum(1 for l in brain.get("links", [])
                        if l.get("source") in real_absorb_ids or l.get("target") in real_absorb_ids)
            lines.append(f"\u2715 Remove: {', '.join(absorb_labels)} ({edges} connections)")
            lines.append(f"\u2192 Re-point all edges to: {keep_label}")
            lines.append(f"\u270E Merge descriptions into: {keep_label}")
            is_destructive = True
            affected_ids.extend([(keep["id"] if keep else "")] + real_absorb_ids)

        elif ot == "add_link":
            src = _find_node_fuzzy(brain, op.get("source_id", "") or op.get("source", ""))
            tgt = _find_node_fuzzy(brain, op.get("target_id", "") or op.get("target", ""))
            sl = src["label"] if src else op.get("source_id", "?")
            tl = tgt["label"] if tgt else op.get("target_id", "?")
            causal = " (causal)" if op.get("causal") else ""
            lines.append(f"+ Add edge: {sl} \u2192 [{op.get('rel', 'related')}] \u2192 {tl}{causal}")
            affected_ids.extend([(src["id"] if src else ""), (tgt["id"] if tgt else "")])

        elif ot == "delete_node":
            node = _find_node_fuzzy(brain, op.get("node_id", ""))
            label = node["label"] if node else op.get("node_id", "?")
            lines.append(f"\u2715 Delete node: {label}")
            lines.append(f"  Reason: {op.get('reason', 'user requested')}")
            is_destructive = True
            affected_ids.append(node["id"] if node else op.get("node_id", ""))

        elif ot == "rename_node":
            node = _find_node_fuzzy(brain, op.get("node_id", ""))
            old = node["label"] if node else op.get("node_id", "?")
            lines.append(f"\u270E Rename: '{old}' \u2192 '{op.get('new_label', '')}'")
            affected_ids.append(node["id"] if node else op.get("node_id", ""))

        elif ot == "update_field":
            node = find_node(brain, op.get("node_id", ""))
            label = node["label"] if node else op.get("node_id", "?")
            lines.append(f"\u270E Update {op.get('field', '')} on '{label}': {str(op.get('new_value', ''))[:60]}")
            affected_ids.append(op.get("node_id", ""))

    return {"preview_lines": lines, "is_destructive": is_destructive,
            "affected_node_ids": list(set(affected_ids))}


def execute_command(operations: list, brain: dict) -> dict:
    changes = []
    affected_ids = []
    FIELD_WHITELIST = {"desc", "where", "company", "label"}

    for raw_op in operations:
        ot, op = _normalize_op(raw_op)

        if ot == "merge_nodes":
            keep_id = op.get("keep_id", "")
            keep_node = _find_node_fuzzy(brain, keep_id)
            if not keep_node:
                changes.append(f"Skipped merge: node '{keep_id}' not found")
                continue
            keep_id = keep_node["id"]  # use canonical ID
            for aid in op.get("absorb_ids", []):
                an = _find_node_fuzzy(brain, aid)
                if not an:
                    continue
                if an.get("desc") and len(an["desc"]) > len(keep_node.get("desc", "")):
                    keep_node["desc"] = an["desc"] + " " + keep_node.get("desc", "")
                for k, v in an.get("metrics", {}).items():
                    keep_node.setdefault("metrics", {})[k] = keep_node["metrics"].get(k, v)
                for link in brain.get("links", []):
                    if link.get("source") == aid:
                        link["source"] = keep_id
                    if link.get("target") == aid:
                        link["target"] = keep_id
                brain["nodes"] = [n for n in brain["nodes"] if n["id"] != aid]
                changes.append(f"Merged {an['label']} into {keep_node['label']}")
            brain["links"] = [l for l in brain.get("links", []) if l.get("source") != l.get("target")]
            seen = set()
            brain["links"] = [l for l in brain["links"]
                              if (l.get("source"), l.get("target")) not in seen
                              and not seen.add((l.get("source"), l.get("target")))]
            affected_ids.append(keep_id)

        elif ot == "add_link":
            raw_sid = op.get("source_id", "") or op.get("source", "")
            raw_tid = op.get("target_id", "") or op.get("target", "")
            src_node = _find_node_fuzzy(brain, raw_sid)
            tgt_node = _find_node_fuzzy(brain, raw_tid)
            if not src_node or not tgt_node:
                changes.append(f"Skipped: nodes not found ({raw_sid}, {raw_tid})")
                continue
            sid, tid = src_node["id"], tgt_node["id"]
            existing = any(l.get("source") == sid and l.get("target") == tid for l in brain.get("links", []))
            if not existing:
                brain.setdefault("links", []).append({
                    "source": sid, "target": tid,
                    "rel": op.get("rel", "related"), "causal": op.get("causal", False)
                })
                changes.append(f"Added edge: {src_node['label']} \u2192 {tgt_node['label']} ({op.get('rel','related')})")
            else:
                changes.append(f"Edge already exists: {src_node['label']} \u2192 {tgt_node['label']}")
            affected_ids.extend([sid, tid])

        elif ot == "delete_node":
            nid = op.get("node_id", "")
            node = _find_node_fuzzy(brain, nid)
            if node:
                brain["nodes"] = [n for n in brain["nodes"] if n["id"] != nid]
                brain["links"] = [l for l in brain.get("links", [])
                                  if l.get("source") != nid and l.get("target") != nid]
                changes.append(f"Deleted node: {node['label']}")

        elif ot == "rename_node":
            node = _find_node_fuzzy(brain, op.get("node_id", ""))
            if node and op.get("new_label"):
                old = node["label"]
                node["label"] = op["new_label"]
                changes.append(f"Renamed: {old} \u2192 {op['new_label']}")
                affected_ids.append(op["node_id"])

        elif ot == "update_field":
            field = op.get("field", "")
            if field not in FIELD_WHITELIST:
                changes.append(f"Blocked: field '{field}' not in whitelist")
                continue
            node = _find_node_fuzzy(brain, op.get("node_id", ""))
            if node and op.get("new_value"):
                node[field] = op["new_value"]
                changes.append(f"Updated {field} on {node.get('label', '')}")
                affected_ids.append(node["id"])

    affected_nodes = []
    for nid in set(affected_ids):
        n = find_node(brain, nid)
        if n:
            affected_nodes.append({"id": n["id"], "label": n["label"], "type": n.get("type", "")})

    return {"success": True, "changes": changes, "affected_nodes": affected_nodes}


def generate_plan_questions(topic: str, brain: dict) -> list:
    from brain_query import find_entry_nodes, traverse_subgraph, serialise_subgraph

    entry_nodes = find_entry_nodes(brain, topic)
    if not entry_nodes:
        return []

    subgraph = traverse_subgraph(brain, [n["id"] for n in entry_nodes], max_nodes=20)
    subgraph_text = serialise_subgraph(subgraph)

    prompt = f"""You are auditing a knowledge graph section for a PM.
Topic: "{topic}"

Relevant graph section:
{subgraph_text}

Generate 4-8 specific, answerable questions that would improve this section.
Focus on: missing owners, unclear causality, unconnected outcomes, ambiguous decisions.

Return JSON array:
[{{"question":"specific question","why":"why this matters","type":"yesno|choice|freetext","options":[],"priority":"high|medium|low"}}]"""

    try:
        result = call_claude_json(prompt, model=MODEL_EXTRACTION)
        if not isinstance(result, list):
            return []
        questions = []
        for i, q in enumerate(result[:8]):
            questions.append({
                "id": f"plan_{int(time.time())}_{i}",
                "type": q.get("type", "freetext"),
                "options": q.get("options", []),
                "question": q.get("question", ""),
                "why": q.get("why", ""),
                "context": entry_nodes[0]["id"] if entry_nodes else "",
                "priority": q.get("priority", "medium"),
                "category": "plan", "category_label": "PLAN",
                "from_doc": "chat_plan"
            })
        return questions
    except Exception as e:
        print(f"[commander] plan gen failed: {e}")
        return []


def _extract_operations_focused(message: str, brain: dict) -> list:
    """Focused extraction when we know it's a command but got no ops."""
    msg_lower = message.lower()
    candidates = []
    for n in brain.get("nodes", []):
        words = n["label"].lower().split()
        if any(w in msg_lower for w in words if len(w) > 3):
            candidates.append({"id": n["id"], "label": n["label"], "type": n.get("type", "")})

    prompt = f"""Extract the graph operation from this command.

COMMAND: "{message}"

CANDIDATE NODES (names appear in the command):
{json.dumps(candidates[:15], indent=1)}

Identify exactly which nodes and what operation. Return JSON:
{{
  "operations": [
    {{"op":"merge_nodes","keep_id":"id_to_keep","absorb_ids":["id_to_remove"],"reason":"why"}}
  ]
}}

For merge: keep_id = the more complete node, absorb_ids = nodes to remove.
Match IDs exactly from candidates. Return {{"operations":[]}} if unclear."""

    try:
        raw = call_claude(prompt, model=MODEL_EXTRACTION, max_tokens=400)
        import re
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(clean)
        return result.get("operations", [])
    except Exception as e:
        print(f"[commander] focused extraction failed: {e}")
        return []


def handle_chat_command(message: str, brain: dict) -> dict:
    print(f"[commander] handling: '{message[:60]}'")
    classification = classify_intent(message, brain)
    intent = classification.get("intent", "query")
    operations = classification.get("operations", [])
    topic = classification.get("topic", "")
    print(f"[commander] classified: intent={intent}, ops={len(operations)}")

    # Imperative pattern override — only match actual commands, not questions
    import re as _re
    IMPERATIVE_PATTERNS = [
        r'^\s*merge\b', r'^\s*delete\b', r'^\s*remove\b', r'^\s*rename\b',
        r'^\s*add\s+(a\s+)?(link|edge|connection)\b',
        r'^\s*connect\s+\w+\s+to\b',
        r'(merge|combine)\s+.+\s+(and|with)\s+',
        r'they\s+are\s+the\s+same\s+(person|thing|node|entity)',
    ]
    is_imperative = any(_re.search(p, message.strip(), _re.IGNORECASE) for p in IMPERATIVE_PATTERNS)
    if is_imperative and intent in ("query", "ambiguous"):
        intent = "command"
        print("[commander] imperative override to command")
    elif not is_imperative and intent == "command":
        # Claude said command but no imperative phrasing — verify
        ops = classification.get("operations", [])
        if not ops:
            intent = "query"
            print("[commander] no imperative + no ops → query")

    # If command but no operations, try focused extraction
    if intent == "command" and not operations:
        print("[commander] no ops from classifier, trying focused extraction")
        operations = _extract_operations_focused(message, brain)
        print(f"[commander] focused extraction got {len(operations)} ops")

    if intent == "command" and operations:
        preview = preview_command(operations, brain)
        return {"response_type": "command_preview", "intent": "command",
                "message": message, "preview": preview,
                "operations": operations, "reasoning": classification.get("reasoning", "")}

    elif intent == "plan" or (topic and intent in ("plan", "ambiguous")):
        topic = topic or message
        questions = generate_plan_questions(topic, brain)
        if questions:
            brain.setdefault("pending_questions", []).extend(questions)
            return {"response_type": "plan_summary", "intent": "plan", "topic": topic,
                    "questions_added": len(questions),
                    "question_previews": [q["question"] for q in questions[:4]],
                    "all_questions": questions}
        return {"response_type": "plan_summary", "intent": "plan", "topic": topic,
                "questions_added": 0, "question_previews": [],
                "message": "No targeted questions found for this topic."}

    return {"response_type": "query", "intent": "query"}
