"""core/test_generator.py 的单元测试。

全部用例 patch core.llm.call_llm 与 core.test_generator.run_tests，
不发起真实网络请求、不真实执行生成的测试文件。
"""

# 文件结构：① mock 数据区 ② prompt 构造 ③ 静态校验 ④ 生成与重试 ⑤ 截断

import pytest

import core.test_generator as test_generator_module
from core import llm
from core.models import ColumnInfo, ParsedRequirement, SchemaInfo, TestResult
from core.test_generator import (
    _build_test_prompt,
    _validate_test_content,
    generate_tests,
)


SCHEMA = SchemaInfo(
    table_name="orders",
    columns=[
        ColumnInfo(name="order_id", dtype="TEXT"),
        ColumnInfo(name="sale_name", dtype="TEXT"),
        ColumnInfo(name="amount", dtype="DOUBLE"),
        ColumnInfo(name="status", dtype="TEXT"),
    ],
)

# 200 列的 schema，用于验证 prompt 超长时优先丢弃上一轮测试文件
SCHEMA_BIG = SchemaInfo(
    table_name="big",
    columns=[
        ColumnInfo(name=f"col_{index}", dtype="TEXT") for index in range(200)
    ],
)

PARSED = ParsedRequirement(
    metrics=["amount"],
    dimensions=["sale_name"],
    filters=["status = 'paid'"],
    original_requirement="统计每个销售的成交额，排除退款",
)

MOCK_SQL = (
    "SELECT sale_name, SUM(amount) AS total_amount FROM t "
    "WHERE status = 'paid' GROUP BY sale_name"
)

SAMPLE_CSV = "order_id,sale_name,amount,status\n1,张三,100,paid\n2,李四,200,refund\n"

# 合法测试文件：三类断言齐全、每个用例有中文 docstring
MOCK_VALID_TEST_FILE = '''import os
import json
import pandas as pd
import duckdb

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.csv")
SQL = """SELECT sale_name, SUM(amount) AS total_amount FROM t WHERE status = 'paid' GROUP BY sale_name"""


def test_columns():
    """验证：结果包含销售与汇总金额两列。"""
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    con = duckdb.connect(":memory:")
    con.register("t", df)
    result = con.execute(SQL).df()
    assert set(result.columns) == {"sale_name", "total_amount"}


def test_exclude_refund():
    """验证：过滤退款后结果中不含退款订单。"""
    data = pd.DataFrame({"status": ["paid", "refund"]})
    paid = data[data["status"] != "refund"]
    assert "refund" not in paid["status"].values


def test_exact_value():
    """验证：自造 3 行数据按销售汇总的结果等于手算值。"""
    data = pd.DataFrame({"sale_name": ["A", "A", "B"], "amount": [100, 200, 300]})
    assert data.groupby("sale_name")["amount"].sum()["A"] == 300
'''

# 含禁用空断言的测试文件
MOCK_EMPTY_ASSERT_FILE = '''import os
import pandas as pd
import duckdb

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.csv")
SQL = """SELECT amount FROM t"""


def test_empty_assert():
    """验证：这里故意使用空断言。"""
    result = duckdb.connect(":memory:").execute(SQL).df()
    assert result is not None
'''

# 缺 docstring 的测试文件
MOCK_MISSING_DOCSTRING_FILE = '''import os
import pandas as pd
import duckdb

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.csv")
SQL = """SELECT amount FROM t"""


def test_no_docstring():
    df = pd.DataFrame({"amount": [1]})
    assert df["amount"].sum() == 1
'''

# 同时含禁用断言与缺 docstring 的测试文件
MOCK_MULTI_VIOLATION_FILE = '''import os
import pandas as pd
import duckdb

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.csv")
SQL = """SELECT amount FROM t"""


def test_multi():
    df = pd.DataFrame({"amount": [1]})
    assert df is not None
'''


