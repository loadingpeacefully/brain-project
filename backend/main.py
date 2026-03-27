"""
Brain - Personal Product Knowledge Graph
FastAPI backend server
"""
import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Add backend dir to path
sys.path.insert(0, str(Path(__file__).parent))

from config import ANTHROPIC_API_KEY, BRAIN_FILE, MODEL_EXTRACTION, MAX_DOC_CHARS
from storage import (load_brain, load_brain_safe, save_brain, get_stats, add_node, add_link,
                      find_node, log_session, queue_question, log_answered_question,
                      load_registry, save_registry, create_brain, set_active_brain, get_active_brain_id)
from document_parser import parse_document
from brain_engine import extract_from_document, process_answer_update, generate_followup_questions, find_existing_node

app = FastAPI(title="Brain API", version="1.0.0")

# Hebbian edge decay scheduler
from apscheduler.schedulers.background import BackgroundScheduler
_scheduler = BackgroundScheduler()

def _run_edge_decay():
    from storage import load_brain, save_brain, decay_edge, EDGE_PRUNE_THRESHOLD, EDGE_PRUNE_MIN_AGE_DAYS
    from datetime import timezone
    brain = load_brain()
    links = brain.get("links", [])
    now = datetime.now(timezone.utc)
    decayed, pruned, graduated, kept = 0, 0, 0, []
    for link in links:
        la = link.get("last_accessed") or link.get("created_at", "")
        days_idle = 0
        if la:
            try:
                dt = datetime.fromisoformat(la.replace("Z", ""))
                if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                days_idle = (now - dt).total_seconds() / 86400
            except Exception: pass
        if link.get("confirmed") or days_idle < 1:
            kept.append(link); continue
        old_w = link.get("weight", 1.0)
        new_w = decay_edge(link, days_idle)
        link["weight"] = new_w
        if new_w < old_w - 0.001: decayed += 1
        ca = link.get("created_at", "")
        age = days_idle
        if ca:
            try:
                ct = datetime.fromisoformat(ca.replace("Z", ""))
                if ct.tzinfo is None: ct = ct.replace(tzinfo=timezone.utc)
                age = (now - ct).total_seconds() / 86400
            except Exception: pass
        if new_w < EDGE_PRUNE_THRESHOLD and age >= EDGE_PRUNE_MIN_AGE_DAYS and not link.get("causal") and not link.get("confirmed"):
            pruned += 1; continue
        if link.get("memory_type") == "episodic" and link.get("access_count", 0) >= 5:
            link["memory_type"] = "semantic"; graduated += 1
        kept.append(link)
    brain["links"] = kept
    if decayed or pruned or graduated: save_brain(brain)
    print(f"[hebbian] Decay: {decayed} decayed, {pruned} pruned, {graduated} graduated")
    return {"decayed": decayed, "pruned": pruned, "graduated": graduated}

@app.on_event("startup")
async def startup_event():
    _scheduler.add_job(_run_edge_decay, 'interval', hours=6, id='edge_decay', replace_existing=True, max_instances=1)
    _scheduler.start()
    print("[hebbian] Edge decay scheduler started (every 6 hours)")

@app.on_event("shutdown")
async def shutdown_event():
    _scheduler.shutdown(wait=False)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve frontend static files
frontend_dir = Path(__file__).parent.parent / "frontend"
if frontend_dir.exists():
    app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")


# ── ROUTES ────────────────────────────────────────────────────────

@app.get("/")
async def root():
    index = frontend_dir / "index.html"
    if index.exists():
        response = FileResponse(str(index))
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response
    return HTMLResponse("<h1>Brain API running. Put frontend/index.html to serve the UI.</h1>")


@app.get("/api/health")
def health():
    brain = load_brain_safe()
    return {
        "status": "ok",
        "api_key_set": bool(ANTHROPIC_API_KEY),
        "stats": get_stats(brain)
    }


@app.get("/api/brain")
def get_brain_state():
    return load_brain_safe()


@app.get("/api/brain/stats")
def brain_statistics():
    brain = load_brain_safe()
    stats = get_stats(brain)
    stats["health_score"] = brain.get("health", {}).get("score", None)
    return stats


@app.delete("/api/brain/reset")
def reset_brain():
    empty = {
        "nodes": [], "links": [], "sessions": [],
        "pending_questions": [],
        "meta": {"created_at": None, "last_updated": None, "version": "1.0"}
    }
    save_brain(empty)
    return {"ok": True, "message": "Brain reset to empty state"}


def _process_upload(filename: str, text: str, brain: dict) -> dict:
    """Parse, extract, and merge a document into the brain. Returns response dict."""
    result = extract_from_document(filename, text, brain)

    nodes_added_ids = []
    nodes_updated_ids = []
    links_added = 0
    id_remap = {}

    for node in result.get("extracted", {}).get("nodes", []):
        node["source_doc"] = filename
        existing = find_existing_node(brain, node)
        if existing:
            # Enrich existing node instead of creating duplicate
            if not existing.get("desc") and node.get("desc"):
                existing["desc"] = node["desc"]
            if node.get("metrics"):
                existing.setdefault("metrics", {}).update(node.get("metrics", {}))
            existing["confidence_score"] = min(1.0, existing.get("confidence_score", 0.3) + 0.05)
            src = node.get("source_doc", "")
            if src and src not in existing.get("source_docs", []):
                existing.setdefault("source_docs", []).append(src)
            from storage import touch_node
            touch_node(existing)
            id_remap[node["id"]] = existing["id"]
            nodes_updated_ids.append(existing["id"])
            print(f"[extraction] Deduped '{node['label']}' -> existing '{existing['label']}'")
        else:
            action = add_node(brain, node)
            id_remap[node["id"]] = node["id"]
            if action == "added":
                nodes_added_ids.append(node["id"])
            else:
                nodes_updated_ids.append(node["id"])

    for link in result.get("extracted", {}).get("links", []):
        link["source"] = id_remap.get(link.get("source", ""), link.get("source", ""))
        link["target"] = id_remap.get(link.get("target", ""), link.get("target", ""))
        if link["source"] != link["target"] and add_link(brain, link) == "added":
            links_added += 1

    # Update document record with actual created/updated split
    doc_record = result.get("doc_record")
    if doc_record:
        doc_record["nodes_created"] = nodes_added_ids
        doc_record["nodes_updated"] = nodes_updated_ids
        from document_store import save_document_record
        save_document_record(doc_record)

    questions = result.get("questions", [])
    existing_q_texts = {
        q.get("question", "").strip().lower()
        for q in brain.get("pending_questions", [])
    }
    for q in questions:
        q_text = q.get("question", "").strip().lower()
        if q_text not in existing_q_texts:
            queue_question(brain, q, filename)
            existing_q_texts.add(q_text)

    log_session(brain, filename, len(nodes_added_ids), links_added, len(questions), 0)
    save_brain(brain)

    return {
        "filename": filename,
        "summary": result.get("summary", ""),
        "nodes_added": len(nodes_added_ids),
        "links_added": links_added,
        "questions": questions,
        "stats": get_stats(brain),
        "doc_summary": result.get("doc_summary", {}),
        "doc_record": {
            "filename": filename,
            "summary": result.get("doc_summary", {}).get("summary", ""),
            "key_facts": result.get("doc_summary", {}).get("key_facts", []),
        },
        "doc_type": result.get("doc_type", "general"),
    }


