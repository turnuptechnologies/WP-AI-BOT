from __future__ import annotations

from typing import Any, Dict, List, Set, Tuple
from datetime import datetime, date

from core.db import get_db_connection
from core.config import USER_REFERENCE_COLUMNS, WP_DB_PREFIX


# --------------------------
# Tunables (reduce payload)
# --------------------------
POST_CONTENT_MAX_CHARS = 3500          # trim post_content
META_VALUE_MAX_CHARS = 12000           # trim very large meta_value
MAX_POSTS = 200                        # hard cap
MAX_META_ROWS = 8000                   # hard cap for postmeta
MAX_USERMETA_ROWS = 3000               # hard cap for usermeta

# Only fetch these meta keys by default (add/remove as per your system)
IMPORTANT_POSTMETA_KEYS = {
    "client",
    "cs_vessel_data",
    "status",
    "introduction",
    "thickness_measurements",
    "report_type",
    "report_year",
    "vessel_id",
    "void_id",
    "_thumbnail_id",
}

IMPORTANT_USERMETA_KEYS = {
    "first_name",
    "last_name",
    "nickname",
    "description",
    "wp_capabilities",
    "wp_user_level",
    "cs_vessel_data",
}

# Tables that almost always cause duplication / huge payloads
SKIP_FALLBACK_TABLES_CONTAINS = (
    "options",
    "actionscheduler",
    "action_scheduler",
    "woocommerce",
    "wc_",
    "yoast",
    "rankmath",
    "mailpoet",
    "icl_",          # WPML
    "wpforms",
    "gf_",           # Gravity forms
    "logs",
)

# Fallback scan should not re-pull core WP tables we already fetch explicitly
SKIP_FALLBACK_EXACT = set()


def _t(name: str) -> str:
    return f"{WP_DB_PREFIX}{name}"


def _trim_text(s: Any, max_chars: int) -> Any:
    if s is None:
        return None
    if isinstance(s, (bytes, bytearray)):
        try:
            s = s.decode("utf-8", errors="ignore")
        except Exception:
            s = str(s)
    if not isinstance(s, str):
        return s
    if len(s) <= max_chars:
        return s
    return s[:max_chars] + f"…(trimmed {len(s) - max_chars} chars)"


def _dedup_rows(rows: List[Dict[str, Any]], key_fields: Tuple[str, ...]) -> List[Dict[str, Any]]:
    """
    Deduplicate list[dict] using given key fields.
    """
    seen: Set[Tuple[Any, ...]] = set()
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        k = tuple(r.get(f) for f in key_fields)
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


def _should_skip_table(name: str) -> bool:
    low = name.lower()
    for piece in SKIP_FALLBACK_TABLES_CONTAINS:
        if piece in low:
            return True
    if name in SKIP_FALLBACK_EXACT:
        return True
    return False