# 反向验证用：三类断言齐全、docstring 齐全、import 正确的合法测试文件
MOCK_REVERSE_VALID_FILE = '''import os
import json
import pandas as pd
import duckdb

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sample.csv")
SQL = """SELECT order_id, amount FROM t"""


def test_structure():
    """验证：结果列包含 order_id 与 amount。"""
    df = pd.read_csv(CSV_PATH, encoding="utf-8-sig")
    assert list(df.columns) == ["order_id", "amount"]


def test_invariant():
    """验证：金额不会为负数。"""
    df = pd.DataFrame({"amount": [-1, 5]})
    assert (df["amount"] >= 0).sum() == 1


def test_exact():
    """验证：两行金额之和等于手算值。"""
    df = pd.DataFrame({"amount": [100, 200]})
    assert df["amount"].sum() == 300
'''


def _patch_llm(monkeypatch, *responses):
    """patch call_llm：单个响应时每次返回它，多个响应时按顺序返回。"""
    if len(responses) == 1:
        monkeypatch.setattr(llm, "call_llm", lambda prompt: responses[0])
        return
    iterator = iter(responses)
    monkeypatch.setattr(llm, "call_llm", lambda prompt: next(iterator))


def _patch_run_tests(monkeypatch, *results):
    """patch run_tests：单个结果时每次返回它，多个结果时按顺序返回。"""
    if len(results) == 1:
        monkeypatch.setattr(
            test_generator_module, "run_tests", lambda content, csv: results[0]
        )
        return
    iterator = iter(results)
    monkeypatch.setattr(
        test_generator_module, "run_tests", lambda content, csv: next(iterator)
    )


def test_generate_tests_success(monkeypatch):
    """首轮生成的测试文件合法且全部通过。"""
    _patch_llm(monkeypatch, MOCK_VALID_TEST_FILE)
    _patch_run_tests(monkeypatch, TestResult(total=3, passed=3, failed=0))

    result = generate_tests(PARSED, SCHEMA, MOCK_SQL, SAMPLE_CSV)

    assert result.final_status == "passed"
    assert result.attempts == 1
    assert result.content == MOCK_VALID_TEST_FILE.strip()
    assert result.failure_reason is None
    assert result.suggestion is None
    assert result.last_test_result is not None
    assert result.last_test_result.passed == 3


def test_generate_tests_retry_once(monkeypatch):
    """首轮静态校验失败、第 2 轮通过。"""
    _patch_llm(monkeypatch, MOCK_EMPTY_ASSERT_FILE, MOCK_VALID_TEST_FILE)
    _patch_run_tests(monkeypatch, TestResult(total=3, passed=3, failed=0))

    result = generate_tests(PARSED, SCHEMA, MOCK_SQL, SAMPLE_CSV)

    assert result.attempts == 2
    assert result.final_status == "passed"


def test_generate_tests_retry_twice(monkeypatch):
    """前两轮失败、第 3 轮通过。"""
    _patch_llm(
        monkeypatch,
        MOCK_EMPTY_ASSERT_FILE,
        MOCK_MISSING_DOCSTRING_FILE,
        MOCK_VALID_TEST_FILE,
    )
    _patch_run_tests(monkeypatch, TestResult(total=3, passed=3, failed=0))

    result = generate_tests(PARSED, SCHEMA, MOCK_SQL, SAMPLE_CSV)

    assert result.attempts == 3
    assert result.final_status == "passed"


def test_generate_tests_three_failures(monkeypatch):
    """三轮都失败时返回结构化失败结果。"""
    _patch_llm(monkeypatch, MOCK_EMPTY_ASSERT_FILE)

    result = generate_tests(PARSED, SCHEMA, MOCK_SQL, SAMPLE_CSV)

    assert result.attempts == 3
    assert result.final_status == "failed"
    assert result.failure_reason
    assert result.suggestion
    assert "请重试" not in result.suggestion


