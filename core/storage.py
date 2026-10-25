"""历史记录存储模块（SQLite）。

职责：启动时建表，提供插入与最近 N 条读取；只做 INSERT / SELECT。
"""

import json
import os
import sqlite3
from datetime import datetime


ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(ROOT_DIR, "storage", "history.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    requirement TEXT NOT NULL,
    schema_text TEXT NOT NULL,
    sample_csv TEXT NOT NULL,
    sql_success INTEGER,
    sql_text TEXT,
    sql_risks TEXT,
    pandas_success INTEGER,
    pandas_text TEXT,
    pandas_risks TEXT,
    tests_status TEXT,
    tests_content TEXT
)
"""

COLUMNS = (
    "created_at",
    "requirement",
    "schema_text",
    "sample_csv",
    "sql_success",
    "sql_text",
    "sql_risks",
    "pandas_success",
    "pandas_text",
    "pandas_risks",
    "tests_status",
    "tests_content",
)


def _connect(db_path: str) -> sqlite3.Connection:
    """建立连接，并确保数据库所在目录存在。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    return sqlite3.connect(db_path)


def init_db(db_path: str = DB_PATH) -> None:
    """建表（幂等）。"""
    with _connect(db_path) as connection:
        connection.execute(SCHEMA_SQL)


def insert_run(record: dict, db_path: str = DB_PATH) -> int:
    """插入一条运行记录，返回自增 id。"""
    init_db(db_path)
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "requirement": record.get("requirement", ""),
        "schema_text": record.get("schema_text", ""),
        "sample_csv": record.get("sample_csv", ""),
        "sql_success": int(bool(record.get("sql_success", False))),
        "sql_text": record.get("sql_text", ""),
        "sql_risks": record.get("sql_risks", "[]"),
        "pandas_success": int(bool(record.get("pandas_success", False))),
        "pandas_text": record.get("pandas_text", ""),
        "pandas_risks": record.get("pandas_risks", "[]"),
        "tests_status": record.get("tests_status", ""),
        "tests_content": record.get("tests_content", ""),
    }
    placeholders = ", ".join("?" for _ in COLUMNS)
    sql = (
        f"INSERT INTO runs ({', '.join(COLUMNS)}) VALUES ({placeholders})"
    )
    with _connect(db_path) as connection:
        cursor = connection.execute(
            sql, tuple(payload[column] for column in COLUMNS)
        )
        return int(cursor.lastrowid)


def read_recent_runs(limit: int = 10, db_path: str = DB_PATH) -> list[dict]:
    """读取最近 limit 条记录，按 id 倒序。"""
    init_db(db_path)
    with _connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def risks_to_json(risks) -> str:
    """把风险列表序列化成 JSON 字符串，供存储使用。"""
    return json.dumps(list(risks), ensure_ascii=False)