@app.post("/api/upload")
async def upload_document(file: UploadFile = File(...)):
    """Upload a document, extract nodes/links, return questions."""
    if not ANTHROPIC_API_KEY:
        raise HTTPException(500, "ANTHROPIC_API_KEY not configured. Add it to your .env file.")

    content = await file.read()
    filename = file.filename

    # Check for duplicate
    from document_store import get_file_hash, check_duplicate, audit_document_record, save_document_record as save_doc_rec
    content_hash = get_file_hash(content)
    duplicate = check_duplicate(filename, content_hash)

    if duplicate:
        existing_record = duplicate["record"]
        match_type = duplicate["match"]
        brain = load_brain()
        audit = audit_document_record(existing_record, brain)
        gaps = audit.get("gaps", [])
        fixed = audit.get("fixed", [])

        if fixed:
            save_brain(brain)

        regenerated = False
        if "missing summary" in gaps or "missing key facts" in gaps:
            try:
                text = parse_document(filename, content)
                text = text[:MAX_DOC_CHARS]
                doc_type = existing_record.get("doc_type", "general")
                from brain_engine import generate_document_summary
                doc_summary = generate_document_summary(filename, text, doc_type)
                existing_record["summary"] = doc_summary.get("summary", "")
                existing_record["key_facts"] = doc_summary.get("key_facts", [])
                existing_record["main_features"] = doc_summary.get("main_features", [])
                existing_record["main_people"] = doc_summary.get("main_people", [])
                save_doc_rec(existing_record)
                gaps = [g for g in gaps if "summary" not in g and "facts" not in g]
                fixed.append("regenerated summary and key facts")
                regenerated = True
            except Exception as e:
                print(f"[upload] summary regen failed: {e}")

        if audit["healthy"] and not regenerated:
            message = "Document already in brain — everything looks complete."
            status = "duplicate_clean"
        else:
            parts = []
            if fixed:
                parts.append("Fixed: " + ", ".join(fixed))
            if gaps:
                parts.append("Still missing: " + ", ".join(gaps))
            message = " · ".join(parts) if parts else "Document updated."
            status = "duplicate_updated"

        return JSONResponse({
            "filename": filename,
            "status": status,
            "duplicate": True,
            "match_type": match_type,
            "message": message,
            "gaps_found": gaps,
            "fixed": fixed,
            "nodes_added": 0,
            "links_added": 0,
            "questions": [],
            "stats": get_stats(load_brain_safe()),
            "doc_summary": {
                "summary": existing_record.get("summary", ""),
                "key_facts": existing_record.get("key_facts", []),
            },
        })

    # Not a duplicate — normal upload
    try:
        text = parse_document(filename, content)
    except Exception as e:
        raise HTTPException(400, f"Could not parse {filename}: {e}")

    if len(text) > MAX_DOC_CHARS:
        text = text[:MAX_DOC_CHARS] + "\n\n[Document truncated]"

    brain = load_brain()

    try:
        result = _process_upload(filename, text, brain)
        # Store content hash in doc record
        from document_store import get_document_record as get_doc_rec
        doc_rec = get_doc_rec(filename)
        if doc_rec:
            doc_rec["content_hash"] = content_hash
            save_doc_rec(doc_rec)
        return result
    except Exception as e:
        raise HTTPException(500, f"Extraction failed: {e}")


@app.post("/api/upload-batch")
async def upload_batch(files: list[UploadFile] = File(...)):
    """Upload multiple files. 50MB combined limit."""
    MAX_COMBINED_BYTES = 50 * 1024 * 1024
    file_data = []
    total_bytes = 0

    for file in files:
        content = await file.read()
        total_bytes += len(content)
        if total_bytes > MAX_COMBINED_BYTES:
            raise HTTPException(413, f"Combined size exceeds 50MB. Processed {len(file_data)} files before limit.")
        file_data.append({"filename": file.filename, "content": content})

    results = []
    for fd in file_data:
        try:
            from io import BytesIO
            fake = UploadFile(filename=fd["filename"], file=BytesIO(fd["content"]))
            resp = await upload_document(fake)
            if hasattr(resp, "body"):
                import json as jlib
                results.append(jlib.loads(resp.body))
            else:
                results.append(resp)
        except Exception as e:
            results.append({"filename": fd["filename"], "error": str(e), "status": "failed"})

    added = sum(r.get("nodes_added", 0) for r in results if not r.get("error"))
    dupes = sum(1 for r in results if r.get("duplicate"))
    failed = sum(1 for r in results if r.get("error"))

    return {
        "batch": True,
        "total_files": len(file_data),
        "total_bytes": total_bytes,
        "results": results,
        "summary": {"nodes_added": added, "duplicates": dupes, "failed": failed, "new_docs": len(file_data) - dupes - failed},
        "stats": get_stats(load_brain_safe()),
    }


class QueryPayload(BaseModel):
    query: str


@app.post("/api/brain/ask")
def ask_brain(payload: QueryPayload):
    """Ask the brain a natural language question."""
    if not payload.query or not payload.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from brain_query import answer_brain_query
    brain = load_brain_safe()
    if not brain.get("nodes"):
        return {"query": payload.query, "answer": "The brain is empty. Upload some documents first.", "cited_node_labels": [], "confidence": "none", "subgraph_size": 0, "entry_nodes": []}
    result = answer_brain_query(payload.query, brain)
    # Store in query history
    brain_rw = load_brain()
    brain_rw.setdefault("query_history", []).append({
        "query": payload.query, "answer": result.get("answer", ""),
        "confidence": result.get("confidence", "low"),
        "cited_nodes": result.get("cited_node_ids", []),
        "timestamp": datetime.utcnow().isoformat() + "Z"
    })
    brain_rw["query_history"] = brain_rw["query_history"][-50:]
    save_brain(brain_rw)
    return result


class ChatPayload(BaseModel):
    query: str


@app.post("/api/brain/chat")
def brain_chat(payload: ChatPayload):
    """Smart chat: query / command / plan."""
    if not payload.query or not payload.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from chat_commander import handle_chat_command
    brain = load_brain()
    result = handle_chat_command(payload.query, brain)
    if result["response_type"] == "query":
        from brain_query import answer_brain_query
        brain_ro = load_brain_safe()
        qr = answer_brain_query(payload.query, brain_ro)
        qr["response_type"] = "query"
        return qr
    elif result["response_type"] == "plan_summary":
        save_brain(brain)
        return result
    elif result["response_type"] == "command_preview":
        return result
    return result


class ConfirmPayload(BaseModel):
    operations: list
    confirmed: bool


@app.post("/api/brain/chat/confirm")
def confirm_command(payload: ConfirmPayload):
    """Execute or cancel a chat command."""
    if not payload.confirmed:
        return {"status": "cancelled"}
    from chat_commander import execute_command
    brain = load_brain()
    result = execute_command(payload.operations, brain)
    save_brain(brain)
    return {"status": "executed", **result}


@app.get("/api/brain/query-history")
def get_query_history():
    brain = load_brain_safe()
    return {"history": brain.get("query_history", [])[-20:]}


class WebEnrichPayload(BaseModel):
    query: str

@app.post("/api/brain/web-enrich")
def web_enrich(payload: WebEnrichPayload):
    """Search web for facts to enrich the brain."""
    if not payload.query.strip():
        raise HTTPException(400, "Query cannot be empty")
    from web_enricher import enrich_brain_from_web
    brain = load_brain_safe()
    result = enrich_brain_from_web(payload.query, brain)
    if result.get("questions"):
        from storage import add_question_safe
        brain_rw = load_brain()
        added_count = 0
        for q in result["questions"]:
            if add_question_safe(brain_rw, q, "web_search"):
                added_count += 1
        save_brain(brain_rw)
        result["added_to_act"] = added_count
    return result


def _build_update_summary(updates, nodes):
    if not updates or (updates[0].get("action") == "skipped"):
        return "No graph changes."
    parts = []
    node_map = {n["id"]: n["label"] for n in nodes}
    for u in updates:
        a = u.get("action", "")
        if "added_node" in a:
            parts.append("Added " + u.get("type", "node") + ": " + node_map.get(u.get("id", ""), u.get("id", "")))
        elif "updated_field" in a:
            parts.append("Updated " + u.get("field", "") + " on " + node_map.get(u.get("node", ""), u.get("node", "")))
        elif "updated_metric" in a:
            parts.append("Added metric to " + node_map.get(u.get("node", ""), u.get("node", "")))
        elif "added_link" in a or "exists_link" in a:
            parts.append("Edge: " + u.get("source", "") + " → " + u.get("target", ""))
        elif "marked_causal" in a:
            parts.append("Causal: " + u.get("source", "") + " → " + u.get("target", ""))
    return " · ".join(parts) if parts else "Answer recorded."


