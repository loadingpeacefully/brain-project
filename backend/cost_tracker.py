"""
Universal API Cost Tracker — logs every Claude API call with token counts and cost.
"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

COST_LOG_PATH = Path("data/api_cost_log.jsonl")
COST_LOG_PATH.parent.mkdir(exist_ok=True)

MODEL_PRICING = {
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
}

_lock = threading.Lock()


def _load_totals_from_log():
    """Load cumulative totals from persistent JSONL log on startup."""
    totals = {"total_input_tokens": 0, "total_output_tokens": 0,
              "total_usd": 0.0, "call_count": 0}
    try:
        if COST_LOG_PATH.exists():
            for line in COST_LOG_PATH.read_text().strip().split("\n"):
                if not line.strip():
                    continue
                r = json.loads(line)
                totals["total_input_tokens"] += r.get("input_tokens", 0)
                totals["total_output_tokens"] += r.get("output_tokens", 0)
                totals["total_usd"] = round(totals["total_usd"] + r.get("usd", 0), 6)
                totals["call_count"] += 1
    except Exception:
        pass
    return totals


_startup_totals = _load_totals_from_log()
_session = {
    "session_start": datetime.now(timezone.utc).isoformat(),
    "total_input_tokens": _startup_totals["total_input_tokens"],
    "total_output_tokens": _startup_totals["total_output_tokens"],
    "total_usd": _startup_totals["total_usd"],
    "call_count": _startup_totals["call_count"],
    "last_call_at": None,
    "last_call_usd": 0.0,
    "last_call_tokens": 0,
    "last_touchpoint": "",
}


def record_call(model: str, input_tokens: int, output_tokens: int,
                touchpoint: str = "", duration_ms: int = 0) -> dict:
    pricing = MODEL_PRICING.get(model, {"input": 3.00, "output": 15.00})
    input_cost = (input_tokens / 1_000_000) * pricing["input"]
    output_cost = (output_tokens / 1_000_000) * pricing["output"]
    total_cost = input_cost + output_cost
    total_tokens = input_tokens + output_tokens

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "touchpoint": touchpoint,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "usd": round(total_cost, 6),
        "duration_ms": duration_ms,
    }

    with _lock:
        _session["total_input_tokens"] += input_tokens
        _session["total_output_tokens"] += output_tokens
        _session["total_usd"] = round(_session["total_usd"] + total_cost, 6)
        _session["call_count"] += 1
        _session["last_call_at"] = record["ts"]
        _session["last_call_usd"] = round(total_cost, 6)
        _session["last_call_tokens"] = total_tokens
        _session["last_touchpoint"] = touchpoint

    try:
        with open(COST_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass

    return record


def get_session_stats() -> dict:
    with _lock:
        return dict(_session)


def get_cost_log(limit: int = 100) -> list:
    try:
        lines = COST_LOG_PATH.read_text().strip().split("\n")
        lines = [l for l in lines if l.strip()]
        return [json.loads(l) for l in lines[-limit:]][::-1]
    except Exception:
        return []
