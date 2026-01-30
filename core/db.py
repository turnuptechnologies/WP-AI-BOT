import os
import mysql.connector
from fastapi import HTTPException
from dotenv import load_dotenv

load_dotenv()  # ensure .env is loaded

def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host=os.getenv("DB_HOST"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
            database=os.getenv("DB_NAME"),  # <-- this MUST be set
            port=int(os.getenv("DB_PORT", 3306)),
            use_pure=True
        )
        return conn
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"DB connection failed: {err}")