class ClarifyPayload(BaseModel):
    question: dict
    followup: str = ""


@app.post("/api/brain/clarify")
def clarify_question(payload: ClarifyPayload):
    """Given a question, return context, rewritten version, and suggested options."""
    from brain_engine import call_claude, MODEL_FAST
    brain = load_brain_safe()
    q = payload.question
    context_id = q.get("context", "")
    context_node = find_node(brain, context_id) if context_id else None

    connections = []
    if context_node:
        for link in brain.get("links", []):
            sid, tid = link.get("source", ""), link.get("target", "")
            if sid == context_id or tid == context_id:
                other = find_node(brain, tid if sid == context_id else sid)
                if other:
                    connections.append(f"{link.get('rel', '')} → {other['label']} ({other['type']})")

    node_ctx = ""
    if context_node:
        node_ctx = (f"Node: {context_node.get('label', '')} ({context_node.get('type', '')})\n"
                    f"Description: {context_node.get('desc', 'none')}\n"
                    f"Where: {context_node.get('where', 'unknown')}\n"
                    f"Metrics: {context_node.get('metrics', {})}\n"
                    f"Connected to: {', '.join(connections[:6]) if connections else 'nothing yet'}\n"
                    f"Source: {context_node.get('source_doc', 'unknown')}")

    followup = payload.followup.strip()
    prompt = (f"A user is trying to answer a knowledge graph question but finds it unclear.\n\n"
              f"ORIGINAL QUESTION: {q.get('question', '')}\n"
              f"WHY IT WAS ASKED: {q.get('why', '')}\n\n"
              f"CONTEXT NODE IN BRAIN:\n{node_ctx or 'No node context available.'}\n\n"
              f"USER SAYS: {followup or 'I need more context to answer this.'}\n\n"
              f"Return JSON:\n"
              f'{{"explanation": "2-3 sentences explaining this question in plain language",'
              f'"rewritten_question": "more specific version",'
              f'"suggested_options": ["option 1", "option 2", "option 3"],'
              f'"node_context_summary": "one sentence about what the brain knows"}}')

    try:
        raw = call_claude(prompt, model=MODEL_FAST, max_tokens=600)
        clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(clean)
        result["original_question"] = q.get("question", "")
        result["node_id"] = context_id
        result["node_label"] = context_node.get("label", "") if context_node else ""
        return result
    except Exception as e:
        return {
            "explanation": f"This question asks about '{context_id}' and its connections.",
            "rewritten_question": q.get("question", ""),
            "suggested_options": [],
            "node_context_summary": "",
            "node_id": context_id,
            "node_label": context_node.get("label", "") if context_node else "",
        }


@app.get("/api/nodes/{node_id}/summary")
def get_node_summary(node_id: str):
    """Generate an intelligent summary for a node using its connections."""
    from brain_engine import call_claude, MODEL_FAST
    brain = load_brain_safe()
    node = find_node(brain, node_id)
    if not node:
        raise HTTPException(404, "Node not found")

    links = brain.get("links", [])
    nm = {n["id"]: n for n in brain.get("nodes", [])}
    conns = []
    for l in links:
        src, tgt = l.get("source", ""), l.get("target", "")
        if src == node_id and nm.get(tgt):
            o = nm[tgt]
            conns.append({"direction": "outgoing", "rel": l.get("rel", "related"), "node_id": tgt,
                          "node_label": o["label"], "node_type": o.get("type", ""), "node_desc": o.get("desc", "")[:80],
                          "causal": l.get("causal", False)})
        elif tgt == node_id and nm.get(src):
            o = nm[src]
            conns.append({"direction": "incoming", "rel": l.get("rel", "related"), "node_id": src,
                          "node_label": o["label"], "node_type": o.get("type", ""), "node_desc": o.get("desc", "")[:80],
                          "causal": l.get("causal", False)})

    conn_text = ""
    for c in conns[:20]:
        arrow = "\u2192" if c["direction"] == "outgoing" else "\u2190"
        conn_text += f"\n  {arrow} {c['rel']}: {c['node_label']} ({c['node_type']})" + (" [CAUSAL]" if c["causal"] else "")

    prompt = (f"Analyze this knowledge graph node.\n\n"
              f"NODE: {node.get('label','')} ({node.get('type','')})\n"
              f"WHERE: {node.get('where','unknown')}\n"
              f"DESCRIPTION: {node.get('desc','none')}\n"
              f"METRICS: {json.dumps(node.get('metrics', {}))}\n"
              f"CONNECTIONS ({len(conns)} total):{conn_text}\n\n"
              f"Return JSON:\n"
              f'{{"summary":"rich 2-3 sentence summary synthesizing connections",'
              f'"role_in_graph":"one sentence about structural role (hub/bridge/endpoint/isolated)",'
              f'"open_questions":["what would make this node more complete"],'
              f'"connection_annotations":{{"node_id":"why this connection matters"}}}}\n'
              f"Be specific — mention actual names, products, features.")

    try:
        raw = call_claude(prompt, model=MODEL_FAST, max_tokens=800)
        import re as _re
        clean = _re.sub(r'^```[a-z]*\n?', '', raw.strip())
        clean = _re.sub(r'\n?```$', '', clean).strip()
        result = json.loads(clean)
        result["node_id"] = node_id
        result["connections_with_context"] = conns
        return result
    except Exception as e:
        return {"node_id": node_id, "summary": node.get("desc", ""), "role_in_graph": "",
                "open_questions": [], "connection_annotations": {},
                "connections_with_context": conns}


from fastapi import Body

@app.post("/api/brain/queue-question")
def queue_question_endpoint(question: dict = Body(...)):
    """Add a question to pending_questions."""
    from storage import add_question_safe
    brain = load_brain()
    added = add_question_safe(brain, question)
    save_brain(brain)
    return {"status": "queued"}


class AnswerPayload(BaseModel):
    question_id: str
    question: dict
    answer: str
    source_doc: str = ""


