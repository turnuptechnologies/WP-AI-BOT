from core.db import get_db_connection
from core.config import USER_REFERENCE_COLUMNS

def get_all_user_data(user_id: int):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    data = {}

    try:
        cursor.execute("SHOW TABLES")
        tables = [list(row.values())[0] for row in cursor.fetchall()]

        for table in tables:
            cursor.execute(f"SHOW COLUMNS FROM `{table}`")
            columns = [c["Field"] for c in cursor.fetchall()]

            matched = USER_REFERENCE_COLUMNS.intersection(columns)
            if not matched:
                continue

            where = " OR ".join([f"`{c}` = %s" for c in matched])
            params = tuple([user_id] * len(matched))

            cursor.execute(f"SELECT * FROM `{table}` WHERE {where}", params)
            rows = cursor.fetchall()

            if rows:
                data[table] = rows

        return data

    finally:
        cursor.close()
        conn.close()
