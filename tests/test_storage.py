"""core/storage.py 的单元测试。

沙箱环境 tmp_path 不可写，统一用 core.executor._make_temp_dir 创建临时目录。
"""

import os
import shutil

from core.executor import _make_temp_dir
from core.storage import init_db, insert_run, read_recent_runs


def test_storage_creates_table():
    """首次调用 init_db 应自动建表，不报错。"""
    tmpdir = _make_temp_dir("zh_storage_test_")
    try:
        db_path = os.path.join(tmpdir, "history.db")

        init_db(db_path)

        assert os.path.exists(db_path)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_storage_insert_and_read():
    """插入一条记录后，能按最近记录读出且字段值正确。"""
    tmpdir = _make_temp_dir("zh_storage_test_")
    try:
        db_path = os.path.join(tmpdir, "history.db")
        record = {
            "requirement": "统计复购率",
            "schema_text": "CREATE TABLE t (a TEXT)",
            "sample_csv": "a\n1\n",
            "sql_success": True,
            "sql_text": "SELECT a FROM t",
            "sql_risks": "[]",
            "pandas_success": False,
            "pandas_text": "",
            "pandas_risks": "[]",
            "tests_status": "passed",
            "tests_content": "def test_a():\n    assert True\n",
        }

        run_id = insert_run(record, db_path=db_path)
        runs = read_recent_runs(1, db_path=db_path)

        assert run_id >= 1
        assert len(runs) == 1
        assert runs[0]["requirement"] == "统计复购率"
        assert runs[0]["sql_success"] == 1
        assert runs[0]["pandas_success"] == 0
        assert runs[0]["tests_status"] == "passed"
        assert runs[0]["created_at"]
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
