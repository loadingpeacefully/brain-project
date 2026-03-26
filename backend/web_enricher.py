"""
Web Enricher — Search the web for facts to enrich brain nodes.
Findings are proposed as Act questions, never auto-applied.
"""
import json
import os
import re
import time
from datetime import datetime
from anthropic import Anthropic
from config import ANTHROPIC_API_KEY, MODEL_EXTRACTION, MODEL_FAST

_client = Anthropic(api_key=ANTHROPIC_API_KEY) if ANTHROPIC_API_KEY else None


def _call_fast(prompt: str, max_tokens: int = 500) -> str:
    if not _client:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    resp = _client.messages.create(
        model=MODEL_FAST, max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}]
    )
    return resp.content[0].text


def _parse_json(text: str):
    clean = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(clean)


# ── STEP 1: Build targeted search queries ─────────────────────────

def build_search_queries(query: str, target_nodes: list,
                         brain: dict) -> list:
    node_ctx = "\n".join([
        f"- {n['label']} ({n['type']}): {n.get('desc', 'no description')[:80]}"
        for n in target_nodes[:6]
    ])
    prompt = f"""A user wants to enrich a knowledge graph with web data.

Their question: "{query}"

Relevant nodes already in the brain:
{node_ctx}

Generate 3-6 specific web search queries that would find:
1. Current/updated facts about these entities
2. Information the brain is likely missing (funding, team, numbers, timeline)
3. Information that could confirm or challenge what's in the brain

Return ONLY a JSON array of search query strings.
Make them specific — include company names, product names, years.
Example: ["BrightCHAMPS Series B funding 2022", "BrightCHAMPS CEO founder name"]"""

    try:
        raw = _call_fast(prompt, max_tokens=300)
        queries = _parse_json(raw)
        if isinstance(queries, list):
            return [str(q) for q in queries[:6]]
    except Exception as e:
        print(f"[web] query gen failed: {e}")
    return [n["label"] + " " + n.get("company", "") for n in target_nodes[:3]]


# ── STEP 2: Run web search via Claude ─────────────────────────────

def fetch_web_facts(queries: list) -> list:
    if not _client:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    results = []
    for query in queries[:5]:
        try:
            response = _client.messages.create(
                model=MODEL_FAST, max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search",
                         "max_uses": 3}],
                messages=[{"role": "user", "content":
                    f"Search for: {query}\n\n"
                    "Extract only concrete facts: names, numbers, dates, "
                    "descriptions. Return a JSON object:\n"
                    '{"facts": ["fact 1", "fact 2"], '
                    '"sources": ["url1", "url2"]}\n'
                    "Return ONLY the JSON object."}]
            )
            text_parts = [
                b.text for b in response.content
                if hasattr(b, "text") and b.text
            ]
            full_text = "\n".join(text_parts)
            try:
                data = _parse_json(full_text)
                results.append({
                    "query": query,
                    "facts": data.get("facts", []),
                    "sources": data.get("sources", []),
                })
            except Exception:
                if full_text.strip():
                    results.append({
                        "query": query,
                        "facts": [full_text.strip()[:300]],
                        "sources": [],
                    })
            print(f"[web] '{query[:40]}' → {len(results[-1].get('facts', []))} facts")
        except Exception as e:
            print(f"[web] search failed for '{query[:40]}': {e}")
    return results


# ── STEP 3: Compare web facts to brain ────────────────────────────