@app.post("/api/answer")
def submit_answer(payload: AnswerPayload):
    """Process a Q&A answer and update the brain graph."""
    from storage import find_node
    brain = load_brain()
    question = payload.question

    # ── Proposed action handler (evolution, web, concept, chat commands) ──
    proposed = question.get("proposed_action")
    answer_lower = payload.answer.strip().lower()
    updates = []
    enriched_nodes = []

    if proposed:
        op = proposed.get("op", "")
        details = proposed.get("details", proposed)
        WEB_FIELD_WHITELIST = {"desc", "where", "company", "label"}

        if answer_lower in ("yes", "y"):
            # ── APPROVE: execute the proposed action ──
            if op == "add_node":
                d = details
                node = {"id": d.get("id", f"web_{int(datetime.utcnow().timestamp())}"),
                        "label": d.get("label", ""), "type": d.get("type", "Feature"),
                        "desc": d.get("desc", ""), "company": d.get("company", ""),
                        "where": d.get("where", ""), "confidence": "web", "source_doc": "web_search"}
                add_node(brain, node)
                updates = [{"action": "added_node", "id": node["id"], "type": node["type"]}]
            elif op == "add_edge" or op == "add_link":
                src = details.get("source_id", "") or details.get("source", "")
                tgt = details.get("target_id", "") or details.get("target", "")
                rel = details.get("rel", "related to")
                add_link(brain, {"source": src, "target": tgt, "rel": rel,
                                  "causal": False, "confirmed": True, "weight": 1.5,
                                  "memory_type": "semantic", "added_by": "user_confirmed"})
                updates = [{"action": "added_link", "source": src, "target": tgt}]
            elif op == "update_field":
                field = details.get("field", "")
                if field in WEB_FIELD_WHITELIST:
                    node = find_node(brain, details.get("node_id", ""))
                    if node:
                        node[field] = details.get("new_value", "")
                        updates = [{"action": "updated_field", "node": details.get("node_id"), "field": field}]
                    else:
                        updates = [{"action": "skipped", "reason": "node not found"}]
                else:
                    updates = [{"action": "skipped", "reason": f"field '{field}' not in whitelist"}]
            elif op == "update_metric":
                node = find_node(brain, details.get("node_id", ""))
                if node:
                    node.setdefault("metrics", {})[details.get("name", "metric")] = details.get("value", "")
                    updates = [{"action": "updated_metric", "node": details.get("node_id")}]
                else:
                    updates = [{"action": "skipped", "reason": "node not found"}]
            elif op == "create_concept_node":
                from storage import add_concept_node
                d = details if isinstance(details, dict) else {}
                new_node = add_concept_node(brain, {"label": d.get("proposed_name", ""),
                    "description": d.get("description", ""), "member_ids": d.get("member_ids", []),
                    "cluster_id": d.get("cluster_id", "")})
                updates = [{"action": "added_node", "id": new_node["id"], "type": "Concept"}]
            elif op == "merge_nodes":
                from chat_commander import execute_command
                result = execute_command([proposed], brain)
                updates = [{"action": c} for c in result.get("changes", [])]
            else:
                updates = [{"action": "skipped", "reason": f"unknown op '{op}'"}]

        elif answer_lower in ("no", "n", "skip"):
            # ── REJECT: record rejection, do nothing to graph ──
            from storage import reject_edge
            if op in ("add_edge", "add_link"):
                src = details.get("source_id", "") or details.get("source", "")
                tgt = details.get("target_id", "") or details.get("target", "")
                if src and tgt:
                    reject_edge(brain, src, tgt)
            updates = [{"action": "skipped", "reason": "rejected by user"}]

        else:
            updates = [{"action": "skipped", "reason": "unclear answer"}]

    else:
        # ── No proposed_action — standard Q&A interpretation ──
        updates = process_answer_update(brain, question, payload.answer)

    # Persist Q&A exchange permanently
    log_answered_question(brain, question, payload.answer, updates)

    # Update node confidence based on answer (after logging, so strings don't pollute qa_history)
    from storage import update_confidence_from_answer
    conf_updates = update_confidence_from_answer(brain, question, payload.answer)
    for cu in conf_updates:
        updates.append({"action": "confidence_update", "detail": cu})

    # Remove ALL copies of this question from pending (by ID + text + context)
    _ans_text = question.get("question", "").strip().lower()[:80]
    _ans_ctx = question.get("context", "")
    _ans_cat = question.get("category", "")
    def _should_remove(q):
        if q.get("id") == payload.question_id: return True
        if _ans_text and q.get("question", "").strip().lower()[:80] == _ans_text: return True
        if _ans_ctx and _ans_cat and q.get("context") == _ans_ctx and q.get("category") == _ans_cat: return True
        return False
    brain["pending_questions"] = [
        q for q in brain.get("pending_questions", [])
        if not _should_remove(q)
    ]

    save_brain(brain)

    # Enrich response with affected node info
    enriched_nodes = []
    for u in updates:
        nid = u.get("id") or u.get("node")
        if nid:
            node = find_node(brain, nid)
            if node:
                enriched_nodes.append({"id": node["id"], "label": node["label"], "type": node["type"], "action": u.get("action", "")})

    return {
        "ok": True,
        "updates": updates,
        "affected_nodes": enriched_nodes,
        "update_summary": _build_update_summary(updates, enriched_nodes),
        "stats": get_stats(brain)
    }


@app.get("/api/brain/health/stream")
async def stream_brain_health():
    """SSE stream of agent progress during brain health run."""
    from fastapi.responses import StreamingResponse
    from consolidation import run_brain_health
    import json as json_lib

    def event_stream():
        brain = load_brain()
        for event in run_brain_health(brain):
            yield f"data: {json_lib.dumps(event)}\n\n"
        save_brain(brain)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@app.post("/api/brain/health")
def run_health_check():
    """Manual brain health run (non-streaming). For curl/testing."""
    from consolidation import run_brain_health
    brain = load_brain()
    result = None
    for event in run_brain_health(brain):
        if event.get("type") == "complete":
            result = event["result"]
    save_brain(brain)
    return {"ok": True, "result": result, "stats": get_stats(brain)}


@app.get("/api/brain/questions")
def get_brain_questions():
    """Get pending questions sorted by priority. Filters orphans + stale."""
    import re as _re, time as _t
    brain = load_brain_safe()
    questions = brain.get("pending_questions", [])
    node_ids = {n["id"] for n in brain.get("nodes", [])}
    # Filter orphaned questions (context node deleted) — skip concept proposals (their context is a cluster ID, not a node)
    questions = [q for q in questions if q.get("category") == "concept_proposal" or not q.get("context") or q.get("context") in node_ids]
    # Filter old low-priority questions (>14 days)
    def _too_old(q):
        if q.get("priority") in ("high", "medium"): return False
        m = _re.search(r'_(\d{10,13})(?:_|$)', q.get("id", ""))
        if not m: return False
        ts = int(m.group(1))
        if ts < 1e12: ts = int(ts * 1000)
        return (_t.time() * 1000 - ts) / 86400000 > 14
    questions = [q for q in questions if not _too_old(q)]
    priority_order = {"high": 0, "medium": 1, "low": 2}
    questions_sorted = sorted(questions, key=lambda q: priority_order.get(q.get("priority", "low"), 2))
    return {
        "questions": questions_sorted,
        "total": len(questions_sorted),
        "high": sum(1 for q in questions if q.get("priority") == "high"),
        "medium": sum(1 for q in questions if q.get("priority") == "medium"),
        "low": sum(1 for q in questions if q.get("priority") == "low"),
    }


@app.post("/api/brain/merge-duplicate-persons")
def merge_duplicate_persons():
    """Find and merge duplicate Person nodes."""
    from evolution_engine import detect_person_duplicates
    from chat_commander import execute_command, _normalize_op
    brain = load_brain()
    candidates = detect_person_duplicates(brain)
    merged, details = 0, []
    # Also remove garbage person nodes (labels > 50 chars = not real names)
    garbage = [n for n in brain.get("nodes", []) if n.get("type") == "Person" and len(n.get("label", "")) > 50]
    for g in garbage:
        brain["nodes"] = [n for n in brain["nodes"] if n["id"] != g["id"]]
        brain["links"] = [l for l in brain.get("links", []) if l.get("source") != g["id"] and l.get("target") != g["id"]]
        details.append(f"Deleted garbage node: {g['label'][:40]}...")
    for c in candidates:
        if c["confidence"] >= 0.75:
            keep_id, absorb_id = c["keep_id"], c["absorb_id"]
            keep_node = find_node(brain, keep_id)
            absorb_node = find_node(brain, absorb_id)
            if not keep_node or not absorb_node:
                continue
            # Merge: re-point links, combine descriptions, remove absorbed
            if absorb_node.get("desc") and len(absorb_node.get("desc", "")) > len(keep_node.get("desc", "")):
                keep_node["desc"] = absorb_node["desc"]
            for k, v in absorb_node.get("metrics", {}).items():
                keep_node.setdefault("metrics", {})[k] = keep_node["metrics"].get(k, v)
            for link in brain.get("links", []):
                if link.get("source") == absorb_id: link["source"] = keep_id
                if link.get("target") == absorb_id: link["target"] = keep_id
            brain["nodes"] = [n for n in brain["nodes"] if n["id"] != absorb_id]
            brain["links"] = [l for l in brain["links"] if l.get("source") != l.get("target")]
            # Remove duplicate edges
            seen = set()
            brain["links"] = [l for l in brain["links"] if (l.get("source"), l.get("target")) not in seen and not seen.add((l.get("source"), l.get("target")))]
            merged += 1
            details.append(f"Merged {c['absorb_label']} \u2192 {c['keep_label']}")
    if merged > 0 or garbage:
        save_brain(brain)
    return {"merged": merged, "garbage_removed": len(garbage), "candidates": len(candidates), "details": details}


