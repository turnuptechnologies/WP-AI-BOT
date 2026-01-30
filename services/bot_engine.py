"""
wp_vessel_bot.py

Deterministic answers from WordPress usermeta:
- meta_key: cs_vessel_data
- meta_value: PHP serialized string (a:...{...})

What it returns (example):
- vessel
- status (Found / Not Found)
- open_defects
- low / medium / high / unknown  (based on priority_rating)
- total_records (how many observations found for that vessel)
- latest_inspection_date (best-effort)

Install:
  pip install phpserialize

Env:
  XAI_API_KEY or GROK_API_KEY (only needed if you keep FORMAT_WITH_AI=True)

Run:
  python wp_vessel_bot.py 15 "Only give me Earth Clipper status and defects count with status."
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Tuple

import phpserialize  # pip install phpserialize

# If you want AI formatting (optional)
FORMAT_WITH_AI = False
MODEL = "grok-4-fast"

if FORMAT_WITH_AI:
    from xai_sdk import Client
    from xai_sdk.chat import system, user
    API_KEY = os.getenv("XAI_API_KEY") or os.getenv("GROK_API_KEY")
    if not API_KEY:
        raise ValueError("Missing XAI_API_KEY / GROK_API_KEY")
    client = Client(api_key=API_KEY)

# Your working function
from services.user_data import get_all_user_data  # ✅


# ----------------------------
# Utilities
# ----------------------------

def json_safe(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [json_safe(v) for v in obj]
    return obj


def _to_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, bytes):
        try:
            return x.decode("utf-8", errors="ignore")
        except Exception:
            return str(x)
    return str(x)


def _normalize_name(s: str) -> str:
    """
    Normalize vessel names so small differences don't break matching.
    - lowercase
    - strip
    - collapse spaces
    - remove common punctuation
    """
    s = s.lower().strip()
    s = re.sub(r"[\-_]+", " ", s)
    s = re.sub(r"[^\w\s]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def deserialize_php_meta(meta_value: str) -> Any:
    """
    Converts PHP serialized string into Python structures.
    """
    if not meta_value or not isinstance(meta_value, str):
        return None
    try:
        raw = phpserialize.loads(meta_value.encode("utf-8"), decode_strings=True)
        return raw
    except Exception:
        return None


def find_cs_vessel_meta(user_data: Any) -> Optional[str]:
    """
    Your get_all_user_data(user_id) seems to return usermeta rows like:
    {
      "umeta_id": ...,
      "meta_key": "cs_vessel_data",
      "meta_value": "a:..."
    }

    This function tries to find that row anywhere in user_data.
    """
    # Common patterns: user_data might be dict with "usermeta" or "meta" or list
    if isinstance(user_data, dict):
        # direct
        if user_data.get("meta_key") == "cs_vessel_data":
            return user_data.get("meta_value")

        # scan possible containers
        for key in ["usermeta", "user_meta", "meta", "metadata", "rows", "data"]:
            val = user_data.get(key)
            if isinstance(val, list):
                for row in val:
                    if isinstance(row, dict) and row.get("meta_key") == "cs_vessel_data":
                        return row.get("meta_value")

        # fallback: deep scan dict values that are lists of dicts
        for v in user_data.values():
            if isinstance(v, list):
                for row in v:
                    if isinstance(row, dict) and row.get("meta_key") == "cs_vessel_data":
                        return row.get("meta_value")

    if isinstance(user_data, list):
        for row in user_data:
            if isinstance(row, dict) and row.get("meta_key") == "cs_vessel_data":
                return row.get("meta_value")
    return None


def extract_available_vessel_names(cs_data: Any) -> List[str]:
    """
    cs_data is typically a dict-like structure (from phpserialize).
    It looks like: {0: {...}, 1: {...}, ...}
    Each item has vessel_name.
    """
    names = set()

    if isinstance(cs_data, dict):
        for _, item in cs_data.items():
            if isinstance(item, dict):
                vn = item.get("vessel_name") or item.get("vessel")
                vn = _to_str(vn).strip()
                if vn:
                    names.add(vn)

    return sorted(names)


def detect_vessel_name_from_question(question: str, available_names: List[str], default_name: str = "") -> str:
    """
    Best effort:
    - If any available vessel name appears in question (normalized contains), use it.
    - Else, use default_name if provided.
    """
    qn = _normalize_name(question)
    scored: List[Tuple[int, str]] = []

    for name in available_names:
        nn = _normalize_name(name)
        if not nn:
            continue
        if nn in qn:
            scored.append((len(nn), name))  # longer match wins

    if scored:
        scored.sort(reverse=True)
        return scored[0][1]

    return default_name


def parse_inspection_date(s: str) -> Optional[datetime]:
    """
    Your sample shows: 18/01/2026 (DD/MM/YYYY)
    """
    s = s.strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


def compute_defect_summary(cs_data: Any, vessel_name: str) -> Dict[str, Any]:
    """
    Filters all observations for a vessel_name and computes:
    - open_defects
    - priority buckets (low/medium/high/unknown) based on priority_rating
    - latest inspection date
    """
    target = _normalize_name(vessel_name)
    if not target:
        return {
            "vessel": vessel_name,
            "status": "Not Found",
            "open_defects": 0,
            "low": 0,
            "medium": 0,
            "high": 0,
            "unknown": 0,
            "total_records": 0,
            "latest_inspection_date": None,
        }

    open_defects = 0
    low = medium = high = unknown = 0
    total = 0
    latest_date: Optional[datetime] = None
    found_any = False

    if isinstance(cs_data, dict):
        for _, item in cs_data.items():
            if not isinstance(item, dict):
                continue

            vn = _to_str(item.get("vessel_name") or "").strip()
            if _normalize_name(vn) != target:
                continue

            found_any = True
            total += 1

            obs = item.get("observation_data") or {}
            if not isinstance(obs, dict):
                obs = {}

            status = _to_str(obs.get("defects_status") or "").strip().lower()
            if status == "open":
                open_defects += 1

            pr = _to_str(obs.get("priority_rating") or "").strip()
            # Common mapping: 1=low, 2=medium, 3=high (adjust if your business rules differ)
            if pr == "1":
                low += 1
            elif pr == "2":
                medium += 1
            elif pr == "3":
                high += 1
            else:
                unknown += 1

            d = parse_inspection_date(_to_str(obs.get("inspection_date") or ""))
            if d and (latest_date is None or d > latest_date):
                latest_date = d

    return {
        "vessel": vessel_name,
        "status": "Found" if found_any else "Not Found",
        "open_defects": open_defects,
        "low": low,
        "medium": medium,
        "high": high,
        "unknown": unknown,
        "total_records": total,
        "latest_inspection_date": latest_date.date().isoformat() if latest_date else None,
    }


def format_answer(payload: Dict[str, Any], question: str) -> str:
    """
    Option A: No AI formatting (recommended for accuracy)
    Option B: AI formatting only (temp=0)
    """
    if not FORMAT_WITH_AI:
        return (
            f"Vessel: {payload['vessel']}\n"
            f"Status: {payload['status']}\n"
            f"Open defects: {payload['open_defects']}\n"
            f"Low: {payload['low']} | Medium: {payload['medium']} | High: {payload['high']} | Unknown: {payload['unknown']}\n"
        )

    # AI formatting only
    prompt = f"""
