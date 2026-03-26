"""
Brain Engine — Claude extraction and Q&A processing.
"""
import os
import json
import re
import time as _time
from typing import Optional
from datetime import datetime
from anthropic import Anthropic

from config import ANTHROPIC_API_KEY, MODEL_EXTRACTION, MODEL_FAST
from storage import find_node, add_node, add_link, touch_node

client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None

# ── DOCUMENT TYPE RULES ───────────────────────────────────────────
DOC_TYPE_RULES = {
    "prd": "Focus on: feature name, which surface it lives on, launch date, owner, success metrics, what problem it solves. Ask about: who owns it, what surface, what the target metric was.",
    "case_study": "Focus on: the problem, the solution built, outcome numbers (before/after), timeline, causal chains (what caused what). Ask about: causality between features and outcomes, exact numbers if vague.",
    "metrics_report": "Focus on: KPIs with values, before/after pairs, attribution (what drove the change). Ask about: what specifically caused the metric movement, which feature to credit.",
    "decision_log": "Focus on: the decision made, alternatives considered, rationale, who decided, what it unlocked. Ask about: what this decision enabled, who the final decision-maker was.",
    "org_chart": "Focus on: people, roles, reporting lines, who owns what product area. Ask about: what each person is directly responsible for.",
    "interview_notes": "Be conservative — only extract things stated as facts, not intentions. Generate more questions than usual since notes are often incomplete.",
    "timeline": "Focus on: features with launch dates, sequence of events. Ask about: which were causally linked vs just sequential (did X enable Y or just happen before it?).",
    "general": "Extract all node types equally. Ask about ownership, surfaces, and causal relationships.",
}


def call_claude(prompt: str, model: str = None, max_tokens: int = 2000, touchpoint: str = "unknown") -> str:
    if not client:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    model = model or MODEL_EXTRACTION
    start_ms = int(_time.time() * 1000)
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    duration = int(_time.time() * 1000) - start_ms
    try:
        from cost_tracker import record_call
        if hasattr(response, "usage") and response.usage:
            record_call(model=model, input_tokens=response.usage.input_tokens,
                        output_tokens=response.usage.output_tokens,
                        touchpoint=touchpoint, duration_ms=duration)
    except Exception:
        pass
    return response.content[0].text


def _repair_json(s: str) -> str:
    """Best-effort repair of truncated or slightly malformed JSON."""
    # Strip trailing commas before } or ]
    s = re.sub(r',\s*([}\]])', r'\1', s)
    # Count open/close braces and brackets, close any that are open
    open_braces = s.count('{') - s.count('}')
    open_brackets = s.count('[') - s.count(']')
    # Trim trailing partial key/value (after last complete value)
    # Remove any trailing partial string like  "key": "unfinis
    s = re.sub(r',\s*"[^"]*"?\s*:?\s*"?[^"]*$', '', s)
    s = s.rstrip().rstrip(',')
    s += ']' * max(0, open_brackets) + '}' * max(0, open_braces)
    # Recount after repair
    open_braces = s.count('{') - s.count('}')
    open_brackets = s.count('[') - s.count(']')
    s += ']' * max(0, open_brackets) + '}' * max(0, open_braces)
    return s


def call_claude_json(prompt: str, model: str = None) -> dict:
    raw = call_claude(prompt, model, max_tokens=4096)
    clean = re.sub(r'^```[a-z]*\n?', '', raw.strip())
    clean = re.sub(r'\n?```$', '', clean).strip()
    # Try direct parse
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        pass
    # Try extracting outermost { ... }
    match = re.search(r'\{.*', clean, re.DOTALL)
    if match:
        candidate = match.group()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Attempt repair (likely truncated)
            try:
                return json.loads(_repair_json(candidate))
            except json.JSONDecodeError:
                pass
    raise ValueError(f"Extraction failed: could not parse JSON from Claude response. First 500 chars: {raw[:500]}")