def test_generate_tests_rejects_empty_assertion(monkeypatch):
    """含 assert result is not None 的文件在生成阶段被拒绝。"""
    _patch_llm(monkeypatch, MOCK_EMPTY_ASSERT_FILE)

    result = generate_tests(PARSED, SCHEMA, MOCK_SQL, SAMPLE_CSV)

    assert result.final_status == "failed"
    assert "禁用断言" in result.failure_reason


def test_generate_tests_rejects_missing_docstring(monkeypatch):
    """缺 docstring 的文件在生成阶段被拒绝。"""
    _patch_llm(monkeypatch, MOCK_MISSING_DOCSTRING_FILE)

    result = generate_tests(PARSED, SCHEMA, MOCK_SQL, SAMPLE_CSV)

    assert result.final_status == "failed"
    assert "缺 docstring" in result.failure_reason


def test_build_test_prompt_constraints():
    """prompt 必须包含 import 白名单、CSV 路径、SQL 常量、三类断言与禁用清单。"""
    prompt = _build_test_prompt(PARSED, SCHEMA, MOCK_SQL)

    assert "只允许 import os、json、pandas、duckdb" in prompt
    assert "CSV_PATH" in prompt
    assert "SQL = " in prompt
    assert "docstring" in prompt
    assert "禁止以下空断言" in prompt
    assert "assert result is not None" in prompt
    assert "assert len(result) > 0" in prompt
    assert "assert df.shape[0] > 0" in prompt
    assert "assert isinstance(result, pd.DataFrame)" in prompt
    assert "assert True" in prompt
    assert "import pandas" in prompt
    assert "import duckdb" in prompt


def test_validate_test_content_reports_violations():
    """_validate_test_content 对违规文件返回具体违规项，合法文件返回空列表。"""
    assert _validate_test_content(MOCK_VALID_TEST_FILE) == []

    violations = _validate_test_content(MOCK_EMPTY_ASSERT_FILE)
    assert violations
    assert any("禁用断言" in item for item in violations)

    multi = _validate_test_content(MOCK_MULTI_VIOLATION_FILE)
    assert any("禁用断言" in item for item in multi)
    assert any("缺 docstring" in item for item in multi)


def test_validate_test_content_accepts_valid_file():
    """反向验证：三类断言与 docstring 齐全的合法文件不应被 _validate 拒绝。"""
    assert _validate_test_content(MOCK_REVERSE_VALID_FILE) == []


def test_generate_tests_input_sql_guard_failure(monkeypatch):
    """入参 SQL 未过 guard 时直接失败，不调用 call_llm。"""
    calls = {"count": 0}

    def fake_call(prompt):
        calls["count"] += 1
        return MOCK_VALID_TEST_FILE

    monkeypatch.setattr(llm, "call_llm", fake_call)

    result = generate_tests(PARSED, SCHEMA, "DROP TABLE t", SAMPLE_CSV)

    assert result.attempts == 0
    assert result.final_status == "failed"
    assert calls["count"] == 0
    assert result.failure_reason.startswith("入参 SQL 未通过 guard：")
    assert result.suggestion == "请先修复 SQL 生成环节"


def test_generate_tests_multiple_violations_reported(monkeypatch):
    """多条违规时 failure_reason 应同时包含两类违规。"""
    _patch_llm(monkeypatch, MOCK_MULTI_VIOLATION_FILE)

    result = generate_tests(PARSED, SCHEMA, MOCK_SQL, SAMPLE_CSV)

    assert "禁用断言" in result.failure_reason
    assert "缺 docstring" in result.failure_reason


def test_build_test_prompt_truncates():
    """超长 SQL、上次内容与错误信息应被截断，超 8000 时优先丢弃上次测试文件。"""
    long_sql = "SELECT " + "x" * 4000
    prompt = _build_test_prompt(
        PARSED,
        SCHEMA_BIG,
        long_sql,
        last_content="y" * 5000,
        last_error="e" * 1000,
    )

    assert "SQL 已截断" in prompt
    assert "…（错误已截断）" in prompt
    assert "上一轮失败原因" in prompt
    assert "上一轮生成的测试文件" not in prompt
    assert len(prompt) < 8000
