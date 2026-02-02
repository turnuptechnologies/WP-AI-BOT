from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple, List

import phpserialize  # pip install phpserialize


def _to_str(x: Any) -> str:
    if x is None:
        return ""
    if isinstance(x, bytes):
        try:
            return x.decode("utf-8", errors="ignore")
        except Exception:
            return str(x)
    return str(x)


def _norm(s: str) -> str:
    s = _to_str(s).strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def deserialize_php(meta_value: str) -> Any:
    if not meta_value or not isinstance(meta_value, str):
        return None
    try:
        return phpserialize.loads(meta_value.encode("utf-8"), decode_strings=True)
    except Exception:
        return None


def _pick_unique_key(obs: Dict[str, Any], item: Dict[str, Any]) -> str:
    """
    Exact unique key priority:
    1) observation_code (best human stable code)
    2) id (hash-like unique)
    3) fallback composite (last resort)
    """
    code = _to_str(obs.get("observation_code")).strip()
    if code:
        return f"code::{code}"

    rid = _to_str(item.get("id")).strip()
    if rid:
        return f"id::{rid}"

    # last resort fallback
    vessel = _to_str(item.get("vessel_name")).strip()
    void = _to_str(item.get("void_name")).strip()
    date = _to_str(obs.get("inspection_date")).strip()
    desc = _to_str(obs.get("description")).strip()[:120]
    return f"fallback::{vessel}::{void}::{date}::{desc}"


def _parse_hours(v: Any) -> float:
    """
    defect_hours can be "20", "150", "", None
    We treat invalid as 0.0
    """
    s = _to_str(v).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except Exception:
        return 0.0


def build_unique_defects_index_from_cs_vessel_data(meta_value: str) -> Dict[str, Any]:
    """
    Returns a strict unique index of defects from cs_vessel_data.
    Dedup is done by unique key.
    """
    cs_data = deserialize_php(meta_value)
    if not isinstance(cs_data, dict):
        return {
            "ok": False,
            "error": "cs_vessel_data invalid or not dict",
            "unique_defects": {},
            "stats": {},
        }

    unique_defects: Dict[str, Dict[str, Any]] = {}
    excluded_duplicates: List[str] = []

    for _, item in cs_data.items():
        if not isinstance(item, dict):
            continue

        obs = item.get("observation_data") or {}
        if not isinstance(obs, dict):
            obs = {}

        uniq = _pick_unique_key(obs, item)
        if uniq in unique_defects:
            excluded_duplicates.append(uniq)
            continue

        vessel_name = _to_str(item.get("vessel_name")).strip()
        void_name = _to_str(item.get("void_name")).strip()

        status = _norm(obs.get("defects_status"))
        hours = _parse_hours(obs.get("defect_hours"))

        unique_defects[uniq] = {
            "vessel_name": vessel_name,
            "void_name": void_name,
            "status": status,  # "open" / "closed" etc
            "hours": hours,
            "observation_code": _to_str(obs.get("observation_code")).strip(),
            "inspection_date": _to_str(obs.get("inspection_date")).strip(),
            "priority_rating": _to_str(obs.get("priority_rating")).strip(),
            "description": _to_str(obs.get("description")).strip(),
        }

    # overall exact stats
    open_keys = [k for k, d in unique_defects.items() if d.get("status") == "open"]
    open_hours = sum(unique_defects[k]["hours"] for k in open_keys)

    return {
        "ok": True,
        "unique_defects": unique_defects,
        "stats": {
            "unique_defects_total": len(unique_defects),
            "open_defects_count": len(open_keys),
            "open_defect_hours": open_hours,
            "duplicates_skipped": len(excluded_duplicates),
        },
        "audit": {
            "open_keys": open_keys[:5000],  # safety cap
            "duplicates_skipped_keys": excluded_duplicates[:5000],
        },
    }


def compute_exact_open_defects(meta_value: str, vessel_name: Optional[str] = None) -> Dict[str, Any]:
    """
    Exact answer:
    - If vessel_name provided: only that vessel (strict match by normalized name)
    - Else: overall
    """
    index = build_unique_defects_index_from_cs_vessel_data(meta_value)
    if not index.get("ok"):
        return index

    unique_defects = index["unique_defects"]
    target = _norm(vessel_name) if vessel_name else ""

    # filter keys
    keys = list(unique_defects.keys())
    if target:
        keys = [k for k in keys if _norm(unique_defects[k].get("vessel_name")) == target]

    open_keys = [k for k in keys if unique_defects[k].get("status") == "open"]
    open_hours = sum(unique_defects[k]["hours"] for k in open_keys)

    return {
        "ok": True,
        "vessel": vessel_name or "ALL",
        "open_defects_count": len(open_keys),
        "open_defect_hours": open_hours,
        # audit helps you verify exactness
        "audit": {
            "open_keys": open_keys[:5000],
        },
    }