def find_existing_node(brain: dict, new_node: dict, threshold: float = 0.88):
    """Check if a semantically similar node already exists. Returns existing node or None."""
    new_label = new_node.get("label", "").strip().lower()
    new_type = new_node.get("type", "")

    # Fast path: exact label match + same type
    for n in brain.get("nodes", []):
        if n.get("label", "").strip().lower() == new_label and n.get("type") == new_type:
            return n

    # Semantic path: embedding similarity
    try:
        from embeddings import load_embedding_store, node_to_text, get_model
        import numpy as np

        matrix, node_ids = load_embedding_store()
        if matrix is None or len(node_ids) < 5:
            return None

        text = node_to_text(new_node)
        if not text.strip():
            return None

        model = get_model()
        q_vec = model.encode(text, normalize_embeddings=True)
        sims = matrix @ q_vec

        nodes_map = {n["id"]: n for n in brain.get("nodes", [])}
        top_idx = np.argsort(sims)[::-1][:5]

        for idx in top_idx:
            if float(sims[idx]) < threshold:
                break
            if idx >= len(node_ids):
                continue
            nid = node_ids[idx]
            existing = nodes_map.get(nid)
            if not existing:
                continue
            if existing.get("type") != new_type:
                continue
            return existing
    except Exception as e:
        print(f"[dedup] Embedding check failed: {e}")

    return None


def classify_document(filename: str, text: str) -> str:
    """Classify document type using fast model. Returns doc_type string."""
    try:
        prompt = (
            "What type is this document? Reply with exactly one word from: "
            "prd, case_study, metrics_report, decision_log, org_chart, "
            "interview_notes, timeline, general.\n\n"
            f"Document name: {filename}\n\n"
            f"First 2000 chars:\n{text[:2000]}"
        )
        raw = call_claude(prompt, model=MODEL_FAST, max_tokens=20)
        doc_type = raw.strip().lower().replace(" ", "_")
        if doc_type not in DOC_TYPE_RULES:
            doc_type = "general"
        print(f"[brain] classified '{filename}' as: {doc_type}")
        return doc_type
    except Exception as e:
        print(f"[brain] classification failed, defaulting to general: {e}")
        return "general"


def build_existing_context(brain: dict) -> str:
    nodes = brain.get("nodes", [])
    if not nodes:
        return "Brain is empty — this is the first document."
    sample = nodes[:20]
    summary = ", ".join(f"{n['label']}({n['type']})" for n in sample)
    if len(nodes) > 20:
        summary += f" ... and {len(nodes)-20} more"
    return f"Brain has {len(nodes)} nodes: {summary}"


def generate_document_summary(filename: str, text: str, doc_type: str) -> dict:
    """Generate a permanent summary and key facts for a document. Stored in document vault."""
    prompt = f"""You are creating a permanent summary record for a knowledge graph document.

Document: {filename}
Type: {doc_type}
Content:
{text[:6000]}

Return JSON:
{{
  "summary": "5-7 sentences. Fact-dense. Every sentence has a specific claim, number, person name, or feature name. No vague language.",
  "key_facts": ["specific fact 1 with numbers/names", "specific fact 2", "specific fact 3", "specific fact 4", "specific fact 5"],
  "main_features": ["feature names mentioned"],
  "main_people": ["person names mentioned"],
  "main_outcomes": ["outcomes with numbers if present"]
}}

Be precise. Include actual numbers, names, and metrics from the text."""

    try:
        result = call_claude_json(prompt, model=MODEL_FAST)
        return result
    except Exception as e:
        print(f"[brain] summary generation failed: {e}")
        return {"summary": f"Document: {filename} ({doc_type})", "key_facts": [], "main_features": [], "main_people": [], "main_outcomes": []}


