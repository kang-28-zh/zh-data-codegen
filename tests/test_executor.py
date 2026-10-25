"""core/executor.py 的单元测试。

覆盖 15 个用例：SQL 7 类、Pandas 4 类、run_tests 2 类、临时目录 helper 2 类。
"""

# 文件结构：① SQL 用例 ② Pandas 用例 ③ run_tests 用例 ④ 临时目录 helper 用例

import os
import shutil

from core.executor import _make_temp_dir, execute_pandas, execute_sql, run_tests


# 基础样例数据
CSV_BASIC = "order_id,amount,status\n1,100,paid\n2,200,refund\n3,300,paid\n"

# 中文列名样例数据
CSV_CHINESE = "订单号,金额\n1,100\n2,200\n"

# 只有表头、没有任何数据行
CSV_HEADER_ONLY = "order_id,amount,status\n"

# 50 行样例数据，用于验证 rows 截断
CSV_50 = "order_id,amount\n" + "".join(f"{index},{index * 10}\n" for index in range(1, 51))


def test_execute_sql_success():
    """正常 SELECT：返回列名、数据、真实行数与耗时。"""
    result = execute_sql(
        "SELECT order_id, amount FROM t WHERE status = 'paid'", CSV_BASIC
    )

    assert result.success is True
    assert result.columns == ["order_id", "amount"]
    assert result.row_count == 2
    assert len(result.rows) == 2
    assert result.elapsed_ms >= 0
    assert result.error is None


def test_execute_sql_syntax_error():
    """语法错误：success=False，error 保留数据库原始信息。"""
    result = execute_sql("SELECT FROM", CSV_BASIC)

    assert result.success is False
    assert result.error is not None
    assert result.error.startswith("[executor] SQL 执行失败：")
    assert len(result.error) > len("[executor] SQL 执行失败：")


def test_execute_sql_missing_column():
    """引用不存在字段：success=False，error 含字段名。"""
    result = execute_sql("SELECT nonexistent_column FROM t", CSV_BASIC)

    assert result.success is False
    assert "nonexistent_column" in result.error


def test_execute_sql_quoted_chinese_columns():
    """5a：列名已加双引号的中文 SQL 应正常执行。"""
    result = execute_sql('SELECT "订单号", "金额" FROM t', CSV_CHINESE)

    assert result.success is True
    assert result.columns == ["订单号", "金额"]
    assert result.row_count == 2


def test_execute_sql_unquoted_chinese_rejected():
    """5b：未加双引号的中文列名应明确报错，不做自动修复。"""
    result = execute_sql("SELECT 订单号 FROM t", CSV_CHINESE)

    assert result.success is False
    assert "中文列名需要加双引号" in result.error


def test_execute_sql_truncates_rows():
    """返回 50 行时：rows 截断为 20，row_count 仍是真实总行数 50。"""
    result = execute_sql("SELECT * FROM t", CSV_50)

    assert result.success is True
    assert result.row_count == 50
    assert len(result.rows) == 20


def test_execute_sql_empty_table():
    """空表查询应成功返回 0 行，不报错。"""
    result = execute_sql("SELECT * FROM t", CSV_HEADER_ONLY)

    assert result.success is True
    assert result.row_count == 0
    assert result.rows == []


def test_execute_pandas_success():
    """正常 Pandas 脚本输出合法 JSON：success=True，行数正确。"""
    script = (
        "import json, os\n"
        "import pandas as pd\n"
        "df = pd.read_csv(os.environ['SAMPLE_CSV_PATH'])\n"
        "paid = df[df['status'] == 'paid']\n"
        "print(json.dumps({\n"
        "    'columns': list(paid.columns),\n"
        "    'rows': json.loads(paid.to_json(orient='records')),\n"
        "}))\n"
    )

    result = execute_pandas(script, CSV_BASIC)

    assert result.success is True
    assert result.row_count == 2
    assert len(result.rows) == 2
    assert result.error is None


def test_execute_pandas_blocks_eval():
    """脚本包含 eval 时应被基础防护拦截。"""
    result = execute_pandas("eval('1 + 1')", CSV_BASIC)

    assert result.success is False
    assert "eval" in result.error


def test_execute_pandas_timeout():
    """脚本睡眠 15 秒应触发 10 秒超时，返回超时错误。"""
    result = execute_pandas("import time\ntime.sleep(15)\n", CSV_BASIC)

    assert result.success is False
    assert "超时" in result.error


def test_execute_pandas_invalid_json():
    """脚本输出非 JSON 时应报错，并带上原始 stdout。"""
    result = execute_pandas("print('hello')", CSV_BASIC)

    assert result.success is False
    assert "非法 JSON" in result.error
    assert "hello" in result.error


def test_run_tests_all_pass():
    """测试文件全部通过：passed=2，failed=0。"""
    content = (
        "def test_a():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "\n"
        "def test_b():\n"
        "    assert 'a' == 'a'\n"
    )

    result = run_tests(content, CSV_BASIC)

    assert result.total == 2
    assert result.passed == 2
    assert result.failed == 0
    assert len(result.details) == 2
    assert all(item["status"] == "passed" for item in result.details)
    assert len({item["name"] for item in result.details}) == 2


def test_run_tests_partial_failure():
    """测试文件部分失败：passed=1，failed=1，details 含失败用例名与原因。"""
    content = (
        "def test_ok():\n"
        "    assert True\n"
        "\n"
        "\n"
        "def test_bad():\n"
        "    assert 1 == 2\n"
    )

    result = run_tests(content, CSV_BASIC)

    assert result.passed == 1
    assert result.failed == 1

    passed_items = [item for item in result.details if item["status"] == "passed"]
    failed_items = [item for item in result.details if item["status"] == "failed"]
    assert len(passed_items) == 1
    assert len(failed_items) == 1
    assert "test_bad" in failed_items[0]["name"]
    assert "assert" in failed_items[0]["reason"]


def test_make_temp_dir_writable():
    """_make_temp_dir 返回的目录必须存在且可写，清理后应消失。"""
    path = _make_temp_dir("test_")
    try:
        assert os.path.isdir(path)
        target = os.path.join(path, "probe.txt")
        with open(target, "w", encoding="utf-8") as file:
            file.write("ok")
        assert os.path.exists(target)
    finally:
        shutil.rmtree(path, ignore_errors=True)
    assert not os.path.exists(path)


def test_execute_pandas_timeout_cleans_up(monkeypatch):
    """超时执行后，实际使用的工作临时目录应被清理。

    说明：沙箱环境 mkdtemp 目录不可删除，glob 会因历史残留误判，
    因此这里直接捕获 _make_temp_dir 的返回路径，断言工作目录已消失。
    """
    import core.executor as executor_module

    created_paths: list[str] = []
    original = executor_module._make_temp_dir

    def tracking(prefix: str) -> str:
        path = original(prefix)
        created_paths.append(path)
        return path

    monkeypatch.setattr(executor_module, "_make_temp_dir", tracking)

    result = execute_pandas("import time\ntime.sleep(15)\n", CSV_BASIC)

    assert result.success is False
    assert created_paths, "execute_pandas 应创建临时工作目录"
    for path in created_paths:
        assert not os.path.exists(path)