def get_all_user_data(user_id: int, full: bool = False) -> Dict[str, Any]:
    """
    Fetch WordPress user-related data (deduped + size-controlled).
    - full=False (default): smaller payload, fast for AI agent usage
    - full=True: bigger payload but still trimmed/deduped
    """

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    data: Dict[str, Any] = {
        "meta": {
            "user_id": user_id,
            "wp_prefix": WP_DB_PREFIX,
            "mode": "full" if full else "compact",
        }
    }

    try:
        # --------------------------
        # 1) Users
        # --------------------------
        cursor.execute(f"SELECT * FROM `{_t('users')}` WHERE ID = %s LIMIT 1", (user_id,))
        users = cursor.fetchall() or []
        if users:
            data[_t("users")] = users

        # --------------------------
        # 2) Usermeta (optionally filtered)
        # --------------------------
        if full:
            cursor.execute(
                f"SELECT * FROM `{_t('usermeta')}` WHERE user_id = %s LIMIT {MAX_USERMETA_ROWS}",
                (user_id,),
            )
        else:
            # only important keys to keep payload small
            placeholders = ",".join(["%s"] * len(IMPORTANT_USERMETA_KEYS))
            cursor.execute(
                f"""
                SELECT * FROM `{_t('usermeta')}`
                WHERE user_id = %s
                  AND meta_key IN ({placeholders})
                LIMIT {MAX_USERMETA_ROWS}
                """,
                (user_id, *IMPORTANT_USERMETA_KEYS),
            )

        usermeta = cursor.fetchall() or []
        # trim huge meta_value
        for r in usermeta:
            if "meta_value" in r:
                r["meta_value"] = _trim_text(r["meta_value"], META_VALUE_MAX_CHARS)

        usermeta = _dedup_rows(usermeta, ("umeta_id",))
        if usermeta:
            data[_t("usermeta")] = usermeta

        # --------------------------
        # 3) Posts (author OR client meta link)
        # - Avoid revisions, trash, auto-drafts
        # - Optional: restrict post_types if you want
        # --------------------------
        cursor.execute(
            f"""
            SELECT *
            FROM `{_t('posts')}`
            WHERE post_author = %s
              AND post_status IN ('publish','private')
              AND post_type NOT IN ('revision','nav_menu_item','attachment')
            ORDER BY post_date DESC
            LIMIT {MAX_POSTS}
            """,
            (user_id,),
        )
        posts_by_author = cursor.fetchall() or []

        cursor.execute(
            f"""
            SELECT p.*
            FROM `{_t('posts')}` p
            INNER JOIN `{_t('postmeta')}` pm ON pm.post_id = p.ID
            WHERE pm.meta_key = 'client'
              AND pm.meta_value = %s
              AND p.post_status IN ('publish','private')
              AND p.post_type NOT IN ('revision','nav_menu_item','attachment')
            ORDER BY p.post_date DESC
            LIMIT {MAX_POSTS}
            """,
            (str(user_id),),
        )
        posts_by_client_meta = cursor.fetchall() or []

        post_map: Dict[int, Dict[str, Any]] = {}
        for p in posts_by_author + posts_by_client_meta:
            pid = int(p["ID"])
            # trim post_content for AI
            if "post_content" in p:
                p["post_content"] = _trim_text(p["post_content"], POST_CONTENT_MAX_CHARS)
            post_map[pid] = p

        all_posts = list(post_map.values())
        # stable ordering (newest first)
        all_posts.sort(key=lambda x: str(x.get("post_date") or ""), reverse=True)

        if all_posts:
            data[_t("posts")] = all_posts

        # --------------------------
        # 4) Postmeta for posts
        # - compact: only important keys
        # - full: all meta but trimmed + capped
        # --------------------------
        if all_posts:
            post_ids = [p["ID"] for p in all_posts]
            placeholders = ",".join(["%s"] * len(post_ids))

            if full:
                cursor.execute(
                    f"""
                    SELECT * FROM `{_t('postmeta')}`
                    WHERE post_id IN ({placeholders})
                    LIMIT {MAX_META_ROWS}
                    """,
                    tuple(post_ids),
                )
            else:
                key_placeholders = ",".join(["%s"] * len(IMPORTANT_POSTMETA_KEYS))
                cursor.execute(
                    f"""
                    SELECT * FROM `{_t('postmeta')}`
                    WHERE post_id IN ({placeholders})
                      AND meta_key IN ({key_placeholders})
                    LIMIT {MAX_META_ROWS}
                    """,
                    tuple(post_ids) + tuple(IMPORTANT_POSTMETA_KEYS),
                )

            postmeta = cursor.fetchall() or []
            for r in postmeta:
                if "meta_value" in r:
                    r["meta_value"] = _trim_text(r["meta_value"], META_VALUE_MAX_CHARS)

            # dedup: (post_id, meta_key, meta_id) is enough
            postmeta = _dedup_rows(postmeta, ("meta_id",))
            if postmeta:
                data[_t("postmeta")] = postmeta

            # --------------------------
            # 5) Terms/Taxonomies for posts (small already)
            # --------------------------
            cursor.execute(
                f"""
                SELECT tr.object_id, tt.taxonomy, tm.term_id, tm.name, tt.description
                FROM `{_t('term_relationships')}` tr
                INNER JOIN `{_t('term_taxonomy')}` tt ON tr.term_taxonomy_id = tt.term_taxonomy_id
                INNER JOIN `{_t('terms')}` tm ON tt.term_id = tm.term_id
                WHERE tr.object_id IN ({placeholders})
                """,
                tuple(post_ids),
            )
            terms = cursor.fetchall() or []
            terms = _dedup_rows(terms, ("object_id", "taxonomy", "term_id"))
            if terms:
                data["terms_join"] = terms

        # --------------------------
        # 6) Fallback scan (VERY RESTRICTED)
        # - compact mode: skip fallback completely (recommended)
        # - full mode: scan but skip noisy tables, and do NOT use meta_key/meta_value matching
        # --------------------------
        if full:
            cursor.execute("SHOW TABLES")
            tables = [list(row.values())[0] for row in cursor.fetchall()]

            # avoid matching meta columns that cause duplication everywhere
            matched_cols_blacklist = {"meta_key", "meta_value"}

            for table in tables:
                if table in data:
                    continue
                if _should_skip_table(table):
                    continue

                cursor.execute(f"SHOW COLUMNS FROM `{table}`")
                columns = [c["Field"] for c in cursor.fetchall()]

                matched = (USER_REFERENCE_COLUMNS.intersection(columns)) - matched_cols_blacklist
                if not matched:
                    continue

                where = " OR ".join([f"`{c}` = %s" for c in matched])
                params = tuple([user_id] * len(matched))

                cursor.execute(f"SELECT * FROM `{table}` WHERE {where} LIMIT 2000", params)
                rows = cursor.fetchall() or []
                if rows:
                    data[table] = rows

        return data

    finally:
        cursor.close()
        conn.close()