def pre_check_existing_knowledge(filename: str, raw_text: str, brain: dict) -> dict:
    """Check what brain already knows about topics in this document."""
    if not brain.get('nodes'):
        return {"already_known": [], "contradictions": [], "genuinely_new": [], "skip_questions_about": []}

    # Build rich context of what brain already knows
    rich_context = []
    for n in brain['nodes']:
        connections = []
        for l in brain.get('links', []):
            sid = l.get('source', '')
            tid = l.get('target', '')
            if sid == n['id'] or tid == n['id']:
                other_id = tid if sid == n['id'] else sid
                other = next((x for x in brain['nodes'] if x['id'] == other_id), None)
                if other:
                    connections.append(f"{l['rel']} -> {other['label']}")

        node_summary = f"{n['label']} ({n['type']})"
        if n.get('where'):
            node_summary += f" on {n['where']}"
        if n.get('desc'):
            node_summary += f": {n['desc']}"
        if n.get('metrics'):
            metrics_str = ', '.join(f"{k}: {v}" for k, v in n['metrics'].items())
            node_summary += f" | metrics: {metrics_str}"
        if connections:
            node_summary += f" | connected: {'; '.join(connections[:4])}"
        rich_context.append(node_summary)

    answered = [
        f"Q: {h['question']} -> A: {h['answer']}"
        for h in brain.get('qa_history', [])[-20:]
    ]

    # Get document summaries from vault for richer context
    from document_store import get_summaries_for_context
    doc_summaries = get_summaries_for_context(limit=8)
    doc_context = ""
    if doc_summaries:
        doc_context = "\n\nPREVIOUSLY PROCESSED DOCUMENTS (summaries):\n" + "\n\n".join(doc_summaries[:6])

    prompt = f"""A new document is being added to a knowledge graph.
Check what the brain already knows about the topics in this document.

DOCUMENT NAME: {filename}
DOCUMENT PREVIEW (first 1500 chars):
{raw_text[:1500]}

WHAT THE BRAIN CURRENTLY KNOWS:
{chr(10).join(rich_context[:50])}{doc_context}

RECENTLY CONFIRMED ANSWERS:
{chr(10).join(answered) if answered else 'None yet'}

Analyze the document preview against what the brain knows.
Return a JSON object:
{{
  "already_known": ["fact or relationship that brain already has confirmed"],
  "contradictions": ["something in the new doc that conflicts with existing brain data"],
  "genuinely_new": ["important information in this doc NOT yet in the brain"],
  "skip_questions_about": ["topic or node name where brain already has enough info"]
}}

Be specific. Use actual node names and feature names.
Return ONLY the JSON object."""

    try:
        result = call_claude_json(prompt, model=MODEL_FAST)
        return result
    except Exception as e:
        print(f"[brain] pre_check failed: {e}")
        return {"already_known": [], "contradictions": [], "genuinely_new": [], "skip_questions_about": []}