@app.post("/api/brain/merge-duplicate-labels")
def merge_duplicate_labels():
    """Find nodes with identical labels (case-insensitive), merge lower-degree into higher-degree."""
    from collections import defaultdict
    from storage import now_iso

    brain = load_brain()
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])

    # Build degree map
    degree = defaultdict(int)
    for l in links:
        degree[l.get("source", "")] += 1
        degree[l.get("target", "")] += 1

    # Group by normalized label
    label_groups = defaultdict(list)
    for n in nodes:
        key = n.get("label", "").strip().lower()
        if key:
            label_groups[key].append(n)

    dupe_groups = {k: v for k, v in label_groups.items() if len(v) > 1}

    merged_count = 0
    deleted_ids = set()
    merge_log = []
    skipped_log = []

    for label, group in dupe_groups.items():
        # Sub-group by type — only merge same-type, or absorb degree-0 orphans
        type_groups = defaultdict(list)
        for n in group:
            type_groups[n.get("type", "Unknown")].append(n)

        # Merge within each type group
        for ntype, tgroup in type_groups.items():
            if len(tgroup) < 2:
                continue
            tgroup_sorted = sorted(tgroup, key=lambda n: (degree.get(n["id"], 0), n.get("confidence_score", 0)), reverse=True)
            keeper = tgroup_sorted[0]
            for victim in tgroup_sorted[1:]:
                vid = victim["id"]
                kid = keeper["id"]
                if vid in deleted_ids:
                    continue
                # Migrate edges
                for l in links:
                    if l.get("source") == vid:
                        l["source"] = kid
                    if l.get("target") == vid:
                        l["target"] = kid
                # Remove self-loops
                links = [l for l in links if l.get("source") != l.get("target")]
                # Remove duplicate edges
                seen_pairs = set()
                deduped = []
                for l in links:
                    pair = (l.get("source", ""), l.get("target", ""))
                    rev = (pair[1], pair[0])
                    if pair not in seen_pairs and rev not in seen_pairs:
                        seen_pairs.add(pair)
                        deduped.append(l)
                links = deduped
                # Copy missing fields
                if not keeper.get("desc") and victim.get("desc"):
                    keeper["desc"] = victim["desc"]
                if not keeper.get("metrics") and victim.get("metrics"):
                    keeper["metrics"] = victim["metrics"]
                if not keeper.get("where") and victim.get("where"):
                    keeper["where"] = victim["where"]
                keeper["confidence_score"] = max(keeper.get("confidence_score", 0), victim.get("confidence_score", 0))
                deleted_ids.add(vid)
                merge_log.append(f"Merged '{victim['label']}' ({vid}) -> '{keeper['label']}' ({kid})")
                merged_count += 1
            keeper["updated_at"] = now_iso()

        # Cross-type: absorb degree-0 orphans into the highest-degree node of any type
        all_sorted = sorted(group, key=lambda n: degree.get(n["id"], 0), reverse=True)
        best = all_sorted[0]
        for n in all_sorted[1:]:
            if n["id"] in deleted_ids:
                continue
            if degree.get(n["id"], 0) == 0 and n.get("type") != best.get("type"):
                deleted_ids.add(n["id"])
                merge_log.append(f"Removed orphan '{n['label']}' ({n['id']}, type={n.get('type')}, deg=0)")
                merged_count += 1
            elif n.get("type") != best.get("type") and n["id"] not in deleted_ids:
                skipped_log.append(f"Skipped cross-type: '{n['label']}' {n.get('type')} vs {best.get('type')}")

    brain["nodes"] = [n for n in nodes if n["id"] not in deleted_ids]
    brain["links"] = links

    # Remove orphaned questions
    node_ids = {n["id"] for n in brain["nodes"]}
    before_q = len(brain.get("pending_questions", []))
    brain["pending_questions"] = [q for q in brain.get("pending_questions", []) if not q.get("context") or q["context"] in node_ids]
    after_q = len(brain.get("pending_questions", []))

    if merged_count > 0:
        save_brain(brain)

    return {
        "duplicate_groups_found": len(dupe_groups),
        "nodes_merged": merged_count,
        "nodes_remaining": len(brain["nodes"]),
        "links_remaining": len(brain["links"]),
        "orphan_questions_removed": before_q - after_q,
        "log": merge_log,
        "skipped_cross_type": skipped_log,
    }


@app.post("/api/brain/backfill-person-careers")
def backfill_person_careers():
    """Infer career history for Person nodes from Company connections + hardcoded known data."""
    from storage import update_person_career, now_iso, touch_node
    brain = load_brain()
    nm = {n["id"]: n for n in brain.get("nodes", [])}
    links = brain.get("links", [])
    updated = 0
    for node in brain.get("nodes", []):
        if node.get("type") != "Person": continue
        if node.get("career"): continue
        company_conns = []
        for l in links:
            other_id = l.get("target") if l.get("source") == node["id"] else (l.get("source") if l.get("target") == node["id"] else None)
            if other_id and nm.get(other_id, {}).get("type") == "Company":
                company_conns.append(nm[other_id]["label"])
        if company_conns:
            node["career"] = [{"company": c, "role": node.get("where", ""), "from": "", "to": "present", "is_current": True} for c in set(company_conns)]
            touch_node(node)
            updated += 1
    # Suneet — hardcoded known timeline
    suneet = next((n for n in brain["nodes"] if "suneet" in n["id"].lower() and n.get("type") == "Person"), None)
    if suneet:
        suneet["career"] = [
            {"company": "Arth", "role": "BIM Strategist", "from": "2019-04", "to": "2020-05", "is_current": False},
            {"company": "WheelsEye", "role": "Product Manager", "from": "2022-03", "to": "2023-01", "is_current": False},
            {"company": "BrightCHAMPS", "role": "Senior Product Manager", "from": "2023", "to": "present", "is_current": True},
        ]
        suneet["company"] = "BrightCHAMPS"
        suneet["where"] = "Senior PM"
        touch_node(suneet)
        updated += 1
    save_brain(brain)
    return {"updated": updated}


@app.post("/api/brain/questions/deduplicate-evolution")
def deduplicate_evolution():
    brain = load_brain()
    qs = brain.get("pending_questions", [])
    links = brain.get("links", [])
    answered = brain.get("qa_history", [])
    rejected = set(brain.get("rejected_edges", []))
    existing_pairs = set("||".join(sorted([l.get("source", ""), l.get("target", "")])) for l in links)
    answered_pairs = set()
    for a in answered:
        p = a.get("proposed_action", {})
        if p and p.get("op") in ("add_edge", "add_link"):
            d = p.get("details", p)
            answered_pairs.add("||".join(sorted([d.get("source_id", "") or d.get("source", ""), d.get("target_id", "") or d.get("target", "")])))
    original = len(qs)
    kept, seen = [], set()
    reasons = {"edge_exists": 0, "answered": 0, "rejected": 0, "dup_pair": 0}
    for q in qs:
        pa = q.get("proposed_action", {})
        if pa and pa.get("op") in ("add_edge", "add_link"):
            d = pa.get("details", pa)
            pk = "||".join(sorted([d.get("source_id", "") or d.get("source", ""), d.get("target_id", "") or d.get("target", "")]))
            if pk in existing_pairs: reasons["edge_exists"] += 1; continue
            if pk in answered_pairs: reasons["answered"] += 1; continue
            if pk in rejected: reasons["rejected"] += 1; continue
            if pk in seen: reasons["dup_pair"] += 1; continue
            seen.add(pk)
        kept.append(q)
    removed = original - len(kept)
    brain["pending_questions"] = kept
    if removed: save_brain(brain)
    return {"original": original, "remaining": len(kept), "removed": removed, "breakdown": reasons}