Format the vessel summary for the user. Do not add, guess, or modify values.

USER QUESTION:
{question}

DATA:
{json.dumps(payload, indent=2)}
""".strip()

    chat = client.chat.create(model=MODEL, temperature=0)
    chat.append(system("Format only. No hallucination. Output plain text."))
    chat.append(user(prompt))
    response = chat.sample()
    return (response.content or "").strip()


def generate_bot_response(user_id: int, question: str, default_vessel: str = "") -> Dict[str, Any]:
    # 1) Get user data
    user_data = json_safe(get_all_user_data(user_id))

    # 2) Extract cs_vessel_data meta_value
    meta_value = find_cs_vessel_meta(user_data)
    if not meta_value:
        payload = {
            "vessel": default_vessel or "Unknown",
            "status": "cs_vessel_data Not Found",
            "open_defects": 0,
            "low": 0,
            "medium": 0,
            "high": 0,
            "unknown": 0,
            "total_records": 0,
            "latest_inspection_date": None,
        }
        return {"question": question, "answer": format_answer(payload, question), "raw_data": payload}

    # 3) Deserialize PHP
    cs_data = deserialize_php_meta(meta_value)
    if cs_data is None:
        payload = {
            "vessel": default_vessel or "Unknown",
            "status": "Invalid PHP Serialized Data",
            "open_defects": 0,
            "low": 0,
            "medium": 0,
            "high": 0,
            "unknown": 0,
            "total_records": 0,
            "latest_inspection_date": None,
        }
        return {"question": question, "answer": format_answer(payload, question), "raw_data": payload}

    # 4) Detect vessel name
    available_names = extract_available_vessel_names(cs_data)
    vessel_name = detect_vessel_name_from_question(question, available_names, default_name=default_vessel)

    # 5) Compute summary deterministically
    payload = compute_defect_summary(cs_data, vessel_name)

    # 6) Format answer
    answer_text = format_answer(payload, question)

    return {"question": question, "answer": answer_text, "raw_data": payload}


# ----------------------------
# CLI
# ----------------------------
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python wp_vessel_bot.py <user_id> <question>")
        sys.exit(1)

    uid = int(sys.argv[1])
    q = " ".join(sys.argv[2:])

    result = generate_bot_response(uid, q, default_vessel="Earth Clipper")
    print(json.dumps(result, indent=2))
