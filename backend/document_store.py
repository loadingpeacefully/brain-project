"""Document Vault — persistent storage of document records per-brain."""
import json
import re
from datetime import datetime


def _docs_dir():
    from storage import get_documents_dir
    return get_documents_dir()


def _safe_filename(filename: str) -> str:
    safe = re.sub(r'[^\w\-_.]', '_', filename)
    return safe + ".json"


def save_document_record(record: dict) -> None:
    dd = _docs_dir()
    dd.mkdir(parents=True, exist_ok=True)
    filename = record.get("filename", "unknown")
    path = dd / _safe_filename(filename)
    record["updated_at"] = datetime.utcnow().isoformat() + "Z"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)


def get_document_record(filename: str):
    path = _docs_dir() / _safe_filename(filename)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def list_documents() -> list:
    dd = _docs_dir()
    if not dd.exists():
        return []
    records = []
    for p in sorted(dd.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
                summary_text = rec.get("summary", "")
                records.append({
                    "filename": rec.get("filename"),
                    "doc_type": rec.get("doc_type"),
                    "uploaded_at": rec.get("uploaded_at"),
                    "summary": (summary_text[:120] + "...") if len(summary_text) > 120 else summary_text,
                    "nodes_created": len(rec.get("nodes_created", [])),
                    "char_count": rec.get("char_count", 0),
                })
        except Exception:
            continue
    return records


def get_documents_for_node(node_id: str) -> list:
    dd = _docs_dir()
    if not dd.exists():
        return []
    results = []
    for p in dd.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
            if node_id in rec.get("nodes_created", []):
                results.append({"filename": rec.get("filename"), "doc_type": rec.get("doc_type"),
                                "uploaded_at": rec.get("uploaded_at"), "summary": rec.get("summary", ""),
                                "contribution": "primary"})
            elif node_id in rec.get("nodes_updated", []):
                results.append({"filename": rec.get("filename"), "doc_type": rec.get("doc_type"),
                                "uploaded_at": rec.get("uploaded_at"), "summary": rec.get("summary", ""),
                                "contribution": "enriched"})
        except Exception:
            continue
    return results


def get_file_hash(content: bytes) -> str:
    import hashlib
    return hashlib.sha256(content).hexdigest()[:16]


def check_duplicate(filename: str, content_hash: str):
    """Check if document already exists by filename or content hash."""
    existing = get_document_record(filename)
    if existing:
        return {"match": "filename", "record": existing}
    dd = _docs_dir()
    if not dd.exists():
        return None
    for p in dd.glob("*.json"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                rec = json.load(f)
            if rec.get("content_hash") == content_hash:
                return {"match": "content", "record": rec}
        except Exception:
            continue
    return None


def audit_document_record(record: dict, brain: dict) -> dict:
    """Check what's missing or outdated in an existing document record."""
    gaps = []
    fixed = []
    if not record.get("summary") or len(record.get("summary", "")) < 30:
        gaps.append("missing summary")
    if not record.get("key_facts") or len(record.get("key_facts", [])) == 0:
        gaps.append("missing key facts")
    nodes_created = record.get("nodes_created", [])
    brain_ids = {n["id"] for n in brain.get("nodes", [])}
    missing_nodes = [nid for nid in nodes_created if nid not in brain_ids]
    if missing_nodes:
        gaps.append(f"{len(missing_nodes)} source nodes missing from brain")
    return {"gaps": gaps, "fixed": fixed, "healthy": len(gaps) == 0}


def get_summaries_for_context(limit: int = 10) -> list:
    dd = _docs_dir()
    if not dd.exists():
        return []
    summaries = []
    records = list_documents()
    for rec in records[:limit]:
        full = get_document_record(rec["filename"])
        if full and full.get("summary"):
            summaries.append(f"[{rec['filename']} - {rec.get('doc_type','?')}] {full['summary']}")
    return summaries