@app.post("/api/brain/questions/deduplicate")
def deduplicate_questions():
    """Deep dedup: removes duplicates, orphans, and already-answered questions."""
    brain = load_brain()
    qs = brain.get("pending_questions", [])
    answered = brain.get("qa_history", [])
    node_ids = {n["id"] for n in brain.get("nodes", [])}
    original = len(qs)
    answered_pfxs = set(a.get("question", "").strip().lower()[:80] for a in answered if a.get("question"))
    answered_ctxs = set(a.get("context", "") for a in answered if a.get("context"))
    pri_ord = {"high": 0, "medium": 1, "low": 2}
    qs_sorted = sorted(qs, key=lambda q: pri_ord.get(q.get("priority", "low"), 2))
    seen_texts, seen_cc = set(), set()
    deduped = []
    reasons = {"orphan": 0, "answered": 0, "duplicate": 0}
    for q in qs_sorted:
        ctx, cat = q.get("context", ""), q.get("category", "")
        pfx = q.get("question", "").strip().lower()[:80]
        if ctx and ctx not in node_ids: reasons["orphan"] += 1; continue
        if pfx and pfx in answered_pfxs: reasons["answered"] += 1; continue
        if ctx and ctx in answered_ctxs and cat in ("no_source", "no_owner", "isolated", "contradiction"): reasons["answered"] += 1; continue
        if pfx and pfx in seen_texts: reasons["duplicate"] += 1; continue
        cc = (ctx, cat)
        if ctx and cat and cc in seen_cc: reasons["duplicate"] += 1; continue
        seen_texts.add(pfx)
        if ctx and cat: seen_cc.add(cc)
        deduped.append(q)
    from storage import normalise_question
    deduped = [normalise_question(q, q.get("from_doc", "")) for q in deduped]
    brain["pending_questions"] = deduped
    save_brain(brain)
    return {"original": original, "remaining": len(deduped), "removed": original - len(deduped), "breakdown": reasons}


@app.post("/api/brain/questions/cleanup")
def cleanup_questions():
    """Remove questions referencing deleted nodes + exact duplicates."""
    brain = load_brain()
    qs = brain.get("pending_questions", [])
    node_ids = {n["id"] for n in brain.get("nodes", [])}
    original = len(qs)
    qs = [q for q in qs if not q.get("context") or q.get("context") in node_ids]
    seen = set()
    deduped = []
    for q in qs:
        text = q.get("question", "").strip().lower()[:60]
        if text not in seen:
            seen.add(text)
            deduped.append(q)
    # Normalise all remaining
    from storage import normalise_question
    deduped = [normalise_question(q, q.get("from_doc", "")) for q in deduped]
    removed = original - len(deduped)
    brain["pending_questions"] = deduped
    if removed > 0:
        save_brain(brain)
    return {"removed": removed, "remaining": len(deduped)}


@app.post("/api/rethink")
def rethink():
    """Analyze graph for gaps and generate follow-up questions."""
    brain = load_brain()
    questions = generate_followup_questions(brain)

    # Queue new questions
    for q in questions:
        queue_question(brain, q, "rethink")

    if questions:
        save_brain(brain)

    return {
        "ok": True,
        "questions": questions,
        "stats": get_stats(brain)
    }


@app.get("/api/documents")
def get_documents():
    """List all processed documents."""
    from document_store import list_documents
    return {"documents": list_documents()}


@app.get("/api/documents/{filename:path}")
def get_document(filename: str):
    """Get a full document record."""
    from document_store import get_document_record
    record = get_document_record(filename)
    if not record:
        raise HTTPException(404, f"Document '{filename}' not found")
    return record


@app.get("/api/nodes/{node_id}/sources")
def get_node_sources(node_id: str):
    """Get which documents contributed to a node."""
    from document_store import get_documents_for_node
    docs = get_documents_for_node(node_id)
    return {"node_id": node_id, "sources": docs}


@app.get("/api/search")
def search_nodes(q: str):
    """Search brain nodes by label or description."""
    brain = load_brain_safe()
    q_lower = q.lower()
    results = [
        n for n in brain["nodes"]
        if q_lower in n.get("label", "").lower()
        or q_lower in n.get("desc", "").lower()
        or q_lower in n.get("where", "").lower()
    ]
    return {"query": q, "results": results[:15]}


@app.get("/api/node/{node_id}")
def get_node(node_id: str):
    """Get a single node with its connections."""
    brain = load_brain_safe()
    node = next((n for n in brain["nodes"] if n["id"] == node_id), None)
    if not node:
        raise HTTPException(404, f"Node '{node_id}' not found")

    connections = []
    for l in brain["links"]:
        if l["source"] == node_id:
            target = next((n for n in brain["nodes"] if n["id"] == l["target"]), None)
            if target:
                connections.append({"node": target, "rel": l["rel"], "dir": "out", "causal": l.get("causal", False)})
        if l["target"] == node_id:
            source = next((n for n in brain["nodes"] if n["id"] == l["source"]), None)
            if source:
                connections.append({"node": source, "rel": l["rel"], "dir": "in", "causal": l.get("causal", False)})

    return {"node": node, "connections": connections}


@app.get("/api/export/markdown")
def export_markdown():
    """Export brain as markdown."""
    brain = load_brain_safe()
    from storage import export_to_markdown
    md = export_to_markdown(brain)
    return {"markdown": md}


@app.get("/api/brain/history")
def get_qa_history():
    brain = load_brain_safe()
    return {"history": brain.get("qa_history", [])}


@app.get("/api/pending-questions")
def get_pending_questions():
    brain = load_brain_safe()
    return {"questions": brain.get("pending_questions", [])}


# ── NODE SUMMARY ──────────────────────────────────────────────────

