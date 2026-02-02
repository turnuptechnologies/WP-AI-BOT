from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, List

from services.user_data import get_all_user_data
from services.funnel_store import load_funnel, save_funnel, is_stale
from services.defects_exact import compute_exact_open_defects


def json_safe(obj: Any) -> Any:
    if isinstance(obj, datetime):
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


def _norm(s: str) -> str:
    return _to_str(s).strip().lower()


def _find_table_rows(data: Dict[str, Any], suffix: str) -> List[Dict[str, Any]]:
    """
    WordPress tables can be: wp_users, wp168_users, mwi_wp_users, etc.
    We find the FIRST key that endswith suffix and looks like list[dict].
    """
    for k, v in data.items():
        if k.endswith(suffix) and isinstance(v, list) and (len(v) == 0 or isinstance(v[0], dict)):
            return v
    return []


def extract_cs_vessel_meta(user_data: Dict[str, Any]) -> Optional[str]:
    """
    Find cs_vessel_data meta_value from any meta table we already loaded
    (usermeta/postmeta) included in get_all_user_data().
    """
    for _, rows in user_data.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("meta_key") == "cs_vessel_data":
                mv = row.get("meta_value")
                if mv:
                    return _to_str(mv)
    return None


def build_user_funnel(user_id: int) -> Dict[str, Any]:
    raw = json_safe(get_all_user_data(user_id))

    funnel: Dict[str, Any] = {
        "user_id": user_id,
        "profile": {},
        "content_index": [],
        "meta_index": {},
        "computed_indexes": {},
        "last_sync": datetime.utcnow().isoformat() + "Z",
    }

    # -------------------------
    # Profile (dynamic users table)
    # -------------------------
    users_rows = _find_table_rows(raw, "users")
    if users_rows:
        u = users_rows[0]
        funnel["profile"] = {
            "ID": u.get("ID"),
            "username": u.get("user_login"),
            "email": u.get("user_email"),
            "display_name": u.get("display_name"),
            "registered": u.get("user_registered"),
        }

    # -------------------------
    # Content index (dynamic posts table)
    # -------------------------
    posts_rows = _find_table_rows(raw, "posts")
    for p in posts_rows:
        if not isinstance(p, dict):
            continue
        funnel["content_index"].append(
            {
                "id": p.get("ID"),
                "title": p.get("post_title"),
                "type": p.get("post_type"),
                "date": p.get("post_date"),
                "status": p.get("post_status"),
            }
        )

    # -------------------------
    # Meta index
    # -------------------------
    meta_keys = set()
    for rows in raw.values():
        if isinstance(rows, list):
            for r in rows:
                if isinstance(r, dict) and "meta_key" in r:
                    meta_keys.add(_to_str(r.get("meta_key")))

    funnel["meta_index"] = {
        "important_keys": sorted(list(meta_keys)),
        "cs_vessel_data_exists": "cs_vessel_data" in meta_keys,
    }

    # -------------------------
    # Exact defect computation (SAFE)
    # -------------------------
    open_count = 0
    open_hours = 0.0

    cs_meta = extract_cs_vessel_meta(raw)
    if cs_meta:
        exact = compute_exact_open_defects(cs_meta)  # returns ok True/False
        if isinstance(exact, dict) and exact.get("ok") is True:
            open_count = int(exact.get("open_defects_count") or 0)
            open_hours = float(exact.get("open_defect_hours") or 0.0)
        else:
            # Keep 0s; store error for debugging
            funnel["computed_indexes"]["cs_error"] = exact.get("error") if isinstance(exact, dict) else "unknown error"
    else:
        funnel["computed_indexes"]["cs_error"] = "cs_vessel_data not found"

    funnel["computed_indexes"]["open_defects_count"] = open_count
    funnel["computed_indexes"]["open_defect_hours"] = open_hours

    return funnel


def detect_intent(question: str) -> str:
    q = _norm(question)

    if "profile" in q or "account" in q or "my details" in q:
        return "profile_summary"

    if "defect" in q or "corrosion" in q:
        return "defects_overview"

    if "hour" in q or "hours" in q:
        return "defect_hours"

    return "general_query"


def generate_bot_response(user_id: int, question: str) -> Dict[str, Any]:
    funnel = load_funnel(user_id)

    if funnel is None or is_stale(funnel):
        funnel = build_user_funnel(user_id)
        save_funnel(user_id, funnel)

    intent = detect_intent(question)

    # -------------------------
    # PROFILE
    # -------------------------
    if intent == "profile_summary":
        p = funnel.get("profile", {}) or {}
        return {
            "ok": True,
            "intent": intent,
            "answer": (
                f"Here are your profile details:\n\n"
                f"- Name: {p.get('display_name') or ''}\n"
                f"- Username: {p.get('username') or ''}\n"
                f"- Email: {p.get('email') or ''}\n"
                f"- Registered: {p.get('registered') or ''}"
            ),
            "data": p,
            "user_funnel": funnel,
        }

    # -------------------------
    # DEFECT OVERVIEW (EXACT)
    # -------------------------
    if intent == "defects_overview":
        stats = funnel.get("computed_indexes", {}) or {}
        open_count = int(stats.get("open_defects_count") or 0)
        open_hours = float(stats.get("open_defect_hours") or 0.0)

        if open_count == 0 and stats.get("cs_error"):
            return {
                "ok": True,
                "intent": intent,
                "answer": (
                    "I couldn't compute your defects from cs_vessel_data yet. "
                    f"Reason: {stats.get('cs_error')}"
                ),
                "data": stats,
                "user_funnel": funnel,
            }

        return {
            "ok": True,
            "intent": intent,
            "answer": (
                f"You currently have **{open_count} open defects** "
                f"with a total of **{open_hours:g} open defect hours**."
            ),
            "data": {
                "open_defects_count": open_count,
                "open_defect_hours": open_hours,
            },
            "user_funnel": funnel,
        }

    # -------------------------
    # DEFECT HOURS (EXACT)
    # -------------------------
    if intent == "defect_hours":
        stats = funnel.get("computed_indexes", {}) or {}
        open_hours = float(stats.get("open_defect_hours") or 0.0)

        if open_hours == 0 and stats.get("cs_error"):
            return {
                "ok": True,
                "intent": intent,
                "answer": f"I couldn't compute hours yet. Reason: {stats.get('cs_error')}",
                "data": stats,
                "user_funnel": funnel,
            }

        return {
            "ok": True,
            "intent": intent,
            "answer": f"The total open defect work hours are **{open_hours:g} hours**.",
            "data": {"open_defect_hours": open_hours},
            "user_funnel": funnel,
        }

    # -------------------------
    # FALLBACK
    # -------------------------
    return {
        "ok": True,
        "intent": intent,
        "answer": (
            "I’ve synced your account data. Ask me about your profile, defects, "
            "open defect hours, vessels, or reports."
        ),
        "data": {},
        "user_funnel": funnel,
    }