EXTRACTION_PROMPT_TEMPLATE = """You are building a personal product knowledge graph from project documents.

The brain tracks:
- Features: things built on a platform (e.g. Quiz Galaxy, Digital KYC, Adhyayan)
- Surfaces: where features live (e.g. Student Dashboard, CMS, WhatsApp, Teacher Dashboard, Admin Portal, Landing Page, Gurukul)
- Outcomes: measurable results with real numbers (e.g. Conversion 5.7%→11.2%, Churn 20%→<5%)
- Decisions: key product/strategic calls with reasoning
- People: owners and stakeholders (first name or full name)
- Companies: organizational context (BrightCHAMPS, WheelsEye, Arth, etc.)

EXISTING BRAIN CONTEXT:
{existing}

DOCUMENT TYPE: {doc_type}
EXTRACTION FOCUS:
{extraction_focus}

EXISTING ENTITIES (do NOT create new nodes for these — use their exact IDs):
{seen_entities}

DOCUMENT: {filename}
---
{text}
---

WHAT BRAIN ALREADY KNOWS FROM THIS DOC'S TOPICS:
Already confirmed: {known_facts}
Contradictions found: {contradictions}
Genuinely new info: {new_info}

CRITICAL RULES FOR QUESTIONS:
- Do NOT ask about: {skip_topics}
- DO ask about: contradictions, genuinely new information, missing owners/surfaces
- If a fact is already confirmed in brain, do not ask for it again
- If the same feature appears in this doc and already exists in brain, ADD to the existing node — do not create a new one

Your task:
1. Extract nodes you are HIGHLY CONFIDENT about from this document
2. Extract links between nodes that are clearly stated
3. Generate 3-5 targeted questions for ambiguous relationships

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{
  "summary": "one sentence about what this document is about",
  "extracted": {{
    "nodes": [
      {{
        "id": "snake_case_id_max_25_chars",
        "label": "Short display name (max 20 chars)",
        "type": "Feature|Surface|Outcome|Decision|Person|Company",
        "where": "which surface it lives on (for Features), or empty string",
        "desc": "one clear sentence description",
        "company": "BrightCHAMPS|WheelsEye|Arth|unknown",
        "confidence": "high",
        "metrics": {{"metric name": "before → after or value"}}
      }}
    ],
    "links": [
      {{
        "source": "node_id",
        "target": "node_id",
        "rel": "short relationship label",
        "causal": false
      }}
    ]
  }},
  "questions": [
    {{
      "id": "q_unique_snake_case",
      "type": "choice|yesno|freetext",
      "question": "specific concrete question",
      "why": "what this answer adds to the graph (one sentence)",
      "context": "node_id this question relates to",
      "options": ["option1", "option2", "option3"]
    }}
  ]
}}

STRICT RULES:
- Node IDs: snake_case, max 25 chars, unique across the whole brain
- Prefix outcomes with o_: o_conversion_boost
- Prefix decisions with d_: d_zoom_over_100ms
- Prefix people with p_: p_suneet
- Do NOT duplicate nodes already in the existing brain
- Set causal:true ONLY for clear cause→effect relationships (not just sequence)
- metrics only for Outcome and Feature nodes with real numbers from the doc
- Max 5 questions — ask the most impactful ones first
- For choice questions, include 3-4 plausible options
- Return ONLY the JSON object"""


def build_extraction_prompt(filename: str, text: str, brain: dict, doc_type: str = "general", knowledge_check: dict = None) -> str:
    kc = knowledge_check or {}
    # Build seen entities lookup so Claude reuses existing IDs
    seen = {n["label"].lower(): n["id"] for n in brain.get("nodes", [])}
    seen_text = chr(10).join(f"{label} \u2192 {nid}" for label, nid in list(seen.items())[:30]) or "None yet"
    return EXTRACTION_PROMPT_TEMPLATE.format(
        existing=build_existing_context(brain),
        doc_type=doc_type,
        extraction_focus=DOC_TYPE_RULES.get(doc_type, DOC_TYPE_RULES["general"]),
        seen_entities=seen_text,
        filename=filename,
        text=text,
        known_facts=chr(10).join(kc.get('already_known', [])[:5]) or 'Unknown',
        contradictions=chr(10).join(kc.get('contradictions', [])[:3]) or 'None',
        new_info=chr(10).join(kc.get('genuinely_new', [])[:5]) or 'Unknown',
        skip_topics=', '.join(kc.get('skip_questions_about', [])) or 'nothing to skip',
    )


