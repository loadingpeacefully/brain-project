"""Semantic embedding engine — all-MiniLM-L6-v2, 384 dimensions, CPU-fast."""
import json
import numpy as np
from pathlib import Path

_model = None

# Legacy constants (kept for backward compat, no longer used internally)
EMBEDDINGS_PATH = Path("data/embeddings.npy")
EMBEDDINGS_INDEX_PATH = Path("data/embeddings_index.json")


def _emb_path():
    from storage import get_embeddings_path
    return get_embeddings_path()


def _emb_index_path():
    from storage import get_embeddings_index_path
    return get_embeddings_index_path()


def get_model():
    global _model
    if _model is None:
        print("[embeddings] Loading all-MiniLM-L6-v2...")
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer("all-MiniLM-L6-v2")
        print("[embeddings] Model ready.")
    return _model


def embed_text(text):
    return get_model().encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts):
    return get_model().encode(texts, normalize_embeddings=True, batch_size=32, show_progress_bar=False).tolist()


def node_to_text(node):
    parts = []
    label = node.get("label", "")
    ntype = node.get("type", "")
    desc = (node.get("desc", "") or "")[:200]
    where = node.get("where", "") or ""
    if label: parts.append(label)
    if ntype and ntype != "Feature": parts.append(f"({ntype})")
    if where: parts.append(where)
    if desc: parts.append(desc)
    metrics = node.get("metrics", {})
    if metrics:
        parts.append(", ".join(f"{k}: {v}" for k, v in list(metrics.items())[:3]))
    return " | ".join(parts)


def load_embedding_store():
    ep = _emb_path()
    ip = _emb_index_path()
    if not ep.exists() or not ip.exists():
        return None, []
    try:
        matrix = np.load(str(ep))
        index = json.loads(ip.read_text())
        return matrix, index.get("node_ids", [])
    except Exception as e:
        print(f"[embeddings] Load failed: {e}")
        return None, []


def save_embedding_store(matrix, node_ids):
    ep = _emb_path()
    ep.parent.mkdir(parents=True, exist_ok=True)
    np.save(str(ep), matrix.astype(np.float32))
    _emb_index_path().write_text(json.dumps({"node_ids": node_ids, "version": len(node_ids)}))


def semantic_search(query, brain, top_k=10, threshold=0.3):
    matrix, node_ids = load_embedding_store()
    if matrix is None or not node_ids: return []
    q_vec = get_model().encode(query, normalize_embeddings=True)
    sims = matrix @ q_vec
    top_indices = np.argsort(sims)[::-1]
    nodes_map = {n["id"]: n for n in brain.get("nodes", [])}
    results = []
    for idx in top_indices:
        if sims[idx] < threshold: break
        if idx >= len(node_ids): continue
        nid = node_ids[idx]
        node = nodes_map.get(nid)
        if not node: continue
        results.append({"node_id": nid, "label": node.get("label", ""), "type": node.get("type", ""),
                         "desc": (node.get("desc", "") or "")[:80], "score": float(sims[idx])})
        if len(results) >= top_k: break
    return results


def find_semantic_neighbors(node_id, brain, top_k=5, threshold=0.55):
    matrix, node_ids = load_embedding_store()
    if matrix is None or node_id not in node_ids: return []
    idx = node_ids.index(node_id)
    q_vec = matrix[idx]
    sims = matrix @ q_vec
    sims[idx] = -1
    existing = set()
    for l in brain.get("links", []):
        s, t = l.get("source", ""), l.get("target", "")
        if s == node_id: existing.add(t)
        if t == node_id: existing.add(s)
    top_indices = np.argsort(sims)[::-1]
    nodes_map = {n["id"]: n for n in brain.get("nodes", [])}
    results = []
    for i in top_indices:
        if sims[i] < threshold: break
        if i >= len(node_ids): continue
        nid = node_ids[i]
        if nid in existing: continue
        node = nodes_map.get(nid)
        if not node: continue
        results.append({"node_id": nid, "label": node.get("label", ""), "type": node.get("type", ""), "score": float(sims[i])})
        if len(results) >= top_k: break
    return results