@app.get("/api/nodes/{node_id}/summary/generate")
async def generate_summary_endpoint(node_id: str):
    from node_summarizer import generate_node_summary
    brain = load_brain_safe()
    node = find_node(brain, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    import asyncio
    summary = await asyncio.to_thread(generate_node_summary, node, brain)
    return summary


@app.post("/api/nodes/{node_id}/summary/save")
def save_node_summary(node_id: str, summary: dict = Body(...)):
    brain = load_brain()
    node = find_node(brain, node_id)
    if not node:
        raise HTTPException(404, "Node not found")
    node["_summary"] = summary
    node["_summary_saved_at"] = datetime.utcnow().isoformat() + "Z"
    save_brain(brain)
    return {"status": "saved", "node_id": node_id}


@app.get("/api/brain/summaries/generate-all")
async def generate_all_summaries_stream():
    from sse_starlette.sse import EventSourceResponse
    from node_summarizer import generate_node_summary
    import asyncio as _aio

    async def gen():
        brain = load_brain_safe()
        nodes = brain.get("nodes", [])
        yield {"data": json.dumps({"type": "start", "total": len(nodes)})}
        for i, node in enumerate(nodes):
            summary = await _aio.to_thread(generate_node_summary, node, brain)
            yield {"data": json.dumps({"type": "summary", "node_id": node["id"],
                                        "node_label": node["label"], "index": i,
                                        "total": len(nodes), "summary": summary})}
            await _aio.sleep(0.05)
        yield {"data": json.dumps({"type": "complete", "total": len(nodes)})}

    return EventSourceResponse(gen())


@app.post("/api/brain/summaries/save-all")
def save_all_summaries(payload: dict = Body(...)):
    brain = load_brain()
    summaries = payload.get("summaries", {})
    saved = 0
    for nid, summary in summaries.items():
        node = find_node(brain, nid)
        if node:
            node["_summary"] = summary
            node["_summary_saved_at"] = datetime.utcnow().isoformat() + "Z"
            saved += 1
    save_brain(brain)
    return {"status": "saved", "count": saved}


@app.get("/api/brain/summaries/status")
def summaries_status():
    brain = load_brain_safe()
    nodes = brain.get("nodes", [])
    has_summary = sum(1 for n in nodes if n.get("_summary"))
    stale = sum(1 for n in nodes
                if n.get("_summary") and n.get("updated_at", "") > n.get("_summary_saved_at", ""))
    return {"total": len(nodes), "has_summary": has_summary,
            "missing": len(nodes) - has_summary, "stale": stale}


@app.get("/api/brain/summaries/generate-stale")
async def generate_stale_summaries_stream():
    from sse_starlette.sse import EventSourceResponse
    from node_summarizer import generate_node_summary
    import asyncio as _aio

    async def gen():
        brain = load_brain_safe()
        nodes = brain.get("nodes", [])
        targets = []
        for n in nodes:
            if not n.get("_summary"):
                targets.append((n, "missing"))
            elif n.get("updated_at", "") > n.get("_summary_saved_at", ""):
                targets.append((n, "stale"))
        if not targets:
            yield {"data": json.dumps({"type": "complete", "total": 0, "message": "All current"})}
            return
        yield {"data": json.dumps({"type": "start", "total": len(targets)})}
        for i, (node, reason) in enumerate(targets):
            summary = await _aio.to_thread(generate_node_summary, node, brain)
            yield {"data": json.dumps({"type": "summary", "node_id": node["id"], "node_label": node["label"],
                                        "reason": reason, "index": i, "total": len(targets), "summary": summary})}
            await _aio.sleep(0.05)
        yield {"data": json.dumps({"type": "complete", "total": len(targets)})}

    return EventSourceResponse(gen())


# ── EVOLUTION ─────────────────────────────────────────────────────

@app.get("/api/evolve/stream")
async def evolution_stream(intensity: str = "medium"):
    from sse_starlette.sse import EventSourceResponse
    from evolution_engine import run_evolution_cycle

    async def gen():
        async for event in run_evolution_cycle(intensity):
            yield {"data": json.dumps(event)}

    return EventSourceResponse(gen())


@app.get("/api/evolve/status")
def evolution_status():
    from evolution_engine import _evolution_running
    return {"running": _evolution_running}


@app.get("/api/evolve/history")
def evolution_history(limit: int = 20):
    from storage import get_evolution_log_path
    try:
        lines = get_evolution_log_path().read_text().strip().split("\n")
        return {"history": [json.loads(l) for l in lines[-limit:] if l.strip()][::-1]}
    except Exception:
        return {"history": []}


# ── SEMANTIC EMBEDDINGS ───────────────────────────────────────────

@app.post("/api/brain/embeddings/rebuild")
def rebuild_embeddings():
    from embeddings import node_to_text, embed_batch, save_embedding_store
    import numpy as np, time as _t
    brain = load_brain()
    nodes = brain.get("nodes", [])
    if not nodes: return {"error": "No nodes"}
    start = _t.time()
    node_ids = [n["id"] for n in nodes]
    texts = [node_to_text(n) for n in nodes]
    vecs = embed_batch(texts)
    matrix = np.array(vecs, dtype=np.float32)
    save_embedding_store(matrix, node_ids)
    return {"nodes_embedded": len(nodes), "dimensions": 384,
            "matrix_size_mb": round(matrix.nbytes / 1048576, 2),
            "elapsed_seconds": round(_t.time() - start, 1)}

@app.get("/api/brain/embeddings/stats")
def embedding_stats():
    from embeddings import load_embedding_store
    matrix, node_ids = load_embedding_store()
    total = len(load_brain_safe().get("nodes", []))
    if matrix is None: return {"status": "not_built", "total_nodes": total}
    return {"status": "ready", "embedded_nodes": len(node_ids), "total_nodes": total,
            "coverage_pct": round(len(node_ids) / max(1, total) * 100, 1),
            "dimensions": matrix.shape[1] if len(matrix.shape) > 1 else 0,
            "size_mb": round(matrix.nbytes / 1048576, 2)}

@app.post("/api/brain/search/semantic")
def semantic_search_endpoint(payload: dict = Body(...)):
    from embeddings import semantic_search
    query = payload.get("query", "")
    if not query: return {"results": []}
    brain = load_brain_safe()
    results = semantic_search(query, brain, payload.get("top_k", 10), payload.get("threshold", 0.3))
    return {"query": query, "results": results, "count": len(results)}


# ── HEBBIAN EDGE DYNAMICS ─────────────────────────────────────────

@app.post("/api/brain/backfill-edge-weights")
def backfill_edge_weights():
    from storage import now_iso
    brain = load_brain()
    links = brain.get("links", [])
    bf = 0
    for l in links:
        changed = False
        if "weight" not in l:
            l["weight"] = 1.5 if l.get("causal") else (0.8 if l.get("added_by") == "evolution" else 1.0)
            changed = True
        for k, v in [("access_count", 0), ("created_at", now_iso()), ("last_accessed", None),
                      ("confirmed", False), ("memory_type", "semantic" if l.get("causal") else "episodic")]:
            if k not in l:
                l[k] = v if v is not None else l.get("created_at", now_iso())
                changed = True
        if changed: bf += 1
    if bf: save_brain(brain)
    return {"total_edges": len(links), "backfilled": bf,
            "causal": sum(1 for l in links if l.get("causal")),
            "semantic": sum(1 for l in links if l.get("memory_type") == "semantic")}


@app.post("/api/brain/backfill-memory-type")
def backfill_memory_type():
    """One-time backfill: set unset memory_type to episodic, graduate eligible nodes."""
    from storage import maybe_graduate_node
    brain = load_brain()
    backfilled = graduated = 0
    for node in brain.get("nodes", []):
        if not node.get("memory_type") or node["memory_type"] == "unset":
            node["memory_type"] = "episodic"
            backfilled += 1
        if maybe_graduate_node(node):
            graduated += 1
    save_brain(brain)
    return {"backfilled": backfilled, "graduated": graduated,
            "total_nodes": len(brain.get("nodes", []))}


@app.post("/api/brain/propagate-test")
def propagate_test(node_id: str, new_confidence: float):
    """Test CCP: manually set a node's confidence and see propagation."""
    from storage import propagate_confidence, find_node, touch_node
    brain = load_brain()
    node = find_node(brain, node_id)
    if not node:
        raise HTTPException(404, f"Node '{node_id}' not found")
    old_conf = node.get("confidence_score", 0.3)
    node["confidence_score"] = round(max(0.05, min(1.0, new_confidence)), 3)
    touch_node(node)
    updates = propagate_confidence(brain, node_id, old_conf, new_confidence)
    save_brain(brain)
    return {
        "node": node.get("label"),
        "old_confidence": old_conf,
        "new_confidence": new_confidence,
        "propagation": updates,
        "nodes_affected": len(updates)
    }


@app.post("/api/brain/infer-ownership")
def infer_ownership():
    """Infer owners for unowned Feature nodes. Batches of 10, max 50."""
    from brain_engine import call_claude_json, MODEL_FAST
    from storage import (add_link, add_question_safe, normalise_question,
                         is_rejected_edge, touch_node)

    brain = load_brain()
    nodes = brain.get("nodes", [])
    links = brain.get("links", [])
    nm = {n["id"]: n for n in nodes}

    persons = [n for n in nodes if n.get("type") == "Person"]
    if not persons:
        return {"error": "No Person nodes", "inferred": 0}

    owner_rels = {"owns", "built", "leads", "product owner"}
    owned_features = set()
    person_owns = {p["id"]: [] for p in persons}
    for l in links:
        if l.get("rel", "").lower() in owner_rels:
            src = nm.get(l.get("source", ""))
            tgt = nm.get(l.get("target", ""))
            if src and src.get("type") == "Person" and tgt:
                owned_features.add(tgt["id"])
                person_owns[src["id"]].append(tgt.get("label", ""))

    features = [n for n in nodes if n.get("type") == "Feature"
                and n["id"] not in owned_features][:50]
    if not features:
        return {"message": "All features have owners", "inferred": 0}

    person_ctx = [{"id": p["id"], "label": p.get("label", ""),
                   "company": p.get("company", ""),
                   "owns": person_owns.get(p["id"], [])} for p in persons]

    applied = queued = skipped = 0

    for i in range(0, len(features), 10):
        batch = features[i:i + 10]
        batch_ctx = [{"id": f["id"], "label": f.get("label", ""),
                      "company": f.get("company", ""),
                      "where": f.get("where", ""),
                      "desc": (f.get("desc", "") or "")[:100]} for f in batch]

        prompt = (
            "You are analyzing a product knowledge graph. For each Feature, "
            "identify the most likely owner from the Person list.\n\n"
            "Rules:\n"
            "- Match by company context (person and feature at same company)\n"
            "- Consider what the person already owns\n"
            "- If no person is a reasonable match, set owner_id to null\n"
            "- Confidence: 0.9+ obvious, 0.7-0.89 likely, <0.7 uncertain\n\n"
            f"Persons:\n{json.dumps(person_ctx, indent=1)}\n\n"
            f"Features:\n{json.dumps(batch_ctx, indent=1)}\n\n"
            'Return JSON array: [{"feature_id": "", "owner_id": "" or null, '
            '"confidence": 0.0-1.0, "reasoning": "short reason"}]'
        )

        try:
            result = call_claude_json(prompt, model=MODEL_FAST)
            if not isinstance(result, list):
                continue
            for r in result:
                fid = r.get("feature_id", "")
                oid = r.get("owner_id")
                conf = r.get("confidence", 0)
                reason = r.get("reasoning", "")
                if not oid or not fid or conf < 0.5:
                    skipped += 1
                    continue
                if fid not in nm or oid not in nm:
                    skipped += 1
                    continue
                if is_rejected_edge(brain, oid, fid):
                    skipped += 1
                    continue

                if conf >= 0.80:
                    add_link(brain, {"source": oid, "target": fid, "rel": "owns",
                                     "causal": False, "confirmed": False, "weight": 0.7})
                    applied += 1
                else:
                    fl = nm[fid].get("label", fid)
                    ol = nm[oid].get("label", oid)
                    q = {"type": "yesno",
                         "question": f"Does {ol} own {fl}? ({reason})",
                         "why": f"Inferred ownership ({conf * 100:.0f}% confidence)",
                         "context": fid, "priority": "medium",
                         "category": "no_owner", "category_label": "OWNERSHIP",
                         "from_doc": "ownership_inference",
                         "proposed_action": {"op": "add_edge",
                            "details": {"source_id": oid, "target_id": fid, "rel": "owns"}}}
                    normalise_question(q)
                    if add_question_safe(brain, q, "ownership_inference"):
                        queued += 1
        except Exception as e:
            print(f"[ownership] batch {i} failed: {e}")

    save_brain(brain)

    # Recompute owned count after new edges added
    new_owned = set()
    for l in brain.get("links", []):
        if l.get("rel", "").lower() in owner_rels:
            new_owned.add(l.get("target", ""))
    remaining = sum(1 for n in nodes if n.get("type") == "Feature"
                    and n["id"] not in new_owned)

    return {"features_checked": len(features), "auto_applied": applied,
            "queued_for_review": queued, "skipped": skipped,
            "unowned_remaining": remaining}


@app.post("/api/brain/decay/run")
def run_decay_now():
    return _run_edge_decay()


@app.get("/api/brain/edge-stats")
def edge_stats():
    brain = load_brain_safe()
    links = brain.get("links", [])
    if not links: return {"total": 0}
    weights = [l.get("weight", 1.0) for l in links]
    return {
        "total": len(links),
        "by_memory_type": {"episodic": sum(1 for l in links if l.get("memory_type") == "episodic"),
                            "semantic": sum(1 for l in links if l.get("memory_type") == "semantic")},
        "by_strength": {"strong": sum(1 for w in weights if w >= 2.0),
                         "normal": sum(1 for w in weights if 1.0 <= w < 2.0),
                         "weak": sum(1 for w in weights if 0.3 <= w < 1.0),
                         "fading": sum(1 for w in weights if w < 0.3)},
        "confirmed": sum(1 for l in links if l.get("confirmed")),
        "causal": sum(1 for l in links if l.get("causal")),
        "avg_weight": round(sum(weights) / len(weights), 3),
        "avg_accesses": round(sum(l.get("access_count", 0) for l in links) / len(links), 1),
        "zero_access": sum(1 for l in links if l.get("access_count", 0) == 0),
    }


@app.get("/api/cost/live")
def cost_live():
    """Current session cost stats."""
    from cost_tracker import get_session_stats
    return get_session_stats()


@app.get("/api/cost/log")
def cost_log(limit: int = 50):
    """Recent API call log."""
    from cost_tracker import get_cost_log
    return {"calls": get_cost_log(limit)}


# ── MULTI-BRAIN MANAGEMENT ────────────────────────────────────────

@app.get("/api/brains")
def list_brains():
    """List all brains with stats."""
    registry = load_registry()
    active = get_active_brain_id()
    result = []
    for b in registry.get("brains", []):
        bid = b["id"]
        try:
            from storage import get_brain_path
            bp = get_brain_path(bid)
            if bp.exists():
                data = json.loads(bp.read_text(encoding="utf-8"))
                node_count = len(data.get("nodes", []))
                edge_count = len(data.get("links", []))
                health_data = data.get("health", {}).get("score", {})
                last_updated = data.get("meta", {}).get("last_updated", "")
            else:
                node_count, edge_count = 0, 0
                health_data = {}
                last_updated = ""
        except Exception:
            node_count, edge_count = 0, 0
            health_data = {}
            last_updated = ""
        result.append({
            "id": bid,
            "label": b.get("label", bid),
            "description": b.get("description", ""),
            "color": b.get("color", "#4da6ff"),
            "node_count": node_count,
            "edge_count": edge_count,
            "health_score": health_data.get("score", 0),
            "health_grade": health_data.get("grade", "\u2014"),
            "last_updated": last_updated,
            "is_active": bid == active,
            "created_at": b.get("created_at", ""),
        })
    return {"brains": result, "active_brain_id": active}


class CreateBrainRequest(BaseModel):
    id: str
    label: str
    description: str = ""
    color: str = "#4da6ff"


@app.post("/api/brains")
def create_brain_endpoint(req: CreateBrainRequest):
    """Create a new brain."""
    if not re.match(r'^[a-z][a-z0-9_]{2,39}$', req.id):
        raise HTTPException(400, "Brain ID must be lowercase alphanumeric + underscores, 3-40 chars")
    try:
        entry = create_brain(req.id, req.label, req.description)
        # Store color in registry
        registry = load_registry()
        for b in registry.get("brains", []):
            if b["id"] == req.id:
                b["color"] = req.color
                break
        save_registry(registry)
        return {"ok": True, "brain": entry}
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/brains/{brain_id}/activate")
def activate_brain(brain_id: str):
    """Switch the active brain."""
    try:
        set_active_brain(brain_id)
        return {"ok": True, "active_brain_id": brain_id}
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.get("/api/brains/active")
def get_active_brain_endpoint():
    """Get current active brain info."""
    bid = get_active_brain_id()
    brain = load_brain_safe()
    return {
        "active_brain_id": bid,
        "node_count": len(brain.get("nodes", [])),
        "edge_count": len(brain.get("links", [])),
    }


# ── MAIN ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    print("\n🧠 Brain server starting...")
    print(f"   API key: {'✓ set' if ANTHROPIC_API_KEY else '✗ NOT SET — add to .env'}")
    print(f"   Active brain: {get_active_brain_id()}")
    print(f"   UI: http://localhost:8000\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