def filter_redundant_questions(candidate_questions: list, brain: dict) -> list:
    """Remove questions semantically equivalent to already-answered ones."""
    if not candidate_questions:
        return []

    qa_history = brain.get("qa_history", [])
    pending = brain.get("pending_questions", [])

    already_asked = []
    for h in qa_history:
        already_asked.append({"question": h.get("question", ""), "answer": h.get("answer", "")})
    for p in pending:
        already_asked.append({"question": p.get("question", ""), "answer": "pending"})

    if not already_asked:
        return candidate_questions

    candidates_text = "\n".join([
        f"{i+1}. {q.get('question', '')}" for i, q in enumerate(candidate_questions)
    ])
    answered_text = "\n".join([
        f"- Q: {a['question'][:100]} -> A: {a['answer'][:60]}" for a in already_asked[-40:]
    ])

    prompt = f"""A knowledge graph brain is about to ask these questions:

CANDIDATE QUESTIONS:
{candidates_text}

ALREADY ASKED AND ANSWERED (or pending):
{answered_text}

For each candidate question, decide if it is REDUNDANT.
A question is redundant if:
- It asks for information already answered (even if phrased differently)
- The answer can be inferred from existing answers
- It is asking the same thing with different words

Return a JSON array with one entry per candidate question, in order:
[
  {{"index": 1, "redundant": true, "reason": "Already answered: who owns Nano Skills -> Suneet"}},
  {{"index": 2, "redundant": false, "reason": "genuinely new"}}
]

Be aggressive — if 70%+ sure it is redundant, mark it redundant.
The user is frustrated by repeated questions."""

    try:
        result = call_claude_json(prompt, model=MODEL_FAST)
        if not isinstance(result, list):
            return candidate_questions

        redundant_indices = {r["index"] for r in result if r.get("redundant") is True}

        filtered = []
        for i, q in enumerate(candidate_questions):
            if (i + 1) not in redundant_indices:
                filtered.append(q)
            else:
                reason = next((r.get("reason", "") for r in result if r.get("index") == i + 1), "")
                print(f"[filter] dropped redundant Q: '{q.get('question','')[:60]}' — {reason}")

        removed = len(candidate_questions) - len(filtered)
        if removed:
            print(f"[filter] removed {removed} redundant question(s)")
        return filtered

    except Exception as e:
        print(f"[filter] redundancy check failed, keeping all: {e}")
        return candidate_questions


def extract_from_document(filename: str, text: str, brain: dict) -> dict:
    """Call Claude to extract nodes and questions from a document."""
    # Step 1: Classify document type (cheap, fast)
    doc_type = classify_document(filename, text)

    # Step 2: Check what brain already knows (cheap, fast)
    print(f"[brain] checking existing knowledge for '{filename}'...")
    knowledge_check = pre_check_existing_knowledge(filename, text, brain)

    already_known = knowledge_check.get('already_known', [])
    skip = knowledge_check.get('skip_questions_about', [])
    contradictions = knowledge_check.get('contradictions', [])

    if already_known:
        print(f"[brain] already know: {already_known[:2]}")
    if skip:
        print(f"[brain] skipping questions about: {skip}")
    if contradictions:
        print(f"[brain] contradictions found: {contradictions}")

    # Step 3: Extract with type-specific rules + knowledge context (Sonnet)
    prompt = build_extraction_prompt(filename, text, brain, doc_type, knowledge_check)
    result = call_claude_json(prompt, MODEL_EXTRACTION)

    # Validate structure
    result.setdefault("summary", "")
    result.setdefault("extracted", {"nodes": [], "links": []})
    result["extracted"].setdefault("nodes", [])
    result["extracted"].setdefault("links", [])
    result.setdefault("questions", [])

    result["doc_type"] = doc_type
    result["knowledge_check"] = knowledge_check

    # Semantic dedup: remove questions already answered (even if phrased differently)
    if result.get("questions"):
        result["questions"] = filter_redundant_questions(result["questions"], brain)

    # Generate and store document summary in vault
    from document_store import save_document_record
    from datetime import datetime as dt

    print(f"[brain] generating document summary for '{filename}'...")
    doc_summary = generate_document_summary(filename, text, doc_type)
    nodes_created_ids = [n["id"] for n in result.get("extracted", {}).get("nodes", [])]

    doc_record = {
        "filename": filename,
        "uploaded_at": dt.utcnow().isoformat() + "Z",
        "doc_type": doc_type,
        "char_count": len(text),
        "summary": doc_summary.get("summary", ""),
        "key_facts": doc_summary.get("key_facts", []),
        "main_features": doc_summary.get("main_features", []),
        "main_people": doc_summary.get("main_people", []),
        "main_outcomes": doc_summary.get("main_outcomes", []),
        "nodes_created": nodes_created_ids,
        "nodes_updated": [],
        "questions_generated": len(result.get("questions", [])),
        "questions_answered": 0,
        "knowledge_check": result.get("knowledge_check", {}),
    }
    save_document_record(doc_record)
    result["doc_summary"] = doc_summary
    result["doc_record"] = doc_record

    return result