def compare_to_brain(web_results: list, brain: dict,
                     target_nodes: list) -> list:
    if not web_results:
        return []
    node_ctx = json.dumps([{
        "id": n["id"], "label": n["label"], "type": n["type"],
        "desc": n.get("desc", ""), "metrics": n.get("metrics", {}),
        "where": n.get("where", ""), "company": n.get("company", "")
    } for n in target_nodes], indent=1)

    all_facts = []
    for r in web_results:
        for fact in r.get("facts", []):
            all_facts.append({
                "fact": fact, "source_query": r["query"],
                "sources": r.get("sources", [])
            })
    if not all_facts:
        return []

    # Cap at 10 facts to keep response within token limits
    all_facts = all_facts[:10]
    facts_text = "\n".join([
        f"{i+1}. {f['fact'][:150]} [from: {f['source_query'][:40]}]"
        for i, f in enumerate(all_facts)
    ])
    prompt = f"""Compare these web-sourced facts against a brain knowledge graph.

BRAIN NODES (relevant subset):
{node_ctx}

WEB FACTS:
{facts_text}

For each fact, classify as:
- "new": completely new information not in brain
- "enrich": adds detail to an existing node
- "conflict": contradicts something in brain

Return a JSON array:
[
  {{
    "fact": "the fact text",
    "classification": "new|enrich|conflict",
    "reason": "why",
    "source_query": "which search",
    "target_node_id": "existing node id or null",
    "target_node_label": "label",
    "proposed_action": {{
      "op": "add_node|update_field|update_metric|add_link",
      "details": {{}}
    }},
    "confidence": "high|medium|low"
  }}
]

For proposed_action.details use these shapes:
- add_node: {{"id":"snake","label":"Name","type":"Feature|Outcome|Person|etc","desc":"desc"}}
- update_field: {{"node_id":"id","field":"desc|where|company|label","old_value":"cur","new_value":"web"}}
- update_metric: {{"node_id":"id","name":"metric","value":"val"}}
- add_link: {{"source":"id","target":"id","rel":"relationship"}}

Rules:
- Ignore facts already well-represented in brain
- Ignore vague or unverifiable facts
- Return [] if nothing actionable
- Return ONLY the JSON array"""

    try:
        if not _client:
            return []
        resp = _client.messages.create(
            model=MODEL_EXTRACTION, max_tokens=4000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text
        try:
            actions = _parse_json(raw)
        except json.JSONDecodeError:
            # Try to repair truncated JSON
            clean = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            # Close any unclosed brackets/braces
            open_b = clean.count("[") - clean.count("]")
            open_c = clean.count("{") - clean.count("}")
            # Remove trailing comma
            clean = re.sub(r',\s*$', '', clean)
            clean += "}" * open_c + "]" * open_b
            try:
                actions = json.loads(clean)
            except json.JSONDecodeError as e2:
                print(f"[web] JSON repair also failed: {e2}")
                return []
        if not isinstance(actions, list):
            return []
        source_map = {r["query"]: r.get("sources", []) for r in web_results}
        for a in actions:
            a["source_urls"] = source_map.get(a.get("source_query", ""), [])
            a["web_fetched_at"] = datetime.utcnow().isoformat() + "Z"
        return actions
    except Exception as e:
        print(f"[web] comparison failed: {e}")
        return []


# ── STEP 4: Convert to Act questions ──────────────────────────────

def actions_to_act_questions(actions: list) -> list:
    questions = []
    for i, action in enumerate(actions):
        cls = action.get("classification", "new")
        fact = action.get("fact", "")
        reason = action.get("reason", "")
        label = action.get("target_node_label", "")
        proposed = action.get("proposed_action", {})
        conf = action.get("confidence", "medium")
        sources = action.get("source_urls", [])

        if cls == "conflict":
            det = proposed.get("details", {})
            if proposed.get("op") == "update_field":
                q_text = (f"Web conflicts with brain about '{label}': "
                          f"current is \"{det.get('old_value','')}\" but "
                          f"web says \"{det.get('new_value','')}\". Fact: {fact}")
            else:
                q_text = (f"Web conflicts about '{label}'. "
                          f"Fact: {fact}. Reason: {reason}")
            pri, cat, cat_label = "high", "web_conflict", "WEB CONFLICT"
        elif cls == "enrich":
            q_text = f"Web found additional info for '{label}': {fact}"
            pri = "medium"
            cat, cat_label = "web_enrich", "WEB ENRICH"
        else:
            q_text = f"Web found new information not in brain: {fact}"
            pri = "medium" if conf == "high" else "low"
            cat, cat_label = "web_new", "WEB NEW"

        questions.append({
            "id": f"web_{cls}_{int(time.time())}_{i}",
            "type": "yesno",
            "question": q_text,
            "why": reason,
            "context": action.get("target_node_id", ""),
            "priority": pri,
            "category": cat,
            "category_label": cat_label,
            "from_doc": "web_search",
            "source_url": sources[0] if sources else "",
            "proposed_action": proposed,
            "web_fact": fact,
            "confidence": conf,
            "requires_approval": True,
        })

    questions.sort(key=lambda q: {"high": 0, "medium": 1, "low": 2}.get(
        q["priority"], 2))
    return questions


# ── MAIN ENTRY POINT ──────────────────────────────────────────────

def enrich_brain_from_web(query: str, brain: dict) -> dict:
    from brain_query import find_entry_nodes

    print(f"[web] enriching brain for: '{query}'")
    entry_nodes = find_entry_nodes(brain, query)
    if not entry_nodes:
        return {"query": query, "status": "no_relevant_nodes",
                "message": "No relevant nodes found to enrich.",
                "questions": [], "searches_run": 0}

    print(f"[web] target nodes: {[n['label'] for n in entry_nodes]}")
    queries = build_search_queries(query, entry_nodes, brain)
    if not queries:
        return {"query": query, "status": "no_queries",
                "message": "Could not generate search queries.",
                "questions": [], "searches_run": 0}

    web_results = fetch_web_facts(queries)
    if not web_results:
        return {"query": query, "status": "no_results",
                "message": "Web search returned no usable results.",
                "questions": [], "searches_run": len(queries)}

    actions = compare_to_brain(web_results, brain, entry_nodes)
    questions = actions_to_act_questions(actions)

    counts = {
        "conflict": sum(1 for q in questions if q["category"] == "web_conflict"),
        "enrich": sum(1 for q in questions if q["category"] == "web_enrich"),
        "new": sum(1 for q in questions if q["category"] == "web_new"),
    }
    print(f"[web] {len(questions)} actions: {counts['conflict']} conflicts, "
          f"{counts['enrich']} enrichments, {counts['new']} new")

    return {
        "query": query, "status": "ok",
        "searches_run": len(queries), "queries_used": queries,
        "target_nodes": [n["label"] for n in entry_nodes],
        "questions": questions, "counts": counts,
        "message": (f"Found {len(questions)} web findings: "
                    f"{counts['conflict']} conflicts, "
                    f"{counts['enrich']} enrichments, "
                    f"{counts['new']} new facts"),
    }
