from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
STORAGE_DIR = os.path.join(BASE_DIR, "storage")
FUNNEL_DIR = os.path.join(STORAGE_DIR, "funnels")

os.makedirs(FUNNEL_DIR, exist_ok=True)

DEFAULT_TTL_MINUTES = int(os.getenv("FUNNEL_TTL_MINUTES") or "60")


def _path(user_id: int) -> str:
    return os.path.join(FUNNEL_DIR, f"{user_id}.json")


def load_funnel(user_id: int) -> Optional[Dict[str, Any]]:
    fp = _path(user_id)
    if not os.path.exists(fp):
        return None
    try:
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_funnel(user_id: int, funnel: Dict[str, Any]) -> None:
    fp = _path(user_id)
    tmp = fp + ".tmp"
    os.makedirs(FUNNEL_DIR, exist_ok=True)

    # stamp sync time
    funnel = dict(funnel)
    funnel.setdefault("user_id", user_id)
    funnel["last_sync"] = datetime.utcnow().isoformat() + "Z"

    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(funnel, f, ensure_ascii=False, indent=2)

    os.replace(tmp, fp)


def is_stale(funnel: Dict[str, Any], ttl_minutes: int = DEFAULT_TTL_MINUTES) -> bool:
    try:
        last_sync = funnel.get("last_sync")
        if not last_sync:
            return True

        clean = str(last_sync).replace("Z", "")
        dt = datetime.fromisoformat(clean)
        return datetime.utcnow() - dt > timedelta(minutes=ttl_minutes)
    except Exception:
        return True