def interpret_answer_with_claude(question: dict, answer: str, context_node: Optional[dict], brain: dict) -> list:
    """Use Claude (fast model) to interpret a Q&A answer into graph operations."""
    context_json = json.dumps(context_node, ensure_ascii=False) if context_node else "none"
    prompt = f"""You are updating a knowledge graph based on a user's answer.

Question asked: {question.get("question", "")}
User's answer: {answer}
Context node: {context_json}

Return a JSON array of graph operations to perform. Each operation is one of:

{{"op": "add_node", "id": "snake_case_id", "label": "Display Name", "type": "Feature|Surface|Outcome|Decision|Person|Company", "where": "", "desc": "one sentence", "company": ""}}
{{"op": "add_link", "source": "node_id", "target": "node_id", "rel": "relationship label", "causal": false}}
{{"op": "update_field", "node_id": "id", "field": "where|desc|company", "value": "new value"}}
{{"op": "update_metric", "node_id": "id", "name": "metric name", "value": "metric value"}}
{{"op": "mark_causal", "source": "node_id", "target": "node_id"}}

Rules:
- Return [] if the answer is a skip or "not sure"
- Return [] if you cannot determine a clear graph operation
- For person names: add_node with type Person, then add_link to context node with rel "owns"
- For surface answers: update_field where on context node, then add_node Surface + add_link "lives on"
- For yes/no causal questions: if yes, mark_causal between the two nodes mentioned
- For metric answers: update_metric on context node
- Return ONLY the JSON array, no explanation"""

    raw = call_claude(prompt, model=MODEL_FAST, max_tokens=500)
    clean = re.sub(r'^```[a-z]*\n?', '', raw.strip())
    clean = re.sub(r'\n?```$', '', clean).strip()
    try:
        result = json.loads(clean)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for v in result.values():
                if isinstance(v, list):
                    return v
        return []
    except json.JSONDecodeError:
        # Try extracting array
        match = re.search(r'\[.*\]', clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return []


def _apply_operations(brain: dict, operations: list) -> list:
    """Apply a list of Claude-interpreted operations to the brain graph."""
    updates = []
    if not isinstance(operations, list):
        return updates

    for op in operations:
        if not isinstance(op, dict) or "op" not in op:
            continue

        op_type = op["op"]

        if op_type == "add_node" and "id" in op and "type" in op:
            node_dict = {
                "id": op["id"],
                "label": op.get("label", op["id"]),
                "type": op["type"],
                "where": op.get("where", ""),
                "desc": op.get("desc", ""),
                "company": op.get("company", "unknown"),
                "confidence": "qa",
            }
            action = add_node(brain, node_dict)
            # Mark as confirmed now
            confirmed = find_node(brain, op["id"])
            if confirmed:
                confirmed["last_confirmed_at"] = datetime.utcnow().isoformat() + "Z"
            updates.append({"action": "added_node" if action == "added" else "updated_node",
                            "id": op["id"], "type": op["type"]})

        elif op_type == "add_link" and "source" in op and "target" in op and "rel" in op:
            link_dict = {
                "source": op["source"],
                "target": op["target"],
                "rel": op["rel"],
                "causal": op.get("causal", False),
            }
            action = add_link(brain, link_dict)
            if action == "added":
                updates.append({"action": "added_link", "source": op["source"],
                                "target": op["target"], "rel": op["rel"]})

        elif op_type == "update_field" and "node_id" in op and "field" in op:
            node = find_node(brain, op["node_id"])
            if node and op["field"] in ("where", "desc", "company", "label"):
                node[op["field"]] = op.get("value", "")
                node["last_confirmed_at"] = datetime.utcnow().isoformat() + "Z"
                updates.append({"action": "updated_field", "node": op["node_id"],
                                "field": op["field"], "value": op.get("value", "")})

        elif op_type == "update_metric" and "node_id" in op:
            node = find_node(brain, op["node_id"])
            if node:
                node.setdefault("metrics", {})[op.get("name", "metric")] = op.get("value", "")
                updates.append({"action": "updated_metric", "node": op["node_id"]})

        elif op_type == "mark_causal" and "source" in op and "target" in op:
            for link in brain.get("links", []):
                src = link["source"]
                tgt = link["target"]
                if (src == op["source"] and tgt == op["target"]) or \
                   (src == op["target"] and tgt == op["source"]):
                    link["causal"] = True
                    updates.append({"action": "marked_causal",
                                    "source": op["source"], "target": op["target"]})
                    break

    return updates


def _heuristic_answer_update(brain: dict, question: dict, answer: str) -> list:
    """Fallback: apply Q&A answer using keyword matching heuristics."""
    updates = []
    q_type = question.get("type", "freetext")
    context_id = question.get("context", "")
    q_text = question.get("question", "").lower()

    # ── YES/NO — typically causal edge confirmation ────────────────
    if q_type == "yesno":
        if answer == "Yes" and ("trigger" in q_text or "cause" in q_text or "enable" in q_text):
            if context_id and find_node(brain, context_id):
                node = find_node(brain, context_id)
                node.setdefault("confirmed_causal", True)
                updates.append({"action": "marked_causal", "node": context_id})
        return updates

    # ── CHOICE — surface placement ─────────────────────────────────
    if q_type == "choice":
        if "surface" in q_text or "where" in q_text or "lives" in q_text or "dashboard" in answer.lower():
            if context_id:
                node = find_node(brain, context_id)
                if node:
                    node["where"] = answer
                    updates.append({"action": "updated_where", "node": context_id, "value": answer})

            surface_id = re.sub(r'[^a-z0-9_]', '', answer.lower().replace(' ', '_'))[:25]
            if not find_node(brain, surface_id):
                add_node(brain, {
                    "id": surface_id, "label": answer, "type": "Surface",
                    "where": "", "desc": f"Platform surface: {answer}",
                    "company": "unknown", "confidence": "qa"
                })
                updates.append({"action": "added_node", "id": surface_id, "type": "Surface"})

            if context_id:
                result = add_link(brain, {
                    "source": context_id, "target": surface_id,
                    "rel": "lives on", "causal": False
                })
                if result == "added":
                    updates.append({"action": "added_link", "source": context_id, "target": surface_id})

        return updates

    # ── FREETEXT — person name, metric, or other ───────────────────
    if q_type == "freetext" and len(answer) > 1:
        if any(kw in q_text for kw in ("who", "owner", "built", "lead", "responsible")):
            person_id = "p_" + re.sub(r'[^a-z0-9_]', '', answer.lower().replace(' ', '_'))[:20]
            if not find_node(brain, person_id):
                add_node(brain, {
                    "id": person_id, "label": answer, "type": "Person",
                    "where": "", "desc": "Identified via Q&A",
                    "company": "unknown", "confidence": "qa"
                })
                updates.append({"action": "added_node", "id": person_id, "type": "Person"})

            if context_id:
                result = add_link(brain, {
                    "source": person_id, "target": context_id,
                    "rel": "owns", "causal": False
                })
                if result == "added":
                    updates.append({"action": "added_link", "source": person_id, "target": context_id, "rel": "owns"})

        elif any(kw in q_text for kw in ("metric", "number", "result", "outcome", "how much", "percentage")):
            if context_id:
                node = find_node(brain, context_id)
                if node:
                    node.setdefault("metrics", {})
                    node["metrics"]["Q&A confirmed"] = answer
                    updates.append({"action": "updated_metric", "node": context_id, "value": answer})

    return updates


def process_answer_update(brain: dict, question: dict, answer: str) -> list:
    """Apply a user's Q&A answer to the graph. Returns list of graph updates."""
    if answer in ("skip", "Not sure / skip", "Not sure", "None of these"):
        return [{"action": "skipped"}]

    context_id = question.get("context", "")
    context_node = find_node(brain, context_id) if context_id else None

    try:
        operations = interpret_answer_with_claude(question, answer, context_node, brain)
        if operations:
            updates = _apply_operations(brain, operations)
            if updates:
                print(f"[brain] claude interpreted answer → {len(updates)} graph updates")
                return updates
    except Exception as e:
        print(f"[brain] claude interpretation failed, using heuristic: {e}")

    return _heuristic_answer_update(brain, question, answer)


FOLLOWUP_PROMPT_TEMPLATE = """You are analyzing a product knowledge graph for gaps and unconnected information.

CURRENT GRAPH ({node_count} nodes, {link_count} links):
{node_summary}

DETECTED GAPS:
{gap_hints}

Generate 3-5 targeted questions to fill the most important gaps. Prioritize:
1. Features that have no owner (Person)
2. Features that have no surface (where they live)
3. Potential causal relationships between features and outcomes
4. Nodes that are completely disconnected from the graph

Return ONLY a JSON array of questions:
[
  {{
    "id": "q_rethink_unique_id",
    "type": "choice|yesno|freetext",
    "question": "specific question",
    "why": "what this fills in the graph",
    "context": "node_id this relates to",
    "options": ["option1", "option2"]
  }}
]

Rules:
- Max 5 questions
- Make questions concrete — name the nodes
- For choice questions, include 2-4 plausible options
- Return ONLY the JSON array"""


def _analyze_graph_gaps(nodes: list, links: list) -> tuple:
    """Build node summary and detect structural gaps. Returns (summary_str, gaps_str)."""
    node_lines = []
    for n in nodes[:30]:
        connected = sum(1 for l in links if l["source"] == n["id"] or l["target"] == n["id"])
        node_lines.append(
            f"- {n['label']} ({n['type']}, where={n.get('where','?')}, "
            f"company={n.get('company','?')}, connections={connected})"
        )

    orphans = [n for n in nodes if not any(
        l["source"] == n["id"] or l["target"] == n["id"] for l in links
    )]
    features_no_surface = [n for n in nodes if n["type"] == "Feature" and not n.get("where")]
    features_no_owner = [n for n in nodes if n["type"] == "Feature" and not any(
        l["target"] == n["id"] and any(
            p["id"] == l["source"] and p["type"] == "Person" for p in nodes
        ) for l in links
    )]
    causal_count = sum(1 for l in links if l.get("causal"))

    gaps = []
    if orphans:
        gaps.append(f"Orphan nodes (no connections): {', '.join(n['label'] for n in orphans[:5])}")
    if features_no_surface:
        gaps.append(f"Features without a surface: {', '.join(n['label'] for n in features_no_surface[:5])}")
    if features_no_owner:
        gaps.append(f"Features without an owner: {', '.join(n['label'] for n in features_no_owner[:5])}")
    if causal_count == 0:
        gaps.append("No causal links confirmed yet — are any features causally connected?")

    return "\n".join(node_lines), "\n".join(gaps) if gaps else "No obvious structural gaps."


def generate_followup_questions(brain: dict) -> list:
    """Analyze the graph for gaps and generate follow-up questions."""
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])
    if not nodes:
        return []

    node_summary, gap_hints = _analyze_graph_gaps(nodes, links)
    prompt = FOLLOWUP_PROMPT_TEMPLATE.format(
        node_count=len(nodes), link_count=len(links),
        node_summary=node_summary, gap_hints=gap_hints,
    )

    try:
        raw = call_claude(prompt, model=MODEL_FAST, max_tokens=800)
        clean = re.sub(r'^```[a-z]*\n?', '', raw.strip())
        clean = re.sub(r'\n?```$', '', clean).strip()
        result = json.loads(clean)
        if isinstance(result, list):
            print(f"[brain] rethink generated {len(result)} follow-up questions")
            return result
        return []
    except Exception as e:
        print(f"[brain] rethink failed: {e}")
        return []
